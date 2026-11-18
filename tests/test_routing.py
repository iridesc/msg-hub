"""路由引擎：条件匹配 + 规则累加 + 扇出聚合。

这一层是整个 v3 的核心，所以按「单个条件 / 规则组合 / 发送聚合」三段覆盖。
"""

from __future__ import annotations

import json

import pytest

from msg_hub import routing
from msg_hub.auth import Identity, reset_identity, set_identity
from msg_hub.models import Channel, Condition, Entry, HubError, Rule
from msg_hub.service import Hub


# ── 条件匹配 ──────────────────────────────────────────────
def _cond(field_name: str, op: str, value: str, side: str = "tag") -> Condition:
    return Condition.from_dict(
        {"field": field_name, "op": op, "value": value},
        side="entry" if side == "entry" else "outbound",
    )


def test_tag_matching_operators():
    tags = ["app:pibox", "env:test", "kind:alert"]
    assert _cond("tag", "exact", "app:pibox").matches(tags)
    assert _cond("tag", "exact", "app:PIbox").matches(tags)  # 大小写不敏感
    assert not _cond("tag", "exact", "app:") .matches(tags)
    assert _cond("tag", "prefix", "app:").matches(tags)
    assert not _cond("tag", "prefix", "env:").matches(["app:pibox"])
    assert _cond("tag", "suffix", ":test").matches(tags)
    assert not _cond("tag", "suffix", ":prod").matches(tags)
    assert _cond("tag", "contains", "alert").matches(tags)
    assert not _cond("tag", "contains", "nope").matches(tags)


def test_condition_rejects_bad_field_and_op():
    with pytest.raises(HubError) as bad_field:
        Condition.from_dict({"field": "domain", "op": "exact", "value": "x"}, side="entry")
    assert bad_field.value.code == "invalid_argument"

    with pytest.raises(HubError) as bad_op:
        Condition.from_dict({"field": "tag", "op": "regex", "value": "x"}, side="outbound")
    assert bad_op.value.code == "invalid_argument"

    with pytest.raises(HubError) as empty:
        Condition.from_dict({"field": "tag", "op": "exact", "value": "  "}, side="entry")
    assert empty.value.code == "invalid_argument"


def test_entry_field_does_not_match_tags():
    """field=entry 只看名称/id，不能因为某个标签长得像就命中。"""
    entry = Entry(id=7, name="pibox", secret="mh_x", tags=["app:pibox"])
    assert routing.entry_matches(_cond("entry", "exact", "pibox", "entry"), entry)
    assert routing.entry_matches(_cond("entry", "exact", "7", "entry"), entry)
    assert not routing.entry_matches(_cond("entry", "exact", "app:pibox", "entry"), entry)
    # tag 字段才看标签
    assert routing.entry_matches(_cond("tag", "exact", "app:pibox", "entry"), entry)


def test_outbound_name_and_type_fields():
    channel = Channel(id=1, name="工作群1", type="wecom_bot", tags=["kind:ops"])
    assert routing.outbound_matches(_cond("name", "exact", "工作群1"), channel)
    assert routing.outbound_matches(_cond("type", "exact", "wecom_bot"), channel)
    assert routing.outbound_matches(_cond("tag", "exact", "kind:ops"), channel)
    # type 字段不会命中名称
    assert not routing.outbound_matches(_cond("type", "exact", "工作群1"), channel)


def test_empty_entry_condition_matches_everyone():
    rule = Rule(id=1, name="所有入口", entry=None, outbound=_cond("tag", "exact", "ops"))
    assert routing.entry_matches(rule.entry, Entry(id=1, name="任意", secret="mh_x"))
    assert routing.entry_matches(rule.entry, None)  # 管理员视角也算命中


def test_admin_view_only_matches_conditions_empty():
    """管理员没有入口对象：带入口条件的规则不命中。"""
    entry_cond = _cond("tag", "exact", "app:pibox", "entry")
    assert routing.entry_matches(entry_cond, None) is False
    assert routing.entry_matches(None, None) is True


# ── 规则组合 ──────────────────────────────────────────────
def _channels() -> list[Channel]:
    return [
        Channel(id=1, name="运维群", type="wecom_bot", tags=["kind:alert", "team:ops"]),
        Channel(id=2, name="测试群", type="wecom_bot", tags=["kind:test"]),
        Channel(id=3, name="值班邮箱", type="email", tags=["kind:alert", "team:ops"]),
    ]


