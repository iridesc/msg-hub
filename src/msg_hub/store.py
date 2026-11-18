"""单一状态文件的持久化：渠道 + 入口 + 路由规则全在一个 `state.json` 里。

**名称就是唯一标识**（没有 id）：`resolve` 只按名称寻址，创建时全局唯一。
好处是接口、日志、规则条件里只有一套标识，不会出现「传 id 还是传名字」的歧义；
代价是改名等于换实体——引用了旧名称的路由规则会失效，所以名称要稳定。

布局（version 4）：

    {
      "version": 4,
      "channels": [ {name, type, enabled, settings, options, tags, …} ],
      "entries":  [ {name, secret, tags, enabled, …} ],
      "rules":    [ {name, enabled, entry, outbound, …} ],
      "deleted":  [ 出口软删除留档 ]
    }

磁盘上的其他文件：
    <data>/logs/        发送审计日志
    <data>/.cache/      插件可用的缓存目录
    <data>/.lock        跨进程锁

所有写入都是「同目录临时文件 + os.replace」原子替换。
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
    ENTRY_BYTES,
    ENTRY_PREFIX,
    Channel,
    Entry,
    HubError,
    Rule,
    normalize_tags,
    now_iso,
)
from .settings import Settings

try:  # POSIX 文件锁；非 POSIX 平台降级为纯线程锁
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

STATE_VERSION = 4
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
    return ENTRY_PREFIX + secrets.token_hex(ENTRY_BYTES)


def _same(a: Any, b: Any) -> bool:
    """名称比较：大小写不敏感（避免 Test / test 两把并存）。"""
    return str(a).casefold() == str(b).casefold()


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
        self._persist_upgrade()
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
            "channels": [],
            "entries": [],
            "rules": [],
            "deleted": [],
        }

    def _upgrade(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把任意旧版本的状态就地升级成当前结构（不改磁盘，由 _save 落盘）。"""
        version = int(raw.get("version") or 1)

        # v2 → v3：tokens 改名 entries 并补 tags；渠道补 tags；新增 rules
        if version < 3:
            legacy_tokens = raw.get("tokens") or []
            raw["entries"] = [
                {**item, "tags": normalize_tags(item.get("tags"))}
                for item in legacy_tokens
                if isinstance(item, dict)
            ]
            raw.pop("tokens", None)
            raw["rules"] = raw.get("rules") or []
            for bucket in ("channels", "deleted"):
                for item in raw.get(bucket) or []:
                    if isinstance(item, dict):
                        item["tags"] = normalize_tags(item.get("tags"))
            version = 3

        # v3 → v4：去掉 id，名称成为唯一标识
        if version < 4:
            for bucket in ("channels", "entries", "rules", "deleted"):
                for item in raw.get(bucket) or []:
                    if isinstance(item, dict):
                        item.pop("id", None)
            for key in (
                "next_id",
                "next_entry_id",
                "next_rule_id",
                "next_token_id",
            ):
                raw.pop(key, None)
            version = 4

        raw["version"] = version
        raw.setdefault("deleted", [])
        raw.setdefault("entries", [])
        raw.setdefault("rules", [])
        return raw

    def _load(self) -> dict[str, Any]:
        try:
            raw = _read_json(self.settings.state_path)
        except FileNotFoundError:
            return self._empty_state()
        except json.JSONDecodeError as exc:
            raise HubError("state_broken", f"state.json 解析失败：{exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("channels"), list):
            raise HubError("state_broken", "state.json 结构不合法")
        return self._upgrade(raw)

    def _save(self, state: dict[str, Any]) -> None:
        state["version"] = STATE_VERSION
        state["deleted"] = state.get("deleted", [])[-MAX_DELETED_KEPT:]
        _atomic_write(
            self.settings.state_path,
            json.dumps(state, ensure_ascii=False, indent=2),
        )

    def _persist_upgrade(self) -> None:
        """启动时把内存里的升级结果落盘一次，让文件本身也是新格式。"""
        try:
            with self._exclusive():
                try:
                    disk = _read_json(self.settings.state_path)
                except (OSError, json.JSONDecodeError):
                    disk = None
                if not isinstance(disk, dict):
                    return
                stale = (
                    int(disk.get("version") or 0) != STATE_VERSION
                    or "tokens" in disk
                    or any(k.startswith("next_") for k in disk)
                    or any("id" in item for item in disk.get("channels") or [] if isinstance(item, dict))
                )
                if stale:
                    self._save(self._load())
        except HubError:
            raise
        except Exception:  # pragma: no cover - 落盘失败不该阻断启动
            pass

    # ── 渠道 ────────────────────────────────────────────
    def channels(self, include_deleted: bool = False) -> list[Channel]:
        with self._lock:
            state = self._load()
            items = [Channel.from_dict(raw) for raw in state["channels"]]
            if include_deleted:
                items += [Channel.from_dict(raw) for raw in state.get("deleted", [])]
            return items

    def get(self, name: str) -> Channel:
        for channel in self.channels():
            if channel.name == name:
                return channel
        raise HubError("not_found", f"出口 {name} 不存在")

    def insert(self, channel: Channel) -> Channel:
        with self._exclusive():
            state = self._load()
            if any(_same(raw["name"], channel.name) for raw in state["channels"]):
                raise HubError("invalid_argument", f"出口名称已存在：{channel.name}")
            state["channels"].append(channel.to_dict())
            self._save(state)
            return channel

    def replace(self, old_name: str, channel: Channel) -> Channel:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["channels"]):
                if raw["name"] != old_name:
                    continue
                if any(
                    _same(other["name"], channel.name) and other["name"] != old_name
                    for other in state["channels"]
                ):
                    raise HubError("invalid_argument", f"出口名称已存在：{channel.name}")
                channel.updated_at = now_iso()
                state["channels"][index] = channel.to_dict()
                self._save(state)
                return channel
            raise HubError("not_found", f"出口 {old_name} 不存在")

    def soft_delete(self, name: str) -> Channel:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["channels"]):
                if raw["name"] != name:
                    continue
                channel = Channel.from_dict(raw)
                del state["channels"][index]
                # 软删除直接留完整对象（含凭据），恢复时不需要再找别的文件
                state["deleted"].append({**channel.to_dict(), "deleted_at": now_iso()})
                self._save(state)
                return channel
            raise HubError("not_found", f"出口 {name} 不存在")

    # ── 入口 ────────────────────────────────────────────
    def entries(self) -> list[Entry]:
        with self._lock:
            return [Entry.from_dict(raw) for raw in self._load()["entries"]]

    def get_entry(self, name: str) -> Entry:
        for entry in self.entries():
            if entry.name == name:
                return entry
        raise HubError("not_found", f"入口 {name} 不存在")

    def insert_entry(self, name: str, tags: list[str] | None = None, enabled: bool = True) -> Entry:
        with self._exclusive():
            state = self._load()
            if any(_same(raw["name"], name) for raw in state["entries"]):
                raise HubError("invalid_argument", f"入口名称已存在：{name}")
            entry = Entry(
                name=name,
                secret=new_secret(),
                tags=normalize_tags(tags),
                enabled=enabled,
            )
            state["entries"].append(entry.to_dict())
            self._save(state)
            return entry

    def replace_entry(self, old_name: str, entry: Entry) -> Entry:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["entries"]):
                if raw["name"] != old_name:
                    continue
                if any(
                    _same(other["name"], entry.name) and other["name"] != old_name
                    for other in state["entries"]
                ):
                    raise HubError("invalid_argument", f"入口名称已存在：{entry.name}")
                state["entries"][index] = entry.to_dict()
                self._save(state)
                return entry
            raise HubError("not_found", f"入口 {old_name} 不存在")

    def delete_entry(self, name: str) -> Entry:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["entries"]):
                if raw["name"] != name:
                    continue
                entry = Entry.from_dict(raw)
                del state["entries"][index]
                self._save(state)
                return entry
            raise HubError("not_found", f"入口 {name} 不存在")

    def find_entry_by_secret(self, secret: str) -> Entry | None:
        """按明文查找启用的入口（认证用）。

        用 secrets.compare_digest 逐个比，避免时序侧信道；入口数量是个位数，
        不需要建哈希索引。
        """
        candidate = (secret or "").strip()
        if not candidate:
            return None
        found: Entry | None = None
        for entry in self.entries():
            if secrets.compare_digest(entry.secret, candidate):
                found = entry  # 不提前 return，让比较次数与位置无关
        if found is None or not found.enabled:
            return None
        return found

    def touch_entry(self, name: str) -> None:
        """记录入口使用情况（last_used_at / usage_count）。失败不影响请求。"""
        try:
            with self._exclusive():
                state = self._load()
                for raw in state["entries"]:
                    if raw["name"] == name:
                        raw["last_used_at"] = now_iso()
                        raw["usage_count"] = int(raw.get("usage_count") or 0) + 1
                        self._save(state)
                        return
        except Exception:  # pragma: no cover - 统计失败不该影响发送
            pass

    # ── 路由规则 ────────────────────────────────────────
    def rules(self) -> list[Rule]:
        with self._lock:
            return [Rule.from_dict(raw) for raw in self._load()["rules"]]

    def get_rule(self, name: str) -> Rule:
        for rule in self.rules():
            if rule.name == name:
                return rule
        raise HubError("not_found", f"规则 {name} 不存在")

    def insert_rule(self, rule: Rule) -> Rule:
        with self._exclusive():
            state = self._load()
            if any(_same(raw["name"], rule.name) for raw in state["rules"]):
                raise HubError("invalid_argument", f"规则名称已存在：{rule.name}")
            state["rules"].append(rule.to_dict())
            self._save(state)
            return rule

    def replace_rule(self, old_name: str, rule: Rule) -> Rule:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["rules"]):
                if raw["name"] != old_name:
                    continue
                if any(
                    _same(other["name"], rule.name) and other["name"] != old_name
                    for other in state["rules"]
                ):
                    raise HubError("invalid_argument", f"规则名称已存在：{rule.name}")
                rule.updated_at = now_iso()
                state["rules"][index] = rule.to_dict()
                self._save(state)
                return rule
            raise HubError("not_found", f"规则 {old_name} 不存在")

    def delete_rule(self, name: str) -> Rule:
        with self._exclusive():
            state = self._load()
            for index, raw in enumerate(state["rules"]):
                if raw["name"] != name:
                    continue
                rule = Rule.from_dict(raw)
                del state["rules"][index]
                self._save(state)
                return rule
            raise HubError("not_found", f"规则 {name} 不存在")

    # ── 旧版兼容 ────────────────────────────────────────
    def _import_legacy_token(self) -> None:
        """把旧的 MSG_HUB_TOKEN 导成一个入口（一次性）。

        v1 时代 MSG_HUB_TOKEN 既是 Basic 密码也是 Bearer 令牌。v2 拆开后：
        管理员密码走 MSG_HUB_ADMIN_PASSWORD，调用面走 state.json 的 entries[]。
        为了不让现有 MCP 客户端（拿旧 token 调）突然断掉，首次启动时把旧值
        导入成名为「旧版 MSG_HUB_TOKEN」的入口。导入后旧变量不再参与鉴权。
        """
        legacy = (self.settings.legacy_token or "").strip()
        if not legacy:
            return
        state = self._load()
        if state["entries"]:
            return  # 已经有入口了，不重复导入
        entry = Entry(
            name="旧版 MSG_HUB_TOKEN（已迁移，可删除）",
            secret=legacy,
            # 旧 token 能发所有渠道，用通配标签标记，但**不会自动生成规则**：
            # 路由必须由管理员显式配置，否则一条消息会广播到所有渠道。
            tags=["legacy"],
        )
        state["entries"].append(entry.to_dict())
        self._save(state)

    def _migrate_legacy(self) -> None:
        """把 v1 的 channels.json + config/<id>.json 合并成 state.json。

        v1 布局：
            channels.json          {version:1, next_id, channels:[元信息], deleted:[]}
            config/<id>.json       {settings:{…}, options:{…}}

        迁移后：
            state.json             渠道内联 settings/options/tags
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
        # 标成 v1：让 _upgrade 把整条迁移链（v1→v3→v4）都跑一遍，
        # 否则会带着 v4 的版本号跳过「剥 id」那一步。
        state["version"] = 1

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

        self._save(self._upgrade(state))

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
            "- `state.json`：全部持久化状态（渠道 + 入口 + 路由规则 + 凭据），权限 0600\n"
            "  **名称就是唯一标识，没有 id**：创建时全局唯一，改名等于换实体\n"
            "- `logs/`：发送审计日志（按天 jsonl）\n"
            "- `.cache/`：插件可用的缓存目录（如 access_token）\n"
            "- `.lock`：状态写入的跨进程锁文件，不要删\n"
            "- `config.migrated-*`：从旧版本（v1）迁移时留档的配置目录，可删\n"
            "- `channels.json.v1.bak`：迁移前的旧索引备份，可删\n",
            mode=0o644,
        )
