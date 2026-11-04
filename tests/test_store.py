from __future__ import annotations

import json
import os
import stat

import pytest

from msg_hub.models import ChannelConfig, HubError
from msg_hub.store import Store


def test_insert_and_get(cfg):
    store = Store(cfg)
    channel = store.insert("工作群1", "wecom_bot")
    assert channel.id == 1
    assert store.get(1).name == "工作群1"
    assert [c.name for c in store.channels()] == ["工作群1"]


def test_duplicate_name_rejected(cfg):
    store = Store(cfg)
    store.insert("工作群1", "wecom_bot")
    with pytest.raises(HubError) as exc:
        store.insert("工作群1", "email")
    assert exc.value.code == "invalid_argument"


def test_id_never_reused(cfg):
    store = Store(cfg)
    first = store.insert("a", "wecom_bot")
    store.insert("b", "email")
    store.soft_delete(first.id)
    third = store.insert("c", "wecom_bot")
    assert third.id == 3


def test_index_is_atomic_and_private(cfg):
    store = Store(cfg)
    store.insert("工作群1", "wecom_bot")
    mode = stat.S_IMODE(os.stat(cfg.index_path).st_mode)
    assert mode == 0o600
    assert json.loads(cfg.index_path.read_text())["next_id"] == 2
    # 不留临时文件
    assert not [p for p in cfg.data_dir.iterdir() if p.is_file() and p.name.endswith(".tmp")]


def test_soft_delete_moves_config_to_trash(cfg):
    store = Store(cfg)
    channel = store.insert("工作群1", "wecom_bot")
    store.write_config(channel.id, ChannelConfig(settings={"webhook_key": "k"}, options={}))
    store.soft_delete(channel.id)

    assert store.channels() == []
    assert len(store.channels(include_deleted=True)) == 1
    assert not store.config_path(channel.id).exists()
    assert len(list(cfg.trash_dir.glob("*.json"))) == 1


def test_corrupt_index_raises(cfg):
    store = Store(cfg)
    cfg.index_path.write_text("{not json")
    with pytest.raises(HubError) as exc:
        store.channels()
    assert exc.value.code == "channel_broken"


def test_legacy_flat_config_is_accepted(cfg):
    store = Store(cfg)
    channel = store.insert("邮箱", "email")
    store.config_path(channel.id).write_text(json.dumps({"smtp_host": "smtp.qq.com", "username": "a@b.c"}))
    config = store.read_config(channel.id)
    assert config is not None
    assert config.settings["smtp_host"] == "smtp.qq.com"
    assert config.options == {}