def test_rules_accumulate_and_dedupe():
    """命中的多条规则取并集，重复的出口只出现一次。"""
    entry = Entry(id=1, name="pibox", secret="mh_x", tags=["app:pibox"])
    rules = [
        Rule(id=1, name="alert", entry=None, outbound=_cond("tag", "exact", "kind:alert")),
        Rule(id=2, name="ops", entry=None, outbound=_cond("tag", "exact", "team:ops")),
        Rule(id=3, name="email", entry=None, outbound=_cond("type", "exact", "email")),
    ]
    selection = routing.select(rules, entry, _channels())
    # 1 命中三条、3 命中两条、2 不命中
    assert [c.id for c in selection.channels] == [1, 3]
    assert [r["id"] for r in selection.matched_rules] == [1, 2, 3]


def test_disabled_rule_is_skipped():
    entry = Entry(id=1, name="pibox", secret="mh_x")
    rules = [
        Rule(id=1, name="停用", entry=None, enabled=False, outbound=_cond("tag", "exact", "kind:alert")),
    ]
    selection = routing.select(rules, entry, _channels())
    assert selection.channels == []
    assert selection.enabled_rules == 0
    assert selection.total_rules == 1


def test_entry_condition_filters_rules():
    pibox = Entry(id=1, name="pibox", secret="mh_x", tags=["app:pibox"])
    other = Entry(id=2, name="其他", secret="mh_y", tags=["app:other"])
    rules = [
        Rule(
            id=1,
            name="只给 pibox",
            entry=_cond("tag", "exact", "app:pibox", "entry"),
            outbound=_cond("tag", "exact", "kind:alert"),
        ),
        Rule(id=2, name="所有入口", entry=None, outbound=_cond("tag", "exact", "kind:test")),
    ]
    # 规则 1 命中带 kind:alert 的 1 和 3；规则 2 命中带 kind:test 的 2
    assert [c.id for c in routing.select(rules, pibox, _channels()).channels] == [1, 3, 2]
    assert [c.id for c in routing.select(rules, other, _channels()).channels] == [2]


def test_explain_reports_every_rule():
    entry = Entry(id=1, name="pibox", secret="mh_x", tags=["app:pibox"])
    rules = [
        Rule(id=1, name="命中", entry=None, outbound=_cond("tag", "exact", "kind:alert")),
        Rule(
            id=2,
            name="入口不匹配",
            entry=_cond("tag", "exact", "app:other", "entry"),
            outbound=_cond("tag", "exact", "kind:alert"),
        ),
    ]
    trace = routing.explain(rules, entry, _channels())
    assert [item["applied"] for item in trace] == [True, False]
    assert trace[1]["entry_matched"] is False


# ── 发送聚合 ──────────────────────────────────────────────
WECOM_SETTINGS = {"webhook_key": "693a-test-key-abcdefg"}


def _hub_with(cfg, *, channels: list[tuple[str, list[str]]], rules: list[Rule]):
    hub = Hub(cfg)
    for name, tags in channels:
        hub.create_channel(name=name, type_name="wecom_bot", settings=WECOM_SETTINGS, tags=tags)
    for rule in rules:
        hub.create_rule(
            rule.name,
            rule.outbound.to_dict(),
            entry=rule.entry.to_dict() if rule.entry else None,
            enabled=rule.enabled,
        )
    entry = hub.create_entry("pibox", tags=["app:pibox"])
    return hub, entry


def test_fanout_sends_to_every_matched_outbound(cfg):
    hub, entry = _hub_with(
        cfg,
        channels=[("群A", ["kind:alert"]), ("群B", ["kind:alert"]), ("群C", ["kind:other"])],
        rules=[Rule(id=1, name="alert", outbound=_cond("tag", "exact", "kind:alert"))],
    )
    token = set_identity(Identity(role="entry", entry_id=entry["id"], entry_name="pibox"))
    try:
        result = hub.send("告警", dry_run=True)
    finally:
        reset_identity(token)
    assert result["routed"] is True
    assert result["counts"] == {"total": 2, "sent": 2, "skipped": 0, "failed": 0}
    assert {item["channel"]["name"] for item in result["results"]} == {"群A", "群B"}


def test_disabled_outbound_is_skipped_not_failed(cfg):
    hub, entry = _hub_with(
        cfg,
        channels=[("群A", ["kind:alert"]), ("群B", ["kind:alert"])],
        rules=[Rule(id=1, name="alert", outbound=_cond("tag", "exact", "kind:alert"))],
    )
    hub.update_channel("群B", enabled=False)
    token = set_identity(Identity(role="entry", entry_id=entry["id"], entry_name="pibox"))
    try:
        result = hub.send("告警", dry_run=True)
    finally:
        reset_identity(token)
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["counts"] == {"total": 2, "sent": 1, "skipped": 1, "failed": 0}
    skipped = [item for item in result["results"] if item["status"] == "skipped"]
    assert skipped[0]["channel"]["name"] == "群B"
    assert skipped[0]["error"]["code"] == "channel_disabled"


