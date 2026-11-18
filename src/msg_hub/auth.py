"""认证与授权：两套凭据，用途彻底分开。

    ┌─ 管理面（人 / 浏览器）──  Basic 用户名 + 密码（环境变量，强制必填）
    │     /dashboard、渠道/入口/规则增删改、日志
    ├─ 调用面（系统 / 脚本 / MCP）── API Token（state.json 的 entries[]，可选）
    │     只能发消息，且**不能指定出口**——出口完全由路由规则决定
    └─ /health 免认证

设计要点：

1. **任何时候都不会出现「完全无鉴权」**：管理员凭据是强制的，token 只是
   给外部系统用的第二条通道；不建 token 时，管理员自己仍然能发消息。
2. **身份通过 contextvar 传播**：HTTP 中间件解析后 set，业务层（审计、MCP 工具）
   随时 `current_identity()` 取。stdio 模式没有 HTTP 层，默认就是管理员身份
   （由宿主进程拉起，天然在信任边界内）。
3. **日志只记 token 的 id / 名称 / 前缀**，绝不落完整凭据。
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal

ADMIN = "admin"
ENTRY = "entry"
TOKEN = ENTRY  # 兼容旧名


@dataclass(frozen=True)
class Identity:
    """当前调用者：管理员，或某个入口（Entry）。"""

    role: Literal["admin", "entry"] = ADMIN
    entry_id: int | None = None
    entry_name: str | None = None
    entry_prefix: str | None = field(default=None)
    entry_tags: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    def audit_actor(self) -> dict[str, Any]:
        """审计日志里的 actor 字段 —— 只放可公开的标识，不含凭据。"""
        if self.is_admin:
            return {"type": ADMIN}
        return {
            "type": ENTRY,
            "id": self.entry_id,
            "name": self.entry_name,
            "prefix": self.entry_prefix,
        }

    def describe(self) -> str:
        if self.is_admin:
            return "管理员"
        return f"入口#{self.entry_id} {self.entry_name or ''} {self.entry_prefix or ''}".strip()


ADMIN_IDENTITY = Identity(role=ADMIN)

_current: contextvars.ContextVar[Identity] = contextvars.ContextVar(
    "msg_hub_identity", default=ADMIN_IDENTITY
)


def current_identity() -> Identity:
    """当前请求的身份；没有请求上下文时（stdio / 后台任务）视为管理员。"""
    return _current.get()


def set_identity(identity: Identity) -> contextvars.Token[Identity]:
    return _current.set(identity)


def reset_identity(token: contextvars.Token[Identity]) -> None:
    _current.reset(token)


# ── 凭据解析 ──────────────────────────────────────────────
def parse_basic(header: str) -> tuple[str, str] | None:
    """解析 `Basic xxx` → (username, password)。格式不对返回 None。"""
    raw = header.strip()
    if not raw.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(raw[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, _, password = decoded.partition(":")
    return username, password


def parse_bearer(header: str) -> str | None:
    raw = header.strip()
    if not raw.lower().startswith("bearer "):
        return None
    value = raw[7:].strip()
    return value or None


def check_admin(username: str, password: str, expected_user: str, expected_password: str) -> bool:
    """管理员凭据比对（定长比较，避免时序侧信道）。"""
    if not expected_password:
        return False
    user_ok = secrets.compare_digest(username, expected_user) if expected_user else True
    pass_ok = secrets.compare_digest(password, expected_password)
    return user_ok and pass_ok
