"""单一状态文件（state.json）的持久化测试：渠道 + token + 迁移。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from msg_hub.models import Channel, HubError
from msg_hub.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def _channel(name="工作群1", **kwargs) -> Channel:
    return Channel(id=0, name=name, type=kwargs.pop("type", "wecom_bot"), **kwargs)


# ── 渠道 ──────────────────────────────────────────────────
def test_insert_and_get(cfg):
    store = Store(cfg)
    channel = store.insert(_channel(settings={"webhook_key": "k1"}))
    assert channel.id == 1
    assert store.get(1).name == "工作群1"
    assert store.get(1).settings == {"webhook_key": "k1"}
    assert [c.name for c in store.channels()] == ["工作群1"]


def test_settings_and_options_round_trip(cfg):
    store = Store(cfg)
    store.insert(
        _channel(
            name="群",
            settings={"webhook_key": "k1", "timeout": 10},
            options={"allow_mention_all": True, "rate_limit_per_min": 5},
        )
    )
    channel = store.get(1)
    assert channel.settings["timeout"] == 10
    assert channel.allow_mention_all is True
    assert channel.rate_limit_per_min == 5


def test_duplicate_name_rejected(cfg):
    store = Store(cfg)
    store.insert(_channel())
    with pytest.raises(HubError) as exc:
        store.insert(_channel(type="email"))
    assert exc.value.code == "invalid_argument"


def test_id_never_reused(cfg):
    store = Store(cfg)
    first = store.insert(_channel("a"))
    store.insert(_channel("b"))
    store.soft_delete(first.id)
    third = store.insert(_channel("c"))
    assert third.id == 3


def test_state_file_is_private_and_atomic(cfg):
    store = Store(cfg)
    store.insert(_channel())
    mode = stat.S_IMODE(os.stat(cfg.state_path).st_mode)
    assert mode == 0o600
    assert json.loads(cfg.state_path.read_text())["next_id"] == 2
    # 不留临时文件
    assert not [p for p in cfg.data_dir.iterdir() if p.is_file() and p.name.endswith(".tmp")]


def test_soft_delete_keeps_full_config(cfg):
    """软删除直接把完整配置留在 deleted[] 里，不再有 config/.trash/ 目录。"""
    store = Store(cfg)
    channel = store.insert(_channel(settings={"webhook_key": "super-secret"}))
    store.soft_delete(channel.id)

    assert store.channels() == []
    deleted = store.channels(include_deleted=True)
    assert len(deleted) == 1
    assert deleted[0].settings == {"webhook_key": "super-secret"}
    raw = json.loads(cfg.state_path.read_text())
    assert raw["deleted"][0]["deleted_at"]
    assert not (cfg.data_dir / "config").exists()


def test_no_config_directory_is_created(cfg):
    """v2 不再有 config/ 目录 —— 配置就在 state.json 里。"""
    store = Store(cfg)
    store.insert(_channel(settings={"webhook_key": "k"}))
    assert not (cfg.data_dir / "config").exists()


def test_corrupt_state_raises(cfg):
    store = Store(cfg)
    cfg.state_path.write_text("{not json")
    with pytest.raises(HubError) as exc:
        store.channels()
    assert exc.value.code == "state_broken"


# ── Token ─────────────────────────────────────────────────
def test_token_lifecycle(cfg):
    store = Store(cfg)
    token = store.insert_entry("监控平台")
    assert token.id == 1
    assert token.secret.startswith("mh_")
    assert len(token.secret) == len("mh_") + 32
    assert token.prefix == token.secret[:9]

    assert store.entries()[0].name == "监控平台"
    assert store.find_entry_by_secret(token.secret) is not None

    store.replace_entry(type(token)(**{**token.to_dict(), "enabled": False}))
    assert store.find_entry_by_secret(token.secret) is None  # 禁用即失效

    store.delete_entry(token.id)
    assert store.entries() == []


def test_token_plaintext_is_stored(cfg):
    """明文存储（用户明确选择）：文件里能看到 secret 原值。"""
    store = Store(cfg)
    token = store.insert_entry("监控平台")
    raw = json.loads(cfg.state_path.read_text())
    assert raw["entries"][0]["secret"] == token.secret


def test_token_public_view_hides_secret(cfg):
    store = Store(cfg)
    token = store.insert_entry("监控平台")
    public = token.to_public()
    assert "secret" not in public
    assert public["prefix"] == token.prefix
    assert public["masked"] == f"***{token.secret[-4:]}"


def test_token_names_are_unique(cfg):
    store = Store(cfg)
    store.insert_entry("监控平台")
    with pytest.raises(HubError):
        store.insert_entry("监控平台")


def test_touch_entry_tracks_usage(cfg):
    store = Store(cfg)
    token = store.insert_entry("监控平台")
    store.touch_entry(token.id)
    store.touch_entry(token.id)
    updated = store.get_entry(token.id)
    assert updated.usage_count == 2
    assert updated.last_used_at is not None


def test_disabled_token_not_found_by_secret(cfg):
    store = Store(cfg)
    token = store.insert_entry("停用的", enabled=False)
    assert store.find_entry_by_secret(token.secret) is None


def test_find_entry_by_secret_ignores_unknown(cfg):
    store = Store(cfg)
    store.insert_entry("监控平台")
    assert store.find_entry_by_secret("mh_deadbeef") is None
    assert store.find_entry_by_secret("") is None


# ── v1 → v2 迁移 ──────────────────────────────────────────
def test_migrates_v1_layout(cfg):
    """v1 的 channels.json + config/<id>.json 自动合并成 state.json。"""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "next_id": 3,
                "channels": [
                    {"id": 1, "name": "工作群1", "type": "wecom_bot", "enabled": True},
                    {"id": 2, "name": "邮箱", "type": "email", "enabled": False},
                ],
                "deleted": [{"id": 9, "name": "旧的", "type": "wecom_bot"}],
            }
        ),
        encoding="utf-8",
    )
    config_dir = cfg.data_dir / "config"
    config_dir.mkdir()
    (config_dir / "1.json").write_text(
        json.dumps({"settings": {"webhook_key": "legacy-key"}, "options": {"rate_limit_per_min": 7}}),
        encoding="utf-8",
    )
    (config_dir / "2.json").write_text(
        json.dumps({"smtp_host": "smtp.qq.com"}),  # 旧式「配置直接写在顶层」
        encoding="utf-8",
    )

    store = Store(cfg)

    channels = {c.id: c for c in store.channels()}
    assert channels[1].settings == {"webhook_key": "legacy-key"}
    assert channels[1].rate_limit_per_min == 7
    assert channels[2].settings == {"smtp_host": "smtp.qq.com"}
    assert channels[2].enabled is False
    # 软删除也迁过来
    assert [c.name for c in store.channels(include_deleted=True) if c.id == 9] == ["旧的"]
    # 旧文件留档，不删
    assert (cfg.data_dir / "channels.json.v1.bak").exists()
    assert list(cfg.data_dir.glob("config.migrated-*"))


def test_migration_is_idempotent(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "channels.json").write_text(
        json.dumps({"version": 1, "next_id": 2, "channels": [{"id": 1, "name": "a", "type": "wecom_bot"}]}),
        encoding="utf-8",
    )
    Store(cfg)
    first = json.loads(cfg.state_path.read_text())
    Store(cfg)  # 再启动一次
    second = json.loads(cfg.state_path.read_text())
    assert len(second["channels"]) == 1
    assert first == second


def test_legacy_entry_is_imported_once(cfg):
    """旧的 MSG_HUB_TOKEN 自动变成一把 token，现有 MCP 客户端不会断。"""
    import dataclasses

    settings = dataclasses.replace(cfg, legacy_token="mh_legacy_token_value")
    store = Store(settings)
    tokens = store.entries()
    assert len(tokens) == 1
    assert tokens[0].secret == "mh_legacy_token_value"
    assert store.find_entry_by_secret("mh_legacy_token_value") is not None

    # 再启动一次不重复导入
    Store(settings)
    assert len(Store(settings).entries()) == 1


# ── 并发 ──────────────────────────────────────────────────
def test_concurrent_processes_do_not_lose_channels(cfg):
    """两个进程同时写同一个数据目录，不能丢更新。

    stdio 与 HTTP 两种形态可能指向同一个数据目录，所以状态写入必须有跨进程锁。
    """
    script = textwrap.dedent(
        """
        import dataclasses, sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from msg_hub.settings import Settings
        from msg_hub.store import Store
        from msg_hub.models import Channel

        settings = dataclasses.replace(
            Settings(), data_dir=Path(sys.argv[2]), types_dir=Path(sys.argv[3])
        )
        store = Store(settings)
        for index in range(15):
            store.insert(Channel(id=0, name=f"{sys.argv[4]}-{index}", type="wecom_bot"))
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(SRC_DIR), str(cfg.data_dir), str(cfg.types_dir), tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for tag in ("a", "b")
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"{out}\n{err}"

    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    names = {channel["name"] for channel in state["channels"]}
    assert len(names) == 30
    assert {channel["id"] for channel in state["channels"]} == set(range(1, 31))


