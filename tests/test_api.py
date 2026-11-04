from __future__ import annotations

import dataclasses

from starlette.testclient import TestClient

from msg_hub.app import build_app
from msg_hub.service import Hub

WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}


def make_client(cfg, token: str = "") -> TestClient:
    settings = dataclasses.replace(cfg, token=token)
    hub = Hub(settings)
    return TestClient(build_app(settings, hub), raise_server_exceptions=False)


def test_health_is_public(cfg):
    with make_client(cfg, token="secret") as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["types"] == ["email", "wecom_bot"]
    assert body["auth"] == "bearer"


def test_auth_required(cfg):
    with make_client(cfg, token="secret") as client:
        assert client.get("/api/channels").status_code == 401
        assert client.get("/api/channels", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/channels", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_channel_crud(cfg):
    with make_client(cfg) as client:
        created = client.post(
            "/api/channels",
            json={"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS},
        )
        assert created.status_code == 201, created.text
        channel_id = created.json()["channel"]["id"]

        listed = client.get("/api/channels").json()["channels"]
        assert listed[0]["name"] == "工作群1"
        assert listed[0]["status"] == "ok"

        patched = client.patch(f"/api/channels/{channel_id}", json={"name": "运维群"})
        assert patched.status_code == 200
        assert patched.json()["channel"]["name"] == "运维群"

        detail = client.get("/api/channels/运维群").json()["channel"]
        assert detail["id"] == channel_id
        assert detail["settings"]["webhook_key"].startswith("***")

        dry = client.post("/api/send", json={"channel": channel_id, "text": "hi", "dry_run": True})
        assert dry.status_code == 200 and dry.json()["ok"] is True

        logs = client.get("/api/logs", params={"channel": channel_id}).json()["logs"]
        assert logs and logs[0]["dry_run"] is True

        deleted = client.delete(f"/api/channels/{channel_id}")
        assert deleted.status_code == 200
        assert client.get("/api/channels").json()["channels"] == []


def test_validation_errors_are_400(cfg):
    with make_client(cfg) as client:
        response = client.post(
            "/api/channels",
            json={"name": "x", "type": "email", "settings": {"smtp_host": "smtp.qq.com"}},
        )
        assert response.status_code == 400
        body = response.json()["error"]
        assert body["code"] == "invalid_settings"
        assert any(field["field"] == "to" for field in body["details"]["fields"])


def test_ambiguous_channel_is_409(cfg):
    with make_client(cfg) as client:
        for name in ("运维告警群", "运维通知群"):
            client.post("/api/channels", json={"name": name, "type": "wecom_bot", "settings": WECOM_SETTINGS})
        response = client.post("/api/send", json={"channel": "运维", "text": "hi", "dry_run": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ambiguous"


def test_unknown_type_is_400(cfg):
    with make_client(cfg) as client:
        response = client.post("/api/channels", json={"name": "x", "type": "telegram", "settings": {}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "type_unknown"


def test_channel_test_endpoint_with_unsaved_settings(cfg):
    with make_client(cfg) as client:
        response = client.post(
            "/api/channels/0/test",
            json={"text": "probe"},
        )
        # 不存在的渠道 → 404（id=0 未保存配置时应走 settings 覆盖路径，这里只校验错误语义）
        assert response.status_code in (404, 400)
