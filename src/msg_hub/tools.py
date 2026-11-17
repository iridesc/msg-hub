"""MCP 工具契约：stdio 与 HTTP 两个前端共用同一份定义。

为什么单独放一个模块：stdio 模式要零第三方依赖（手写 MCP 协议），HTTP 模式用
官方 SDK。若两个前端各维护一份「工具名 + 描述 + 参数 + 处理函数」，必然漂移——
历史上 `send_message` 的 @ 参数归一化就只在 stdio 侧实现过，HTTP 侧会直接报错。

本模块只依赖标准库（不 import mcp SDK），保证 stdio 模式零依赖可用：

    tools.py      契约（纯数据 + 处理函数）—— 唯一来源
    stdio.py      直接用契约里的 JSON Schema 生成 tools/list
    mcp_server.py 把契约里的 JSON Schema 翻译成 Python 注解，交给 SDK 生成 schema

处理函数统一签名 `(hub, **参数)`；缺参数由函数签名抛 TypeError，SDK 侧转成
校验错误，stdio 侧转成 JSON-RPC -32602。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import HubError
from .service import Hub
from .auth import current_identity

# 服务器级说明（写给模型的用法指引），stdio 与 HTTP 两个前端共用
INSTRUCTIONS = (
    "统一的消息渠道中心：维护「渠道」（企业微信群机器人 / 邮箱）并通过渠道发消息。\n"
    "典型流程：1) 需要新建渠道时先调 channel_types 拿到该类型的 settings_schema；"
    "2) channel_create 创建；3) send_message 发送（可用 dry_run 先校验）。\n"
    "只记得群名时先 channel_list 确认 id；名称命中多个渠道时工具会报错并返回候选。"
)


class _Required:
    """哨兵：标记「该参数必填」。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "REQUIRED"


REQUIRED = _Required()


