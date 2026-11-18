"""Dashboard 测试：页面可达性、鉴权、聚合接口、手工操作闭环。"""

from __future__ import annotations

import base64
import dataclasses

from starlette.testclient import TestClient

from msg_hub.app import build_app
from msg_hub.service import Hub

WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}


def make_client(cfg, password: str = "") -> TestClient:
    settings = dataclasses.replace(cfg, admin_password=password)
    hub = Hub(settings)
    return TestClient(build_app(settings, hub), raise_server_exceptions=False)


def basic(token: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{token}".encode()).decode()}


def _setup_route(client: TestClient, password: str = "secret") -> dict:
    """建好「出口 + 入口 + 规则」，返回带明文 token 的入口。"""
    headers = basic(password)
    client.post(
        "/api/channels",
        json={"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS, "tags": ["kind:test"]},
        headers=headers,
    )
    entry = client.post(
        "/api/entries", json={"name": "监控平台", "tags": ["app:pibox"]}, headers=headers
    ).json()["entry"]
    client.post(
        "/api/rules",
        json={
            "name": "pibox → 全部出口",
            "entry": {"field": "tag", "op": "prefix", "value": "app:"},
            "outbound": {"field": "tag", "op": "exact", "value": "kind:test"},
        },
        headers=headers,
    )
    return entry


def test_dashboard_requires_auth(cfg):
    with make_client(cfg, password="secret") as client:
        response = client.get("/dashboard")
    assert response.status_code == 401
    assert "Basic" in response.headers.get("www-authenticate", "")


def test_dashboard_page_served_with_basic_auth(cfg):
    password = "admin-pass-abc-123"
    with make_client(cfg, password=password) as client:
        response = client.get("/dashboard", headers=basic(password))
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "msg-hub" in body
    # 三栏并列 + 每栏右上角 ＋，且不再有页签按钮
    for list_id in ("list-entries", "list-rules", "list-channels"):
        assert f'id="{list_id}"' in body, list_id
    for add_id in ("add-entry", "add-rule", "add-channel"):
        assert f'id="{add_id}"' in body, add_id
    assert 'class="add"' in body
    assert "btn-tab-" not in body
    # 点行弹窗修改：三个编辑弹窗都在
    for modal in ("entry-modal", "rule-modal", "channel-modal"):
        assert f'id="{modal}"' in body, modal
    # 页面里不能出现密码
    assert password not in body


def test_entry_identity_cannot_open_dashboard(cfg):
    """入口 token 只能发消息，碰管理面（看板/渠道/入口/规则/日志）一律 403。"""
    with make_client(cfg, password="secret") as client:
        secret = _setup_route(client)["secret"]
        bearer = {"Authorization": f"Bearer {secret}"}

        # 管理面：全部 403
        assert client.get("/dashboard", headers=bearer).status_code == 403
        assert client.get("/api/channels", headers=bearer).status_code == 403
        assert client.get("/api/entries", headers=bearer).status_code == 403
        assert client.get("/api/rules", headers=bearer).status_code == 403
        assert client.get("/api/route/preview", headers=bearer).status_code == 403
        assert client.get("/api/logs", headers=bearer).status_code == 403
        assert client.get("/api/stats", headers=bearer).status_code == 403

        # 调用面：不能指定出口，只能走路由
        sent = client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["ok"] is True
        assert sent.json()["routed"] is True
        assert client.post(
            "/api/send",
            json={"channel": "工作群1", "text": "hi", "dry_run": True},
            headers=bearer,
        ).status_code == 403


def test_disabled_entry_is_rejected(cfg):
    with make_client(cfg, password="secret") as client:
        entry = _setup_route(client)
        bearer = {"Authorization": f"Bearer {entry['secret']}"}
        assert client.post(
            "/api/send", json={"text": "x", "dry_run": True}, headers=bearer
        ).status_code == 200

        # 禁用后立刻失效
        client.patch(f"/api/entries/{entry['id']}", json={"enabled": False}, headers=basic("secret"))
        assert client.post(
            "/api/send", json={"text": "x", "dry_run": True}, headers=bearer
        ).status_code == 401


def test_root_redirects_to_dashboard(cfg):
    with make_client(cfg, password="secret") as client:
        response = client.get("/", headers=basic("secret"), follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/dashboard"


def test_dashboard_can_be_disabled(cfg):
    settings = dataclasses.replace(cfg, admin_password="secret", dashboard_enabled=False)
    hub = Hub(settings)
    with TestClient(build_app(settings, hub), raise_server_exceptions=False) as client:
        assert client.get("/dashboard", headers=basic("secret")).status_code == 404
        assert client.get("/api/channels", headers=basic("secret")).status_code == 200


def test_stats_aggregates_channels_and_sends(cfg):
    with make_client(cfg, password="secret") as client:
        client.post(
            "/api/channels",
            json={"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS},
            headers=basic("secret"),
        )
        client.post("/api/send", json={"channel": "工作群1", "text": "hi", "dry_run": True}, headers=basic("secret"))

        body = client.get("/api/stats", headers=basic("secret")).json()
    assert body["channels"]["total"] == 1
    assert body["channels"]["ok"] == 1
    assert body["sends"]["sampled"] == 1
    assert body["sends"]["ok"] == 1
    assert body["sends"]["by_channel"][0]["name"] == "工作群1"


def test_manual_channel_lifecycle_through_api(cfg):
    """dashboard 的手工操作走的就是这套 API，这里做闭环验证。"""
    with make_client(cfg, password="secret") as client:
        auth = basic("secret")
        created = client.post(
            "/api/channels",
            json={"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS},
            headers=auth,
        )
        assert created.status_code == 201
        channel_id = created.json()["channel"]["id"]

        # 停用 → 列表里状态变 disabled
        patched = client.patch(f"/api/channels/{channel_id}", json={"enabled": False}, headers=auth)
        assert patched.status_code == 200
        listed = client.get("/api/channels", headers=auth).json()["channels"]
        assert listed[0]["enabled"] is False
        assert listed[0]["status"] == "disabled"

        # 停用后不能发送
        blocked = client.post("/api/send", json={"channel": channel_id, "text": "hi"}, headers=auth)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "channel_disabled"

        # 启用回来
        client.patch(f"/api/channels/{channel_id}", json={"enabled": True}, headers=auth)
        detail = client.get(f"/api/channels/{channel_id}", headers=auth).json()["channel"]
        assert detail["enabled"] is True

        # 删除
        assert client.delete(f"/api/channels/{channel_id}", headers=auth).status_code == 200
        assert client.get("/api/channels", headers=auth).json()["channels"] == []