# ── v2 → v3 迁移 ──────────────────────────────────────────
def test_v2_state_is_upgraded_to_v3(cfg):
    """v2 的 tokens 要变成 entries，并获得空标签；渠道补 tags；新增 rules。"""
    legacy = {
        "version": 2,
        "next_id": 3,
        "next_token_id": 5,
        "channels": [
            {
                "id": 1,
                "name": "工作群1",
                "type": "wecom_bot",
                "enabled": True,
                "settings": {"webhook_key": "k" * 36},
                "options": {},
                "created_at": "2026-01-01T00:00:00+08:00",
                "updated_at": "2026-01-01T00:00:00+08:00",
            }
        ],
        "deleted": [],
        "tokens": [
            {
                "id": 4,
                "name": "旧 token",
                "secret": "mh_0123456789abcdef0123456789abcdef",
                "enabled": True,
                "created_at": "2026-01-01T00:00:00+08:00",
                "last_used_at": None,
                "usage_count": 7,
            }
        ],
    }
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_path.write_text(json.dumps(legacy), encoding="utf-8")

    store = Store(cfg)
    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))

    assert state["version"] == 3
    assert "tokens" not in state
    assert state["entries"][0]["name"] == "旧 token"
    assert state["entries"][0]["tags"] == []
    assert state["entries"][0]["usage_count"] == 7
    # id 游标要接着老值走，不能重号
    assert state["next_entry_id"] == 5
    assert state["rules"] == []
    assert state["channels"][0]["tags"] == []

    # 迁移后旧 token 仍然能认证
    assert store.find_entry_by_secret("mh_0123456789abcdef0123456789abcdef") is not None


def test_v2_migration_does_not_create_rules(cfg):
    """迁移不会自动生成规则——否则一条消息会广播到所有渠道。"""
    legacy = {
        "version": 2,
        "next_id": 2,
        "next_token_id": 2,
        "channels": [
            {
                "id": 1,
                "name": "群",
                "type": "wecom_bot",
                "settings": {"webhook_key": "k" * 36},
            }
        ],
        "tokens": [{"id": 1, "name": "t", "secret": "mh_" + "a" * 32}],
    }
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_path.write_text(json.dumps(legacy), encoding="utf-8")
    Store(cfg)
    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert state["rules"] == []
