from __future__ import annotations

import json
import subprocess
import sys

import pytest

from msg_hub.models import HubError, Message
from msg_hub.plugins import Registry


def test_discovers_both_types(cfg):
    registry = Registry(cfg)
    types = {manifest.type for manifest in registry.all()}
    assert types == {"email", "wecom_bot"}


def test_defaults_applied(cfg):
    registry = Registry(cfg)
    manifest = registry.get("email")
    normalized = registry.normalize_settings(
        manifest,
        {
            "smtp_host": "smtp.qq.com",
            "username": "me@qq.com",
            "password": "auth-code",
            "to": ["a@b.c"],
        },
    )
    assert normalized["smtp_port"] == 465
    assert normalized["security"] == "ssl"


def test_missing_required_field_reported(cfg):
    registry = Registry(cfg)
    manifest = registry.get("email")
    with pytest.raises(HubError) as exc:
        registry.normalize_settings(manifest, {"smtp_host": "smtp.qq.com"})
    assert exc.value.code == "invalid_settings"
    fields = {item["field"] for item in exc.value.extra["fields"]}
    assert "username" in fields and "to" in fields


def test_wecom_requires_key_or_url(cfg):
    registry = Registry(cfg)
    manifest = registry.get("wecom_bot")
    with pytest.raises(HubError) as exc:
        registry.normalize_settings(manifest, {})
    assert exc.value.code == "invalid_settings"
    # 只给 webhook_url 也合法
    registry.normalize_settings(manifest, {"webhook_url": "http://127.0.0.1:1/hook"})


def test_secrets_are_masked(cfg):
    registry = Registry(cfg)
    manifest = registry.get("email")
    masked = registry.mask_settings(
        manifest,
        {"smtp_host": "h", "username": "u", "password": "supersecret123", "to": ["a@b.c"]},
    )
    assert masked["password"] == "***t123"
    assert masked["username"] == "u"


def test_webhook_url_is_masked_but_keeps_host(cfg):
    registry = Registry(cfg)
    manifest = registry.get("wecom_bot")
    masked = registry.mask_settings(
        manifest, {"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef123456"}
    )
    assert masked["webhook_url"] == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef***"


def test_unknown_type_lists_available(cfg):
    registry = Registry(cfg)
    with pytest.raises(HubError) as exc:
        registry.get("nope")
    assert exc.value.code == "type_unknown"
    assert exc.value.extra["available"] == ["email", "wecom_bot"]


def test_dry_run_invocation(cfg, tmp_path):
    registry = Registry(cfg)
    manifest = registry.get("wecom_bot")
    config_path = tmp_path / "1.json"
    config_path.write_text('{"settings": {"webhook_key": "abc"}, "options": {}}')
    result = registry.invoke(
        manifest,
        settings={"webhook_key": "abc"},
        channel={"id": 1, "name": "工作群1", "type": "wecom_bot"},
        message=Message(text="你好"),
        options={},
        request_id="req1",
        dry_run=True,
    )
    assert result.ok
    assert result.raw["dry_run"] is True
    assert result.raw["msgtype"] == "text"
    assert result.raw["url"].endswith("key=abc***")


def test_plugin_rejects_overlong_text(cfg, tmp_path):
    registry = Registry(cfg)
    manifest = registry.get("wecom_bot")
    config_path = tmp_path / "1.json"
    config_path.write_text('{"settings": {"webhook_key": "abc"}, "options": {}}')
    with pytest.raises(HubError) as exc:
        registry.invoke(
            manifest,
            settings={"webhook_key": "abc"},
            channel={"id": 1, "name": "工作群1", "type": "wecom_bot"},
            message=Message(text="啊" * 800),  # 2400 字节 > 2048
            options={},
            request_id="req2",
            dry_run=True,
        )
    assert exc.value.code == "invalid_argument"


def test_plugin_env_does_not_leak_service_secrets(cfg, monkeypatch):
    """插件子进程不该拿到主服务的令牌，也不该继承宿主代理变量。"""
    monkeypatch.setenv("MSG_HUB_TOKEN", "super-secret-token")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:11001")
    env = Registry(cfg)._plugin_env("req-1")
    assert "MSG_HUB_TOKEN" not in env
    assert "http_proxy" not in env
    assert env["MSG_HUB_REQUEST_ID"] == "req-1"
    assert env["MSG_HUB_DATA_DIR"] == str(cfg.data_dir)


def test_email_plugin_with_socks_reports_error_instead_of_crashing(cfg, tmp_path):
    """配了 socks5_proxy 但环境没装 PySocks 时，插件要返回结构化错误，而不是抛 traceback。"""
    registry = Registry(cfg)
    manifest = registry.get("email")
    config_path = tmp_path / "1.json"
    config_path.write_text(
        json.dumps(
            {
                "settings": {
                    "smtp_host": "smtp.qq.com",
                    "username": "me@qq.com",
                    "password": "auth-code",
                    "to": ["a@b.c"],
                    "socks5_proxy": "socks5://127.0.0.1:1",
                },
                "options": {},
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(manifest.entry), "--dry-run"],
        input=json.dumps(
            {
                "settings": {
                    "smtp_host": "smtp.qq.com",
                    "username": "me@qq.com",
                    "password": "auth-code",
                    "to": ["a@b.c"],
                    "socks5_proxy": "socks5://127.0.0.1:1",
                },
                "message": {"text": "hi"},
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.stdout.strip(), f"插件没有任何输出：{proc.stderr[-500:]}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] in ("invalid_settings", "send_failed")
