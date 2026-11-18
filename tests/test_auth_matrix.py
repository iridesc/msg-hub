"""权限矩阵：Basic（管理员）vs Bearer（入口 token）。

核心不变量（v3）：
1. 入口 token **只能发消息**，碰任何管理面一律 403；
2. 入口 token **不能指定出口**——投递目标完全由路由规则决定；
3. 管理员可以直连某个出口（应急/调试），也可以走路由。
"""

from __future__ import annotations

import base64
import dataclasses

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


def make_entry(client: TestClient, name: str = "监控平台", tags: list[str] | None = None) -> dict:
    return client.post(
        "/api/entries", json={"name": name, "tags": tags or []}, headers=admin()
    ).json()["entry"]


def make_channel(client: TestClient, name: str = "工作群1", tags: list[str] | None = None) -> int:
    return client.post(
        "/api/channels",
        json={
            "name": name,
            "type": "wecom_bot",
            "settings": WECOM_SETTINGS,
            "tags": tags or [],
        },
        headers=admin(),
    ).json()["channel"]["id"]


def make_rule(
    client: TestClient,
    name: str = "全部入口→全部出口",
    entry: dict | None = None,
    outbound: dict | None = None,
) -> dict:
    return client.post(
        "/api/rules",
        json={
            "name": name,
            "entry": entry,
            "outbound": outbound or {"field": "type", "op": "exact", "value": "wecom_bot"},
        },
        headers=admin(),
    ).json()["rule"]


def routed_entry(client: TestClient, name: str = "监控平台") -> dict:
    """建一个「入口 + 出口 + 规则」齐全的场景，返回带明文的入口。"""
    make_channel(client)
    entry = make_entry(client, name)
    make_rule(client)
    return entry


# ── 入口生命周期 ──────────────────────────────────────────
def test_create_entry_returns_plaintext_once(cfg):
    with make_client(cfg) as client:
        entry = make_entry(client, tags=["app:pibox"])
        assert entry["secret"].startswith("mh_")
        assert entry["prefix"] == entry["secret"][:9]
        assert entry["tags"] == ["app:pibox"]

        # 后续列表不再给明文
        listed = client.get("/api/entries", headers=admin()).json()["entries"]
        assert len(listed) == 1
        assert "secret" not in listed[0]
        assert listed[0]["prefix"] == entry["prefix"]
        assert listed[0]["tags"] == ["app:pibox"]


def test_list_entries_empty_by_default(cfg):
    with make_client(cfg) as client:
        assert client.get("/api/entries", headers=admin()).json()["entries"] == []


def test_disable_and_delete_entry(cfg):
    with make_client(cfg) as client:
        entry = make_entry(client)
        make_channel(client)
        make_rule(client)

        # 启用时能发
        assert client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(entry["secret"])
        ).status_code == 200

        # 停用后立刻 401
        patched = client.patch(f"/api/entries/{entry['id']}", json={"enabled": False}, headers=admin())
        assert patched.status_code == 200
        assert client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(entry["secret"])
        ).status_code == 401

        # 删掉后同样失效
        assert client.delete(f"/api/entries/{entry['id']}", headers=admin()).status_code == 200
        assert client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(entry["secret"])
        ).status_code == 401


def test_entry_tags_can_be_updated(cfg):
    with make_client(cfg) as client:
        entry = make_entry(client, tags=["app:pibox"])
        renamed = client.patch(
            f"/api/entries/{entry['id']}",
            json={"name": "改名后的入口", "tags": ["app:pibox", "kind:alert"]},
            headers=admin(),
        ).json()["entry"]
        assert renamed["name"] == "改名后的入口"
        assert renamed["tags"] == ["app:pibox", "kind:alert"]


def test_duplicate_entry_name_rejected(cfg):
    with make_client(cfg) as client:
        make_entry(client, "监控平台")
        response = client.post("/api/entries", json={"name": "监控平台"}, headers=admin())
        assert response.status_code == 400


# ── 管理面权限 ────────────────────────────────────────────
def test_entry_cannot_touch_management_surface(cfg):
    with make_client(cfg) as client:
        entry = make_entry(client)
        headers = bearer(entry["secret"])
        for method, path in [
            ("get", "/dashboard"),
            ("get", "/api/channels"),
            ("post", "/api/channels"),
            ("get", "/api/entries"),
            ("post", "/api/entries"),
            ("get", "/api/rules"),
            ("post", "/api/rules"),
            ("get", "/api/route/preview"),
            ("get", "/api/logs"),
            ("get", "/api/types"),
            ("get", "/api/stats"),
        ]:
            response = getattr(client, method)(
                path,
                headers=headers,
                **({"json": {"name": "x"}} if method == "post" else {}),
            )
            assert response.status_code == 403, f"{method.upper()} {path} → {response.status_code}"


# ── 发送面权限 ────────────────────────────────────────────
def test_entry_cannot_specify_outbound(cfg):
    """入口 token 传 channel 必须被拒，否则「出口完全由服务端决定」就不成立。"""
    with make_client(cfg) as client:
        channel_id = make_channel(client)
        entry = make_entry(client)
        make_rule(client)
        response = client.post(
            "/api/send",
            json={"channel": channel_id, "text": "hi", "dry_run": True},
            headers=bearer(entry["secret"]),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"


def test_entry_sends_via_route(cfg):
    with make_client(cfg) as client:
        entry = routed_entry(client)
        response = client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(entry["secret"])
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["routed"] is True
        assert body["counts"]["sent"] == 1


def test_entry_without_matching_rule_gets_no_route(cfg):
    with make_client(cfg) as client:
        make_channel(client)
        entry = make_entry(client, tags=["app:pibox"])
        make_rule(
            client,
            entry={"field": "tag", "op": "exact", "value": "app:其他应用"},
            outbound={"field": "type", "op": "exact", "value": "wecom_bot"},
        )
        response = client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(entry["secret"])
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "no_route"


def test_admin_can_send_directly(cfg):
    """管理员直连出口（应急/调试），不受路由规则限制。"""
    with make_client(cfg) as client:
        channel_id = make_channel(client)
        sent = client.post(
            "/api/send",
            json={"channel": channel_id, "text": "hi", "dry_run": True},
            headers=admin(),
        )
        assert sent.status_code == 200
        assert sent.json()["routed"] is False


def test_admin_without_channel_also_uses_route(cfg):
    """管理员不传 channel 时也走路由；只匹配「入口条件为空」的规则。"""
    with make_client(cfg) as client:
        make_channel(client)
        # 有入口条件的规则不适用于管理员
        make_rule(
            client,
            name="只给 pibox",
            entry={"field": "tag", "op": "exact", "value": "app:pibox"},
        )
        no_route = client.post("/api/send", json={"text": "hi", "dry_run": True}, headers=admin())
        assert no_route.status_code == 409
        assert no_route.json()["error"]["code"] == "no_route"

        # 补一条「所有入口」的规则后就能发
        make_rule(client, name="所有入口")
        ok_response = client.post("/api/send", json={"text": "hi", "dry_run": True}, headers=admin())
        assert ok_response.status_code == 200


def test_unknown_bearer_is_unauthorized(cfg):
    with make_client(cfg) as client:
        make_entry(client)
        assert client.post(
            "/api/send", json={"text": "hi"}, headers=bearer("mh_deadbeefdeadbeef")
        ).status_code == 401
