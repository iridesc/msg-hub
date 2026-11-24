#!/usr/bin/env python3
"""企业微信群机器人插件。

用法（由 msg-hub 主程序调用，也可手工调试）：

    echo '{"settings":{"webhook_key":"xxx"},"message":{"text":"hello"}}' | python3 send_msg.py
    echo '{"settings":{...},"message":{...}}' | python3 send_msg.py --dry-run

配置从 stdin 的 `settings` 字段读取（契约 v2），不再需要配置文件。
契约见项目 README「插件契约」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://qyapi.weixin.qq.com"
TEXT_MAX_BYTES = 2048
MARKDOWN_MAX_BYTES = 4096

# 群机器人返回码：仅这些值得重试
RETRYABLE_ERRCODES = {45009, 45033}  # 频率限制 / 接口并发超限


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(code: str, message: str, retryable: bool = False, **extra: Any) -> int:
    emit({"ok": False, "provider": "wecom_bot", "error": {"code": code, "message": message, "retryable": retryable, **extra}})
    return 1 if retryable else 2


def read_request() -> dict[str, Any]:
    """读 stdin 的单行 JSON（契约 v2：config 在 payload 里，不再读文件）。"""
    data = sys.stdin.read()
    if not data.strip():
        return {}
    return json.loads(data)


def load_settings(request: dict[str, Any]) -> dict[str, Any]:
    settings = request.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("payload 缺少 settings 对象（插件契约 v2）")
    return settings


def build_webhook(settings: dict[str, Any]) -> str:
    explicit = str(settings.get("webhook_url") or "").strip()
    if explicit:
        return explicit
    key = str(settings.get("webhook_key") or "").strip()
    if not key:
        raise ValueError("webhook_key 与 webhook_url 至少需要填一个")
    base = str(settings.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/cgi-bin/webhook/send?key={key}"


def _mentions(value: Any) -> list[str]:
    """@成员：既接受数组也接受单个字符串。"""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [str(item).strip() for item in items if str(item).strip()]


def mask_url(url: str) -> str:
    if "key=" not in url:
        return url
    head, _, tail = url.partition("key=")
    return f"{head}key={tail[:6]}***"


def build_payload(message: dict[str, Any], options: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """组装企业微信的 payload。

    出口相关的参数都在 extra 里，核心不解析、原样透传，这里自己按需要取。
    """
    text = str(message.get("text") or "")
    extra = message.get("extra") or {}
    fmt = str(extra.get("format") or "plain").lower()
    users = [str(u) for u in _mentions(extra.get("mention_users")) if u]
    mobiles = [str(m) for m in _mentions(extra.get("mention_mobiles")) if m]
    mention_all = bool(extra.get("mention_all"))

    # @所有人 要出口显式授权（options.allow_mention_all），否则容易误伤全群
    if mention_all and not options.get("allow_mention_all"):
        raise ValueError("该出口未开启 @所有人（需要 options.allow_mention_all=true）")

    if fmt == "markdown":
        content = text
        # markdown 消息没有 mentioned_list 字段，企业微信支持在正文里写 <@userid>
        tags = [f"<@{u}>" for u in users]
        if mention_all:
            tags.append("<@all>")
        if tags:
            content = f"{content}\n\n{' '.join(tags)}"
        size = len(content.encode("utf-8"))
        if size > MARKDOWN_MAX_BYTES:
            raise ValueError(f"markdown 消息 {size} 字节，超过企业微信上限 {MARKDOWN_MAX_BYTES} 字节")
        return {"msgtype": "markdown", "markdown": {"content": content}}, size

    if mention_all:
        users = ["@all"]
        mobiles = []
    payload: dict[str, Any] = {"msgtype": "text", "text": {"content": text}}
    if users:
        payload["text"]["mentioned_list"] = users
    if mobiles:
        payload["text"]["mentioned_mobile_list"] = mobiles
    size = len(text.encode("utf-8"))
    if size > TEXT_MAX_BYTES:
        raise ValueError(f"文本消息 {size} 字节，超过企业微信上限 {TEXT_MAX_BYTES} 字节")
    return payload, size


def main() -> int:
    parser = argparse.ArgumentParser(description="企业微信群机器人发送插件")
    parser.add_argument("--dry-run", action="store_true", help="只组装并校验请求，不真正发送")
    args = parser.parse_args()

    try:
        request = read_request()
        settings = load_settings(request)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return fail("invalid_settings", f"读取配置或请求失败：{exc}")

    message = request.get("message") or {}
    try:
        url = build_webhook(settings)
        payload, size = build_payload(message, request.get("options") or {})
    except ValueError as exc:
        return fail("invalid_argument", str(exc))

    timeout = float(settings.get("timeout") or 10)
    started = time.monotonic()

    if args.dry_run or request.get("dry_run"):
        emit(
            {
                "ok": True,
                "provider": "wecom_bot",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "raw": {
                    "dry_run": True,
                    "url": mask_url(url),
                    "msgtype": payload.get("msgtype"),
                    "payload_bytes": size,
                },
            }
        )
        return 0

    # 标准库 urllib 实现（插件要求零第三方依赖，可在 pi-box 容器里直接跑）。
    # 默认忽略环境里的 http_proxy/https_proxy：容器常被注入宿主机代理（如 127.0.0.1:xxxx），
    # 在容器内不可达，会表现为莫名其妙的 Connection refused。需要代理时必须显式配置 http_proxy。
    proxy = str(settings.get("http_proxy") or "").strip()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # HTTP 4xx/5xx
        status = exc.code
        text = exc.read().decode("utf-8", "replace") if exc.fp else ""
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return fail("send_failed", f"请求企业微信超时（{timeout:g}s）", retryable=True)
        return fail("send_failed", f"请求企业微信失败：{reason}", retryable=True)
    except TimeoutError:
        return fail("send_failed", f"请求企业微信超时（{timeout:g}s）", retryable=True)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if status >= 500:
        return fail("send_failed", f"企业微信返回 HTTP {status}", retryable=True)
    try:
        body = json.loads(text)
    except ValueError:
        return fail("send_failed", f"企业微信返回非 JSON（HTTP {status}）：{text[:200]}")

    errcode = int(body.get("errcode", -1))
    errmsg = str(body.get("errmsg") or "")
    if status == 200 and errcode == 0:
        emit(
            {
                "ok": True,
                "provider": "wecom_bot",
                "elapsed_ms": elapsed_ms,
                "raw": {"errcode": errcode, "errmsg": errmsg, "msgtype": payload.get("msgtype"), "payload_bytes": size},
            }
        )
        return 0

    hint = ""
    if errcode == 93000:
        hint = "（webhook key 无效或群机器人已被移除）"
    elif errcode in RETRYABLE_ERRCODES:
        hint = "（触发企业微信频率限制）"
    return fail(
        "send_failed",
        f"企业微信返回 errcode={errcode} errmsg={errmsg}{hint}",
        retryable=errcode in RETRYABLE_ERRCODES,
        errcode=errcode,
        errmsg=errmsg,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
