"""MCP / REST 的工具契约（纯数据 + 处理函数）—— 唯一来源。

历史上 `send_message` 的 @ 参数归一化就只在 stdio 侧实现过，HTTP 侧会直接报错。
本模块只依赖标准库（不 import mcp SDK），保证 stdio 模式零依赖可用：

    tools.py      契约（纯数据 + 处理函数）—— 唯一来源
    stdio.py      直接用契约里的 JSON Schema 生成 tools/list
    mcp_server.py 把契约里的 JSON Schema 翻译成 Python 注解，交给 SDK 生成 schema

处理函数统一签名 `(hub, **参数)`；缺参数由函数签名抛 TypeError，SDK 侧转成
校验错误，stdio 侧转成 JSON-RPC -32602。

发送模型（v3）：调用方只传 token + 正文，**不指定出口**；
出口由「入口标签 + 路由规则」决定。管理员可用 channel 直连（应急/调试）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .auth import Identity, current_identity, reset_identity, set_identity
from .models import HubError
from .service import Hub

# 服务器级说明（写给模型的用法指引），stdio 与 HTTP 两个前端共用
INSTRUCTIONS = (
    "统一的消息路由中心：入口（持有 token 的调用方）→ 路由规则 → 出口（企业微信群机器人 / 邮箱）。\n"
    "入口/出口/规则都用**名称**标识（没有 id），名称全局唯一，创建时重名会被拒绝。\n"
    "发消息时只给正文，**不要指定出口**：投递到哪些出口由服务端的路由规则决定。\n"
    "典型流程：1) channel_types 拿 settings_schema；2) channel_create 建出口并打上 tags；"
    "3) entry_create 建入口（拿到 token，只返回一次）并给它打 tags；"
    "4) rule_create 建规则把入口标签映射到出口标签；"
    "5) send_message 发送（可先 dry_run）；6) route_preview 可查看某个入口会投递到哪些出口。\n"
    "排查「为什么没发出去」时先调 route_preview，它会返回每条规则的判定明细。"
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
    # 是否只允许管理员（Basic）调用。入口身份只能发消息 + 读只读信息，
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
_STRING = {"type": "string"}
_OBJECT = {"type": "object"}
_BOOL = {"type": "boolean"}
# @成员：既接受数组也接受单个字符串（模型经常只传一个）
_MENTIONS = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]}
# 标签：数组或逗号分隔字符串
_TAGS = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]}
# 路由条件：{field, op, value}
_CONDITION = {"type": "object"}

# 消息体的控制字段：全部收进 extra，顶层只留正文 + dry_run。
# 这里不列具体字段 —— 出口相关参数是「出口类型」的概念，核心不解析，
# 硬编码一份字段表反而会挡住新类型自己的参数。
_EXTRA = {"type": "object"}

EXTRA_DESCRIPTION = (
    "可选的控制字段，原样透传给出口，不传就用出口自己的默认值。\n"
    "常用字段（企业微信）：format（plain/markdown）、title（邮件主题）、"
    "mention_users（@成员 userid）、mention_mobiles（@成员手机号）、"
    "mention_all（@所有人，需出口开启授权）。\n"
    "不同出口类型支持的字段不一样，以出口自己的要求为准。"
)

SEND_DESCRIPTION = (
    "发送消息。正文放 text（调用面用 msg），可选控制字段放 extra。\n"
    "**不要也不要想着指定出口**：投递目标由服务端的入口标签 + 路由规则决定，"
    "响应里的 results 会列出每个出口的结果。dry_run=true 只校验不真发。\n"
    "只接受 text/msg、dry_run、extra 三个参数（调用面另加 token）；"
    "format、title、mention_* 这些必须放进 extra，写在顶层会被拒绝。"
)


def guard(spec: ToolSpec) -> None:
    """执行前的权限检查：管理类工具只允许管理员身份。

    入口身份能访问 /mcp（发消息用），但不能建/改/删渠道、入口、规则，
    也不能看日志或路由拓扑——那属于管理面。
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
    tags: list[str] | str | None = None,
) -> dict[str, Any]:
    return hub.create_channel(
        name=name,
        type_name=type,
        settings=settings,
        options=options,
        enabled=enabled,
        tags=tags,
    )


