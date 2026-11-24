"""两个 MCP 端点的行为：/mcp（调用面，免认证）vs /manager_mcp（管理面，Basic）。

设计意图：
- 调用方只需要一个工具 + token 参数，不用配任何认证头
- 管理面单独一个端点，必须 Basic，工具集是全量
"""

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
    # Host 必须是 allowed_hosts 里的值，否则会被 DNS rebinding 防护挡掉
    return TestClient(
        build_app(settings, hub), base_url="http://127.0.0.1:8788", raise_server_exceptions=False
    )


def admin() -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()}


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ── MCP 会话小工具（Streamable HTTP 是 SSE 响应，取 data: 行）──
def _rpc(client: TestClient, path: str, payload: dict, headers: dict | None = None):
    response = client.post(
        path,
        json=payload,
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )
    body = response.text
    for line in body.splitlines():
        if line.startswith("data: "):
            return response.status_code, json.loads(line[6:])
    return response.status_code, {"raw": body}


def _init(client: TestClient, path: str, headers: dict | None = None) -> int:
    status, _ = _rpc(
        client,
        path,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        headers,
    )
    return status


def _tools(client: TestClient, path: str, headers: dict | None = None) -> set[str]:
    _status, body = _rpc(client, path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    return {tool["name"] for tool in body["result"]["tools"]}


def _call(client: TestClient, path: str, name: str, args: dict, headers: dict | None = None):
    _status, body = _rpc(
        client,
        path,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}},
        headers,
    )
    result = body["result"]
    if result.get("isError"):
        # 错误内容是一段 JSON 字符串（SDK 把 ToolError 的 message 原样抛出）
        text = result["content"][0]["text"]
        start = text.find("{")
        return True, json.loads(text[start:]) if start >= 0 else {"raw": text}
    return False, json.loads(result["content"][0]["text"])


def _setup(cfg) -> tuple[TestClient, str]:
    """建一个最小可用的「出口 + 入口 + 规则」，返回客户端和入口 token。"""
    client = make_client(cfg)
    client.post(
        "/manager_api/channels",
        json={"name": "测试群", "type": "wecom_bot", "settings": WECOM_SETTINGS, "tags": ["kind:test"]},
        headers=admin(),
    )
    token = client.post(
        "/manager_api/entries", json={"name": "调用方", "tags": ["app:x", "kind:test"]}, headers=admin()
    ).json()["entry"]["secret"]
    client.post(
        "/manager_api/rules",
        json={
            "name": "调用方 → 测试群",
            "entry": {"field": "tag", "op": "exact", "value": "kind:test"},
            "outbound": {"field": "tag", "op": "exact", "value": "kind:test"},
        },
        headers=admin(),
    )
    return client, token


# ── /mcp：调用面 ─────────────────────────────────────────
def test_sender_endpoint_needs_no_auth(cfg):
    """调用面完全不需要认证头，initialize 就能过。"""
    with make_client(cfg) as client:
        assert _init(client, "/mcp") == 200
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code in (200, 400, 406)


def test_sender_endpoint_exposes_only_send_message(cfg):
    with make_client(cfg) as client:
        assert _tools(client, "/mcp") == {"send_message"}


def test_sender_endpoint_sends_with_token_argument(cfg):
    client, token = _setup(cfg)
    with client:
        error, data = _call(
            client, "/mcp", "send_message", {"token": token, "msg": "只给 token 和正文", "dry_run": True}
        )
        assert error is False, data
        assert data["ok"] is True
        assert data["routed"] is True
        assert data["counts"]["sent"] == 1
        assert [r["channel"]["name"] for r in data["results"]] == ["测试群"]


def test_unknown_token_reports_unauthorized(cfg):
    with make_client(cfg) as client:
        error, payload = _call(
            client, "/mcp", "send_message", {"token": "mh_deadbeefdeadbeef", "msg": "hi"}
        )
        assert error is True
        assert payload["code"] == "unauthorized"
        assert "不存在" in payload["message"]


def test_disabled_entry_token_is_rejected(cfg):
    client, token = _setup(cfg)
    with client:
        client.patch("/manager_api/entries/调用方", json={"enabled": False}, headers=admin())
        error, payload = _call(client, "/mcp", "send_message", {"token": token, "msg": "hi"})
        assert error is True
        assert payload["code"] == "unauthorized"


def test_sender_endpoint_has_no_management_tools(cfg):
    """调用面拿不到管理工具：即使直接点名调用也是「工具不存在」。"""
    with make_client(cfg) as client:
        assert "entry_list" not in _tools(client, "/mcp")
        # 点名调一个不存在的工具 → JSON-RPC 错误而不是 403
        status, body = _rpc(
            client,
            "/mcp",
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "entry_list", "arguments": {}}},
        )
        assert status == 200
        # SDK 把它包成 isError 的工具结果（"Unknown tool: entry_list"）
        assert body["result"]["isError"] is True
        assert "Unknown tool" in body["result"]["content"][0]["text"]


def test_sender_endpoint_records_entry_as_actor(cfg):
    client, token = _setup(cfg)
    with client:
        _call(client, "/mcp", "send_message", {"token": token, "msg": "审计", "dry_run": True})
        logs = client.get("/manager_api/logs", headers=admin()).json()["logs"]
        assert logs[0]["actor"]["type"] == "entry"
        assert logs[0]["actor"]["name"] == "调用方"
        assert token not in json.dumps(logs, ensure_ascii=False)


# ── /manager_mcp：管理面 ─────────────────────────────────
def test_manager_endpoint_requires_basic(cfg):
    with make_client(cfg) as client:
        assert _init(client, "/manager_mcp") == 401
        # Basic 可以
        assert _init(client, "/manager_mcp", admin()) == 200


def test_manager_endpoint_rejects_bearer(cfg):
    """入口 token 打不开管理端点：管理面只认 Basic。"""
    client, token = _setup(cfg)
    with client:
        assert _init(client, "/manager_mcp", bearer(token)) == 403


def test_manager_endpoint_exposes_admin_tools_only(cfg):
    """管理端点只管配置：不发消息（发送唯一入口是 /mcp）。"""
    with make_client(cfg) as client:
        tools = _tools(client, "/manager_mcp", admin())
        assert "send_message" not in tools
        assert {"entry_list", "rule_create", "channel_delete", "message_logs", "route_preview"} <= tools
        assert len(tools) == 17


def test_manager_endpoint_cannot_send_message(cfg):
    """管理端点直接点名调 send_message 也是「工具不存在」。"""
    with make_client(cfg) as client:
        _status, body = _rpc(
            client,
            "/manager_mcp",
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "send_message", "arguments": {"text": "x"}}},
            admin(),
        )
        assert body["result"]["isError"] is True
        assert "Unknown tool" in body["result"]["content"][0]["text"]