def test_no_route_lists_channels_and_rules(cfg):
    hub, entry = _hub_with(cfg, channels=[("群A", ["kind:alert"])], rules=[])
    token = set_identity(Identity(role="entry", entry_id=entry["id"], entry_name="pibox"))
    try:
        with pytest.raises(HubError) as exc:
            hub.send("告警", dry_run=True)
    finally:
        reset_identity(token)
    assert exc.value.code == "no_route"
    assert exc.value.extra["total_rules"] == 0
    assert exc.value.extra["available_channels"][0]["name"] == "群A"


def test_all_failed_raises_and_reports(cfg):
    hub, entry = _hub_with(
        cfg,
        channels=[("群A", ["kind:alert"]), ("群B", ["kind:alert"])],
        rules=[Rule(id=1, name="alert", outbound=_cond("tag", "exact", "kind:alert"))],
    )
    hub.update_channel("群A", enabled=False)
    hub.update_channel("群B", enabled=False)
    token = set_identity(Identity(role="entry", entry_id=entry["id"], entry_name="pibox"))
    try:
        with pytest.raises(HubError) as exc:
            hub.send("告警")
    finally:
        reset_identity(token)
    assert exc.value.code == "all_failed"
    assert len(exc.value.extra["results"]) == 2


def test_single_outbound_keeps_legacy_top_level_fields(cfg):
    """单出口时仍返回顶层 provider / elapsed_ms，老调用方不用改。"""
    hub, entry = _hub_with(
        cfg,
        channels=[("群A", ["kind:alert"])],
        rules=[Rule(id=1, name="alert", outbound=_cond("tag", "exact", "kind:alert"))],
    )
    token = set_identity(Identity(role="entry", entry_id=entry["id"], entry_name="pibox"))
    try:
        result = hub.send("告警", dry_run=True)
    finally:
        reset_identity(token)
    assert result["channel"]["name"] == "群A"
    assert "provider" in result
    assert "elapsed_ms" in result


def test_audit_log_written_per_outbound_with_same_request_id(cfg):
    hub, entry = _hub_with(
        cfg,
        channels=[("群A", ["kind:alert"]), ("群B", ["kind:alert"])],
        rules=[Rule(id=1, name="alert", outbound=_cond("tag", "exact", "kind:alert"))],
    )
    token = set_identity(Identity(role="entry", entry_id=entry["id"], entry_name="pibox"))
    try:
        result = hub.send("告警", dry_run=True)
    finally:
        reset_identity(token)
    records = hub.logs()
    assert len(records) == 2
    assert {record["request_id"] for record in records} == {result["request_id"]}
    assert {record["outcome"] for record in records} == {"sent"}
    assert {record["actor"]["type"] for record in records} == {"entry"}
    # 日志里只出现入口名，不出现 token 明文
    assert entry["secret"] not in json.dumps(records, ensure_ascii=False)


def test_route_preview_shows_trace(cfg):
    hub, entry = _hub_with(
        cfg,
        channels=[("群A", ["kind:alert"])],
        rules=[Rule(id=1, name="alert", outbound=_cond("tag", "exact", "kind:alert"))],
    )
    preview = hub.route_preview(entry["id"])
    assert [item["name"] for item in preview["outbounds"]] == ["群A"]
    assert preview["trace"][0]["applied"] is True
    assert preview["trace"][0]["outbound_condition"]


def test_rule_and_channel_conditions_are_independent(cfg):
    """入口条件只筛入口、出口条件只筛出口，互不串味。"""
    hub, entry = _hub_with(
        cfg,
        channels=[("群A", ["kind:alert"]), ("群B", ["kind:other"])],
        rules=[
            Rule(
                id=1,
                name="pibox→alert",
                entry=_cond("tag", "prefix", "app:", "entry"),
                outbound=_cond("tag", "suffix", ":alert"),
            )
        ],
    )
    token = set_identity(Identity(role="entry", entry_id=entry["id"], entry_name="pibox"))
    try:
        result = hub.send("x", dry_run=True)
    finally:
        reset_identity(token)
    assert [item["channel"]["name"] for item in result["results"]] == ["群A"]
