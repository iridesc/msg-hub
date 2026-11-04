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
    "send_retryable": ("发送失败（可重试）", True),
    "plugin_timeout": ("发送超时", True),
    "plugin_error": ("插件异常退出", False),
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
    """channels.json 里的一条记录：只放元信息，凭据在 config/<id>.json。"""

    id: int
    name: str
    type: str
    enabled: bool = True
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Channel":
        return cls(
            id=int(raw["id"]),
            name=str(raw["name"]),
            type=str(raw["type"]),
            enabled=bool(raw.get("enabled", True)),
            created_at=str(raw.get("created_at") or now_iso()),
            updated_at=str(raw.get("updated_at") or now_iso()),
        )


@dataclass
class ChannelConfig:
    """config/<id>.json：settings 由类型 schema 校验，options 是跨类型护栏。"""

    settings: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"settings": self.settings, "options": self.options}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChannelConfig":
        # 兼容"配置直接写在顶层"的旧手写文件：没有 settings/options 时整体视为 settings
        if "settings" not in raw and "options" not in raw:
            return cls(settings=dict(raw), options={})
        return cls(
            settings=dict(raw.get("settings") or {}),
            options=dict(raw.get("options") or {}),
        )

    @property
    def allow_mention_all(self) -> bool:
        return bool(self.options.get("allow_mention_all", False))

    @property
    def rate_limit_per_min(self) -> int | None:
        value = self.options.get("rate_limit_per_min")
        return int(value) if isinstance(value, (int, float)) else None


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
