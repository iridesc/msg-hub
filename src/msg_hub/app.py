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
from .mcp_server import SENDER_INSTRUCTIONS, build_mcp
from .service import Hub
from .tools import ADMIN_TOOLS, SENDER_TOOLS
from .settings import Settings

# 免认证路径：/mcp 是调用面（身份来自参数里的 token，不需要 HTTP 认证）
PUBLIC_PATHS = {"/health", "/mcp"}

# 只有管理员能访问的路径前缀。前缀本身就是权限边界：
#   /manager_api  管理面（Basic）
#   /manager_mcp  管理面（Basic）
SENDER_ONLY_PREFIXES = ("/api",)

ADMIN_ONLY_PREFIXES = (
    "/manager_mcp",
    "/manager_api",
    "/dashboard",
)
# 管理员才能访问的精确路径
ADMIN_ONLY_PATHS = {"/"}


def _requires_sender(path: str) -> bool:
    """发送面路径（/api/*）只接受入口 token。"""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in SENDER_ONLY_PREFIXES)


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
                self.hub.store.touch_entry(entry.name)
                return Identity(
                    role="entry",
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
        if identity.is_admin and _requires_sender(path):
            await self._deny(
                send,
                403,
                "forbidden",
                "该接口是发送面，只接受入口 token。管理操作请用 /manager_api（Basic），"
                "发消息请用入口 token 调 /api/send 或 /mcp",
            )
            return

        token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(token)


def build_app(settings: Settings, hub: Hub | None = None) -> Starlette:
    hub = hub or Hub(settings)

    # 两个端点分工：
    #   /mcp         免认证，只有 send_message（token 在参数里）——发送的唯一入口
    #   /manager_mcp 必须 Basic，17 个管理工具（不含 send_message）——只管配置
    sender = build_mcp(
        hub,
        SENDER_TOOLS,
        title="msg-hub 发送端点",
        instructions=SENDER_INSTRUCTIONS,
    )
    manager = build_mcp(hub, ADMIN_TOOLS, title="msg-hub 管理端点")

    security = dict(
        # 无状态：每个请求独立，pi-box 等客户端重连不需要恢复会话
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=_origins_for(settings.allowed_hosts),
        ),
        host=settings.host,
    )
    sender_app = sender.streamable_http_app(streamable_http_path="/mcp", **security)
    manager_app = manager.streamable_http_app(streamable_http_path="/manager_mcp", **security)

    @asynccontextmanager
    async def lifespan(_app):
        # StreamableHTTP 的会话管理器必须在这里启动；挂载子应用不会自动跑它的 lifespan
        async with sender.session_manager.run(), manager.session_manager.run():
            yield

    return Starlette(
        routes=[
            *build_routes(hub, settings),
            *build_dashboard_routes(hub, enabled=settings.dashboard_enabled),
            *sender_app.routes,
            *manager_app.routes,
        ],
        middleware=[Middleware(AuthMiddleware, hub=hub, settings=settings)],
        lifespan=lifespan,
    )
