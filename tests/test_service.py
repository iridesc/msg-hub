from __future__ import annotations

import json

import pytest

from msg_hub.models import HubError

WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}


def _create_wecom(hub, name="工作群1", **options):
    return hub.create_channel(name=name, type_name="wecom_bot", settings=dict(WECOM_SETTINGS), options=options)


def test_create_and_describe(hub):
    channel = _create_wecom(hub)
    assert channel["id"] == 1
    assert channel["status"] == "ok"
    assert channel["settings"]["webhook_key"] == "***defg"


def test_duplicate_name(hub):
    _create_wecom(hub)
    with pytest.raises(HubError) as exc:
        _create_wecom(hub)
    assert exc.value.code == "invalid_argument"


def test_resolve_by_id_and_name(hub):
    created = _create_wecom(hub)
    assert hub.resolve(created["id"]).name == "工作群1"
    assert hub.resolve("工作群1").id == created["id"]
    assert hub.resolve("作群").id == created["id"]  # 唯一子串命中


def test_ambiguous_name_is_rejected(hub):
    _create_wecom(hub, name="运维告警群")
    _create_wecom(hub, name="运维通知群")
    with pytest.raises(HubError) as exc:
        hub.resolve("运维")
    assert exc.value.code == "ambiguous"
    assert len(exc.value.extra["candidates"]) == 2


def test_unknown_channel_lists_available(hub):
    _create_wecom(hub)
    with pytest.raises(HubError) as exc:
        hub.resolve("不存在")
    assert exc.value.code == "not_found"
    assert exc.value.extra["available"][0]["name"] == "工作群1"


def test_broken_when_config_missing(hub, cfg):
    channel = _create_wecom(hub)
    hub.store.config_path(channel["id"]).unlink()
    status, detail = hub.channel_status(hub.resolve(channel["id"]))
    assert status == "broken"
    assert "config" in detail
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "hi")
    assert exc.value.code == "channel_broken"


def test_disabled_channel_cannot_send(hub):
    channel = _create_wecom(hub)
    hub.update_channel(channel["id"], enabled=False)
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "hi")
    assert exc.value.code == "channel_disabled"


def test_mention_all_guard(hub):
    channel = _create_wecom(hub)
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "hi", mention_all=True, dry_run=True)
    assert exc.value.code == "mention_all_denied"

    hub.update_channel(channel["id"], options={"allow_mention_all": True})
    result = hub.send(channel["id"], "hi", mention_all=True, dry_run=True)
    assert result["ok"]


def test_format_must_be_supported(hub):
    channel = _create_wecom(hub)
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "hi", format="html", dry_run=True)
    assert exc.value.code == "invalid_argument"


def test_rate_limit(hub, cfg):
    channel = hub.create_channel(
        name="限流群",
        type_name="wecom_bot",
        settings=dict(WECOM_SETTINGS),
        options={"rate_limit_per_min": 2},
    )
    hub.send(channel["id"], "1", dry_run=True)
    hub.send(channel["id"], "2", dry_run=True)
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "3", dry_run=True)
    assert exc.value.code == "rate_limited"
    assert exc.value.retryable is True


def test_real_send_to_mock_wecom(hub, mock_wecom):
    channel = hub.create_channel(
        name="工作群1",
        type_name="wecom_bot",
        settings={"webhook_url": f"{mock_wecom.base_url}/cgi-bin/webhook/send?key=abc123456"},
    )
    result = hub.send(channel["id"], "线上告警：CPU 95%", mention_users=["zhangsan"])
    assert result["ok"] is True

    request = mock_wecom.last
    assert request["path"].endswith("key=abc123456")
    assert request["json"]["msgtype"] == "text"
    assert request["json"]["text"]["content"].startswith("线上告警")
    assert request["json"]["text"]["mentioned_list"] == ["zhangsan"]

    logs = hub.logs(limit=5)
    assert logs[0]["ok"] is True
    assert logs[0]["channel"]["name"] == "工作群1"
    assert logs[0]["text_preview"].startswith("线上告警")


def test_real_send_error_mapping(hub, mock_wecom):
    mock_wecom.body = {"errcode": 93000, "errmsg": "invalid webhook url"}
    channel = hub.create_channel(
        name="坏群",
        type_name="wecom_bot",
        settings={"webhook_url": f"{mock_wecom.base_url}/send?key=bad"},
    )
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "hi")
    assert exc.value.code == "send_failed"
    assert exc.value.retryable is False
    assert "93000" in exc.value.message

    logs = hub.logs(limit=5)
    assert logs[0]["ok"] is False
    assert logs[0]["error_code"] == "send_failed"


def test_markdown_mention_uses_inline_syntax(hub, mock_wecom):
    channel = hub.create_channel(
        name="md群",
        type_name="wecom_bot",
        settings={"webhook_url": f"{mock_wecom.base_url}/send?key=k"},
    )
    hub.send(channel["id"], "**告警**", format="markdown", mention_users=["lisi"])
    payload = mock_wecom.last["json"]
    assert payload["msgtype"] == "markdown"
    assert "<@lisi>" in payload["markdown"]["content"]


def test_audit_log_written_as_jsonl(hub, cfg):
    channel = _create_wecom(hub)
    hub.send(channel["id"], "审计一下", dry_run=True)
    files = list(cfg.log_dir.glob("*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text().strip())
    assert record["dry_run"] is True
    assert record["source"] == "api"


def test_retryable_semantics_propagates_from_plugin(hub, mock_wecom):
    """下游 5xx → 插件判为可重试，HubError.retryable 必须为 True。"""
    channel = hub.create_channel(
        name="抖动群",
        type_name="wecom_bot",
        settings={"webhook_url": f"{mock_wecom.base_url}/send?key=k"},
    )
    mock_wecom.status = 500
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "hi")
    assert exc.value.retryable is True

    mock_wecom.status = 200
    mock_wecom.body = {"errcode": 45009, "errmsg": "api freq out of limit"}
    with pytest.raises(HubError) as exc:
        hub.send(channel["id"], "hi")
    assert exc.value.retryable is True  # 企微限流 45009 可重试


def test_plugin_ignores_ambient_proxy_env(hub, mock_wecom, monkeypatch):
    """容器常被注入不可达的 http_proxy；插件必须默认忽略，只在显式配置时走代理。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")  # 故意指向死端口
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")

    channel = hub.create_channel(
        name="代理干扰群",
        type_name="wecom_bot",
        settings={"webhook_url": f"{mock_wecom.base_url}/send?key=k"},
    )
    result = hub.send(channel["id"], "应当绕过环境代理")
    assert result["ok"] is True
    assert mock_wecom.last["json"]["text"]["content"] == "应当绕过环境代理"
