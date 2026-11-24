#!/usr/bin/env python3
"""SMTP 邮件插件。

用法（由 msg-hub 主程序调用，也可手工调试）：

    echo '{"settings":{"smtp_host":"...",...},"message":{"text":"hello"}}' | python3 send_msg.py

配置从 stdin 的 `settings` 字段读取（契约 v2），不再需要配置文件。

注意：
- QQ / 163 等邮箱的 password 必须填「授权码」，不是登录密码
- 发往 Gmail 等被墙的目标时，可填 socks5_proxy（如 socks5://PROXY_HOST:PORT）
"""

from __future__ import annotations

import argparse
import json
import re
import smtplib
import socket
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any

RETRYABLE_SMTP_CODES = {421, 450, 451, 452}  # 临时性错误


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(code: str, message: str, retryable: bool = False, **extra: Any) -> int:
    emit({"ok": False, "provider": "email", "error": {"code": code, "message": message, "retryable": retryable, **extra}})
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


def apply_socks_proxy(proxy_url: str) -> None:
    """在子进程内把 socket 全局替换成 SOCKS5 socket（一次性进程，可接受）。"""
    try:
        import socks  # PySocks
    except ImportError as exc:  # 零依赖环境（如 pi-box 容器）没装 PySocks
        raise ValueError(
            "当前环境没有安装 PySocks，无法使用 socks5_proxy（pip install PySocks，或改用直连）"
        ) from exc

    match = re.match(r"^socks5h?://(?:(?P<user>[^:@/]+)(?::(?P<pwd>[^@/]*))?@)?(?P<host>[^:/]+):(?P<port>\d+)/?$", proxy_url.strip())
    if not match:
        raise ValueError("socks5_proxy 格式应为 socks5://host:port 或 socks5://user:pass@host:port")
    socks.set_default_proxy(
        socks.SOCKS5,
        match.group("host"),
        int(match.group("port")),
        username=match.group("user"),
        password=match.group("pwd"),
        rdns=True,
    )
    socket.socket = socks.socksocket  # type: ignore[misc,assignment]


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_message(settings: dict[str, Any], message: dict[str, Any]) -> EmailMessage:
    """组装邮件。

    出口相关的参数都在 extra 里，核心不解析、原样透传，这里自己按需要取。
    """
    text = str(message.get("text") or "")
    message_extra = message.get("extra") or {}
    fmt = str(message_extra.get("format") or "plain").lower()
    subject = str(message_extra.get("title") or "").strip() or text.strip().splitlines()[0][:80]
    prefix = str(settings.get("subject_prefix") or "")
    sender_name, sender_addr = parseaddr(str(settings.get("sender") or settings["username"]))
    msg = EmailMessage()
    msg["Subject"] = f"{prefix}{subject}"
    msg["From"] = formataddr((sender_name, sender_addr)) if sender_name else sender_addr
    to = [str(x) for x in (settings.get("to") or []) if str(x).strip()]
    cc = [str(x) for x in (settings.get("cc") or []) if str(x).strip()]
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Message-ID"] = make_msgid(domain=(sender_addr.split("@")[-1] or None))
    if fmt == "markdown":
        # 邮件不渲染 markdown，原样发送纯文本
        msg.set_content(text)
    elif fmt == "html" or text.lstrip().startswith("<"):
        msg.set_content(html_to_text(text))
        msg.add_alternative(text, subtype="html")
    else:
        msg.set_content(text)
    return msg


def connect(settings: dict[str, Any]) -> smtplib.SMTP:
    host = str(settings["smtp_host"])
    port = int(settings.get("smtp_port") or 465)
    security = str(settings.get("security") or "ssl").lower()
    timeout = float(settings.get("timeout") or 15)
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    client = smtplib.SMTP(host, port, timeout=timeout)
    client.ehlo()
    if security == "starttls":
        client.starttls()
        client.ehlo()
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description="SMTP 邮件发送插件")
    parser.add_argument("--dry-run", action="store_true", help="只连到 SMTP 服务器验证网络，不登录、不发送")
    args = parser.parse_args()

    try:
        request = read_request()
        settings = load_settings(request)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return fail("invalid_settings", f"读取配置或请求失败：{exc}")

    for required in ("smtp_host", "username", "password", "to"):
        if not settings.get(required):
            return fail("invalid_settings", f"缺少必填配置：{required}")

    message = request.get("message") or {}
    message_extra = message.get("extra") or {}
    if not str(message.get("text") or "").strip() and not str(message_extra.get("title") or "").strip():
        return fail("invalid_argument", "邮件正文不能为空")

    proxy = str(settings.get("socks5_proxy") or "").strip()
    if proxy:
        try:
            apply_socks_proxy(proxy)
        except ValueError as exc:
            return fail("invalid_settings", str(exc))

    host = str(settings["smtp_host"])
    port = int(settings.get("smtp_port") or 465)
    timeout = float(settings.get("timeout") or 15)
    started = time.monotonic()

    if args.dry_run or request.get("dry_run"):
        # 只验证 DNS 与 TCP 可达，不登录（避免反复触发 SMTP 认证风控）
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError as exc:
            return fail("send_failed", f"无法连接 {host}:{port}（{exc}）", retryable=True)
        emit(
            {
                "ok": True,
                "provider": "email",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "raw": {
                    "dry_run": True,
                    "smtp": f"{host}:{port}",
                    "security": settings.get("security") or "ssl",
                    "via_proxy": bool(proxy),
                    "to_count": len(settings.get("to") or []),
                },
            }
        )
        return 0

    msg = build_message(settings, message)
    try:
        client = connect(settings)
    except smtplib.SMTPException as exc:
        return fail("send_failed", f"连接 SMTP 失败：{exc}", retryable=True)
    except OSError as exc:
        return fail("send_failed", f"连接 SMTP 失败：{exc}", retryable=True)

    try:
        client.login(str(settings["username"]), str(settings["password"]))
    except smtplib.SMTPAuthenticationError as exc:
        client.close()
        return fail(
            "send_failed",
            f"SMTP 认证失败（QQ/163 请填授权码，Gmail 需 App Password）：{exc.smtp_code} {exc.smtp_error!r}",
            retryable=False,
        )
    except (smtplib.SMTPException, OSError) as exc:
        client.close()
        return fail("send_failed", f"SMTP 登录异常：{exc}", retryable=True)

    try:
        refused = client.send_message(msg)
    except smtplib.SMTPRecipientsRefused as exc:
        client.close()
        return fail(
            "send_failed",
            f"收件人被拒绝：{exc.recipients}",
            retryable=False,
        )
    except smtplib.SMTPResponseException as exc:
        client.close()
        return fail(
            "send_failed",
            f"SMTP 返回 {exc.smtp_code}：{exc.smtp_error!r}",
            retryable=exc.smtp_code in RETRYABLE_SMTP_CODES,
        )
    except (smtplib.SMTPException, OSError) as exc:
        client.close()
        return fail("send_failed", f"发送失败：{exc}", retryable=True)

    try:
        client.quit()
    except (smtplib.SMTPException, OSError):
        pass

    if refused:
        return fail("send_failed", f"部分收件人被拒绝：{refused}", retryable=False, refused=list(refused))

    emit(
        {
            "ok": True,
            "provider": "email",
            "message_id": msg["Message-ID"],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "raw": {
                "smtp": f"{host}:{port}",
                "security": settings.get("security") or "ssl",
                "to_count": len(settings.get("to") or []),
                "via_proxy": bool(proxy),
            },
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
