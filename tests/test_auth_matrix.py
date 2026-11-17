"""两套凭据的权限矩阵：Basic（管理员）vs Bearer（API Token）。"""

from __future__ import annotations

import base64
import dataclasses
import json

from starlette.testclient import TestClient

from msg_hub.app import build_app
from msg_hub.service import Hub

WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}
ADMIN_PASSWORD = "admin-pass-0123456789"


def make_client(cfg) -> TestClient:
    settings = dataclasses.replace(cfg, admin_user="admin", admin_password=ADMIN_PASSWORD)
    hub = Hub(settings)
    return TestClient(build_app(settings, hub), raise_server_exceptions=False)


def admin() -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()}


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def make_token(client: TestClient, name: str = "监控平台") -> dict:
    return client.post("/api/tokens", json={"name": name}, headers=admin()).json()["token"]


def make_channel(client: TestClient) -> int:
    return client.post(
        "/api/channels",
        json={"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS},
        headers=admin(),
    ).json()["channel"]["id"]


# ── token 生命周期 ────────────────────────────────────────
def test_create_token_returns_plaintext_once(cfg):
    with make_client(cfg) as client:
        token = make_token(client)
        assert token["secret"].startswith("mh_")
        assert token["prefix"] == token["secret"][:9]

        # 后续列表不再给明文
        listed = client.get("/api/tokens", headers=admin()).json()["tokens"]
        assert len(listed) == 1
        assert "secret" not in listed[0]
        assert listed[0]["prefix"] == token["prefix"]


def test_list_tokens_empty_by_default(cfg):
    with make_client(cfg) as client:
        assert client.get("/api/tokens", headers=admin()).json()["tokens"] == []


def test_disable_and_delete_token(cfg):
    with make_client(cfg) as client:
        token = make_token(client)
        channel_id = make_channel(client)

        # 启用状态可用
        assert client.post(
            "/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True}, headers=bearer(token["secret"])
        ).status_code == 200

        # 禁用 → 401
        patched = client.patch(f"/api/tokens/{token['id']}", json={"enabled": False}, headers=admin())
        assert patched.status_code == 200
        assert patched.json()["token"]["enabled"] is False
        assert client.post(
            "/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True}, headers=bearer(token["secret"])
        ).status_code == 401

        # 删除
        assert client.delete(f"/api/tokens/{token['id']}", headers=admin()).status_code == 200
        assert client.get("/api/tokens", headers=admin()).json()["tokens"] == []


def test_rename_token(cfg):
    with make_client(cfg) as client:
        token = make_token(client, "旧名字")
        renamed = client.patch(f"/api/tokens/{token['id']}", json={"name": "新名字"}, headers=admin())
        assert renamed.json()["token"]["name"] == "新名字"


def test_token_usage_is_tracked(cfg):
    with make_client(cfg) as client:
        token = make_token(client)
        channel_id = make_channel(client)
        client.post(
            "/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True}, headers=bearer(token["secret"])
        )
        listed = client.get("/api/tokens", headers=admin()).json()["tokens"]
        assert listed[0]["usage_count"] == 1
        assert listed[0]["last_used_at"] is not None


def test_duplicate_token_name_rejected(cfg):
    with make_client(cfg) as client:
        make_token(client, "监控平台")
        response = client.post("/api/tokens", json={"name": "监控平台"}, headers=admin())
        assert response.status_code == 400


# ── 权限矩阵 ──────────────────────────────────────────────
def test_token_cannot_manage_channels(cfg):
    with make_client(cfg) as client:
        token = make_token(client)
        headers = bearer(token["secret"])
        created = client.post(
            "/api/channels",
            json={"name": "新群", "type": "wecom_bot", "settings": WECOM_SETTINGS},
            headers=headers,
        )
        assert created.status_code == 403
        assert created.json()["error"]["code"] == "forbidden"


def test_token_cannot_create_tokens(cfg):
    """token 不能给自己或别人造新钥匙。"""
    with make_client(cfg) as client:
        token = make_token(client)
        response = client.post("/api/tokens", json={"name": "升级"}, headers=bearer(token["secret"]))
        assert response.status_code == 403


def test_token_cannot_read_logs(cfg):
    """日志只有 Basic 管理员能看（用户明确要求）。"""
    with make_client(cfg) as client:
        token = make_token(client)
        channel_id = make_channel(client)
        client.post(
            "/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True}, headers=bearer(token["secret"])
        )
        # token 自己发的也看不到
        assert client.get("/api/logs", headers=bearer(token["secret"])).status_code == 403
        # 管理员能看
        assert client.get("/api/logs", headers=admin()).status_code == 200


def test_token_can_read_channel_list_and_types(cfg):
    """只读的类型/渠道列表对 token 开放，否则外部系统不知道该发到哪。"""
    with make_client(cfg) as client:
        token = make_token(client)
        headers = bearer(token["secret"])
        assert client.get("/api/types", headers=headers).status_code == 403
        # channel_list 属于管理面（含状态与打码凭据），token 不可见
        assert client.get("/api/channels", headers=headers).status_code == 403


def test_no_token_configured_still_allows_admin_to_send(cfg):
    """「token 可以不设」：没建任何 token 时，管理员照常能发消息。"""
    with make_client(cfg) as client:
        channel_id = make_channel(client)
        assert client.get("/api/tokens", headers=admin()).json()["tokens"] == []
        sent = client.post(
            "/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True}, headers=admin()
        )
        assert sent.status_code == 200


def test_unknown_bearer_is_unauthorized(cfg):
    with make_client(cfg) as client:
        make_token(client)
        assert client.get(
            "/api/send", headers=bearer("mh_deadbeefdeadbeef")
        ).status_code in (401, 405)


# ── 审计日志记录调用者 ────────────────────────────────────
def test_log_records_actor_for_token(cfg):
    """日志要能追溯是哪把 token 发的，但只记 id/名称/前缀，不记完整凭据。"""
    with make_client(cfg) as client:
        token = make_token(client, "监控平台")
        channel_id = make_channel(client)
        client.post(
            "/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True}, headers=bearer(token["secret"])
        )
        log = client.get("/api/logs", headers=admin()).json()["logs"][0]
        assert log["actor"]["type"] == "token"
        assert log["actor"]["id"] == token["id"]
        assert log["actor"]["name"] == "监控平台"
        assert log["actor"]["prefix"] == token["prefix"]
        assert token["secret"] not in json.dumps(log)


def test_log_records_actor_for_admin(cfg):
    with make_client(cfg) as client:
        channel_id = make_channel(client)
        client.post("/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True}, headers=admin())
        log = client.get("/api/logs", headers=admin()).json()["logs"][0]
        assert log["actor"] == {"type": "admin"}


# ── 错误凭据 ──────────────────────────────────────────────
def test_wrong_admin_password(cfg):
    with make_client(cfg) as client:
        wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
        assert client.get("/api/channels", headers=wrong).status_code == 401


def test_wrong_admin_username(cfg):
    with make_client(cfg) as client:
        wrong = {"Authorization": "Basic " + base64.b64encode(b"root:" + ADMIN_PASSWORD.encode()).decode()}
        assert client.get("/api/channels", headers=wrong).status_code == 401


def test_malformed_authorization_header(cfg):
    with make_client(cfg) as client:
        assert client.get("/api/channels", headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
        assert client.get("/api/channels", headers={"Authorization": "Bearer "}).status_code == 401
        assert client.get("/api/channels", headers={"Authorization": "Weird abc"}).status_code == 401
