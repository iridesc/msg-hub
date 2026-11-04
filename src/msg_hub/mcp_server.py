"""MCP 工具定义（Streamable HTTP，路径 /mcp）。

设计原则：
- 工具少、语义单一，模型好选
- 建渠道前先调 `channel_types` 拿 settings_schema，避免瞎填字段
- 渠道寻址支持 id 或名称；名称模糊命中多个时直接报错并给候选（绝不猜）
"""

from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .models import HubError
from .service import Hub

T = TypeVar("T")


def call(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """把 HubError 转成 ToolError（模型能看到 code/message/retryable 与候选列表）。"""
    try:
        return fn(*args, **kwargs)
    except HubError as exc:
        raise ToolError(json.dumps(exc.to_dict()["error"], ensure_ascii=False)) from exc


def build_mcp(hub: Hub) -> MCPServer:
    mcp = MCPServer(
        name="msg-hub",
        title="消息渠道中心",
        version="0.1.0",
        instructions=(
            "统一的消息渠道中心：维护「渠道」（企业微信群机器人 / 邮箱）并通过渠道发消息。\n"
            "典型流程：1) 需要新建渠道时先调 channel_types 拿到该类型的 settings_schema；"
            "2) channel_create 创建；3) send_message 发送（可用 dry_run 先校验）。\n"
            "只记得群名时先 channel_list 确认 id；名称命中多个渠道时工具会报错并返回候选。"
        ),
    )

    @mcp.tool(
        description=(
            "列出所有可用的渠道类型（wecom_bot 企业微信群机器人 / email 邮箱），"
            "返回每个类型的能力与 settings_schema。新建渠道前先调用它，按 schema 填 settings。"
        )
    )
    def channel_types() -> dict[str, Any]:
        return {"types": call(hub.types)}

    @mcp.tool(description="列出已配置的消息渠道（id、名称、类型、是否可用）。发送前用它确认渠道 id。")
    def channel_list(include_deleted: bool = False) -> dict[str, Any]:
        return {"channels": call(hub.list_channels, include_deleted=include_deleted)}

    @mcp.tool(
        description="查看单个渠道详情（敏感字段已打码）。channel 可传 id（如 2）或名称（如 工作群1）。"
    )
    def channel_get(channel: str | int) -> dict[str, Any]:
        return call(hub.get_channel, channel)

    @mcp.tool(
        description=(
            "新建消息渠道。settings 必须符合该类型 manifest 的 settings_schema（先调 channel_types）。"
            "options 是跨类型护栏：allow_mention_all（是否允许 @所有人，默认 false）、"
            "rate_limit_per_min（每分钟发送上限）。"
        )
    )
    def channel_create(
        name: str,
        type: str,
        settings: dict[str, Any],
        options: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        return call(
            hub.create_channel,
            name=name,
            type_name=type,
            settings=settings,
            options=options,
            enabled=enabled,
        )

    @mcp.tool(
        description=(
            "修改渠道：名称、类型、配置或护栏。只传需要改的字段；"
            "更换 type 时必须同时提供完整的 settings。"
        )
    )
    def channel_update(
        channel: str | int,
        name: str | None = None,
        type: str | None = None,
        settings: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        return call(
            hub.update_channel,
            channel,
            name=name,
            type_name=type,
            settings=settings,
            options=options,
            enabled=enabled,
        )

    @mcp.tool(description="删除渠道（软删除：配置移到 data/config/.trash/，可人工恢复）。")
    def channel_delete(channel: str | int) -> dict[str, Any]:
        return call(hub.delete_channel, channel)

    @mcp.tool(
        description=(
            "通过渠道发送消息。channel 传 id 或名称；text 为正文；format 支持 plain / markdown；"
            "title 用作邮件主题（企微忽略）。dry_run=true 只校验不真发。"
            "mention_users / mention_mobiles 需类型支持，mention_all 需渠道开启 allow_mention_all。"
        )
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
        return call(
            hub.send,
            channel,
            text,
            format=format,
            title=title,
            mention_users=mention_users,
            mention_mobiles=mention_mobiles,
            mention_all=mention_all,
            dry_run=dry_run,
            source="mcp",
        )

    @mcp.tool(
        description=(
            "测试渠道配置：默认 dry_run（只做配置校验与连通性探测，不真发）；dry_run=false 会真发一条测试消息。"
            "也可以对「尚未保存」的配置做试探：传 settings（配合 type）即按临时配置测试。"
        )
    )
    def channel_test(
        channel: str | int = "",
        text: str = "这是一条来自 msg-hub 的测试消息",
        dry_run: bool = True,
        settings: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        type: str | None = None,
    ) -> dict[str, Any]:
        return call(
            hub.test_channel,
            channel,
            text,
            dry_run=dry_run,
            settings_override=settings,
            options_override=options,
            type_name=type,
        )

    @mcp.tool(
        description="查询发送审计日志（默认最近 50 条，可按渠道过滤）。用来确认消息是否真的发出去了。"
    )
    def message_logs(channel: str | int | None = None, limit: int = 50) -> dict[str, Any]:
        return {"logs": call(hub.logs, channel, limit=limit)}

    return mcp
