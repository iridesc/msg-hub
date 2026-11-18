"""业务层：MCP 与 REST 共用同一套逻辑。

发送模型（v3 起）：

    调用方只拿 token 发消息，**不带任何出口信息**。
    → 认证得到「入口 Entry」
    → 跑一遍所有路由规则，得到出口集合（并集去重）
    → 逐个出口发送，聚合结果

管理员（Basic）例外：可以显式传 channel 直连某个出口，用于应急/调试。
API Token 传 channel 会被拒（否则「出口完全由服务端决定」就不成立了）。
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections import deque
from typing import Any

from . import routing
from .audit import Audit
from .auth import Identity, current_identity
from .models import (
    MESSAGE_FORMATS,
    Channel,
    Condition,
    Entry,
    HubError,
    Message,
    Rule,
    normalize_tags,
    now_iso,
)
from .plugins import Manifest, Registry
from .settings import Settings
from .store import Store


class Hub:
    def __init__(self, settings: Settings, store: Store | None = None, registry: Registry | None = None) -> None:
        self.settings = settings
        self.store = store or Store(settings)
        self.registry = registry or Registry(settings)
        self.audit = Audit(settings)
        self._rate_lock = threading.Lock()
        self._rate: dict[int, deque[float]] = {}

    # ── 类型 ────────────────────────────────────────────
    def types(self) -> list[dict[str, Any]]:
        return [manifest.summary() for manifest in self.registry.all()]

    # ── 渠道查询 ────────────────────────────────────────
    def channel_status(self, channel: Channel) -> tuple[str, str | None]:
        """渠道可用性。配置就在 Channel 对象里，不再需要额外读文件。"""
        if not channel.enabled:
            return "disabled", None
        try:
            manifest = self.registry.get(channel.type)
        except HubError as exc:
            return "broken", str(exc)
        try:
            self.registry.normalize_settings(manifest, channel.settings)
        except HubError as exc:
            return "broken", exc.message
        return "ok", None

    def _describe(self, channel: Channel) -> dict[str, Any]:
        status, detail = self.channel_status(channel)
        data: dict[str, Any] = {
            "id": channel.id,
            "name": channel.name,
            "type": channel.type,
            "enabled": channel.enabled,
            "tags": list(channel.tags),
            "status": status,
            "created_at": channel.created_at,
            "updated_at": channel.updated_at,
        }
        if detail:
            data["status_detail"] = detail
        return data

    def list_channels(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        return [self._describe(channel) for channel in self.store.channels(include_deleted=include_deleted)]

    def resolve(self, ref: int | str) -> Channel:
        """按 id 或名称寻址；名称模糊匹配到多个时报错并给候选，绝不猜。"""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            try:
                return self.store.get(int(ref))
            except HubError:
                # id 不存在时再按名称找一次（渠道名可能恰好是纯数字）
                pass
        name = str(ref).strip()
        if not name:
            raise HubError("invalid_argument", "channel 不能为空")
        channels = self.store.channels()
        exact = [c for c in channels if c.name.casefold() == name.casefold()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise HubError(
                "ambiguous",
                f"名称 {name} 匹配到多个渠道，请改用 id",
                candidates=[{"id": c.id, "name": c.name} for c in exact],
            )
        partial = [c for c in channels if name.casefold() in c.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise HubError(
                "ambiguous",
                f"{name} 匹配到多个渠道，请改用 id 指定",
                candidates=[{"id": c.id, "name": c.name} for c in partial],
            )
        available = [{"id": c.id, "name": c.name, "type": c.type} for c in channels]
        raise HubError("not_found", f"渠道 {name} 不存在", available=available)

    def get_channel(self, ref: int | str) -> dict[str, Any]:
        channel = self.resolve(ref)
        manifest = self._manifest_or_none(channel)
        data = self._describe(channel)
        data["options"] = channel.options
        if manifest is not None:
            data["settings"] = self.registry.mask_settings(manifest, channel.settings)
        else:
            data["settings"] = {key: "***" for key in channel.settings}
        return data

    def _manifest_or_none(self, channel: Channel) -> Manifest | None:
        try:
            return self.registry.get(channel.type)
        except HubError:
            return None

    # ── 渠道写入 ────────────────────────────────────────
    def create_channel(
        self,
        name: str,
        type_name: str,
        settings: dict[str, Any],
        options: dict[str, Any] | None = None,
        enabled: bool = True,
        tags: list[str] | str | None = None,
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise HubError("invalid_argument", "name 不能为空")
        manifest = self.registry.get(str(type_name))
        normalized = self.registry.normalize_settings(manifest, settings or {})
        # 元信息与凭据在同一个对象里：一次写入 = 原子落盘，不需要回滚
        channel = self.store.insert(
            Channel(
                id=0,
                name=name,
                type=manifest.type,
                enabled=enabled,
                settings=normalized,
                options=dict(options or {}),
                tags=normalize_tags(tags),
            )
        )
        return self.get_channel(channel.id)

    def update_channel(
        self,
        ref: int | str,
        *,
        name: str | None = None,
        type_name: str | None = None,
        settings: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        enabled: bool | None = None,
        tags: list[str] | str | None = None,
    ) -> dict[str, Any]:
        channel = self.resolve(ref)
        target_type = str(type_name) if type_name else channel.type
        manifest = self.registry.get(target_type)

        next_settings = channel.settings if settings is None else settings
        if type_name and type_name != channel.type and settings is None:
            raise HubError("invalid_argument", "更换渠道类型时必须同时提供 settings")
        normalized = self.registry.normalize_settings(manifest, next_settings)
        next_options = channel.options if options is None else dict(options)

        if name is not None:
            channel.name = str(name).strip()
        channel.type = manifest.type
        channel.settings = normalized
        channel.options = next_options
        if tags is not None:
            channel.tags = normalize_tags(tags)
        if enabled is not None:
            channel.enabled = bool(enabled)
        self.store.replace(channel)
        return self.get_channel(channel.id)

    def delete_channel(self, ref: int | str) -> dict[str, Any]:
        channel = self.resolve(ref)
        deleted = self.store.soft_delete(channel.id)
        with self._rate_lock:
            self._rate.pop(deleted.id, None)
        return {"ok": True, "deleted": {"id": deleted.id, "name": deleted.name, "type": deleted.type}}

    # ── 入口 ────────────────────────────────────────────
    def list_entries(self) -> list[dict[str, Any]]:
        return [entry.to_public() for entry in self.store.entries()]

    def resolve_entry(self, ref: int | str) -> Entry:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            try:
                return self.store.get_entry(int(ref))
            except HubError:
                pass
        name = str(ref).strip()
        if not name:
            raise HubError("invalid_argument", "entry 不能为空")
        entries = self.store.entries()
        exact = [e for e in entries if e.name.casefold() == name.casefold()]
        if len(exact) == 1:
            return exact[0]
        available = [{"id": e.id, "name": e.name} for e in entries]
        raise HubError("not_found", f"入口 {name} 不存在", available=available)

    def get_entry(self, ref: int | str) -> dict[str, Any]:
        return self.resolve_entry(ref).to_public()

    def create_entry(
        self,
        name: str,
        tags: list[str] | str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """新建入口。返回值**包含明文 token**，仅此一次。"""
        name = str(name or "").strip()
        if not name:
            raise HubError("invalid_argument", "入口名称不能为空")
        entry = self.store.insert_entry(name, tags=normalize_tags(tags), enabled=enabled)
        data = entry.to_public()
        data["secret"] = entry.secret  # 只在这里出现一次
        return data

    def update_entry(
        self,
        ref: int | str,
        *,
        name: str | None = None,
        tags: list[str] | str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        entry = self.resolve_entry(ref)
        if name is not None:
            entry.name = str(name).strip()
            if not entry.name:
                raise HubError("invalid_argument", "入口名称不能为空")
        if tags is not None:
            entry.tags = normalize_tags(tags)
        if enabled is not None:
            entry.enabled = bool(enabled)
        return self.store.replace_entry(entry).to_public()

    def delete_entry(self, ref: int | str) -> dict[str, Any]:
        entry = self.resolve_entry(ref)
        deleted = self.store.delete_entry(entry.id)
        return {"ok": True, "deleted": {"id": deleted.id, "name": deleted.name}}

    # ── 路由规则 ────────────────────────────────────────
    def list_rules(self) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in self.store.rules()]

    def resolve_rule(self, ref: int | str) -> Rule:
        if isinstance(ref, int) or (isinstance(ref, str) and str(ref).strip().isdigit()):
            try:
                return self.store.get_rule(int(ref))
            except HubError:
                pass
        name = str(ref).strip()
        rules = self.store.rules()
        exact = [r for r in rules if r.name.casefold() == name.casefold()]
        if len(exact) == 1:
            return exact[0]
        raise HubError(
            "not_found",
            f"规则 {name} 不存在",
            available=[{"id": r.id, "name": r.name} for r in rules],
        )

    def get_rule(self, ref: int | str) -> dict[str, Any]:
        return self.resolve_rule(ref).to_dict()

    @staticmethod
    def _condition(raw: Any, *, side: str) -> Condition:
        return Condition.from_dict(raw, side=side)

    def create_rule(
        self,
        name: str,
        outbound: dict[str, Any],
        entry: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise HubError("invalid_argument", "规则名称不能为空")
        rule = Rule(
            id=0,
            name=name,
            entry=self._condition(entry, side="entry") if entry else None,
            outbound=self._condition(outbound, side="outbound"),
            enabled=enabled,
        )
        return self.store.insert_rule(rule).to_dict()

    def update_rule(
        self,
        ref: int | str,
        *,
        name: str | None = None,
        outbound: dict[str, Any] | None = None,
        entry: dict[str, Any] | None = None,
        clear_entry: bool = False,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        rule = self.resolve_rule(ref)
        if name is not None:
            rule.name = str(name).strip()
        if outbound is not None:
            rule.outbound = self._condition(outbound, side="outbound")
        if clear_entry:
            rule.entry = None
        elif entry is not None:
            rule.entry = self._condition(entry, side="entry")
        if enabled is not None:
            rule.enabled = bool(enabled)
        return self.store.replace_rule(rule).to_dict()

    def delete_rule(self, ref: int | str) -> dict[str, Any]:
        rule = self.resolve_rule(ref)
        deleted = self.store.delete_rule(rule.id)
        return {"ok": True, "deleted": {"id": deleted.id, "name": deleted.name}}

    def route_preview(self, entry_ref: int | str | None = None) -> dict[str, Any]:
        """给定入口，看看会发到哪些出口（不发送）。"""
        entry: Entry | None = None
        if entry_ref not in (None, ""):
            entry = self.resolve_entry(entry_ref)
        channels = self.store.channels()
        rules = self.store.rules()
        selection = routing.select(rules, entry, channels)
        return {
            "entry": entry.to_public() if entry else None,
            "outbounds": [self._describe(channel) for channel in selection.channels],
            "matched_rules": selection.matched_rules,
            "trace": routing.explain(rules, entry, channels),
            "total_rules": selection.total_rules,
            "enabled_rules": selection.enabled_rules,
        }

    # ── 发送 ────────────────────────────────────────────
    def _check_rate(self, channel: Channel, *, consume: bool = True) -> None:
        """单渠道限流。

        consume=False 用于 dry_run：只校验不真发，不占配额也不被限流拦下
        （限流保护的是下游服务，不是本地校验）。
        """
        limit = channel.rate_limit_per_min
        if limit is None:
            limit = self.settings.default_rate_limit
        if not limit or limit <= 0:
            return
        now = time.monotonic()
        with self._rate_lock:
            bucket = self._rate.setdefault(channel.id, deque())
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= limit:
                wait = 60 - (now - bucket[0])
                raise HubError(
                    "rate_limited",
                    f"渠道 {channel.name} 超出频率限制（{limit} 条/分钟），请 {wait:.0f}s 后重试",
                    limit_per_min=limit,
                    retry_after_seconds=round(wait, 1),
                )
            if consume:
                bucket.append(now)

    def _validate_message(
        self,
        channel: Channel,
        manifest: Manifest,
        text: str,
        fmt: str,
        mention_users: list[str],
        mention_mobiles: list[str],
        mention_all: bool,
    ) -> Message:
        """渠道相关的消息校验；不通过就抛 HubError（调用方决定是跳过还是失败）。"""
        if fmt not in MESSAGE_FORMATS:
            raise HubError("invalid_argument", f"format 只支持 {MESSAGE_FORMATS}")
        formats = (manifest.capabilities or {}).get("formats")
        if isinstance(formats, list) and fmt not in formats:
            raise HubError(
                "invalid_argument",
                f"渠道类型 {manifest.type} 不支持 format={fmt}（支持：{formats}）",
            )
        if mention_all:
            allowed = channel.allow_mention_all and bool((manifest.capabilities or {}).get("mention_all"))
            if not allowed:
                raise HubError(
                    "mention_all_denied",
                    f"渠道 {channel.name} 未开启 @所有人（需在 options.allow_mention_all=true 且类型支持）",
                )
        if (mention_users or mention_mobiles) and not (manifest.capabilities or {}).get("mention"):
            raise HubError("invalid_argument", f"渠道类型 {manifest.type} 不支持 @指定成员")
        return Message(
            text=text,
            format=fmt,  # type: ignore[arg-type]
            title=None,
            mention_users=list(mention_users),
            mention_mobiles=list(mention_mobiles),
            mention_all=bool(mention_all),
        )

    @staticmethod
    def _error_of(exc: HubError) -> dict[str, Any]:
        error = exc.to_dict()["error"]
        return {"code": error["code"], "message": error["message"], "retryable": error["retryable"]}

    def _deliver(
        self,
        channel: Channel,
        message: Message,
        request_id: str,
        dry_run: bool,
        source: str,
        *,
        strict: bool,
        matched_rules: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """向单个出口投递。

        strict=True（管理员直连）：任何问题都抛出，保持 v2 的语义。
        strict=False（路由扇出）：问题记为 skipped/failed，不中断其他出口。

        matched_rules 是**选中本出口的规则**（不是本次请求命中的全部规则）——
        日志是按出口一行的，所以每一行只记「为什么发到你这个出口」。
        """
        info = self._info(channel)
        try:
            status, detail = self.channel_status(channel)
            if status == "disabled":
                raise HubError("channel_disabled", f"渠道 {channel.name} 已停用")
            if status == "broken":
                raise HubError(
                    "channel_broken", f"渠道 {channel.name} 不可用：{detail}", channel_id=channel.id
                )
            manifest = self.registry.get(channel.type)
            prepared = self._validate_message(
                channel,
                manifest,
                message.text,
                message.format,
                message.mention_users,
                message.mention_mobiles,
                message.mention_all,
            )
            prepared.title = message.title
            self._check_rate(channel, consume=not dry_run)
        except HubError as exc:
            if strict:
                self._record(
                    info, message, request_id, source, dry_run, exc.code, exc.message, None,
                    outcome="failed", matched_rules=matched_rules, routed=not strict,
                )
                raise
            # 配置类问题算「跳过」，不算投递失败——避免一个不兼容的出口
            # 把整个多出口请求污染成 partial
            self._record(
                info, message, request_id, source, dry_run, exc.code, exc.message, None,
                outcome="skipped", matched_rules=matched_rules, routed=not strict,
            )
            return {
                "channel": info,
                "status": "skipped",
                "ok": False,
                "error": self._error_of(exc),
            }

        try:
            result = self.registry.invoke(
                manifest,
                settings=channel.settings,
                channel=info,
                message=prepared,
                options=channel.options,
                request_id=request_id,
                dry_run=dry_run,
            )
        except HubError as exc:
            if strict:
                self._record(
                    info, message, request_id, source, dry_run, exc.code, exc.message, None,
                    outcome="failed", matched_rules=matched_rules, routed=not strict,
                )
                raise
            self._record(
                info, message, request_id, source, dry_run, exc.code, exc.message, None,
                outcome="failed", matched_rules=matched_rules, routed=not strict,
            )
            return {
                "channel": info,
                "status": "failed",
                "ok": False,
                "error": self._error_of(exc),
            }

        self._record(
            info, message, request_id, source, dry_run, None, "发送成功", result.elapsed_ms,
            outcome="sent", matched_rules=matched_rules, routed=not strict,
        )
        return {
            "channel": info,
            "status": "sent",
            "ok": True,
            "provider": result.provider,
            "message_id": result.message_id,
            "elapsed_ms": result.elapsed_ms,
        }

    @staticmethod
    def _rules_for(channel: Channel, matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """从命中规则里挑出「真正选中了这个出口」的那些（日志按出口一行）。"""
        return [
            {"id": rule["id"], "name": rule["name"]}
            for rule in matched
            if channel.id in (rule.get("outbounds") or [])
        ]

    def _identity_entry(self, identity: Identity) -> Entry | None:
        """把当前身份翻译成入口对象（管理员返回 None）。"""
        if identity.is_admin or identity.entry_id is None:
            return None
        try:
            return self.store.get_entry(int(identity.entry_id))
        except HubError:
            return None

    def send(
        self,
        text: str | None,
        *,
        channel: int | str | None = None,
        format: str = "plain",  # noqa: A002 - 参数名与对外契约一致
        title: str | None = None,
        mention_users: list[str] | None = None,
        mention_mobiles: list[str] | None = None,
        mention_all: bool = False,
        dry_run: bool = False,
        source: str = "api",
    ) -> dict[str, Any]:
        identity = current_identity()
        request_id = uuid.uuid4().hex[:12]

        body = "" if text is None else str(text)
        if not body.strip():
            raise HubError("invalid_argument", "text 不能为空")
        if len(body) > self.settings.max_text_chars:
            raise HubError(
                "text_too_long",
                f"消息过长（{len(body)} 字符，上限 {self.settings.max_text_chars}）",
            )
        fmt = str(format or "plain").lower()

        message = Message(
            text=body,
            format=fmt,  # type: ignore[arg-type]
            title=title,
            mention_users=list(mention_users or []),
            mention_mobiles=list(mention_mobiles or []),
            mention_all=bool(mention_all),
        )

        # ── 1. 决定出口集合 ──────────────────────────────
        direct = channel not in (None, "")
        matched_rules: list[dict[str, Any]] = []
        if direct:
            if not identity.is_admin:
                raise HubError(
                    "forbidden",
                    "调用方不能指定出口：只传 token 即可，投递目标由服务端的路由规则决定",
                )
            targets = [self.resolve(channel)]  # type: ignore[arg-type]
            strict = True
        else:
            entry = self._identity_entry(identity)
            channels = self.store.channels()
            rules = self.store.rules()
            selection = routing.select(rules, entry, channels)
            matched_rules = selection.matched_rules
            targets = selection.channels
            strict = False
            if not targets:
                raise HubError(
                    "no_route",
                    self._no_route_message(identity, entry, selection),
                    entry=entry.to_public() if entry else {"type": "admin"},
                    total_rules=selection.total_rules,
                    enabled_rules=selection.enabled_rules,
                    available_channels=[
                        {"id": c.id, "name": c.name, "type": c.type, "tags": c.tags, "enabled": c.enabled}
                        for c in channels
                    ],
                )

        # ── 2. 逐个出口发送（串行，保持可预期）─────────────
        results = [
            self._deliver(
                target, message, request_id, dry_run, source, strict=strict,
                matched_rules=self._rules_for(channel=target, matched=matched_rules),
            )
            for target in targets
        ]

        # ── 3. 聚合 ─────────────────────────────────────
        sent = [r for r in results if r["status"] == "sent"]
        skipped = [r for r in results if r["status"] == "skipped"]
        failed = [r for r in results if r["status"] == "failed"]

        if not sent and not dry_run:
            raise HubError(
                "all_failed",
                f"所有 {len(results)} 个出口都未成功发送",
                request_id=request_id,
                results=results,
            )

        payload: dict[str, Any] = {
            "ok": True,
            "request_id": request_id,
            "dry_run": dry_run,
            "routed": not direct,
            "partial": bool(skipped or failed),
            "counts": {
                "total": len(results),
                "sent": len(sent),
                "skipped": len(skipped),
                "failed": len(failed),
            },
            "results": results,
        }
        if not direct:
            payload["matched_rules"] = matched_rules
        # 单出口时保留 v2 的顶层字段，老调用方不用改
        if len(results) == 1:
            only = results[0]
            payload["channel"] = only["channel"]
            if only["status"] == "sent":
                payload["provider"] = only.get("provider")
                payload["message_id"] = only.get("message_id")
                payload["elapsed_ms"] = only.get("elapsed_ms")
        return payload

    def _no_route_message(self, identity: Identity, entry: Entry | None, selection: routing.Selection) -> str:
        who = identity.describe()
        if selection.total_rules == 0:
            return (
                f"入口「{who}」没有匹配到任何出口：当前没有任何路由规则。"
                "请在 dashboard 的「路由」页签里新建规则（入口条件可留空表示所有入口）。"
            )
        if selection.enabled_rules == 0:
            return f"入口「{who}」没有匹配到任何出口：全部 {selection.total_rules} 条规则都已停用。"
        tags = entry.tags if entry else []
        return (
            f"入口「{who}」没有匹配到任何出口（标签：{tags or '无'}）。"
            f"共 {selection.total_rules} 条规则、{selection.enabled_rules} 条启用，但都不适用于该入口，"
            "或选中的出口不存在。可用 route_preview 查看每条规则的判定明细。"
        )

    def test_channel(
        self,
        ref: int | str,
        text: str = "这是一条来自 msg-hub 的测试消息",
        *,
        dry_run: bool = True,
        settings_override: dict[str, Any] | None = None,
        options_override: dict[str, Any] | None = None,
        type_name: str | None = None,
    ) -> dict[str, Any]:
        """测试发送：默认 dry_run（只校验不真发），也支持对已有渠道真发一条。"""
        if settings_override is None:
            return self.send(text, channel=ref, dry_run=dry_run, source="test")
        # 尚未保存的配置：直接走 stdin 交给插件（不再写临时文件）
        target_type = str(type_name or "")
        if not target_type:
            channel = self.resolve(ref) if ref not in ("", None) else None
            target_type = channel.type if channel else ""
        manifest = self.registry.get(target_type)
        normalized = self.registry.normalize_settings(manifest, settings_override)
        options = dict(options_override or {})
        request_id = uuid.uuid4().hex[:12]
        message = Message(text=text)
        info = {"id": 0, "name": "(未保存的配置)", "type": manifest.type}
        try:
            result = self.registry.invoke(
                manifest,
                settings=normalized,
                channel=info,
                message=message,
                options=options,
                request_id=request_id,
                dry_run=dry_run,
            )
        except HubError as exc:
            self._record(info, message, request_id, "test", dry_run, exc.code, exc.message, None,
                         outcome="failed")
            raise
        self._record(info, message, request_id, "test", dry_run, None, "发送成功", result.elapsed_ms,
                     outcome="sent")
        return {"ok": True, "dry_run": dry_run, "provider": result.provider, "elapsed_ms": result.elapsed_ms}

    @staticmethod
    def _info(channel: Channel) -> dict[str, Any]:
        return {"id": channel.id, "name": channel.name, "type": channel.type}

    def _record(
        self,
        channel: dict[str, Any],
        message: Message,
        request_id: str,
        source: str,
        dry_run: bool,
        error_code: str | None,
        error_message: str,
        elapsed_ms: int | None,
        *,
        outcome: str = "sent",
        matched_rules: list[dict[str, Any]] | None = None,
        routed: bool | None = None,
    ) -> None:
        """写审计日志。

        审计失败绝不能反过来让发送失败：消息可能已经发出去了，
        此时返回错误只会诱导调用方重试，造成重复发送。

        actor 记录「是谁发的」，只记入口的 id / 名称 / 前缀，不记完整凭据。
        扇出时每个出口写一条，共用同一个 request_id，便于按请求聚合查看。
        """
        preview = message.text.replace("\n", " ")[:100]
        actor = current_identity().audit_actor()
        try:
            self.audit.append(
                {
                    "ts": now_iso(),
                    "request_id": request_id,
                    "source": source,
                    "actor": actor,
                    "dry_run": dry_run,
                    "outcome": outcome,
                    "matched_rules": list(matched_rules or []),
                    "routed": routed,
                    "channel": channel,
                    "format": message.format,
                    "chars": len(message.text),
                    "text_preview": preview,
                    "ok": error_code is None,
                    "error_code": error_code,
                    "error_message": None if error_code is None else error_message,
                    "elapsed_ms": elapsed_ms,
                }
            )
        except Exception as exc:  # pragma: no cover - 磁盘满/权限异常
            print(f"[msg-hub] 审计日志写入失败（不影响发送结果）：{exc}", file=sys.stderr)

    # ── 日志 ────────────────────────────────────────────
    def logs(self, channel_ref: int | str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        channel_id = None
        if channel_ref not in (None, ""):
            channel_id = self.resolve(channel_ref).id
        return self.audit.query(channel_id=channel_id, limit=limit)
