"""数据模型与统一错误。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .:_\-]{0,63}$")

MESSAGE_FORMATS = ("plain", "markdown")

# 错误码：既用于 REST 响应，也用于审计日志，便于上游做重试判定
ERROR_CODES = {
    "not_found": ("渠道不存在", False),
    "ambiguous": ("渠道名称匹配到多个，请用 id 指定", False),
    "invalid_argument": ("参数不合法", False),
    "invalid_settings": ("渠道配置不符合该类型的 schema", False),
    "type_unknown": ("渠道类型不存在", False),
    "channel_disabled": ("渠道已停用", False),
    "channel_broken": ("渠道配置缺失或损坏，需修复后再发送", False),
    "rate_limited": ("超出该渠道的发送频率限制", True),
    "mention_all_denied": ("该渠道未开启 @所有人", False),
    "text_too_long": ("消息超过该渠道的长度上限", False),
    "send_failed": ("发送失败", False),
    "plugin_timeout": ("发送超时", True),
    "plugin_error": ("插件异常退出", False),
    "unauthorized": ("缺少或错误的凭据", False),
    "forbidden": ("当前凭据无权执行该操作", False),
}


class HubError(Exception):
    """业务错误：code 见 ERROR_CODES，extra 用于返回候选列表等附加信息。"""

    def __init__(
        self, code: str, message: str | None = None, *, retryable: bool | None = None, **extra: Any
    ) -> None:
        self.code = code
        self.retryable = retryable if retryable is not None else ERROR_CODES.get(code, ("", False))[1]
        super().__init__(message or ERROR_CODES.get(code, ("未知错误", False))[0])
        self.message = message or ERROR_CODES.get(code, ("未知错误", False))[0]
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message, "retryable": self.retryable},
        }
        if self.extra:
            data["error"]["details"] = self.extra
        return data


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Channel:
    """一个渠道：元信息 + 凭据（settings）+ 护栏（options）全在一个对象里。

    凭据曾经单独存 `config/<id>.json`，现在合并进 `state.json`：
    一个文件 = 一次原子写 = 不会出现「有渠道无配置」的 broken 状态。
    """

    id: int
    name: str
    type: str
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "settings": self.settings,
            "options": self.options,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Channel":
        # 兼容 v1 时代「配置直接写在顶层」的旧手写文件
        if "settings" not in raw and "options" not in raw:
            settings = {
                key: value
                for key, value in raw.items()
                if key not in _CHANNEL_META
            }
            options: dict[str, Any] = {}
        else:
            settings = dict(raw.get("settings") or {})
            options = dict(raw.get("options") or {})
        return cls(
            id=int(raw["id"]),
            name=str(raw["name"]),
            type=str(raw["type"]),
            enabled=bool(raw.get("enabled", True)),
            settings=settings,
            options=options,
            created_at=str(raw.get("created_at") or now_iso()),
            updated_at=str(raw.get("updated_at") or now_iso()),
        )

    @property
    def allow_mention_all(self) -> bool:
        return bool(self.options.get("allow_mention_all", False))

    @property
    def rate_limit_per_min(self) -> int | None:
        value = self.options.get("rate_limit_per_min")
        return int(value) if isinstance(value, (int, float)) else None


# Channel.to_dict() 产出的字段名，从_dict 反推旧式配置时要跳过
_CHANNEL_META = {"id", "name", "type", "enabled", "created_at", "updated_at", "deleted_at"}


TOKEN_PREFIX = "mh_"
TOKEN_BYTES = 16  # 32 个 hex 字符


@dataclass
class Token:
    """一把调用面的 API Token（明文存储）。

    明文存储是有意选择：方便随时复制，与 webhook_key 同等待遇。
    防护靠 0600 权限 + 接口返回时打码（只露前缀），日志也只记前缀。
    """

    id: int
    name: str
    secret: str
    enabled: bool = True
    created_at: str = field(default_factory=now_iso)
    last_used_at: str | None = None
    usage_count: int = 0

    @property
    def prefix(self) -> str:
        """展示与日志用的短标识：`mh_3f2a9c`。"""
        return self.secret[: len(TOKEN_PREFIX) + 6]

    @property
    def masked(self) -> str:
        return f"***{self.secret[-4:]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "secret": self.secret,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "usage_count": self.usage_count,
        }

    def to_public(self) -> dict[str, Any]:
        """接口返回用：不包含完整 secret。"""
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "masked": self.masked,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "usage_count": self.usage_count,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Token":
        return cls(
            id=int(raw["id"]),
            name=str(raw["name"]),
            secret=str(raw["secret"]),
            enabled=bool(raw.get("enabled", True)),
            created_at=str(raw.get("created_at") or now_iso()),
            last_used_at=raw.get("last_used_at"),
            usage_count=int(raw.get("usage_count") or 0),
        )


@dataclass
class Message:
    text: str
    format: Literal["plain", "markdown"] = "plain"
    title: str | None = None
    mention_users: list[str] = field(default_factory=list)
    mention_mobiles: list[str] = field(default_factory=list)
    mention_all: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "format": self.format,
            "title": self.title,
            "mention_users": self.mention_users,
            "mention_mobiles": self.mention_mobiles,
            "mention_all": self.mention_all,
        }
