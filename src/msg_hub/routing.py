"""路由引擎：入口 → 规则 → 出口集合。

语义（刻意保持简单，没有优先级）：

    一条消息进来，遍历**所有**启用的规则；
    入口条件为空（None）表示匹配所有入口；
    每条命中的规则把它选中的出口并进结果集；
    最后按「首次出现顺序」去重。

规则之间正交：新增一条规则不会改变已有规则的行为，也不会出现
「改了 A 规则导致 B 规则失效」这种 sing-box 式的优先级陷阱。

条件匹配收敛成 `field × op`：

    入口侧 field: entry(名称/id) | tag
    出口侧 field: name | type | tag
    op:           exact | contains | prefix | suffix

每个 field 只暴露自己的候选集合（入口名称不会混进标签集合里），
避免「field=entry 却匹配到某个标签」的意外命中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Channel, Condition, Entry, Rule


def entry_candidates(entry: Entry, field_name: str) -> list[str]:
    """入口侧候选值：按 field 取对应集合。"""
    if field_name == "entry":
        return [entry.name, str(entry.id)]
    return list(entry.tags)


def outbound_candidates(channel: Channel, field_name: str) -> list[str]:
    """出口侧候选值：按 field 取对应集合。"""
    if field_name == "name":
        return [channel.name]
    if field_name == "type":
        return [channel.type]
    return list(channel.tags)


def entry_matches(condition: Condition | None, entry: Entry | None) -> bool:
    """入口条件是否命中。condition 为 None → 匹配所有入口。"""
    if condition is None:
        return True
    if entry is None:
        # 管理员直连场景没有入口对象：只有「匹配所有」的规则能命中
        return False
    return condition.matches(entry_candidates(entry, condition.field))


def outbound_matches(condition: Condition, channel: Channel) -> bool:
    return condition.matches(outbound_candidates(channel, condition.field))


@dataclass
class Selection:
    """一次路由的结果。"""

    channels: list[Channel] = field(default_factory=list)
    # 命中的规则摘要，放进响应/日志，便于排查「为什么发到了这个群」
    matched_rules: list[dict[str, Any]] = field(default_factory=list)
    # 全部规则数 / 启用规则数，用于判断「是不是压根没配规则」
    total_rules: int = 0
    enabled_rules: int = 0

    @property
    def empty(self) -> bool:
        return not self.channels


def select(
    rules: list[Rule],
    entry: Entry | None,
    channels: list[Channel],
) -> Selection:
    """跑一遍所有规则，返回命中的出口（含停用的，由调用方决定跳过）。"""
    result = Selection(total_rules=len(rules))
    seen: set[int] = set()

    for rule in rules:
        if not rule.enabled:
            continue
        result.enabled_rules += 1
        if not entry_matches(rule.entry, entry):
            continue
        hit_ids = [c.id for c in channels if outbound_matches(rule.outbound, c)]
        if not hit_ids:
            continue
        result.matched_rules.append(
            {
                "id": rule.id,
                "name": rule.name,
                "outbounds": hit_ids,
            }
        )
        for channel in channels:
            if channel.id in hit_ids and channel.id not in seen:
                seen.add(channel.id)
                result.channels.append(channel)

    return result


def explain(
    rules: list[Rule],
    entry: Entry | None,
    channels: list[Channel],
) -> list[dict[str, Any]]:
    """逐条规则的判定明细（dashboard / 调试接口用）。"""
    trace: list[dict[str, Any]] = []
    for rule in rules:
        entry_ok = entry_matches(rule.entry, entry)
        hit = [c for c in channels if outbound_matches(rule.outbound, c)]
        trace.append(
            {
                "id": rule.id,
                "name": rule.name,
                "enabled": rule.enabled,
                "entry_condition": rule.entry.describe() if rule.entry else "（所有入口）",
                "outbound_condition": rule.outbound.describe(),
                "entry_matched": entry_ok,
                "outbounds": [{"id": c.id, "name": c.name, "enabled": c.enabled} for c in hit],
                "applied": bool(rule.enabled and entry_ok and hit),
            }
        )
    return trace
