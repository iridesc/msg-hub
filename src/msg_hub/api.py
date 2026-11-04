"""REST 接口：与 MCP 工具完全等价，方便 bash / curl / 定时任务直接调用。"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

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
    "text_too_long": 400,
    "plugin_timeout": 504,
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
        return ok(
            {
                "status": "ok",
                "version": settings.version,
                "auth": "bearer" if settings.auth_enabled else "none",
                "types": [manifest["type"] for manifest in hub.types()],
                "channels": {
                    "total": len(channels),
                    "ok": sum(1 for c in channels if c["status"] == "ok"),
                    "broken": sum(1 for c in channels if c["status"] == "broken"),
                    "disabled": sum(1 for c in channels if c["status"] == "disabled"),
                },
            }
        )

    async def list_types(_request: Request) -> Response:
        return ok({"types": hub.types()})

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
            )
        except HubError as exc:
            return fail(exc)
        return ok(result)

    async def send(request: Request) -> Response:
        body = await _json_body(request)
        if "channel" not in body:
            return fail(HubError("invalid_argument", "缺少 channel（渠道 id 或名称）"))
        try:
            result = hub.send(
                body["channel"],
                str(body.get("text") or ""),
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
        Route("/api/channels", list_channels, methods=["GET"]),
        Route("/api/channels", create_channel, methods=["POST"]),
        Route("/api/channels/{ref}", get_channel, methods=["GET"]),
        Route("/api/channels/{ref}", update_channel, methods=["PATCH"]),
        Route("/api/channels/{ref}", delete_channel, methods=["DELETE"]),
        Route("/api/channels/{ref}/test", test_channel, methods=["POST"]),
        Route("/api/send", send, methods=["POST"]),
        Route("/api/logs", logs, methods=["GET"]),
    ]
