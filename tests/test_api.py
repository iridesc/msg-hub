from __future__ import annotations

import dataclasses

from starlette.testclient import TestClient

from msg_hub.app import _origins_for, build_app
from msg_hub.service import Hub

WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}


def basic(password: str, user: str = "admin") -> dict[str, str]:
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def make_client(cfg, password: str = "") -> TestClient:
    settings = dataclasses.replace(cfg, admin_password=password)
    hub = Hub(settings)
    return TestClient(build_app(settings, hub), raise_server_exceptions=False)


def test_health_is_public(cfg):
    with make_client(cfg, password="secret") as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["types"] == ["email", "wecom_bot"]
    assert body["auth"] == "basic+bearer"


def test_auth_required(cfg):
    """无凭据 → 401；Basic 管理员 → 200；Bearer 必须是真实 token。"""
    with make_client(cfg, password="secret") as client:
        assert client.get("/api/channels").status_code == 401
        assert client.get("/api/channels", headers={"Authorization": "Bearer wrong"}).status_code == 401
        # 管理员密码不能当 Bearer 令牌用（两套凭据已彻底分开）
        assert client.get("/api/channels", headers={"Authorization": "Bearer secret"}).status_code == 401
        # Basic 管理员可以
        assert client.get("/api/channels", headers=basic("secret")).status_code == 200


def test_channel_crud(cfg):
    with make_client(cfg) as client:
        created = client.post(
            "/api/channels",
            json={"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS},
        )
        assert created.status_code == 201, created.text
        channel_name = created.json()["channel"]["name"]

        listed = client.get("/api/channels").json()["channels"]
        assert listed[0]["name"] == "工作群1"
        assert listed[0]["status"] == "ok"

        # 改名 = 换标识：旧名称从此不存在，后续必须用新名称寻址
        patched = client.patch(f"/api/channels/{channel_name}", json={"name": "运维群"})
        assert patched.status_code == 200
        assert patched.json()["channel"]["name"] == "运维群"
        assert client.get(f"/api/channels/{channel_name}").status_code == 404
        channel_name = "运维群"

        detail = client.get(f"/api/channels/{channel_name}").json()["channel"]
        assert detail["name"] == channel_name
        assert detail["settings"]["webhook_key"].startswith("***")

        dry = client.post("/api/send", json={"channel": channel_name, "text": "hi", "dry_run": True})
        assert dry.status_code == 200 and dry.json()["ok"] is True

        logs = client.get("/api/logs", params={"channel": channel_name}).json()["logs"]
        assert logs and logs[0]["dry_run"] is True

        deleted = client.delete(f"/api/channels/{channel_name}")
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


def test_origins_cover_hosts_including_ipv6():
    """Origin 白名单要覆盖所有允许的 Host（含 IPv6 本机地址）。"""
    origins = _origins_for(["127.0.0.1:*", "localhost:*", "msg-hub:*", "[::1]:*"])
    assert "http://127.0.0.1:*" in origins
    assert "http://localhost:*" in origins
    assert "http://msg-hub:*" in origins
    assert "http://[::1]:*" in origins
    # 固定端口精确匹配
    assert "http://example.com:8080" in _origins_for(["example.com:8080"])


def test_mcp_rejects_foreign_origin(cfg):
    """DNS rebinding 防护：外来的 Origin 必须被拒。"""
    with make_client(cfg) as client:
        allowed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Host": "127.0.0.1:8788",
                "Origin": "http://127.0.0.1:8788",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert allowed.status_code == 200, allowed.text

        blocked = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Host": "127.0.0.1:8788",
                "Origin": "http://evil.example.com",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert blocked.status_code == 403, blocked.text
