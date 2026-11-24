"""MCP 工具定义（Streamable HTTP，两个端点）。

    /mcp           调用面：免 HTTP 认证，只有 send_message（参数里带 token）
    /manager_mcp   管理面：必须 Basic 认证，全量工具（出口 / 入口 / 规则 / 日志）

两个端点各建一个 MCPServer 实例，工具集不同，别的都一样。


工具契约只有一份，在 `msg_hub.tools`（stdio 前端共用）。本模块只负责把契约
翻译成官方 SDK 能理解的函数签名：

    tools.py      契约：工具名 + 描述 + 参数 JSON Schema + 处理函数
    mcp_server.py JSON Schema → Python 注解 → SDK 据此生成 inputSchema

这样两个前端对模型暴露的工具面完全一致：同名、同描述、同参数、同默认值、
同必填性，行为也一致（缺参数都是校验错误，未知参数都被忽略）。
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Any, Callable, Literal, Optional, TypedDict, Union

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import __version__
from .models import HubError
from .service import Hub
from .tools import INSTRUCTIONS, TOOLS, Param, ToolSpec, guard

# JSON Schema 基础类型 → Python 类型
_SIMPLE_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict[str, Any],
    "array": list[Any],
}


def _typed_dict(schema: dict[str, Any]) -> Any:
    """带 properties 的 object → TypedDict，让 SDK 生成完整的嵌套 schema。

    否则模型只看到 {"type": "object"}，全靠 description 猜里面能填什么。
    """
    props = schema.get("properties") or {}
    annotations: dict[str, Any] = {}
    for name, sub in props.items():
        if not isinstance(sub, dict):
            continue
        types = _types_of(sub)
        # 多类型（如 array | string）要保留成 Union，否则 pydantic 会把单一写法拒掉
        inner = types[0] if len(types) == 1 else Union[types]  # type: ignore[arg-type]
        annotations[name] = Optional[inner]
    return TypedDict("Extra", annotations, total=False)  # type: ignore[operator]


def _types_of(schema: dict[str, Any]) -> tuple[Any, ...]:
    """JSON Schema 片段 → 可用类型元组。"""
    if "anyOf" in schema:
        found: list[Any] = []
        for branch in schema["anyOf"]:
            for item in _types_of(branch):
                if item not in found:
                    found.append(item)
        return tuple(found)
    if schema.get("type") == "string" and schema.get("enum"):
        return (Literal[tuple(schema["enum"])],)
    kind = schema.get("type")
    if kind == "object" and isinstance(schema.get("properties"), dict) and schema["properties"]:
        return (_typed_dict(schema),)
    if kind == "array":
        items = schema.get("items")
        inner = _types_of(items)[0] if isinstance(items, dict) else Any
        return (list[inner],)
    return (_SIMPLE_TYPES.get(kind, Any),)


def _annotation(param: Param) -> Any:
    """参数 → Python 注解（SDK 用它反推 inputSchema）。

    可选参数的默认值类型也要能被接受（如默认 None → 允许 null）。
    """
    types = _types_of(param.schema)
    if param.required:
        candidates = types
    else:
        extra = type(None) if param.default is None else type(param.default)
        candidates = tuple(item for item in types if item is not type(None))
        if extra not in candidates:
            candidates = candidates + (extra,)
    annotation = candidates[0] if len(candidates) == 1 else Union[candidates]
    if param.description:
        annotation = Annotated[annotation, Field(description=param.description)]
    return annotation


def _sdk_tool(spec: ToolSpec, hub: Hub) -> Callable[..., Any]:
    """把契约里的一个工具包成 SDK 可调用的函数。"""

    def tool(**kwargs: Any) -> dict[str, Any]:
        try:
            guard(spec)
            return spec.handler(hub, **kwargs)
        except HubError as exc:
            # 业务错误转成 ToolError：模型能看到 code / message / retryable 与候选列表
            raise ToolError(json.dumps(exc.to_dict()["error"], ensure_ascii=False)) from exc

    annotations = {param.name: _annotation(param) for param in spec.params}
    parameters = [
        inspect.Parameter(
            param.name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=annotations[param.name],
            **({} if param.required else {"default": param.default}),
        )
        for param in spec.params
    ]
    tool.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])  # type: ignore[arg-type]
    tool.__annotations__ = {**annotations, "return": dict[str, Any]}
    tool.__name__ = spec.name
    tool.__doc__ = spec.description
    return tool


SENDER_INSTRUCTIONS = (
    "消息发送端点：只提供 send_message 一个工具。\n"
    "调用时必须提供 token（入口的 API Token，mh_ 开头）和 msg（正文）。\n"
    "**不要把出口写在参数里**：发到哪些出口由服务端的入口标签 + 路由规则决定。\n"
    "token 无效会返回 unauthorized；只想验证配置可以先用 dry_run=true。"
)


def build_mcp(
    hub: Hub,
    tools: tuple[ToolSpec, ...] | None = None,
    *,
    title: str = "消息渠道中心",
    instructions: str = INSTRUCTIONS,
) -> MCPServer:
    """建一个 MCP 服务实例。

    tools 默认是全量（stdio 形态用这个）；HTTP 两个端点各自传：
        /mcp          → SENDER_TOOLS（1 个，token + msg）
        /manager_mcp  → ADMIN_TOOLS（17 个管理工具，**不含 send_message**）
    """
    mcp = MCPServer(
        name="msg-hub",
        title=title,
        version=__version__,
        instructions=instructions,
    )
    for spec in (TOOLS if tools is None else tools):
        mcp.add_tool(_sdk_tool(spec, hub), name=spec.name, description=spec.description)
    return mcp
