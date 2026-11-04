#!/usr/bin/env python3
"""msg-hub 的 stdio MCP 入口（零第三方依赖）。

用法（pi-box / Claude Desktop 等通过 stdio 拉起）：

    python3 /opt/msg-hub/src/msg_hub/stdio.py

可直接执行源码树（无需 pip install）：脚本会把 src/ 加进 sys.path。
数据目录默认 ~/.msg-hub，可用 MSG_HUB_DATA_DIR 覆盖（pi-box 里建议指向
/home/agent/.pi-boxd/msg-hub，随 agent-home 持久化）。

实现说明：MCP stdio 传输 = 换行分隔的 JSON-RPC 2.0。只实现 pi-mcp 等客户端
会用到的方法：initialize / notifications/initialized / ping / tools/list / tools/call。
不依赖官方 mcp SDK，使其能在仅有 python3 的环境（如 pi-box 容器）中运行。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

# 允许直接执行源码树文件（pi-box 挂载场景），把 src/ 加进模块搜索路径
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from msg_hub import __version__  # noqa: E402
from msg_hub.models import HubError  # noqa: E402
from msg_hub.service import Hub  # noqa: E402
from msg_hub.settings import Settings  # noqa: E402

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

SERVER_INFO = {"name": "msg-hub", "title": "消息渠道中心", "version": __version__}

INSTRUCTIONS = (
    "统一的消息渠道中心：维护「渠道」（企业微信群机器人 / 邮箱）并通过渠道发消息。\n"
    "典型流程：1) 需要新建渠道时先调 channel_types 拿到该类型的 settings_schema；"
    "2) channel_create 创建；3) send_message 发送（可用 dry_run 先校验）。\n"
    "只记得群名时先 channel_list 确认 id；名称命中多个渠道时工具会报错并返回候选。"
)

_CHANNEL = {"anyOf": [{"type": "string"}, {"type": "integer"}], "description": "渠道 id（如 2）或名称（如 工作群1）"}


def _log(message: str) -> None:
    print(f"[msg-hub] {message}", file=sys.stderr, flush=True)


# ── 工具注册表 ────────────────────────────────────────────
class ToolSpec:
    def __init__(self, name: str, description: str, schema: dict[str, Any], handler: Callable[..., Any]) -> None:
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler

    def to_mcp(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


def _apply_argument_defaults(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    merged = dict(arguments)
    for name, prop in (schema.get("properties") or {}).items():
        if name not in merged and isinstance(prop, dict) and "default" in prop:
            merged[name] = prop["default"]
    return merged


def _as_list(value: Any) -> list[Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value
    return [value]


def build_tools(hub: Hub) -> list[ToolSpec]:
    tools: list[ToolSpec] = []

    def tool(name: str, description: str, schema: dict[str, Any]) -> Callable[[Callable[..., Any]], None]:
        def register(handler: Callable[..., Any]) -> None:
            tools.append(ToolSpec(name, description, schema, handler))

        return register

    @tool(
        "channel_types",
        "列出所有可用的渠道类型（wecom_bot 企业微信群机器人 / email 邮箱），返回每个类型的能力与"
        "settings_schema。新建渠道前先调用它，按 schema 填 settings。",
        {"type": "object", "properties": {}, "required": []},
    )
    def channel_types() -> dict[str, Any]:
        return {"types": hub.types()}

    @tool(
        "channel_list",
        "列出已配置的消息渠道（id、名称、类型、是否可用）。发送前用它确认渠道 id。",
        {
            "type": "object",
            "properties": {
                "include_deleted": {"type": "boolean", "default": False, "description": "是否包含已删除渠道"}
            },
            "required": [],
        },
    )
    def channel_list(include_deleted: bool = False) -> dict[str, Any]:
        return {"channels": hub.list_channels(include_deleted=include_deleted)}

    @tool(
        "channel_get",
        "查看单个渠道详情（敏感字段已打码）。channel 可传 id 或名称。",
        {"type": "object", "properties": {"channel": _CHANNEL}, "required": ["channel"]},
    )
    def channel_get(channel: str | int) -> dict[str, Any]:
        return hub.get_channel(channel)

    @tool(
        "channel_create",
        "新建消息渠道。settings 必须符合该类型 manifest 的 settings_schema（先调 channel_types）。"
        "options 是跨类型护栏：allow_mention_all（是否允许 @所有人，默认 false）、"
        "rate_limit_per_min（每分钟发送上限）。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "渠道名称，全局唯一，如 工作群1"},
                "type": {"type": "string", "description": "渠道类型，如 wecom_bot / email"},
                "settings": {"type": "object", "description": "该类型的配置（凭据）"},
                "options": {"type": "object", "description": "护栏：allow_mention_all / rate_limit_per_min"},
                "enabled": {"type": "boolean", "default": True},
            },
            "required": ["name", "type", "settings"],
        },
    )
    def channel_create(
        name: str, type: str, settings: dict[str, Any], options: dict[str, Any] | None = None, enabled: bool = True
    ) -> dict[str, Any]:
        return hub.create_channel(name=name, type_name=type, settings=settings, options=options, enabled=enabled)

    @tool(
        "channel_update",
        "修改渠道：名称、类型、配置或护栏。只传需要改的字段；更换 type 时必须同时提供完整的 settings。",
        {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "name": {"type": "string"},
                "type": {"type": "string"},
                "settings": {"type": "object"},
                "options": {"type": "object"},
                "enabled": {"type": "boolean"},
            },
            "required": ["channel"],
        },
    )
    def channel_update(
        channel: str | int,
        name: str | None = None,
        type: str | None = None,
        settings: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        return hub.update_channel(
            channel, name=name, type_name=type, settings=settings, options=options, enabled=enabled
        )

    @tool(
        "channel_delete",
        "删除渠道（软删除：配置移到 data/config/.trash/，可人工恢复）。",
        {"type": "object", "properties": {"channel": _CHANNEL}, "required": ["channel"]},
    )
    def channel_delete(channel: str | int) -> dict[str, Any]:
        return hub.delete_channel(channel)

    @tool(
        "send_message",
        "通过渠道发送消息。channel 传 id 或名称；text 为正文；format 支持 plain / markdown；"
        "title 用作邮件主题（企微忽略）。dry_run=true 只校验不真发。"
        "mention_users / mention_mobiles 需类型支持，mention_all 需渠道开启 allow_mention_all。",
        {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "text": {"type": "string", "description": "消息正文"},
                "format": {"type": "string", "enum": ["plain", "markdown"], "default": "plain"},
                "title": {"type": "string", "description": "标题（邮件主题）"},
                "mention_users": {"type": "array", "items": {"type": "string"}, "description": "@成员 userid 列表"},
                "mention_mobiles": {"type": "array", "items": {"type": "string"}, "description": "@成员手机号列表"},
                "mention_all": {"type": "boolean", "default": False, "description": "@所有人（需渠道授权）"},
                "dry_run": {"type": "boolean", "default": False, "description": "只校验不真发"},
            },
            "required": ["channel", "text"],
        },
    )
    def send_message(
        channel: str | int,
        text: str,
        format: str = "plain",
        title: str | None = None,
        mention_users: list[str] | None = None,
        mention_mobiles: list[str] | None = None,
        mention_all: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return hub.send(
            channel,
            text,
            format=format,
            title=title,
            mention_users=_as_list(mention_users),
            mention_mobiles=_as_list(mention_mobiles),
            mention_all=mention_all,
            dry_run=dry_run,
            source="mcp",
        )

    @tool(
        "channel_test",
        "测试渠道配置：默认 dry_run（只做配置校验与连通性探测，不真发）；dry_run=false 会真发一条测试消息。"
        "也可以对「尚未保存」的配置做试探：传 settings（配合 type）即按临时配置测试。",
        {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "text": {"type": "string", "default": "这是一条来自 msg-hub 的测试消息"},
                "dry_run": {"type": "boolean", "default": True},
                "settings": {"type": "object", "description": "未保存的配置（临时文件测试）"},
                "options": {"type": "object"},
                "type": {"type": "string"},
            },
            "required": [],
        },
    )
    def channel_test(
        channel: str | int = "",
        text: str = "这是一条来自 msg-hub 的测试消息",
        dry_run: bool = True,
        settings: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        type: str | None = None,
    ) -> dict[str, Any]:
        return hub.test_channel(
            channel, text, dry_run=dry_run, settings_override=settings, options_override=options, type_name=type
        )

    @tool(
        "message_logs",
        "查询发送审计日志（默认最近 50 条，可按渠道过滤）。用来确认消息是否真的发出去了。",
        {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
    )
    def message_logs(channel: str | int | None = None, limit: int = 50) -> dict[str, Any]:
        return {"logs": hub.logs(channel, limit=limit)}

    return tools


# ── JSON-RPC / MCP 协议层 ─────────────────────────────────
class StdioServer:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.tools = {spec.name: spec for spec in build_tools(hub)}
        self.initialized = False

    # 输入输出
    def _send(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(
        self, request_id: Any, code: int, message: str, data: Any = None, **extra: Any
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        if extra:  # 容忍误传关键字参数，避免响应发不出去导致客户端假死
            error.setdefault("data", {}).update(extra)
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    # 方法分发
    def handle(self, message: dict[str, Any]) -> None:
        if "id" not in message:
            return  # notification（notifications/initialized、cancelled 等）直接忽略
        request_id = message["id"]
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            self._result(request_id, self._initialize(params))
        elif method == "ping":
            self._result(request_id, {})
        elif method == "tools/list":
            self._result(request_id, {"tools": [spec.to_mcp() for spec in self.tools.values()]})
        elif method == "tools/call":
            self._call_tool(request_id, params)
        elif method in ("resources/list", "prompts/list"):
            # 未实现的能力，按协议返回空列表
            self._result(request_id, {"resources" if method == "resources/list" else "prompts": []})
        else:
            self._error(request_id, -32601, f"未知方法：{method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or LATEST_PROTOCOL_VERSION)
        version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> None:
        name = str(params.get("name") or "")
        spec = self.tools.get(name)
        if spec is None:
            self._error(request_id, -32602, f"未知工具：{name}", {"available": sorted(self.tools)})
            return
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            self._error(request_id, -32602, "arguments 必须是对象")
            return
        arguments = _apply_argument_defaults(spec.schema, arguments)
        try:
            result = spec.handler(**arguments)
        except TypeError as exc:
            # 参数不匹配：缺必填 / 类型错误（注意 _error 的第 4 参是 data）
            self._error(request_id, -32602, f"参数不合法：{exc}", {"tool": name})
            return
        except HubError as exc:
            # 业务错误：作为工具结果（isError=true）返回，模型可读到结构化错误
            error = exc.to_dict()["error"]
            self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(error, ensure_ascii=False)}],
                    "structuredContent": {"error": error},
                    "isError": True,
                },
            )
            return
        except Exception as exc:  # 未预期异常：兜底，避免整个 server 崩掉
            _log(f"工具 {name} 执行异常：{exc}\n{traceback.format_exc()}")
            self._result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps({"code": "internal_error", "message": str(exc)}, ensure_ascii=False)}
                    ],
                    "isError": True,
                },
            )
            return
        self._result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": {"result": result},
                "isError": False,
            },
        )

    def run(self) -> int:
        _log(f"stdio MCP server 就绪：tools={len(self.tools)} data={self.hub.settings.data_dir}")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                _log(f"忽略非法 JSON 行：{line[:120]}")
                continue
            if not isinstance(message, dict):
                continue
            try:
                self.handle(message)
            except Exception as exc:  # pragma: no cover
                _log(f"处理消息异常：{exc}")
        return 0


def main() -> int:
    settings = Settings()
    if not settings.auth_enabled:
        pass  # stdio 模式由宿主进程（pi-box）拉起，天然在信任边界内，不需要 token
    hub = Hub(settings)
    return StdioServer(hub).run()


if __name__ == "__main__":
    raise SystemExit(main())
