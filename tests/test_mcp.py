from __future__ import annotations

import json

from mcp.server.mcpserver.exceptions import ToolError

from msg_hub.mcp_server import build_mcp
from msg_hub.tools import TOOLS

WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}


def _error_payload(exc: ToolError) -> dict:
    """ToolError 文本形如 `Error executing tool x: {...}`，取出其中的 JSON。"""
    text = str(exc)
    start = text.find("{")
    assert start >= 0, text
    return json.loads(text[start:])


def _data(result):
    """优先取结构化结果（SDK 把返回值放在 structured_content['result']）。"""
    if isinstance(result.structured_content, dict) and "result" in result.structured_content:
        return result.structured_content["result"]
    return json.loads("".join(block.text for block in result.content if getattr(block, "type", "") == "text"))


async def test_tool_list_exposes_full_surface(cfg, hub):
    mcp = build_mcp(hub)
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == {
        "channel_types",
        "channel_list",
        "channel_get",
        "channel_create",
        "channel_update",
        "channel_delete",
        "entry_list",
        "entry_create",
        "entry_update",
        "entry_delete",
        "rule_list",
        "rule_create",
        "rule_update",
        "rule_delete",
        "route_preview",
        "send_message",
        "channel_test",
        "message_logs",
    }


async def test_sdk_advertises_the_same_contract_as_stdio(hub):
    """两个前端必须暴露同一份工具契约。

    契约的唯一来源是 msg_hub.tools；stdio 直接用它，SDK 侧由它翻译而来。
    这里比对两边广告给模型的 schema，防止以后又漂移出两份。
    """
    mcp = build_mcp(hub)
    sdk_tools = {tool.name: tool.input_schema for tool in await mcp.list_tools()}
    contract = {spec.name: spec.input_schema() for spec in TOOLS}
    assert set(sdk_tools) == set(contract)
    for name, schema in contract.items():
        advertised = sdk_tools[name]
        assert advertised.get("required", []) == schema.get("required", []), name
        assert set(advertised["properties"]) == set(schema["properties"]), name
        for param, prop in schema["properties"].items():
            assert advertised["properties"][param].get("default") == prop.get("default"), f"{name}.{param}"


async def test_mention_param_accepts_single_string(hub):
    """@成员既接受数组也接受单个字符串（两个前端行为一致）。"""
    mcp = build_mcp(hub)
    await mcp.call_tool(
        "channel_create",
        {"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS},
    )
    result = await mcp.call_tool(
        "send_message",
        {"channel": 1, "text": "hi", "mention_users": "zhangsan", "dry_run": True},
    )
    assert _data(result)["ok"] is True


async def test_types_tool_returns_schema(cfg, hub):
    mcp = build_mcp(hub)
    result = await mcp.call_tool("channel_types", {})
    types = _data(result)["types"]
    email = next(item for item in types if item["type"] == "email")
    assert "settings_schema" in email
    assert "smtp_host" in email["settings_schema"]["properties"]


async def test_create_send_and_delete_via_mcp(cfg, hub):
    mcp = build_mcp(hub)
    created = await mcp.call_tool(
        "channel_create",
        {"name": "工作群1", "type": "wecom_bot", "settings": WECOM_SETTINGS},
    )
    channel = _data(created)
    assert channel["id"] == 1

    sent = await mcp.call_tool("send_message", {"channel": "工作群1", "text": "hi", "dry_run": True})
    assert _data(sent)["ok"] is True

    logs = await mcp.call_tool("message_logs", {"channel": 1, "limit": 5})
    assert _data(logs)["logs"][0]["dry_run"] is True

    deleted = await mcp.call_tool("channel_delete", {"channel": "1"})
    assert _data(deleted)["ok"] is True
    assert _data(await mcp.call_tool("channel_list", {}))["channels"] == []


async def test_tool_errors_are_structured(cfg, hub):
    mcp = build_mcp(hub)
    try:
        await mcp.call_tool("send_message", {"channel": "不存在", "text": "hi"})
    except ToolError as exc:
        payload = _error_payload(exc)
        assert payload["code"] == "not_found"
        assert payload["retryable"] is False
    else:  # pragma: no cover
        raise AssertionError("应当抛出 ToolError")


async def test_mention_all_guard_via_mcp(cfg, hub):
    mcp = build_mcp(hub)
    await mcp.call_tool("channel_create", {"name": "群", "type": "wecom_bot", "settings": WECOM_SETTINGS})
    try:
        await mcp.call_tool("send_message", {"channel": 1, "text": "hi", "mention_all": True, "dry_run": True})
    except ToolError as exc:
        assert _error_payload(exc)["code"] == "mention_all_denied"
    else:  # pragma: no cover
        raise AssertionError("应当抛出 ToolError")
