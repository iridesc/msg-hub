"""REST 接口：与 MCP 工具完全等价，方便 bash / curl / 定时任务直接调用。"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .dashboard import summarize_channels, summarize_sends
from .models import HubError
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
    "all_failed": 502,
}


def ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def fail(exc: HubError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=STATUS_BY_CODE.get(exc.code, 502))


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
        channels = hub.list_channels()
        entries = hub.list_entries()
        rules = hub.list_rules()
        return ok(
            {
                "status": "ok",
                "version": settings.version,
                "auth": "basic+bearer" if settings.auth_enabled else "none",
                "types": [manifest["type"] for manifest in hub.types()],
                "channels": {
                    "total": len(channels),
                    "ok": sum(1 for c in channels if c["status"] == "ok"),
                    "broken": sum(1 for c in channels if c["status"] == "broken"),
                    "disabled": sum(1 for c in channels if c["status"] == "disabled"),
                },
                "entries": {"total": len(entries), "enabled": sum(1 for e in entries if e["enabled"])},
                "rules": {"total": len(rules), "enabled": sum(1 for r in rules if r["enabled"])},
            }
        )

    async def list_types(_request: Request) -> Response:
        return ok({"types": hub.types()})

    async def stats(_request: Request) -> Response:
        """dashboard 用的聚合：渠道状态 + 最近发送情况。"""
        return ok(
            {
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

    async def create_channel(request: Request) -> Response:
        body = await _json_body(request)
        try:
            channel = hub.create_channel(
                name=str(body.get("name") or ""),
                type_name=str(body.get("type") or ""),
                settings=body.get("settings") or {},
                options=body.get("options"),
                enabled=bool(body.get("enabled", True)),
                tags=body.get("tags"),
            )
        except HubError as exc:
            return fail(exc)
        return ok({"ok": True, "channel": channel}, status=201)

    async def get_channel(request: Request) -> Response:
        try:
            return ok({"channel": hub.get_channel(request.path_params["ref"])})
        except HubError as exc:
            return fail(exc)

    async def update_channel(request: Request) -> Response:
        body = await _json_body(request)
        try:
            channel = hub.update_channel(
                request.path_params["ref"],
                name=body.get("name"),
                type_name=body.get("type"),
                settings=body.get("settings"),
                options=body.get("options"),
                enabled=body.get("enabled"),
                tags=body.get("tags"),
            )
        except HubError as exc:
            return fail(exc)
        return ok({"ok": True, "channel": channel})

    async def delete_channel(request: Request) -> Response:
        try:
            return ok(hub.delete_channel(request.path_params["ref"]))
        except HubError as exc:
            return fail(exc)

    async def test_channel(request: Request) -> Response:
        body = await _json_body(request)
        try:
            result = hub.test_channel(
                request.path_params["ref"],
                str(body.get("text") or "这是一条来自 msg-hub 的测试消息"),
                dry_run=bool(body.get("dry_run", True)),
                # 与 MCP 的 channel_test 对齐：可以拿「尚未保存」的 settings 来试
                settings_override=body.get("settings"),
                options_override=body.get("options"),
                type_name=body.get("type"),
            )
        except HubError as exc:
            return fail(exc)
        return ok(result)

    # ── 入口 ────────────────────────────────────────────
    async def list_entries(_request: Request) -> Response:
        return ok({"entries": hub.list_entries()})

    async def create_entry(request: Request) -> Response:
        body = await _json_body(request)
        try:
            entry = hub.create_entry(
                str(body.get("name") or ""),
                tags=body.get("tags"),
                enabled=bool(body.get("enabled", True)),
            )
        except HubError as exc:
            return fail(exc)
        # 明文只在这一次响应里出现
        return ok({"ok": True, "entry": entry}, status=201)

    async def get_entry(request: Request) -> Response:
        try:
            return ok({"entry": hub.get_entry(request.path_params["ref"])})
        except HubError as exc:
            return fail(exc)

    async def update_entry(request: Request) -> Response:
        body = await _json_body(request)
        try:
            entry = hub.update_entry(
                request.path_params["ref"],
                name=body.get("name"),
                tags=body.get("tags"),
                enabled=body.get("enabled"),
            )
        except HubError as exc:
            return fail(exc)
        return ok({"ok": True, "entry": entry})

    async def delete_entry(request: Request) -> Response:
        try:
            return ok(hub.delete_entry(request.path_params["ref"]))
        except HubError as exc:
            return fail(exc)

    # ── 路由规则 ────────────────────────────────────────
    async def list_rules(_request: Request) -> Response:
        return ok({"rules": hub.list_rules()})

    async def create_rule(request: Request) -> Response:
        body = await _json_body(request)
        try:
            rule = hub.create_rule(
                str(body.get("name") or ""),
                body.get("outbound") or {},
                entry=body.get("entry"),
                enabled=bool(body.get("enabled", True)),
            )
        except HubError as exc:
            return fail(exc)
        return ok({"ok": True, "rule": rule}, status=201)

    async def get_rule(request: Request) -> Response:
        try:
            return ok({"rule": hub.get_rule(request.path_params["ref"])})
        except HubError as exc:
            return fail(exc)

    async def update_rule(request: Request) -> Response:
        body = await _json_body(request)
        try:
            rule = hub.update_rule(
                request.path_params["ref"],
                name=body.get("name"),
                outbound=body.get("outbound"),
                entry=body.get("entry"),
                clear_entry=bool(body.get("clear_entry", False)),
                enabled=body.get("enabled"),
            )
        except HubError as exc:
            return fail(exc)
        return ok({"ok": True, "rule": rule})

    async def delete_rule(request: Request) -> Response:
        try:
            return ok(hub.delete_rule(request.path_params["ref"]))
        except HubError as exc:
            return fail(exc)

    async def route_preview(request: Request) -> Response:
        entry = request.query_params.get("entry")
        try:
            return ok(hub.route_preview(entry))
        except HubError as exc:
            return fail(exc)

    # ── 发送 ────────────────────────────────────────────
    async def send(request: Request) -> Response:
        body = await _json_body(request)
        try:
            result = hub.send(
                body.get("text"),
                channel=body.get("channel"),
                format=str(body.get("format") or "plain"),
                title=body.get("title"),
                mention_users=body.get("mention_users"),
                mention_mobiles=body.get("mention_mobiles"),
                mention_all=bool(body.get("mention_all", False)),
                dry_run=bool(body.get("dry_run", False)),
                source=str(body.get("source") or "api"),
            )
        except HubError as exc:
            return fail(exc)
        return ok(result)

    async def logs(request: Request) -> Response:
        channel = request.query_params.get("channel")
        limit = request.query_params.get("limit")
        try:
            records = hub.logs(channel, limit=int(limit) if limit else 50)
        except HubError as exc:
            return fail(exc)
        except ValueError:
            return fail(HubError("invalid_argument", "limit 必须是整数"))
        return ok({"logs": records})

    return [
        Route("/health", health, methods=["GET"]),
        Route("/api/types", list_types, methods=["GET"]),
        Route("/api/stats", stats, methods=["GET"]),
        Route("/api/channels", list_channels, methods=["GET"]),
        Route("/api/channels", create_channel, methods=["POST"]),
        Route("/api/channels/{ref}", get_channel, methods=["GET"]),
        Route("/api/channels/{ref}", update_channel, methods=["PATCH"]),
        Route("/api/channels/{ref}", delete_channel, methods=["DELETE"]),
        Route("/api/channels/{ref}/test", test_channel, methods=["POST"]),
        Route("/api/entries", list_entries, methods=["GET"]),
        Route("/api/entries", create_entry, methods=["POST"]),
        Route("/api/entries/{ref}", get_entry, methods=["GET"]),
        Route("/api/entries/{ref}", update_entry, methods=["PATCH"]),
        Route("/api/entries/{ref}", delete_entry, methods=["DELETE"]),
        Route("/api/rules", list_rules, methods=["GET"]),
        Route("/api/rules", create_rule, methods=["POST"]),
        Route("/api/rules/{ref}", get_rule, methods=["GET"]),
        Route("/api/rules/{ref}", update_rule, methods=["PATCH"]),
        Route("/api/rules/{ref}", delete_rule, methods=["DELETE"]),
        Route("/api/route/preview", route_preview, methods=["GET"]),
        Route("/api/send", send, methods=["POST"]),
        Route("/api/logs", logs, methods=["GET"]),
    ]
