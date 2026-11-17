"""单一状态文件的持久化：渠道 + API Token 全在一个 `state.json` 里。

为什么合成一个文件：凭据曾经单独存 `config/<id>.json`，导致一个渠道的数据
劈成两半——索引写成功、配置写失败（或误删、备份没对上）就会出现
「有渠道无配置」的 broken 状态，还要为它写回滚逻辑。合起来之后：

    一次 read  = 拿到全部状态（不再是 N 个渠道 N 次 open）
    一次写盘   = 原子替换，不会出现半截状态

布局：
    <data>/state.json   全部持久化状态（渠道 + tokens），权限 0600
    <data>/logs/        发送审计日志
    <data>/.cache/      插件可用的缓存目录
    <data>/.lock        跨进程锁

所有写入都是「同目录临时文件 + os.replace`」原子替换。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from .models import (
    TOKEN_BYTES,
    TOKEN_PREFIX,
    Channel,
    HubError,
    Token,
    now_iso,
)
from .settings import Settings

try:  # POSIX 文件锁；非 POSIX 平台降级为纯线程锁
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

STATE_VERSION = 2
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


def new_secret() -> str:
    """生成一把新 token：mh_ + 32 个 hex。"""
    return TOKEN_PREFIX + secrets.token_hex(TOKEN_BYTES)


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._lock_depth = 0
        settings.ensure_dirs()
        self._lock_fd = os.open(settings.data_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        self._migrate_legacy()
        if not settings.state_path.exists():
            self._save(self._empty_state())
        self._import_legacy_token()
        self._write_readme()

    # ── 跨进程互斥 ──────────────────────────────────────
    @contextlib.contextmanager
    def _exclusive(self):
        """写状态前加锁：进程内用 RLock，进程间用 flock。

        stdio 与 HTTP 两种形态可能指向同一个数据目录，单靠线程锁挡不住
        两个进程同时改状态（会丢更新）。
        """
        with self._lock:
            self._lock_depth += 1
            if self._lock_depth == 1 and fcntl is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if self._lock_depth == 1 and fcntl is not None:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_depth -= 1

    # ── 状态读写 ────────────────────────────────────────
    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "next_id": 1,
            "next_token_id": 1,
            "channels": [],
            "deleted": [],
            "tokens": [],
        }

    def _load(self) -> dict[str, Any]:
        try:
            raw = _read_json(self.settings.state_path)
        except FileNotFoundError:
            return self._empty_state()
        except json.JSONDecodeError as exc:
            raise HubError("state_broken", f"state.json 解析失败：{exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("channels"), list):
            raise HubError("state_broken", "state.json 结构不合法")
        raw.setdefault("version", STATE_VERSION)
        raw.setdefault("deleted", [])
        raw.setdefault("tokens", [])
        raw.setdefault(
            "next_id",
            max((int(c.get("id", 0)) for c in raw["channels"]), default=0) + 1,
        )
        raw.setdefault(
            "next_token_id",
            max((int(t.get("id", 0)) for t in raw["tokens"]), default=0) + 1,
        )
        return raw

    def _save(self, state: dict[str, Any]) -> None:
        state["version"] = STATE_VERSION
        state["deleted"] = state.get("deleted", [])[-MAX_DELETED_KEPT:]
        _atomic_write(
            self.settings.state_path,
            json.dumps(state, ensure_ascii=False, indent=2),
        )

    # ── 渠道 ────────────────────────────────────────────
    def channels(self, include_deleted: bool = False) -> list[Channel]:
        with self._lock:
            state = self._load()
            items = [Channel.from_dict(raw) for raw in state["channels"]]
            if include_deleted:
                items += [Channel.from_dict(raw) for raw in state.get("deleted", [])]
            return items

    def get(self, channel_id: int) -> Channel:
        for channel in self.channels():
            if channel.id == channel_id:
                return channel
        raise HubError("not_found", f"渠道 id={channel_id} 不存在")

    def insert(self, channel: Channel) -> Channel:
        with self._exclusive():
            state = self._load()
            if any(
                str(raw["name"]).casefold() == channel.name.casefold()
                for raw in state["channels"]
            ):
                raise HubError("invalid_argument", f"渠道名称已存在：{channel.name}")
            channel.id = int(state["next_id"])
            state["next_id"] = channel.id + 1
            state["channels"].append(channel.to_dict())
            self._save(state)
            return channel

    def replace(self, channel: Channel) -> Channel:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["channels"]):
                if int(raw["id"]) != channel.id:
                    continue
                if any(
                    str(other["name"]).casefold() == channel.name.casefold()
                    and int(other["id"]) != channel.id
                    for other in state["channels"]
                ):
                    raise HubError("invalid_argument", f"渠道名称已存在：{channel.name}")
                channel.updated_at = now_iso()
                state["channels"][index] = channel.to_dict()
                self._save(state)
                return channel
            raise HubError("not_found", f"渠道 id={channel.id} 不存在")

    def soft_delete(self, channel_id: int) -> Channel:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["channels"]):
                if int(raw["id"]) != channel_id:
                    continue
                channel = Channel.from_dict(raw)
                del state["channels"][index]
                # 软删除直接留完整对象（含凭据），恢复时不需要再找别的文件
                state["deleted"].append({**channel.to_dict(), "deleted_at": now_iso()})
                self._save(state)
                return channel
            raise HubError("not_found", f"渠道 id={channel_id} 不存在")

    # ── API Token ───────────────────────────────────────
    def tokens(self) -> list[Token]:
        with self._lock:
            return [Token.from_dict(raw) for raw in self._load()["tokens"]]

    def get_token(self, token_id: int) -> Token:
        for token in self.tokens():
            if token.id == token_id:
                return token
        raise HubError("not_found", f"token id={token_id} 不存在")

    def create_token(self, name: str, enabled: bool = True) -> Token:
        with self._exclusive():
            state = self._load()
            if any(str(raw["name"]).casefold() == name.casefold() for raw in state["tokens"]):
                raise HubError("invalid_argument", f"token 名称已存在：{name}")
            token = Token(
                id=int(state["next_token_id"]),
                name=name,
                secret=new_secret(),
                enabled=enabled,
            )
            state["next_token_id"] = token.id + 1
            state["tokens"].append(token.to_dict())
            self._save(state)
            return token

    def replace_token(self, token: Token) -> Token:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["tokens"]):
                if int(raw["id"]) != token.id:
                    continue
                if any(
                    str(other["name"]).casefold() == token.name.casefold()
                    and int(other["id"]) != token.id
                    for other in state["tokens"]
                ):
                    raise HubError("invalid_argument", f"token 名称已存在：{token.name}")
                state["tokens"][index] = token.to_dict()
                self._save(state)
                return token
            raise HubError("not_found", f"token id={token.id} 不存在")

    def delete_token(self, token_id: int) -> Token:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["tokens"]):
                if int(raw["id"]) != token_id:
                    continue
                token = Token.from_dict(raw)
                del state["tokens"][index]
                self._save(state)
                return token
            raise HubError("not_found", f"token id={token_id} 不存在")

    def find_token_by_secret(self, secret: str) -> Token | None:
        """按明文查找启用的 token（认证用）。

        用 secrets.compare_digest 逐个比，避免时序侧信道；token 数量是个位数，
        不需要建哈希索引。
        """
        candidate = (secret or "").strip()
        if not candidate:
            return None
        found: Token | None = None
        for token in self.tokens():
            if secrets.compare_digest(token.secret, candidate):
                found = token  # 不提前 return，让比较次数与位置无关
        if found is None or not found.enabled:
            return None
        return found

    def touch_token(self, token_id: int) -> None:
        """记录 token 使用情况（last_used_at / usage_count）。失败不影响请求。"""
        try:
            with self._exclusive():
                state = self._load()
                for raw in state["tokens"]:
                    if int(raw["id"]) == token_id:
                        raw["last_used_at"] = now_iso()
                        raw["usage_count"] = int(raw.get("usage_count") or 0) + 1
                        self._save(state)
                        return
        except Exception:  # pragma: no cover - 统计失败不该影响发送
            pass

    # ── v1 → v2 迁移 ────────────────────────────────────
    def _import_legacy_token(self) -> None:
        """把旧的 MSG_HUB_TOKEN 导成一把 API Token（一次性）。

        v1 时代 MSG_HUB_TOKEN 既是 Basic 密码也是 Bearer 令牌。v2 拆开后：
        管理员密码走 MSG_HUB_ADMIN_PASSWORD，调用面走 state.json 的 tokens[].
        为了不让现有 MCP 客户端（拿旧 token 调）突然断掉，首次启动时把旧值
        导入成名为「旧版 MSG_HUB_TOKEN」的 token。导入后旧变量不再参与鉴权。
        """
        legacy = (self.settings.legacy_token or "").strip()
        if not legacy:
            return
        state = self._load()
        if state["tokens"]:
            return  # 已经有 token 了，不重复导入
        if any(str(raw.get("secret")) == legacy for raw in state["tokens"]):
            return
        token = Token(
            id=int(state["next_token_id"]),
            name="旧版 MSG_HUB_TOKEN（已迁移，可删除）",
            secret=legacy,
        )
        state["next_token_id"] = token.id + 1
        state["tokens"].append(token.to_dict())
        self._save(state)

    def _migrate_legacy(self) -> None:
        """把 v1 的 channels.json + config/<id>.json 合并成 state.json。

        v1 布局：
            channels.json          {version:1, next_id, channels:[元信息], deleted:[]}
            config/<id>.json       {settings:{…}, options:{…}}

        迁移后：
            state.json             渠道内联 settings/options
            config.migrated-<ts>/  旧的 config 目录整体改名保留（可回滚，不删）
            channels.json.v1.bak   旧索引备份
        """
        legacy_index = self.settings.data_dir / "channels.json"
        if self.settings.state_path.exists() or not legacy_index.exists():
            return
        try:
            raw = _read_json(legacy_index)
        except (OSError, json.JSONDecodeError):
            return  # 坏文件不阻断启动，交给后续正常流程重建空状态
        if not isinstance(raw, dict) or not isinstance(raw.get("channels"), list):
            return

        config_dir = self.settings.data_dir / "config"
        state = self._empty_state()
        state["next_id"] = int(raw.get("next_id") or 1)

        for item in raw["channels"]:
            if not isinstance(item, dict):
                continue
            merged = dict(item)
            path = config_dir / f"{item.get('id')}.json"
            if path.exists():
                try:
                    config = _read_json(path)
                except (OSError, json.JSONDecodeError):
                    config = {}
                if isinstance(config, dict):
                    if "settings" in config or "options" in config:
                        merged["settings"] = dict(config.get("settings") or {})
                        merged["options"] = dict(config.get("options") or {})
                    else:
                        merged["settings"] = dict(config)
                        merged["options"] = {}
            state["channels"].append(merged)

        for item in raw.get("deleted") or []:
            if not isinstance(item, dict):
                continue
            merged = dict(item)
            stamp = str(merged.get("deleted_at") or "")[:19].replace(":", "").replace("-", "")
            path = config_dir / f"{merged.get('id')}.json"
            if path.exists():
                try:
                    config = _read_json(path)
                except (OSError, json.JSONDecodeError):
                    config = {}
                if isinstance(config, dict):
                    merged["settings"] = dict(config.get("settings") or {})
                    merged["options"] = dict(config.get("options") or {})
            state["deleted"].append(merged)
            if stamp:  # 保留原有删除时间
                merged["deleted_at"] = merged.get("deleted_at")

        self._save(state)

        # 旧文件整体留档，不删——迁移出错可以人工回滚
        backup = self.settings.data_dir / "channels.json.v1.bak"
        try:
            if not backup.exists():
                legacy_index.replace(backup)
        except OSError:  # pragma: no cover
            pass
        if config_dir.exists():
            stamp = re.sub(r"[^0-9]", "", now_iso())[:14]
            target = self.settings.data_dir / f"config.migrated-{stamp}"
            try:
                config_dir.rename(target)
            except OSError:  # pragma: no cover
                pass

    # ── 数据目录说明 ────────────────────────────────────
    def _write_readme(self) -> None:
        readme = self.settings.data_dir / "README.md"
        if readme.exists():
            return
        _atomic_write(
            readme,
            "# msg-hub 数据目录\n\n"
            "- `state.json`：全部持久化状态（渠道 + 凭据 + 护栏 + API Token），权限 0600\n"
            "- `logs/`：发送审计日志（按天 jsonl）\n"
            "- `.cache/`：插件可用的缓存目录（如 access_token）\n"
            "- `.lock`：状态写入的跨进程锁文件，不要删\n"
            "- `config.migrated-*`：从旧版本（v1）迁移时留档的配置目录，可删\n"
            "- `channels.json.v1.bak`：迁移前的旧索引备份，可删\n",
            mode=0o644,
        )
