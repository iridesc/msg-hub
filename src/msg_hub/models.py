"""数据模型与统一错误。

三个核心对象对应「入口 → 规则 → 出口」：

    入口 Entry    持 token 的调用方（有 0..n 个标签）
    规则 Rule     一条规则 = 一个入口条件 + 一个出口条件
    出口 Channel  真正的投递通道（有 0..n 个标签）

调用方只拿 token 发消息，**不带任何出口信息**；出口完全由规则决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any



# 错误码：既用于 REST 响应，也用于审计日志，便于上游做重试判定
ERROR_CODES = {
    "not_found": ("对象不存在", False),
    "ambiguous": ("名称匹配到多个，请用 id 指定", False),
    "invalid_argument": ("参数不合法", False),
    "invalid_settings": ("渠道配置不符合该类型的 schema", False),
    "type_unknown": ("渠道类型不存在", False),
    "channel_disabled": ("渠道已停用", False),
    "channel_broken": ("渠道配置缺失或损坏，需修复后再发送", False),
    "rate_limited": ("超出该渠道的发送频率限制", True),
    "text_too_long": ("消息超过该渠道的长度上限", False),
    "send_failed": ("发送失败", False),
    "plugin_timeout": ("发送超时", True),
    "plugin_error": ("插件异常退出", False),
    "unauthorized": ("缺少或错误的凭据", False),
    "forbidden": ("当前凭据无权执行该操作", False),
    # 路由相关
    "no_route": ("当前入口没有匹配到任何可用出口，请检查标签与路由规则", False),
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


# ── 标签 ──────────────────────────────────────────────────
# 约定 key:value 形式（app:pibox / env:prod / kind:alert）。
# 不做格式强制校验——硬性校验会让存量数据迁移变复杂，收益却很小。
def normalize_tags(raw: Any) -> list[str]:
    """把 tags 归一化成字符串列表：去重、去空、保序、大小写不敏感去重。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = [item for item in re.split(r"[,\n]", raw)]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        raise HubError("invalid_argument", "tags 必须是字符串数组")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        # 去掉误加的前后引号
        text = text.strip("\"'").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


# ── 渠道（出口）────────────────────────────────────────────
@dataclass
class Channel:
    """一个渠道：元信息 + 凭据（settings）+ 护栏（options）+ 标签（tags）。

    凭据曾经单独存 `config/<id>.json`，现在合并进 `state.json`：
    一个文件 = 一次原子写 = 不会出现「有渠道无配置」的 broken 状态。
    """

    # 名称就是唯一标识（没有 id）：改名等于换一个实体，路由规则会跟着失效，
    # 所以名称要稳定；创建时全局唯一。
    name: str
    type: str
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "settings": self.settings,
            "options": self.options,
            "tags": self.tags,
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
            name=str(raw["name"]),
            type=str(raw["type"]),
            enabled=bool(raw.get("enabled", True)),
            settings=settings,
            options=options,
            tags=normalize_tags(raw.get("tags")),
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
_CHANNEL_META = {
    "name",
    "type",
    "enabled",
    "tags",
    "created_at",
    "updated_at",
    "deleted_at",
}


# ── 入口 ──────────────────────────────────────────────────
ENTRY_PREFIX = "mh_"
ENTRY_BYTES = 16  # 32 个 hex 字符


