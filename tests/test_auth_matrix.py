"""权限矩阵：Basic（管理员）vs Bearer（入口 token）。

核心不变量（v3）：
1. 入口 token **只能发消息**，碰任何管理面一律 403；
2. 入口 token **不能指定出口**——投递目标完全由路由规则决定；
3. 管理员可以直连某个出口（应急/调试），也可以走路由。
"""

from __future__ import annotations

import base64
from urllib.parse import quote
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
        "/manager_api/entries", json={"name": name, "tags": tags or []}, headers=admin()
    ).json()["entry"]


def make_channel(client: TestClient, name: str = "工作群1", tags: list[str] | None = None) -> int:
    return client.post(
        "/manager_api/channels",
        json={
            "name": name,
            "type": "wecom_bot",
            "settings": WECOM_SETTINGS,
            "tags": tags or [],
        },
        headers=admin(),
    ).json()["channel"]["name"]


def make_rule(
    client: TestClient,
    name: str = "全部入口→全部出口",
    entry: dict | None = None,
    outbound: dict | None = None,
) -> dict:
    return client.post(
        "/manager_api/rules",
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
def test_entry_secret_is_always_readable(cfg):
    """token 不是「只显示一次」：列表和详情随时能拿回明文。"""
    with make_client(cfg) as client:
        entry = make_entry(client, tags=["app:pibox"])
        assert entry["secret"].startswith("mh_")
        assert entry["prefix"] == entry["secret"][:9]
        assert entry["tags"] == ["app:pibox"]

        listed = client.get("/manager_api/entries", headers=admin()).json()["entries"]
        assert len(listed) == 1
        assert listed[0]["secret"] == entry["secret"]
        assert listed[0]["tags"] == ["app:pibox"]

        detail = client.get(f"/manager_api/entries/{quote(entry['name'])}", headers=admin()).json()["entry"]
        assert detail["secret"] == entry["secret"]

        # 改了标签再读，token 不变
        client.patch(f"/manager_api/entries/{quote(entry['name'])}", json={"tags": ["app:x"]}, headers=admin())
        again = client.get(f"/manager_api/entries/{quote(entry['name'])}", headers=admin()).json()["entry"]
        assert again["secret"] == entry["secret"]


def test_list_entries_empty_by_default(cfg):
    with make_client(cfg) as client:
        assert client.get("/manager_api/entries", headers=admin()).json()["entries"] == []


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
        patched = client.patch(f"/manager_api/entries/{quote(entry['name'])}", json={"enabled": False}, headers=admin())
        assert patched.status_code == 200
        assert client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(entry["secret"])
        ).status_code == 401

        # 删掉后同样失效
        assert client.delete(f"/manager_api/entries/{quote(entry['name'])}", headers=admin()).status_code == 200
        assert client.post(
            "/api/send", json={"text": "hi", "dry_run": True}, headers=bearer(entry["secret"])
        ).status_code == 401


def test_entry_tags_can_be_updated(cfg):
    with make_client(cfg) as client:
        entry = make_entry(client, tags=["app:pibox"])
        renamed = client.patch(
            f"/manager_api/entries/{quote(entry['name'])}",
            json={"name": "改名后的入口", "tags": ["app:pibox", "kind:alert"]},
            headers=admin(),
        ).json()["entry"]
        assert renamed["name"] == "改名后的入口"
        assert renamed["tags"] == ["app:pibox", "kind:alert"]


def test_duplicate_entry_name_rejected(cfg):
    with make_client(cfg) as client:
        make_entry(client, "监控平台")
        response = client.post("/manager_api/entries", json={"name": "监控平台"}, headers=admin())
        assert response.status_code == 400


# ── 管理面权限 ────────────────────────────────────────────
def test_entry_cannot_touch_management_surface(cfg):
    with make_client(cfg) as client:
        entry = make_entry(client)
        headers = bearer(entry["secret"])
        for method, path in [
            ("get", "/dashboard"),
            ("get", "/manager_api/channels"),
            ("post", "/manager_api/channels"),
            ("get", "/manager_api/entries"),
            ("post", "/manager_api/entries"),
            ("get", "/manager_api/rules"),
            ("post", "/manager_api/rules"),
            ("get", "/manager_api/route/preview"),
            ("get", "/manager_api/logs"),
            ("get", "/manager_api/types"),
            ("get", "/manager_api/stats"),
        ]:
            response = getattr(client, method)(
                path,
                headers=headers,
                **({"json": {"name": "x"}} if method == "post" else {}),
            )
            assert response.status_code == 403, f"{method.upper()} {path} → {response.status_code}"


# ── 发送面权限 ────────────────────────────────────────────
def test_message_body_has_no_outbound_field(cfg):
    """消息体里根本没有「指定出口」这个字段 —— 传了直接 400，谁都不例外。

    以前入口传 channel 会 403、管理员传了能直连，等于同一个字段两套语义。
    现在出口完全由服务端决定，没有例外，也不存在「忘了拒绝」的可能。
    """
    with make_client(cfg) as client:
        channel_id = make_channel(client)
        entry = make_entry(client)
        make_rule(client)
        # 入口 token：字段不存在 → 400
        response = client.post(
            "/api/send",
            json={"channel": channel_id, "text": "hi", "dry_run": True},
            headers=bearer(entry["secret"]),
        )
        assert response.status_code == 400
        assert "channel" in response.json()["error"]["message"]
        # 管理员连发送面都进不来 → 403（先卡在门口）
        assert client.post(
            "/api/send", json={"channel": channel_id, "text": "hi"}, headers=admin()
        ).status_code == 403


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


def test_admin_has_no_rest_send_path(cfg):
    """发送面只认入口 token，Basic 一律 403 —— 管理员没有 REST 发送路径。

    管理员的发送能力只在 stdio（本地）里存在，HTTP 上想单发某个出口用 channel_test。
    """
    with make_client(cfg) as client:
        channel_id = make_channel(client)
        response = client.post("/api/send", json={"text": "hi", "dry_run": True}, headers=admin())
        assert response.status_code == 403
        assert "只接受入口 token" in response.json()["error"]["message"]
        # 管理面照常可用
        assert client.get("/manager_api/channels", headers=admin()).status_code == 200
        # 单发某个出口走 channel_test
        assert client.post(
            f"/manager_api/channels/{channel_id}/test", json={"dry_run": True}, headers=admin()
        ).status_code == 200


def test_entry_cannot_read_its_own_secret(cfg):
    """明文只对管理员开放：入口 token 自己删不掉、也查不到明文。"""
    with make_client(cfg) as client:
        entry = make_entry(client)
        headers = bearer(entry["secret"])
        assert client.get("/manager_api/entries", headers=headers).status_code == 403
        assert client.get(f"/manager_api/entries/{quote(entry['name'])}", headers=headers).status_code == 403


def test_unknown_bearer_is_unauthorized(cfg):
    with make_client(cfg) as client:
        make_entry(client)
        assert client.post(
            "/api/send", json={"text": "hi"}, headers=bearer("mh_deadbeefdeadbeef")
        ).status_code == 401
