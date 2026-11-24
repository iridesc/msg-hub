"""REST 接口：与 MCP 工具完全等价，方便 bash / curl / 定时任务直接调用。"""

from __future__ import annotations

import functools
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .dashboard import summarize_channels, summarize_sends
from .models import HubError, parse_message_body
from .service import Hub
from .settings import Settings

STATUS_BY_CODE = {
    "not_found": 404,
    "ambiguous": 409,
    "invalid_argument": 400,
    "invalid_settings": 400,
    "type_unknown": 400,
    "channel_disabled": 409,
    "channel_broken": 409,
    "rate_limited": 429,
    "mention_all_denied": 403,
    "forbidden": 403,
    "text_too_long": 400,
    "plugin_timeout": 504,
    "unauthorized": 401,
    "state_broken": 500,
    "no_route": 409,
}


def ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def fail(exc: HubError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=STATUS_BY_CODE.get(exc.code, 502))


def endpoint(func: Any) -> Any:
    """把 HubError 转成 HTTP 响应。

    handler 里只管业务，出错直接抛；转换逻辑集中在这里一处，
    免得每个 handler 都抄一遍 try/except（漏一处就是 500 而不是 4xx）。
    """

    @functools.wraps(func)
    async def wrapper(request: Request) -> Response:
        try:
            return await func(request)
        except HubError as exc:
            return fail(exc)

    return wrapper


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HubError("invalid_argument", f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise HubError("invalid_argument", "请求体必须是 JSON 对象")
    return data


def build_routes(hub: Hub, settings: Settings) -> list[Route]:
    async def health(_request: Request) -> Response:
        """纯探活：只回答「进程能响应」。

        刻意不返回版本、渠道数、入口数这些 —— 它免认证，不该成为匿名方的情报源。
        配置概览在 /manager_api/stats（Basic）。
        也刻意不因为「某个渠道配错了」就报不健康：那是业务问题，
        让容器被判定 unhealthy 然后反复重启只会更糟。
        """
        return ok({"status": "ok"})

    async def list_types(_request: Request) -> Response:
        return ok({"types": hub.types()})

    async def stats(_request: Request) -> Response:
        """dashboard 用的聚合：渠道状态 + 最近发送情况。"""
        return ok(
            {
                "version": settings.version,
                "auth": "basic+bearer" if settings.auth_enabled else "none",
                "types": [manifest["type"] for manifest in hub.types()],
                "channels": summarize_channels(hub),
                "sends": summarize_sends(hub),
                "topology": {
                    "entries": hub.list_entries(),
                    "rules": hub.list_rules(),
                },
            }
        )

    # ── 渠道（出口）──────────────────────────────────────
    async def list_channels(request: Request) -> Response:
        include_deleted = request.query_params.get("include_deleted") in ("1", "true", "yes")
        return ok({"channels": hub.list_channels(include_deleted=include_deleted)})

    @endpoint
    async def create_channel(request: Request) -> Response:
        body = await _json_body(request)
        channel = hub.create_channel(
            name=str(body.get("name") or ""),
            type_name=str(body.get("type") or ""),
            settings=body.get("settings") or {},
            options=body.get("options"),
            enabled=bool(body.get("enabled", True)),
            tags=body.get("tags"),
        )
        return ok({"ok": True, "channel": channel}, status=201)

    @endpoint
    async def get_channel(request: Request) -> Response:
        return ok({"channel": hub.get_channel(request.path_params["ref"])})

    @endpoint
    async def update_channel(request: Request) -> Response:
        body = await _json_body(request)
        channel = hub.update_channel(
            request.path_params["ref"],
            name=body.get("name"),
            type_name=body.get("type"),
            settings=body.get("settings"),
            options=body.get("options"),
            enabled=body.get("enabled"),
            tags=body.get("tags"),
        )
        return ok({"ok": True, "channel": channel})

    @endpoint
    async def delete_channel(request: Request) -> Response:
        return ok(hub.delete_channel(request.path_params["ref"]))

    @endpoint
    async def test_channel(request: Request) -> Response:
        body = await _json_body(request)
        result = hub.test_channel(
            request.path_params["ref"],
            str(body.get("text") or "这是一条来自 msg-hub 的测试消息"),
            dry_run=bool(body.get("dry_run", True)),
            # 与 MCP 的 channel_test 对齐：可以拿「尚未保存」的 settings 来试
            settings_override=body.get("settings"),
            options_override=body.get("options"),
            type_name=body.get("type"),
        )
        return ok(result)

    # ── 入口 ────────────────────────────────────────────
    async def list_entries(_request: Request) -> Response:
        return ok({"entries": hub.list_entries()})

    @endpoint
    async def create_entry(request: Request) -> Response:
        body = await _json_body(request)
        entry = hub.create_entry(
            str(body.get("name") or ""),
            tags=body.get("tags"),
            enabled=bool(body.get("enabled", True)),
        )
        # 明文只在这一次响应里出现
        return ok({"ok": True, "entry": entry}, status=201)

    @endpoint
    async def get_entry(request: Request) -> Response:
        return ok({"entry": hub.get_entry(request.path_params["ref"])})

    @endpoint
    async def update_entry(request: Request) -> Response:
        body = await _json_body(request)
        entry = hub.update_entry(
            request.path_params["ref"],
            name=body.get("name"),
            tags=body.get("tags"),
            enabled=body.get("enabled"),
        )
        return ok({"ok": True, "entry": entry})

    @endpoint
    async def delete_entry(request: Request) -> Response:
        return ok(hub.delete_entry(request.path_params["ref"]))

    # ── 路由规则 ────────────────────────────────────────
    async def list_rules(_request: Request) -> Response:
        return ok({"rules": hub.list_rules()})

    @endpoint
    async def create_rule(request: Request) -> Response:
        body = await _json_body(request)
        rule = hub.create_rule(
            str(body.get("name") or ""),
            body.get("outbound") or {},
            entry=body.get("entry"),
            enabled=bool(body.get("enabled", True)),
        )
        return ok({"ok": True, "rule": rule}, status=201)

    @endpoint
    async def get_rule(request: Request) -> Response:
        return ok({"rule": hub.get_rule(request.path_params["ref"])})

    @endpoint
    async def update_rule(request: Request) -> Response:
        body = await _json_body(request)
        rule = hub.update_rule(
            request.path_params["ref"],
            name=body.get("name"),
            outbound=body.get("outbound"),
            entry=body.get("entry"),
            clear_entry=bool(body.get("clear_entry", False)),
            enabled=body.get("enabled"),
        )
        return ok({"ok": True, "rule": rule})

    @endpoint
    async def delete_rule(request: Request) -> Response:
        return ok(hub.delete_rule(request.path_params["ref"]))

    @endpoint
    async def route_preview(request: Request) -> Response:
        entry = request.query_params.get("entry")
        return ok(hub.route_preview(entry))

    # ── 发送 ────────────────────────────────────────────
    @endpoint
    async def send(request: Request) -> Response:
        """发送。请求体只有三块：text / dry_run / extra。

        出口由服务端按入口标签 + 路由规则决定，调用方无从指定（也没有 channel 参数）；
        来源标记由服务端按调用路径打，不接受调用方自报。
        """
        body = await _json_body(request)
        text, dry_run, extra = parse_message_body(body)
        result = hub.send(text, dry_run=dry_run, source="api", extra=extra)
        return ok(result)

    @endpoint
    async def logs(request: Request) -> Response:
        channel = request.query_params.get("channel")
        limit = request.query_params.get("limit")
        records = hub.logs(channel, limit=int(limit) if limit else 50)
        return ok({"logs": records})

    # ── 两组路由，前缀即权限边界 ──────────────────────
    # /api         调用面：只认 Bearer（入口 token），只有发消息
    # /manager_api 管理面：只认 Basic（管理员），没有发消息
    # 这样「谁能发送」「谁能改配置」在结构上就分开了，不靠中间件逐个判断。
    return [
        Route("/health", health, methods=["GET"]),
        Route("/api/send", send, methods=["POST"]),
        Route("/manager_api/types", list_types, methods=["GET"]),
        Route("/manager_api/stats", stats, methods=["GET"]),
        Route("/manager_api/channels", list_channels, methods=["GET"]),
        Route("/manager_api/channels", create_channel, methods=["POST"]),
        Route("/manager_api/channels/{ref}", get_channel, methods=["GET"]),
        Route("/manager_api/channels/{ref}", update_channel, methods=["PATCH"]),
        Route("/manager_api/channels/{ref}", delete_channel, methods=["DELETE"]),
        Route("/manager_api/channels/{ref}/test", test_channel, methods=["POST"]),
        Route("/manager_api/entries", list_entries, methods=["GET"]),
        Route("/manager_api/entries", create_entry, methods=["POST"]),
        Route("/manager_api/entries/{ref}", get_entry, methods=["GET"]),
        Route("/manager_api/entries/{ref}", update_entry, methods=["PATCH"]),
        Route("/manager_api/entries/{ref}", delete_entry, methods=["DELETE"]),
        Route("/manager_api/rules", list_rules, methods=["GET"]),
        Route("/manager_api/rules", create_rule, methods=["POST"]),
        Route("/manager_api/rules/{ref}", get_rule, methods=["GET"]),
        Route("/manager_api/rules/{ref}", update_rule, methods=["PATCH"]),
        Route("/manager_api/rules/{ref}", delete_rule, methods=["DELETE"]),
        Route("/manager_api/route/preview", route_preview, methods=["GET"]),
        Route("/manager_api/logs", logs, methods=["GET"]),
    ]
