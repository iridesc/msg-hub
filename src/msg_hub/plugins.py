"""渠道类型插件：发现 / 校验 / 调用。

一个「类型」= `types/<type>/` 目录，包含：

    manifest.json   类型元信息 + settings_schema（JSON Schema）
    send_msg.py     发送实现

调用契约（子进程，进程隔离）：

    argv:  <python> send_msg.py --config <config/<id>.json 或临时文件> [--dry-run]
    stdin: 单行 JSON
           {
             "version": 1,
             "request_id": "...",
             "channel": {"id": 1, "name": "工作群1", "type": "wecom_bot"},
             "message": {"text": "...", "format": "plain|markdown", "title": null,
                         "mention_users": [], "mention_mobiles": [], "mention_all": false},
             "options": {...},          # config/<id>.json 的 options
             "dry_run": false,
             "cache_dir": "<data>/.cache"
           }
    stdout: 单行 JSON 结果
           {"ok": true,  "provider": "wecom_bot", "message_id": "...", "elapsed_ms": 231, "raw": {...}}
           {"ok": false, "error": {"code": "send_failed", "message": "...", "retryable": false}}
    exit:   0 成功 / 1 可重试失败 / 2 不可重试失败
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schema as _schema
from .models import HubError, Message
from .settings import Settings

MANIFEST_REQUIRED = ("type", "name", "entry", "settings_schema")


@dataclass
class Manifest:
    type: str
    name: str
    description: str
    entry: Path
    settings_schema: dict[str, Any]
    capabilities: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"

    def summary(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.capabilities,
            "settings_schema": self.settings_schema,
        }


@dataclass
class PluginResult:
    ok: bool
    code: str
    message: str
    provider: str
    message_id: str | None = None
    elapsed_ms: int = 0
    retryable: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "provider": self.provider,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.message_id:
            data["message_id"] = self.message_id
        if self.raw:
            data["raw"] = self.raw
        if not self.ok:
            data["error"] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        return data


def details_from(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    """排序去重，报错稳定（任何字段缺失的 required 排最前）。"""
    seen: set[str] = set()
    unique = []
    for item in errors:
        key = f"{item['field']}|{item['message']}"
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _secret_paths(schema: dict[str, Any]) -> dict[str, str]:
    """收集 schema 里标了 secret: true 的字段路径 → 该字段的 title（用于展示）。"""
    found: dict[str, str] = {}
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        if prop.get("secret") is True:
            found[name] = str(prop.get("title") or name)
    return found


def _mask_value(value: str) -> str:
    """URL 形态的密钥只打码 key 参数（保留主机便于排障），其余只留末 4 位。"""
    if value.startswith(("http://", "https://")) and "key=" in value:
        head, _, tail = value.partition("key=")
        return f"{head}key={tail[:6]}***"
    return f"***{value[-4:]}" if len(value) > 8 else "***"


def _apply_defaults(schema: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    result = dict(settings)
    for name, prop in (schema.get("properties") or {}).items():
        if isinstance(prop, dict) and name not in result and "default" in prop:
            result[name] = prop["default"]
    return result


class Registry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, Manifest] = {}
        self._mtime: dict[str, float] = {}

    # ── 发现 ────────────────────────────────────────────
    def _load_one(self, directory: Path) -> Manifest:
        manifest_path = directory / "manifest.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise HubError("type_unknown", f"{directory.name}: 缺少 manifest.json") from exc
        except json.JSONDecodeError as exc:
            raise HubError("type_unknown", f"{directory.name}: manifest.json 解析失败：{exc}") from exc
        missing = [key for key in MANIFEST_REQUIRED if not raw.get(key)]
        if missing:
            raise HubError("type_unknown", f"{directory.name}: manifest.json 缺少字段 {missing}")
        if raw["type"] != directory.name:
            raise HubError("type_unknown", f"{directory.name}: manifest.type 必须与目录名一致")
        entry = directory / str(raw["entry"])
        if not entry.exists():
            raise HubError("type_unknown", f"{directory.name}: 入口文件不存在 {raw['entry']}")
        if not isinstance(raw["settings_schema"], dict) or raw["settings_schema"].get("type") != "object":
            raise HubError("type_unknown", f"{directory.name}: settings_schema 必须是 object schema")
        return Manifest(
            type=str(raw["type"]),
            name=str(raw["name"]),
            description=str(raw.get("description") or ""),
            entry=entry,
            settings_schema=dict(raw["settings_schema"]),
            capabilities=dict(raw.get("capabilities") or {}),
            version=str(raw.get("version") or "1.0.0"),
        )

    def load(self) -> dict[str, Manifest]:
        types_dir = self.settings.types_dir
        manifests: dict[str, Manifest] = {}
        if not types_dir.is_dir():
            self._cache, self._mtime = {}, {}
            return manifests
        for directory in sorted(types_dir.iterdir()):
            if not directory.is_dir() or directory.name.startswith((".", "_")):
                continue
            manifest_path = directory / "manifest.json"
            if not manifest_path.exists():
                continue
            mtime = manifest_path.stat().st_mtime
            cached = self._cache.get(directory.name)
            if cached is not None and self._mtime.get(directory.name) == mtime:
                manifests[directory.name] = cached
                continue
            try:
                manifest = self._load_one(directory)
            except HubError as exc:
                # 单个坏插件不影响其它类型
                print(f"[plugins] 跳过 {directory.name}: {exc}", file=sys.stderr)
                continue
            self._cache[directory.name] = manifest
            self._mtime[directory.name] = mtime
            manifests[directory.name] = manifest
        self._cache = {k: v for k, v in self._cache.items() if k in manifests}
        return manifests

    def all(self) -> list[Manifest]:
        return list(self.load().values())

    def get(self, type_name: str) -> Manifest:
        manifests = self.load()
        if type_name not in manifests:
            raise HubError(
                "type_unknown",
                f"未知渠道类型：{type_name}（可用：{', '.join(sorted(manifests)) or '无'}）",
                available=sorted(manifests),
            )
        return manifests[type_name]

    # ── 校验 ────────────────────────────────────────────
    def normalize_settings(self, manifest: Manifest, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise HubError("invalid_settings", "settings 必须是对象")
        merged = _apply_defaults(manifest.settings_schema, settings)
        try:
            errors = _schema.validate(manifest.settings_schema, merged)
        except _schema.SchemaError as exc:
            raise HubError("type_unknown", f"{manifest.type}: manifest schema 不合法：{exc}") from exc
        if errors:
            raise HubError(
                "invalid_settings",
                "; ".join(f"{d['field']}: {d['message']}" for d in errors),
                fields=details_from(errors),
            )
        return merged

    def mask_settings(self, manifest: Manifest, settings: dict[str, Any]) -> dict[str, Any]:
        masked = dict(settings)
        for name in _secret_paths(manifest.settings_schema):
            value = masked.get(name)
            if isinstance(value, str) and value:
                masked[name] = _mask_value(value)
        return masked

    # ── 调用 ────────────────────────────────────────────
    def invoke(
        self,
        manifest: Manifest,
        *,
        config_path: Path,
        channel: dict[str, Any],
        message: Message,
        options: dict[str, Any],
        request_id: str,
        dry_run: bool,
    ) -> PluginResult:
        payload = {
            "version": 1,
            "request_id": request_id,
            "channel": channel,
            "message": message.to_dict(),
            "options": options,
            "dry_run": dry_run,
            "cache_dir": str(self.settings.cache_dir),
        }
        argv = [sys.executable, str(manifest.entry), "--config", str(config_path)]
        if dry_run:
            argv.append("--dry-run")
        env = {
            **os.environ,
            "MSG_HUB_DATA_DIR": str(self.settings.data_dir),
            "MSG_HUB_TYPES_DIR": str(self.settings.types_dir),
            "MSG_HUB_CACHE_DIR": str(self.settings.cache_dir),
            "MSG_HUB_REQUEST_ID": request_id,
        }
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=self.settings.plugin_timeout,
                env=env,
                cwd=str(manifest.entry.parent),
            )
        except subprocess.TimeoutExpired as exc:
            raise HubError(
                "plugin_timeout",
                f"{manifest.type} 插件超时（>{self.settings.plugin_timeout:g}s）",
            ) from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stderr = (proc.stderr or "").strip()
        parsed = _parse_stdout(proc.stdout)
        if parsed is None:
            detail = stderr.splitlines()[-1] if stderr else (proc.stdout or "").strip()
            code = "plugin_error"
            return PluginResult(
                ok=False,
                code=code,
                message=f"{manifest.type} 插件未返回合法 JSON（exit={proc.returncode}）：{detail[:500]}",
                provider=manifest.type,
                elapsed_ms=elapsed_ms,
                retryable=proc.returncode == 1,
                stderr=stderr[-2000:],
            )
        ok = bool(parsed.get("ok")) and proc.returncode == 0
        error = parsed.get("error") or {}
        result = PluginResult(
            ok=ok,
            code=str(error.get("code") or ("send_failed" if not ok else "ok")),
            message=str(error.get("message") or ("发送成功" if ok else "发送失败")),
            provider=str(parsed.get("provider") or manifest.type),
            message_id=parsed.get("message_id"),
            elapsed_ms=int(parsed.get("elapsed_ms") or elapsed_ms),
            retryable=bool(error.get("retryable", proc.returncode == 1)),
            raw=parsed.get("raw") if isinstance(parsed.get("raw"), dict) else {},
            stderr=stderr[-2000:],
        )
        if not result.ok:
            # retryable 以插件的判定为准（插件最了解下游错误，如限流/网络抖动）
            raise HubError(
                result.code,
                result.message,
                retryable=result.retryable,
                provider=result.provider,
                raw=result.raw,
            )
        return result


def _parse_stdout(stdout: str) -> dict[str, Any] | None:
    """插件可能打日志到 stdout，取最后一个能解析成 JSON 对象的行。"""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None