@dataclass
class Entry:
    """一个调用入口 = 一把 token + 一组标签（明文存储）。

    明文存储 + 明文回显是有意选择：token 随时要拿给调用方配置，只显示一次
    很容易丢（丢了只能删入口重建，会把线上调用打断）。防护只剩三层：

      1. state.json 权限 0600
      2. 入口相关的接口全部只对管理员开放（入口 token 看不到自己的明文）
      3. 审计日志只记前缀，不记明文

    代价：能打开 dashboard 的人就能看到所有 token 明文（截图/录屏会带出去）。
    """

    # 名称即标识，没有 id；创建时全局唯一
    name: str
    secret: str
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    created_at: str = field(default_factory=now_iso)
    last_used_at: str | None = None
    usage_count: int = 0

    @property
    def prefix(self) -> str:
        """展示与日志用的短标识：`mh_3f2a9c`。"""
        return self.secret[: len(ENTRY_PREFIX) + 6]

    @property
    def masked(self) -> str:
        return f"***{self.secret[-4:]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "secret": self.secret,
            "tags": self.tags,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "usage_count": self.usage_count,
        }

    def to_public(self) -> dict[str, Any]:
        """管理面返回用：**包含明文 secret**（管理员随时可查）。

        只用于 /api/entries 系列与 dashboard；嵌入到其他响应里请改用 to_brief()，
        避免 token 在无关的地方扩散。
        """
        return {
            "name": self.name,
            "secret": self.secret,
            "prefix": self.prefix,
            "masked": self.masked,
            "tags": self.tags,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "usage_count": self.usage_count,
        }

    def to_brief(self) -> dict[str, Any]:
        """嵌进其他响应时用：不含明文，只留身份与标签。"""
        return {
            "name": self.name,
            "prefix": self.prefix,
            "tags": self.tags,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Entry":
        return cls(
            name=str(raw["name"]),
            secret=str(raw["secret"]),
            tags=normalize_tags(raw.get("tags")),
            enabled=bool(raw.get("enabled", True)),
            created_at=str(raw.get("created_at") or now_iso()),
            last_used_at=raw.get("last_used_at"),
            usage_count=int(raw.get("usage_count") or 0),
        )


# 兼容旧名字（v2 时代叫 Token）
Token = Entry


# ── 路由条件 ──────────────────────────────────────────────
# 入口侧可筛的字段；出口侧可筛的字段
ENTRY_FIELDS = ("entry", "tag")
OUTBOUND_FIELDS = ("name", "type", "tag")
CONDITION_OPS = ("exact", "contains", "prefix", "suffix")

OP_LABELS = {
    "exact": "完全匹配",
    "contains": "包含",
    "prefix": "以…开头",
    "suffix": "以…结尾",
}
FIELD_LABELS = {
    "entry": "入口名称",
    "tag": "标签",
    "name": "出口名称",
    "type": "出口类型",
}


@dataclass(frozen=True)
class Condition:
    """一个匹配条件：在候选字符串列表里找任一满足 op 的项。

    field 决定候选集合由谁提供（入口侧 / 出口侧），op 决定怎么比。
    把「你列的 5 种 + 6 种筛选方式」收敛成 field × op 的组合：
    表达能力不变，但匹配逻辑只有一份实现。
    """

    field: str
    op: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value}

    @classmethod
    def from_dict(cls, raw: Any, *, side: str) -> "Condition":
        if not isinstance(raw, dict):
            raise HubError("invalid_argument", f"{side} 条件必须是对象")
        field_name = str(raw.get("field") or "").strip()
        op = str(raw.get("op") or "exact").strip()
        value = str(raw.get("value") or "").strip()
        allowed = ENTRY_FIELDS if side == "entry" else OUTBOUND_FIELDS
        if field_name not in allowed:
            raise HubError(
                "invalid_argument",
                f"{side} 条件的 field 只支持 {allowed}，收到 {field_name!r}",
            )
        if op not in CONDITION_OPS:
            raise HubError(
                "invalid_argument",
                f"{side} 条件的 op 只支持 {CONDITION_OPS}，收到 {op!r}",
            )
        if not value:
            raise HubError("invalid_argument", f"{side} 条件的 value 不能为空")
        return cls(field=field_name, op=op, value=value)

    def matches(self, candidates: list[str]) -> bool:
        """候选集中任一命中即为真。大小写不敏感。"""
        target = self.value.casefold()
        for candidate in candidates:
            text = str(candidate).casefold()
            if self.op == "exact":
                if text == target:
                    return True
            elif self.op == "contains":
                if target in text:
                    return True
            elif self.op == "prefix":
                if text.startswith(target):
                    return True
            elif self.op == "suffix":
                if text.endswith(target):
                    return True
        return False

    def describe(self) -> str:
        return f"{FIELD_LABELS.get(self.field, self.field)} {OP_LABELS.get(self.op, self.op)} {self.value!r}"


# ── 规则 ──────────────────────────────────────────────────
@dataclass
class Rule:
    """一条路由规则：一个入口条件（可空=所有入口）+ 一个出口条件。

    规则之间是**累加**关系：命中的所有规则，其选中的出口取并集再去重。
    没有优先级，规则之间正交——加一条新规则不会改变已有规则的行为。
    """

    # 名称即标识，没有 id
    name: str
    outbound: Condition
    entry: Condition | None = None
    enabled: bool = True
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "entry": self.entry.to_dict() if self.entry else None,
            "outbound": self.outbound.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rule":
        entry_raw = raw.get("entry")
        return cls(
            name=str(raw["name"]),
            entry=Condition.from_dict(entry_raw, side="entry") if entry_raw else None,
            outbound=Condition.from_dict(raw.get("outbound"), side="outbound"),
            enabled=bool(raw.get("enabled", True)),
            created_at=str(raw.get("created_at") or now_iso()),
            updated_at=str(raw.get("updated_at") or now_iso()),
        )


@dataclass
class Message:
    """核心眼里的消息：正文 + 一个不透明的 extra。

    extra 原样带到插件，核心不读里面的任何 key。
    """

    text: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "extra": dict(self.extra)}


# ── 消息体 ────────────────────────────────────────────────
# 核心只认识两个字段：text（要校验长度、写审计 preview）和 dry_run（决定是否
# 消费配额）。其余控制字段一律收在 extra 里，核心**不解析、不校验、不归一化**，
# 原样透传给出口插件，由插件按自己的需要去取。
#
# 为什么不在核心校验：format / title / mention_* 是「某个出口类型」的概念，
# 不是 msg-hub 的概念。核心一旦开始校验它们，就等于把 wecom_bot 的字段表
# 硬编码进了路由层，以后加一个参数完全不同的类型（比如卡片消息、附件）
# 就得回来改核心。边界应该停在「路由」这里。
MESSAGE_FIELDS = ("text", "dry_run", "extra")


def parse_message_body(body: dict[str, Any]) -> tuple[Any, bool, dict[str, Any]]:
    """拆开消息体：顶层只认 text / dry_run / extra。

    顶层多一个字段仍然报错（这一层是 msg-hub 自己的契约，不是出口的），
    但 extra 里装什么核心一概不管 —— 拼错字段名由出口插件负责发现。
    """
    unknown = [key for key in body if key not in MESSAGE_FIELDS]
    if unknown:
        raise HubError(
            "invalid_argument",
            f"消息体里有不认识的字段：{sorted(unknown)}（支持：text / dry_run / extra；"
            "format、title、mention_* 这些控制字段请放进 extra）",
        )
    raw = body.get("extra")
    if raw is None:
        extra: dict[str, Any] = {}
    elif isinstance(raw, dict):
        extra = dict(raw)
    else:
        raise HubError("invalid_argument", "extra 必须是对象")
    return body.get("text"), bool(body.get("dry_run", False)), extra
