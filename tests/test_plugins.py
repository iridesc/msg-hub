from __future__ import annotations

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
        config_path=config_path,
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
            config_path=config_path,
            channel={"id": 1, "name": "工作群1", "type": "wecom_bot"},
            message=Message(text="啊" * 800),  # 2400 字节 > 2048
            options={},
            request_id="req2",
            dry_run=True,
        )
    assert exc.value.code == "invalid_argument"
