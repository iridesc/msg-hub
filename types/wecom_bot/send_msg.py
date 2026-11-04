#!/usr/bin/env python3
"""企业微信群机器人插件。

用法（由 msg-hub 主程序调用，也可手工调试）：

    echo '{"message":{"text":"hello"}}' | python3 send_msg.py --config data/config/1.json
    python3 send_msg.py --config data/config/1.json --dry-run < payload.json

契约见项目 README「插件契约」。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

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


def load_config(path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and ("settings" in raw or "options" in raw):
        return dict(raw.get("settings") or {}), dict(raw.get("options") or {})
    return dict(raw or {}), {}


def read_request() -> dict[str, Any]:
    data = sys.stdin.read()
    if not data.strip():
        return {}
    return json.loads(data)


def build_webhook(settings: dict[str, Any]) -> str:
    explicit = str(settings.get("webhook_url") or "").strip()
    if explicit:
        return explicit
    key = str(settings.get("webhook_key") or "").strip()
    if not key:
        raise ValueError("webhook_key 与 webhook_url 至少需要填一个")
    base = str(settings.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/cgi-bin/webhook/send?key={key}"


def mask_url(url: str) -> str:
    if "key=" not in url:
        return url
    head, _, tail = url.partition("key=")
    return f"{head}key={tail[:6]}***"


def build_payload(message: dict[str, Any]) -> tuple[dict[str, Any], int]:
    text = str(message.get("text") or "")
    fmt = str(message.get("format") or "plain").lower()
    users = [str(u) for u in (message.get("mention_users") or []) if str(u).strip()]
    mobiles = [str(m) for m in (message.get("mention_mobiles") or []) if str(m).strip()]
    mention_all = bool(message.get("mention_all"))

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
    parser.add_argument("--config", required=True, help="渠道配置文件路径（config/<id>.json）")
    parser.add_argument("--dry-run", action="store_true", help="只组装并校验请求，不真正发送")
    args = parser.parse_args()

    try:
        settings, _options = load_config(args.config)
        request = read_request()
    except (OSError, json.JSONDecodeError) as exc:
        return fail("invalid_settings", f"读取配置或请求失败：{exc}")

    message = request.get("message") or {}
    try:
        url = build_webhook(settings)
        payload, size = build_payload(message)
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

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
    except httpx.TimeoutException:
        return fail("send_failed", f"请求企业微信超时（{timeout:g}s）", retryable=True)
    except httpx.HTTPError as exc:
        return fail("send_failed", f"请求企业微信失败：{exc}", retryable=True)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if response.status_code >= 500:
        return fail("send_failed", f"企业微信返回 HTTP {response.status_code}", retryable=True)
    try:
        body = response.json()
    except ValueError:
        return fail("send_failed", f"企业微信返回非 JSON（HTTP {response.status_code}）：{response.text[:200]}")

    errcode = int(body.get("errcode", -1))
    errmsg = str(body.get("errmsg") or "")
    if response.status_code == 200 and errcode == 0:
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
