"""ASGI 装配：一个 Starlette 应用同时提供 /mcp（MCP Streamable HTTP）与 /api（REST）。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .api import build_routes
from .auth import (
    ADMIN_IDENTITY,
    Identity,
    check_admin,
    parse_basic,
    parse_bearer,
    reset_identity,
    set_identity,
)
from .dashboard import build_dashboard_routes
from .mcp_server import build_mcp
from .service import Hub
from .settings import Settings

PUBLIC_PATHS = {"/health"}

# 只有管理员能访问的路径前缀（顺序无关，命中即判定）
ADMIN_ONLY_PREFIXES = (
    "/dashboard",
    "/api/channels",
    "/api/entries",
    "/api/rules",
    "/api/route",
    "/api/logs",
    "/api/types",
    "/api/stats",
)
# 管理员才能访问的精确路径
ADMIN_ONLY_PATHS = {"/"}


def _requires_admin(path: str) -> bool:
    if path in ADMIN_ONLY_PATHS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in ADMIN_ONLY_PREFIXES)


def _origins_for(hosts: list[str]) -> list[str]:
    """把 allowed_hosts 翻译成 Origin 白名单。

    allowed_hosts 用 "host:*" 表示任意端口；SDK 对 Origin 的通配规则同样是
    "前缀:*"，所以保持同样的形状即可：

        "127.0.0.1:*"     → "http://127.0.0.1:*"
        "example.com:80"  → "http://example.com:80"（固定端口精确匹配）
        "[::1]:*"         → "http://[::1]:*"

    http/https 都放行：服务本身只提供 http，但前面挂 TLS 反代时 Origin 是 https。
    """
    origins: list[str] = []
    for host in hosts:
        if host.endswith(":*"):
            base, suffix = host[:-2], ":*"
        else:
            base, suffix = host, ""
        origins.append(f"http://{base}{suffix}")
        origins.append(f"https://{base}{suffix}")
    return origins


class AuthMiddleware:
    """两层认证：Basic（管理员）+ Bearer（API Token），用途分开。

    - ``Authorization: Basic <base64(user:pass)>`` → 管理员身份（用户名/密码来自环境变量）
      浏览器靠它弹原生认证框，凭据会被缓存并自动带上后续请求。
    - ``Authorization: Bearer <token>`` → 入口身份（state.json 里的 entries[]），
      只能发消息，碰管理类路由直接 403；发消息时也不能指定出口。

    解析出身份后放进 contextvar，业务层（审计 / MCP 工具）随时能取。
    """

    def __init__(self, app: ASGIApp, hub: Hub, settings: Settings) -> None:
        self.app = app
        self.hub = hub
        self.settings = settings

    def _resolve(self, header: str) -> Identity | None:
        basic = parse_basic(header)
        if basic is not None:
            username, password = basic
            if check_admin(username, password, self.settings.admin_user, self.settings.admin_password):
                return ADMIN_IDENTITY
            return None
        secret = parse_bearer(header)
        if secret:
            entry = self.hub.store.find_entry_by_secret(secret)
            if entry is not None:
                self.hub.store.touch_entry(entry.id)
                return Identity(
                    role="entry",
                    entry_id=entry.id,
                    entry_name=entry.name,
                    entry_prefix=entry.prefix,
                    entry_tags=tuple(entry.tags),
                )
        return None

    @staticmethod
    async def _deny(send: Send, status: int, code: str, message: str) -> None:
        body = json.dumps(
            {"ok": False, "error": {"code": code, "message": message, "retryable": False}},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ]
        if status == 401:
            # Basic 供浏览器弹框（凭据会被浏览器缓存复用），Bearer 供脚本
            headers.append((b"www-authenticate", b'Basic realm="msg-hub", Bearer realm="msg-hub"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.settings.auth_enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        header = (headers.get(b"authorization") or b"").decode("latin-1").strip()
        identity = self._resolve(header) if header else None
        if identity is None:
            await self._deny(send, 401, "unauthorized", "缺少或错误的凭据")
            return
        if not identity.is_admin and _requires_admin(path):
            await self._deny(
                send, 403, "forbidden", f"该接口只允许管理员访问（当前身份：{identity.describe()}）"
            )
            return

        token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(token)


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
            allowed_origins=_origins_for(settings.allowed_hosts),
        ),
        host=settings.host,
    )

    @asynccontextmanager
    async def lifespan(_app):
        # StreamableHTTP 的会话管理器必须在这里启动；挂载子应用不会自动跑它的 lifespan
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            *build_routes(hub, settings),
            *build_dashboard_routes(hub, enabled=settings.dashboard_enabled),
            *mcp_app.routes,
        ],
        middleware=[Middleware(AuthMiddleware, hub=hub, settings=settings)],
        lifespan=lifespan,
    )
