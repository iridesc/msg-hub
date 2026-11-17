"""MCP 工具定义（Streamable HTTP，路径 /mcp）。

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
from typing import Annotated, Any, Callable, Literal, Union

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


def build_mcp(hub: Hub) -> MCPServer:
    mcp = MCPServer(
        name="msg-hub",
        title="消息渠道中心",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    for spec in TOOLS:
        mcp.add_tool(_sdk_tool(spec, hub), name=spec.name, description=spec.description)
    return mcp