def _channel_update(
    hub: Hub,
    channel: str | int,
    name: str | None = None,
    type: str | None = None,  # noqa: A002 - 工具参数名就是 type
    settings: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    enabled: bool | None = None,
    tags: list[str] | str | None = None,
) -> dict[str, Any]:
    return hub.update_channel(
        channel,
        name=name,
        type_name=type,
        settings=settings,
        options=options,
        enabled=enabled,
        tags=tags,
    )


def _channel_delete(hub: Hub, channel: str | int) -> dict[str, Any]:
    return hub.delete_channel(channel)


def _send_message(
    hub: Hub,
    text: str,
    dry_run: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return hub.send(text, dry_run=dry_run, source="mcp", extra=extra)


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


# ── 入口 ──────────────────────────────────────────────────
def _entry_list(hub: Hub) -> dict[str, Any]:
    return {"entries": hub.list_entries()}


def _entry_create(
    hub: Hub,
    name: str,
    tags: list[str] | str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    return {"ok": True, "entry": hub.create_entry(name, tags=tags, enabled=enabled)}


def _entry_update(
    hub: Hub,
    entry: str | int,
    name: str | None = None,
    tags: list[str] | str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    return {"ok": True, "entry": hub.update_entry(entry, name=name, tags=tags, enabled=enabled)}


def _entry_delete(hub: Hub, entry: str | int) -> dict[str, Any]:
    return hub.delete_entry(entry)


# ── 路由规则 ──────────────────────────────────────────────
def _rule_list(hub: Hub) -> dict[str, Any]:
    return {"rules": hub.list_rules()}


def _rule_create(
    hub: Hub,
    name: str,
    outbound: dict[str, Any],
    entry: dict[str, Any] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    return {"ok": True, "rule": hub.create_rule(name, outbound, entry=entry, enabled=enabled)}


def _rule_update(
    hub: Hub,
    rule: str | int,
    name: str | None = None,
    outbound: dict[str, Any] | None = None,
    entry: dict[str, Any] | None = None,
    clear_entry: bool = False,
    enabled: bool | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "rule": hub.update_rule(
            rule,
            name=name,
            outbound=outbound,
            entry=entry,
            clear_entry=clear_entry,
            enabled=enabled,
        ),
    }


def _rule_delete(hub: Hub, rule: str | int) -> dict[str, Any]:
    return hub.delete_rule(rule)


def _route_preview(hub: Hub, entry: str | int | None = None) -> dict[str, Any]:
    return hub.route_preview(entry)


_CONDITION_HELP = (
    '形状 {"field": "…", "op": "…", "value": "…"}。'
    "入口条件 field 支持 entry（入口名称/id）、tag（入口标签）；"
    "出口条件 field 支持 name（出口名称）、type（出口类型）、tag（出口标签）。"
    "op 支持 exact / contains / prefix / suffix，大小写不敏感。"
)


# ── 工具清单 ──────────────────────────────────────────────────
ADMIN_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="channel_types",
        description=(
            "列出所有可用的出口类型（wecom_bot 企业微信群机器人 / email 邮箱），"
            "返回每个类型的能力与 settings_schema。新建出口前先调用它，按 schema 填 settings。"
        ),
        params=(),
        handler=_channel_types,
    ),
    ToolSpec(
        name="channel_list",
        description=(
            "列出已配置的出口（id、名称、类型、标签 tags、是否可用）。"
            "发消息不需要指定出口，但建路由规则时要靠这里的 tags。"
        ),
        params=(
            Param("include_deleted", _BOOL, "是否包含已删除出口", False),
        ),
        handler=_channel_list,
    ),
    ToolSpec(
        name="channel_get",
        description="查看单个出口详情（敏感字段已打码）。channel 传出口名称。",
        params=(Param("channel", _STRING, "出口名称"),),
        handler=_channel_get,
    ),
    ToolSpec(
        name="channel_create",
        description=(
            "新建出口（通知渠道）。settings 必须符合该类型 manifest 的 settings_schema（先调 channel_types）。"
            "tags 是路由用的标签，建议 key:value 形式（如 kind:alert、env:prod），可被 prefix/suffix 匹配。"
            "options 是跨类型护栏：allow_mention_all、rate_limit_per_min。"
        ),
        params=(
            Param("name", _STRING, "出口名称，全局唯一，如 运维告警群"),
            Param("type", _STRING, "出口类型，如 wecom_bot / email"),
            Param("settings", _OBJECT, "该类型的配置（凭据）"),
            Param("options", _OBJECT, "护栏：allow_mention_all / rate_limit_per_min", None),
            Param("enabled", _BOOL, "是否启用", True),
            Param("tags", _TAGS, "路由标签，数组或逗号分隔字符串（如 kind:alert,env:prod）", None),
        ),
        handler=_channel_create,
        admin_only=True,
    ),
    ToolSpec(
        name="channel_update",
        description=(
            "修改出口：名称、类型、配置、护栏或标签。只传需要改的字段；"
            "更换 type 时必须同时提供完整的 settings。"
        ),
        params=(
            Param("channel", _STRING, "出口名称"),
            Param("name", _STRING, "新名称", None),
            Param("type", _STRING, "新类型（如同时给出完整 settings）", None),
            Param("settings", _OBJECT, "新的完整配置", None),
            Param("options", _OBJECT, "新的护栏配置", None),
            Param("enabled", _BOOL, "是否启用", None),
            Param("tags", _TAGS, "新的标签集合（整体替换）", None),
        ),
        handler=_channel_update,
        admin_only=True,
    ),
    ToolSpec(
        name="channel_delete",
        description="删除出口（软删除，配置留档，可人工恢复）。",
        params=(Param("channel", _STRING, "出口名称"),),
        handler=_channel_delete,
        admin_only=True,
    ),
    ToolSpec(
        name="entry_list",
        description=(
            "列出所有入口（持 token 的调用方）：id、名称、标签、**明文 token**、是否启用、用量。"
            "token 随时可查，丢了不用重建入口。"
        ),
        params=(),
        handler=_entry_list,
        admin_only=True,
    ),
    ToolSpec(
        name="entry_create",
        description=(
            "新建入口并生成一把 token（之后可随时用 entry_list 查回明文）。"
            "tags 决定能命中哪些路由规则，建议 key:value 形式（如 app:pibox、kind:alert）。"
        ),
        params=(
            Param("name", _STRING, "入口名称，全局唯一，如 pibox-alert"),
            Param("tags", _TAGS, "入口标签，数组或逗号分隔字符串", None),
            Param("enabled", _BOOL, "是否启用", True),
        ),
        handler=_entry_create,
        admin_only=True,
    ),
    ToolSpec(
        name="entry_update",
        description="修改入口：名称、标签、是否启用。不会更换 token。",
        params=(
            Param("entry", _STRING, "入口名称"),
            Param("name", _STRING, "新名称", None),
            Param("tags", _TAGS, "新的标签集合（整体替换）", None),
            Param("enabled", _BOOL, "是否启用", None),
        ),
        handler=_entry_update,
        admin_only=True,
    ),
    ToolSpec(
        name="entry_delete",
        description="删除入口（该 token 立即失效）。名称即标识，没有 id。",
        params=(Param("entry", _STRING, "入口名称"),),
        handler=_entry_delete,
        admin_only=True,
    ),
    ToolSpec(
        name="rule_list",
        description="列出所有路由规则（入口条件 + 出口条件）。规则用名称标识。",
        params=(),
        handler=_rule_list,
        admin_only=True,
    ),
    ToolSpec(
        name="rule_create",
        description=(
            "新建路由规则。一条规则 = 一个入口条件 + 一个出口条件，两者都是单条件。"
            "entry 留空表示匹配所有入口。规则之间是累加关系：命中的所有规则的出口取并集去重。"
            + _CONDITION_HELP
        ),
        params=(
            Param("name", _STRING, "规则名称，全局唯一"),
            Param("outbound", _CONDITION, "出口条件"),
            Param("entry", _CONDITION, "入口条件（留空 = 所有入口）", None),
            Param("enabled", _BOOL, "是否启用", True),
        ),
        handler=_rule_create,
        admin_only=True,
    ),
    ToolSpec(
        name="rule_update",
        description="修改规则：名称、入口条件、出口条件、是否启用。clear_entry=true 可清空入口条件。",
        params=(
            Param("rule", _STRING, "规则名称"),
            Param("name", _STRING, "新名称", None),
            Param("outbound", _CONDITION, "新的出口条件", None),
            Param("entry", _CONDITION, "新的入口条件", None),
            Param("clear_entry", _BOOL, "清空入口条件（变为匹配所有入口）", False),
            Param("enabled", _BOOL, "是否启用", None),
        ),
        handler=_rule_update,
        admin_only=True,
    ),
    ToolSpec(
        name="rule_delete",
        description="删除路由规则。",
        params=(Param("rule", _STRING, "规则名称"),),
        handler=_rule_delete,
        admin_only=True,
    ),
    ToolSpec(
        name="route_preview",
        description=(
            "查看某个入口会投递到哪些出口（不发送）。返回命中的出口、命中的规则，"
            "以及每条规则的判定明细 trace，用来排查「为什么没发出去 / 为什么发到了这个群」。"
            "entry 留空则按管理员视角（只有入口条件为空的规则会命中）。入口/出口都用名称。"
        ),
        params=(Param("entry", _STRING, "入口 id 或名称（留空=管理员视角）", None),),
        handler=_route_preview,
        admin_only=True,
    ),
    ToolSpec(
        name="channel_test",
        description=(
            "测试出口配置：默认 dry_run（只做配置校验，不真发）；dry_run=false 会真发一条测试消息。"
            "也可以对「尚未保存」的配置做试探：传 settings（配合 type）即按临时配置测试。"
            "这是管理员工具，不受路由规则影响。"
        ),
        params=(
            Param("channel", _STRING, "出口名称（测未保存配置时可留空）", ""),
            Param("text", _STRING, "测试消息正文", "这是一条来自 msg-hub 的测试消息"),
            Param("dry_run", _BOOL, "只校验不真发", True),
            Param("settings", _OBJECT, "未保存的配置（临时配置测试）", None),
            Param("options", _OBJECT, "护栏配置", None),
            Param("type", _STRING, "出口类型（配合 settings 使用）", None),
        ),
        handler=_channel_test,
        admin_only=True,
    ),
    ToolSpec(
        name="message_logs",
        description="查询发送审计日志（默认最近 50 条，可按出口过滤）。用来确认消息是否真的发出去了。",
        params=(
            Param("channel", _STRING, "出口名称（留空查全部）", None),
            Param("limit", {"type": "integer"}, "返回条数（1-500）", 50),
        ),
        handler=_message_logs,
        admin_only=True,
    ),
)


# ── 调用面（/mcp，免 HTTP 认证）────────────────────────────
# 只暴露一个工具：token + msg。身份来自参数里的 token，而不是 Authorization 头，
# 所以调用方不需要配置任何认证头。
def _send_with_token(
    hub: Hub,
    token: str,
    msg: str,
    dry_run: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = hub.store.find_entry_by_secret(str(token or ""))
    if entry is None:
        # 不区分「不存在」和「已停用」：否则等于送出一个「哪些 token 存在」的探针
        raise HubError("unauthorized", "token 不存在或已停用")

    identity = Identity(
        role="entry",
        entry_name=entry.name,
        entry_prefix=entry.prefix,
        entry_tags=tuple(entry.tags),
    )
    ctx = set_identity(identity)
    try:
        hub.store.touch_entry(entry.name)
        return hub.send(msg, dry_run=dry_run, source="mcp", extra=extra)
    finally:
        reset_identity(ctx)


# 管理员版发送（带 channel 直连）。只给 stdio 形态用——HTTP 的管理端点不发消息，
# 发送一律走 /mcp 调用面，保持「发送只有一条路」。
ADMIN_SEND_TOOL = ToolSpec(
    name="send_message",
    description=SEND_DESCRIPTION,
    params=(
        Param("text", _STRING, "消息正文"),
        Param("dry_run", _BOOL, "只校验不真发", False),
        Param("extra", _EXTRA, EXTRA_DESCRIPTION, None),
    ),
    handler=_send_message,
)


# 调用面（/mcp）：只有这一个工具
SENDER_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="send_message",
        description=SEND_DESCRIPTION + "token 无效时返回 unauthorized。",
        params=(
            Param("token", _STRING, "入口的 API Token（mh_ 开头）"),
            Param("msg", _STRING, "消息正文"),
            Param("dry_run", _BOOL, "只校验不真发", False),
            Param("extra", _EXTRA, EXTRA_DESCRIPTION, None),
        ),
        handler=_send_with_token,
    ),
)

# stdio 形态在宿主进程里跑（天然在信任边界内）：管理工具 + 管理员版发送
TOOLS: tuple[ToolSpec, ...] = ADMIN_TOOLS + (ADMIN_SEND_TOOL,)