@dataclass(frozen=True)
class Param:
    """一个工具参数。

    schema: JSON Schema 片段（type / anyOf / enum / items…），是参数的**唯一类型来源**
    description: 给模型看的说明
    default: 默认值；REQUIRED 表示必填
    """

    name: str
    schema: dict[str, Any]
    description: str = ""
    default: Any = REQUIRED

    @property
    def required(self) -> bool:
        return self.default is REQUIRED


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整契约。"""

    name: str
    description: str
    params: tuple[Param, ...]
    handler: Callable[..., Any]
    # 是否只允许管理员（Basic）调用。API Token 身份只能发消息 + 读只读信息，
    # 碰管理类工具会被拒绝——即使它同时能访问 /mcp。
    admin_only: bool = False

    def input_schema(self) -> dict[str, Any]:
        """工具的 inputSchema（MCP 协议格式）。"""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.params:
            node = dict(param.schema)
            if param.description:
                node["description"] = param.description
            if param.required:
                required.append(param.name)
            else:
                node["default"] = param.default
            properties[param.name] = node
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema


# ── 参数片段（复用同一批定义，避免两处漂移）────────────────────
_CHANNEL = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
_STRING = {"type": "string"}
_OBJECT = {"type": "object"}
_BOOL = {"type": "boolean"}
# @成员：既接受数组也接受单个字符串（模型经常只传一个）
_MENTIONS = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]}


def _as_list(value: Any) -> list[str] | None:
    """把单个字符串归一化成列表；None/空串 → None。"""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def guard(spec: ToolSpec) -> None:
    """执行前的权限检查：管理类工具只允许管理员身份。

    token 身份能访问 /mcp（发消息用），但不能建/改/删渠道、不能看日志、
    不能生成 token——那属于管理面。
    """
    if not spec.admin_only:
        return
    identity = current_identity()
    if not identity.is_admin:
        raise HubError(
            "forbidden",
            f"工具 {spec.name} 只允许管理员调用（当前身份：{identity.describe()}）",
        )


# ── 处理函数（业务逻辑仍在 Hub，这里只做参数搬运）──────────────
def _channel_types(hub: Hub) -> dict[str, Any]:
    return {"types": hub.types()}


def _channel_list(hub: Hub, include_deleted: bool = False) -> dict[str, Any]:
    return {"channels": hub.list_channels(include_deleted=include_deleted)}


def _channel_get(hub: Hub, channel: str | int) -> dict[str, Any]:
    return hub.get_channel(channel)


def _channel_create(
    hub: Hub,
    name: str,
    type: str,  # noqa: A002 - 工具参数名就是 type
    settings: dict[str, Any],
    options: dict[str, Any] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    return hub.create_channel(
        name=name, type_name=type, settings=settings, options=options, enabled=enabled
    )


def _channel_update(
    hub: Hub,
    channel: str | int,
    name: str | None = None,
    type: str | None = None,  # noqa: A002 - 工具参数名就是 type
    settings: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    return hub.update_channel(
        channel,
        name=name,
        type_name=type,
        settings=settings,
        options=options,
        enabled=enabled,
    )


def _channel_delete(hub: Hub, channel: str | int) -> dict[str, Any]:
    return hub.delete_channel(channel)


def _send_message(
    hub: Hub,
    channel: str | int,
    text: str,
    format: str = "plain",  # noqa: A002 - 工具参数名就是 format
    title: str | None = None,
    mention_users: list[str] | str | None = None,
    mention_mobiles: list[str] | str | None = None,
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


def _channel_test(
    hub: Hub,
    channel: str | int = "",
    text: str = "这是一条来自 msg-hub 的测试消息",
    dry_run: bool = True,
    settings: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    type: str | None = None,  # noqa: A002 - 工具参数名就是 type
) -> dict[str, Any]:
    return hub.test_channel(
        channel,
        text,
        dry_run=dry_run,
        settings_override=settings,
        options_override=options,
        type_name=type,
    )


def _message_logs(hub: Hub, channel: str | int | None = None, limit: int = 50) -> dict[str, Any]:
    return {"logs": hub.logs(channel, limit=limit)}


# ── 工具清单 ──────────────────────────────────────────────────
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="channel_types",
        description=(
            "列出所有可用的渠道类型（wecom_bot 企业微信群机器人 / email 邮箱），"
            "返回每个类型的能力与 settings_schema。新建渠道前先调用它，按 schema 填 settings。"
        ),
        params=(),
        handler=_channel_types,
    ),
    ToolSpec(
        name="channel_list",
        description="列出已配置的消息渠道（id、名称、类型、是否可用）。发送前用它确认渠道 id。",
        params=(
            Param("include_deleted", _BOOL, "是否包含已删除渠道", False),
        ),
        handler=_channel_list,
    ),
    ToolSpec(
        name="channel_get",
        description="查看单个渠道详情（敏感字段已打码）。channel 可传 id（如 2）或名称（如 工作群1）。",
        params=(Param("channel", _CHANNEL, "渠道 id 或名称"),),
        handler=_channel_get,
    ),
    ToolSpec(
        name="channel_create",
        description=(
            "新建消息渠道。settings 必须符合该类型 manifest 的 settings_schema（先调 channel_types）。"
            "options 是跨类型护栏：allow_mention_all（是否允许 @所有人，默认 false）、"
            "rate_limit_per_min（每分钟发送上限）。"
        ),
        params=(
            Param("name", _STRING, "渠道名称，全局唯一，如 工作群1"),
            Param("type", _STRING, "渠道类型，如 wecom_bot / email"),
            Param("settings", _OBJECT, "该类型的配置（凭据）"),
            Param("options", _OBJECT, "护栏：allow_mention_all / rate_limit_per_min", None),
            Param("enabled", _BOOL, "是否启用", True),
        ),
        handler=_channel_create,
        admin_only=True,
    ),
    ToolSpec(
        name="channel_update",
        description=(
            "修改渠道：名称、类型、配置或护栏。只传需要改的字段；"
            "更换 type 时必须同时提供完整的 settings。"
        ),
        params=(
            Param("channel", _CHANNEL, "渠道 id 或名称"),
            Param("name", _STRING, "新名称", None),
            Param("type", _STRING, "新类型（如同时给出完整 settings）", None),
            Param("settings", _OBJECT, "新的完整配置", None),
            Param("options", _OBJECT, "新的护栏配置", None),
            Param("enabled", _BOOL, "是否启用", None),
        ),
        handler=_channel_update,
        admin_only=True,
    ),
    ToolSpec(
        name="channel_delete",
        description="删除渠道（软删除：配置移到 data/config/.trash/，可人工恢复）。",
        params=(Param("channel", _CHANNEL, "渠道 id 或名称"),),
        handler=_channel_delete,
        admin_only=True,
    ),
    ToolSpec(
        name="send_message",
        description=(
            "通过渠道发送消息。channel 传 id 或名称；text 为正文；format 支持 plain / markdown；"
            "title 用作邮件主题（企微忽略）。dry_run=true 只校验不真发。"
            "mention_users / mention_mobiles 需类型支持，mention_all 需渠道开启 allow_mention_all。"
        ),
        params=(
            Param("channel", _CHANNEL, "渠道 id 或名称"),
            Param("text", _STRING, "消息正文"),
            Param("format", {**_STRING, "enum": ["plain", "markdown"]}, "正文格式", "plain"),
            Param("title", _STRING, "标题（邮件主题）", None),
            Param("mention_users", _MENTIONS, "@成员 userid 列表（也可只给单个）", None),
            Param("mention_mobiles", _MENTIONS, "@成员手机号列表（也可只给单个）", None),
            Param("mention_all", _BOOL, "@所有人（需渠道授权）", False),
            Param("dry_run", _BOOL, "只校验不真发", False),
        ),
        handler=_send_message,
    ),
    ToolSpec(
        name="channel_test",
        description=(
            "测试渠道配置：默认 dry_run（只做配置校验与连通性探测，不真发）；dry_run=false 会真发一条测试消息。"
            "也可以对「尚未保存」的配置做试探：传 settings（配合 type）即按临时配置测试。"
        ),
        params=(
            Param("channel", _CHANNEL, "渠道 id 或名称（测未保存配置时可留空）", ""),
            Param("text", _STRING, "测试消息正文", "这是一条来自 msg-hub 的测试消息"),
            Param("dry_run", _BOOL, "只校验不真发", True),
            Param("settings", _OBJECT, "未保存的配置（临时文件测试）", None),
            Param("options", _OBJECT, "护栏配置", None),
            Param("type", _STRING, "渠道类型（配合 settings 使用）", None),
        ),
        handler=_channel_test,
        admin_only=True,
    ),
    ToolSpec(
        name="message_logs",
        description="查询发送审计日志（默认最近 50 条，可按渠道过滤）。用来确认消息是否真的发出去了。",
        params=(
            Param("channel", _CHANNEL, "渠道 id 或名称（留空查全部）", None),
            Param("limit", {"type": "integer"}, "返回条数（1-500）", 50),
        ),
        handler=_message_logs,
        admin_only=True,
    ),
)
