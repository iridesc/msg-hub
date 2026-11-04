"""业务层：MCP 与 REST 共用同一套逻辑。"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any

from .audit import Audit
from .models import MESSAGE_FORMATS, Channel, ChannelConfig, HubError, Message, now_iso
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
        if not channel.enabled:
            return "disabled", None
        try:
            manifest = self.registry.get(channel.type)
        except HubError as exc:
            return "broken", str(exc)
        try:
            config = self.store.read_config(channel.id)
        except HubError as exc:
            return "broken", exc.message
        if config is None:
            return "broken", f"缺少 config/{channel.id}.json"
        try:
            self.registry.normalize_settings(manifest, config.settings)
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
            return self.store.get(int(ref))
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
        config = self.store.read_config(channel.id) or ChannelConfig()
        data = self._describe(channel)
        data["options"] = config.options
        if manifest is not None:
            data["settings"] = self.registry.mask_settings(manifest, config.settings)
        else:
            data["settings"] = {key: "***" for key in config.settings}
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
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise HubError("invalid_argument", "name 不能为空")
        manifest = self.registry.get(str(type_name))
        normalized = self.registry.normalize_settings(manifest, settings or {})
        config = ChannelConfig(settings=normalized, options=dict(options or {}))
        channel = self.store.insert(name, manifest.type, enabled=enabled)
        try:
            self.store.write_config(channel.id, config)
        except Exception:
            # 配置写失败则回滚索引，避免留下"有渠道无配置"的烂数据
            self.store.soft_delete(channel.id)
            raise
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
    ) -> dict[str, Any]:
        channel = self.resolve(ref)
        existing = self.store.read_config(channel.id) or ChannelConfig()
        target_type = str(type_name) if type_name else channel.type
        manifest = self.registry.get(target_type)

        next_settings = existing.settings if settings is None else settings
        if type_name and type_name != channel.type and settings is None:
            raise HubError("invalid_argument", "更换渠道类型时必须同时提供 settings")
        normalized = self.registry.normalize_settings(manifest, next_settings)
        next_options = existing.options if options is None else dict(options)

        if name is not None:
            channel.name = str(name).strip()
        channel.type = manifest.type
        if enabled is not None:
            channel.enabled = bool(enabled)
        self.store.replace(channel)
        self.store.write_config(channel.id, ChannelConfig(settings=normalized, options=next_options))
        return self.get_channel(channel.id)

    def delete_channel(self, ref: int | str) -> dict[str, Any]:
        channel = self.resolve(ref)
        deleted = self.store.soft_delete(channel.id)
        with self._rate_lock:
            self._rate.pop(deleted.id, None)
        return {"ok": True, "deleted": {"id": deleted.id, "name": deleted.name, "type": deleted.type}}

    # ── 发送 ────────────────────────────────────────────
    def _check_rate(self, channel: Channel, config: ChannelConfig) -> None:
        limit = config.rate_limit_per_min
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
            bucket.append(now)

    def send(
        self,
        channel_ref: int | str,
        text: str,
        *,
        format: str = "plain",
        title: str | None = None,
        mention_users: list[str] | None = None,
        mention_mobiles: list[str] | None = None,
        mention_all: bool = False,
        dry_run: bool = False,
        source: str = "api",
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex[:12]
        channel = self.resolve(channel_ref)
        status, detail = self.channel_status(channel)
        if status == "disabled":
            raise HubError("channel_disabled", f"渠道 {channel.name} 已停用")
        if status == "broken":
            raise HubError("channel_broken", f"渠道 {channel.name} 不可用：{detail}", channel_id=channel.id)

        manifest = self.registry.get(channel.type)
        config = self.store.read_config(channel.id) or ChannelConfig()

        text = "" if text is None else str(text)
        if not text.strip():
            raise HubError("invalid_argument", "text 不能为空")
        if len(text) > self.settings.max_text_chars:
            raise HubError(
                "invalid_argument",
                f"消息过长（{len(text)} 字符，上限 {self.settings.max_text_chars}）",
            )
        fmt = str(format or "plain").lower()
        if fmt not in MESSAGE_FORMATS:
            raise HubError("invalid_argument", f"format 只支持 {MESSAGE_FORMATS}")
        formats = (manifest.capabilities or {}).get("formats")
        if isinstance(formats, list) and fmt not in formats:
            raise HubError(
                "invalid_argument",
                f"渠道类型 {manifest.type} 不支持 format={fmt}（支持：{formats}）",
            )
        if mention_all:
            allowed = config.allow_mention_all and bool((manifest.capabilities or {}).get("mention_all"))
            if not allowed:
                raise HubError(
                    "mention_all_denied",
                    f"渠道 {channel.name} 未开启 @所有人（需在 options.allow_mention_all=true 且类型支持）",
                )
        if (mention_users or mention_mobiles) and not (manifest.capabilities or {}).get("mention"):
            raise HubError("invalid_argument", f"渠道类型 {manifest.type} 不支持 @指定成员")

        self._check_rate(channel, config)

        message = Message(
            text=text,
            format=fmt,  # type: ignore[arg-type]
            title=title,
            mention_users=list(mention_users or []),
            mention_mobiles=list(mention_mobiles or []),
            mention_all=bool(mention_all),
        )
        try:
            result = self.registry.invoke(
                manifest,
                config_path=self.store.config_path(channel.id),
                channel={"id": channel.id, "name": channel.name, "type": channel.type},
                message=message,
                options=config.options,
                request_id=request_id,
                dry_run=dry_run,
            )
        except HubError as exc:
            self._record(channel, message, request_id, source, dry_run, exc.code, exc.message, None)
            raise
        self._record(channel, message, request_id, source, dry_run, None, "发送成功", result.elapsed_ms)
        return {
            "ok": True,
            "request_id": request_id,
            "dry_run": dry_run,
            "channel": {"id": channel.id, "name": channel.name, "type": channel.type},
            "provider": result.provider,
            "message_id": result.message_id,
            "elapsed_ms": result.elapsed_ms,
        }

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
            return self.send(ref, text, dry_run=dry_run, source="test")
        # 尚未保存的配置：写临时文件交给插件
        target_type = str(type_name or "")
        if not target_type:
            channel = self.resolve(ref) if ref not in ("", None) else None
            target_type = channel.type if channel else ""
        manifest = self.registry.get(target_type)
        normalized = self.registry.normalize_settings(manifest, settings_override)
        config = ChannelConfig(settings=normalized, options=dict(options_override or {}))
        temp_path = self.store.write_temp_config(config)
        try:
            result = self.registry.invoke(
                manifest,
                config_path=temp_path,
                channel={"id": 0, "name": "(未保存的配置)", "type": manifest.type},
                message=Message(text=text),
                options=config.options,
                request_id=uuid.uuid4().hex[:12],
                dry_run=dry_run,
            )
        finally:
            temp_path.unlink(missing_ok=True)
        return {"ok": True, "dry_run": dry_run, "provider": result.provider, "elapsed_ms": result.elapsed_ms}

    def _record(
        self,
        channel: Channel,
        message: Message,
        request_id: str,
        source: str,
        dry_run: bool,
        error_code: str | None,
        error_message: str,
        elapsed_ms: int | None,
    ) -> None:
        preview = message.text.replace("\n", " ")[:100]
        self.audit.append(
            {
                "ts": now_iso(),
                "request_id": request_id,
                "source": source,
                "dry_run": dry_run,
                "channel": {"id": channel.id, "name": channel.name, "type": channel.type},
                "format": message.format,
                "chars": len(message.text),
                "text_preview": preview,
                "ok": error_code is None,
                "error_code": error_code,
                "error_message": None if error_code is None else error_message,
                "elapsed_ms": elapsed_ms,
            }
        )

    # ── 日志 ────────────────────────────────────────────
    def logs(self, channel_ref: int | str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        channel_id = None
        if channel_ref not in (None, ""):
            channel_id = self.resolve(channel_ref).id
        return self.audit.query(channel_id=channel_id, limit=limit)
