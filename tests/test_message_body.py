"""消息体结构：顶层只有 text / dry_run / extra，服务端独占出口与来源。

设计约束（这几条是这个文件要守住的）：
- 顶层多一个字段就报错，不静默忽略 —— 拼错 mention_all 不能一路发到线上却不生效
- 消息体里压根没有「指定出口」这个字段，所有人（含管理员）一视同仁
- 来源标记由服务端按调用路径打，调用方传不进来
"""

from __future__ import annotations

import base64
import dataclasses

import pytest
from starlette.testclient import TestClient

from msg_hub.app import build_app
from msg_hub.models import HubError, parse_message_body
from msg_hub.service import Hub

WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}
ADMIN_PASSWORD = "admin-pass-0123456789"


def make_client(cfg) -> TestClient:
    settings = dataclasses.replace(cfg, admin_user="admin", admin_password=ADMIN_PASSWORD)
    return TestClient(build_app(settings, Hub(settings)), raise_server_exceptions=False)


def admin() -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()}


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def routed(cfg) -> tuple[TestClient, str]:
    """出口 + 入口 + 规则，返回客户端和入口 token。"""
    client = make_client(cfg)
    client.post(
        "/manager_api/channels",
        json={"name": "群", "type": "wecom_bot", "settings": WECOM_SETTINGS, "tags": ["kind:test"]},
        headers=admin(),
    )
    token = client.post(
        "/manager_api/entries", json={"name": "调用方", "tags": ["kind:test"]}, headers=admin()
    ).json()["entry"]["secret"]
    client.post(
        "/manager_api/rules",
        json={
            "name": "调用方 → 群",
            "entry": {"field": "tag", "op": "exact", "value": "kind:test"},
            "outbound": {"field": "tag", "op": "exact", "value": "kind:test"},
        },
        headers=admin(),
    )
    return client, token


# ── 解析层 ─────────────────────────────────────────────
def test_extra_is_passed_through_verbatim():
    """extra 原样透传：核心不解析、不校验、不归一化。

    出口相关参数是「出口类型」的概念，核心一旦开始校验就等于把某个类型的
    字段表硬编码进路由层。所以这里放什么核心都不管。
    """
    raw = {"format": "markdown", "mentionall": True, "任意键": [1, 2], "nested": {"a": 1}}
    _, _, extra = parse_message_body({"text": "hi", "extra": raw})
    assert extra == raw


def test_extra_defaults_to_empty():
    _, _, extra = parse_message_body({"text": "hi"})
    assert extra == {}


def test_extra_rejects_non_object():
    with pytest.raises(HubError) as exc:
        parse_message_body({"text": "hi", "extra": ["format"]})
    assert exc.value.code == "invalid_argument"


def test_body_top_level_only_three_fields():
    text, dry_run, extra = parse_message_body({"text": "hi", "dry_run": True, "extra": {"format": "markdown"}})
    assert text == "hi" and dry_run is True and extra["format"] == "markdown"


@pytest.mark.parametrize("field", ["channel", "source", "format", "title", "mention_all"])
def test_body_rejects_legacy_top_level_fields(field):
    """老结构（format/title/mention_* 在顶层）现在必须报错并指路到 extra。"""
    with pytest.raises(HubError) as exc:
        parse_message_body({"text": "hi", field: "x"})
    assert exc.value.code == "invalid_argument"
    assert field in exc.value.message
    assert "extra" in exc.value.message


# ── HTTP 层 ────────────────────────────────────────────
def test_send_accepts_extra_object(cfg):
    client, token = routed(cfg)
    with client:
        response = client.post(
            "/api/send",
            json={
                "text": "hi",
                "dry_run": True,
                "extra": {"format": "markdown", "title": "标题"},
            },
            headers=bearer(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True


def test_send_rejects_channel_field(cfg):
    """消息体里没有出口字段 —— 入口传了是 400（管理员连发送面都进不来，403）。"""
    client, token = routed(cfg)
    with client:
        response = client.post("/api/send", json={"text": "hi", "channel": "群"}, headers=bearer(token))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_argument"
        assert client.post(
            "/api/send", json={"text": "hi", "channel": "群"}, headers=admin()
        ).status_code == 403


def test_source_is_decided_by_server(cfg):
    """调用方塞 source 会被拒 —— 审计里的来源只能信服务端。"""
    client, token = routed(cfg)
    with client:
        response = client.post(
            "/api/send", json={"text": "hi", "source": "cron"}, headers=bearer(token)
        )
        assert response.status_code == 400
        assert "source" in response.json()["error"]["message"]

        # 正常发一条，审计里记的是服务端打的 api
        client.post("/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(token))
        logs = client.get("/manager_api/logs", headers=admin()).json()["logs"]
        assert logs[0]["source"] == "api"


def test_unknown_extra_key_passes_through(cfg):
    """extra 里拼错核心不管 —— 原样送到出口，由出口自己决定认不认。"""
    client, token = routed(cfg)
    with client:
        response = client.post(
            "/api/send",
            json={"text": "hi", "dry_run": True, "extra": {"mentionall": True}},
            headers=bearer(token),
        )
        assert response.status_code == 200, response.text


def test_dry_run_is_top_level_not_in_extra(cfg):
    """dry_run 是基本字段；写进 extra 不会生效，只是被当成出口的普通参数丢掉。"""
    client, token = routed(cfg)
    with client:
        response = client.post(
            "/api/send",
            json={"text": "hi", "dry_run": True, "extra": {"dry_run": False}},
            headers=bearer(token),
        )
        assert response.status_code == 200
        assert response.json()["dry_run"] is True


def test_empty_text_still_rejected(cfg):
    client, token = routed(cfg)
    with client:
        response = client.post("/api/send", json={"text": ""}, headers=bearer(token))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_argument"
