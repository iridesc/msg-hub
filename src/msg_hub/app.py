"""ASGI 装配：一个 Starlette 应用同时提供 /mcp（MCP Streamable HTTP）与 /api（REST）。"""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .api import build_routes
from .mcp_server import build_mcp
from .service import Hub
from .settings import Settings

PUBLIC_PATHS = {"/health"}


class BearerAuthMiddleware:
    """整个服务的最小鉴权：Authorization: Bearer <MSG_HUB_TOKEN>。"""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.token or scope.get("path") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        provided = (headers.get(b"authorization") or b"").decode("latin-1").strip()
        expected = f"Bearer {self.token}"
        if not secrets.compare_digest(provided, expected):
            body = json.dumps(
                {"ok": False, "error": {"code": "unauthorized", "message": "缺少或错误的 Bearer Token", "retryable": False}},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="msg-hub"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_app(settings: Settings, hub: Hub | None = None) -> Starlette:
    hub = hub or Hub(settings)
    mcp = build_mcp(hub)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # 无状态：每个请求独立，pi-box 等客户端重连不需要恢复会话
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=[f"http://{host}" for host in settings.allowed_hosts if not host.startswith("[")],
        ),
        host=settings.host,
    )

    @asynccontextmanager
    async def lifespan(_app):
        # StreamableHTTP 的会话管理器必须在这里启动；挂载子应用不会自动跑它的 lifespan
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[*build_routes(hub, settings), *mcp_app.routes],
        middleware=[Middleware(BearerAuthMiddleware, token=settings.token)],
        lifespan=lifespan,
    )
