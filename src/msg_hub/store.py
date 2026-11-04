"""渠道库持久化。

布局：
    <data>/channels.json      渠道索引（id / 名称 / 类型 / 启用状态）
    <data>/config/<id>.json   每个渠道的配置（凭据 + 护栏），权限 0600
    <data>/config/.trash/     软删除后的配置
    <data>/logs/*.jsonl       发送审计日志

所有写入都是「同目录临时文件 + os.replace」原子替换，避免半截文件。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any

from .models import Channel, ChannelConfig, HubError, now_iso
from .settings import Settings

INDEX_VERSION = 1
MAX_DELETED_KEPT = 100


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _atomic_write(path: Path, payload: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        settings.ensure_dirs()
        if not settings.index_path.exists():
            self._save_index({"version": INDEX_VERSION, "next_id": 1, "channels": [], "deleted": []})
        # 首次运行给一份可读的说明
        readme = settings.data_dir / "README.md"
        if not readme.exists():
            _atomic_write(
                readme,
                "# msg-hub 数据目录\n\n"
                "- `channels.json`：渠道索引（id / 名称 / 类型），可手工编辑但建议走 API\n"
                "- `config/<id>.json`：每个渠道的配置，含凭据，权限 0600\n"
                "- `config/.trash/`：被删除渠道的配置备份\n"
                "- `logs/`：发送审计日志（按天 jsonl）\n"
                "- `.cache/`：插件可用的缓存目录（如 access_token）\n",
                mode=0o644,
            )

    # ── 索引 ────────────────────────────────────────────
    def _load_index(self) -> dict[str, Any]:
        try:
            raw = _read_json(self.settings.index_path)
        except FileNotFoundError:
            return {"version": INDEX_VERSION, "next_id": 1, "channels": [], "deleted": []}
        except json.JSONDecodeError as exc:
            raise HubError("channel_broken", f"channels.json 解析失败：{exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("channels"), list):
            raise HubError("channel_broken", "channels.json 结构不合法")
        raw.setdefault("version", INDEX_VERSION)
        raw.setdefault("deleted", [])
        raw.setdefault("next_id", max((int(c.get("id", 0)) for c in raw["channels"]), default=0) + 1)
        return raw

    def _save_index(self, index: dict[str, Any]) -> None:
        index["deleted"] = index.get("deleted", [])[-MAX_DELETED_KEPT:]
        _atomic_write(self.settings.index_path, json.dumps(index, ensure_ascii=False, indent=2))

    def channels(self, include_deleted: bool = False) -> list[Channel]:
        with self._lock:
            index = self._load_index()
            items = [Channel.from_dict(raw) for raw in index["channels"]]
            if include_deleted:
                items += [Channel.from_dict(raw) for raw in index.get("deleted", [])]
            return items

    def get(self, channel_id: int) -> Channel:
        for channel in self.channels():
            if channel.id == channel_id:
                return channel
        raise HubError("not_found", f"渠道 id={channel_id} 不存在")

    def find_by_name(self, name: str) -> list[Channel]:
        target = name.strip().casefold()
        exact = [c for c in self.channels() if c.name.casefold() == target]
        return exact

    def insert(self, name: str, type_name: str, enabled: bool = True) -> Channel:
        with self._lock:
            index = self._load_index()
            if any(c["name"].casefold() == name.casefold() for c in index["channels"]):
                raise HubError("invalid_argument", f"渠道名称已存在：{name}")
            channel = Channel(id=int(index["next_id"]), name=name, type=type_name, enabled=enabled)
            index["next_id"] = channel.id + 1
            index["channels"].append(channel.to_dict())
            self._save_index(index)
            return channel

    def replace(self, channel: Channel) -> Channel:
        with self._lock:
            index = self._load_index()
            for idx, raw in enumerate(index["channels"]):
                if int(raw["id"]) == channel.id:
                    channel.updated_at = now_iso()
                    if any(
                        c["name"].casefold() == channel.name.casefold() and int(c["id"]) != channel.id
                        for c in index["channels"]
                    ):
                        raise HubError("invalid_argument", f"渠道名称已存在：{channel.name}")
                    index["channels"][idx] = channel.to_dict()
                    self._save_index(index)
                    return channel
            raise HubError("not_found", f"渠道 id={channel.id} 不存在")

    def soft_delete(self, channel_id: int) -> Channel:
        with self._lock:
            channel = self.get(channel_id)
            index = self._load_index()
            index["channels"] = [c for c in index["channels"] if int(c["id"]) != channel_id]
            index["deleted"].append({**channel.to_dict(), "deleted_at": now_iso()})
            self._save_index(index)
            self._trash_config(channel_id)
            return channel

    # ── 渠道配置 ────────────────────────────────────────
    def config_path(self, channel_id: int) -> Path:
        return self.settings.config_dir / f"{channel_id}.json"

    def read_config(self, channel_id: int) -> ChannelConfig | None:
        path = self.config_path(channel_id)
        if not path.exists():
            return None
        try:
            raw = _read_json(path)
        except json.JSONDecodeError as exc:
            raise HubError("channel_broken", f"config/{channel_id}.json 解析失败：{exc}") from exc
        if not isinstance(raw, dict):
            raise HubError("channel_broken", f"config/{channel_id}.json 不是 JSON 对象")
        return ChannelConfig.from_dict(raw)

    def write_config(self, channel_id: int, config: ChannelConfig) -> None:
        _atomic_write(
            self.config_path(channel_id),
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
        )

    def _trash_config(self, channel_id: int) -> None:
        path = self.config_path(channel_id)
        if path.exists():
            stamp = now_iso().replace(":", "").replace("+", "_")
            shutil.move(str(path), str(self.settings.trash_dir / f"{channel_id}-{stamp}.json"))

    def write_temp_config(self, config: ChannelConfig) -> Path:
        """测试发送用：把配置写到临时文件，交给插件读完即删。"""
        self.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.tmp_dir / f"probe-{os.getpid()}-{secrets.token_hex(6)}.json"
        _atomic_write(path, json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
        return path
