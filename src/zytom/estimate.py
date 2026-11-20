"""Filling missing values: stated > stat > llm > default (Plan.md「估算與預估標記」).

The server never calls an LLM. ``llm`` values arrive through ``item_upsert``
from the agent and are cached on the item until new evidence or a user
correction replaces them. This module provides ``stat`` and ``default``.
"""

from __future__ import annotations

import statistics
from typing import Iterable

from .models import Commitment, Item, Log, Observation, Value

# v, lo, hi in hours. Overridable through the ``default_effort`` pref.
DEFAULT_EFFORT: dict[str, tuple[float, float, float]] = {
    "作業": (2.0, 1.0, 5.0),
    "測驗": (1.0, 0.5, 2.0),
    "問卷": (0.25, 0.1, 0.5),
    "報告": (4.0, 2.0, 10.0),
    "程式": (3.0, 1.0, 8.0),
    "數學": (2.0, 1.0, 4.0),
    "閱讀": (1.0, 0.5, 3.0),
    "習慣": (0.5, 0.25, 1.0),
    "雜事": (0.5, 0.25, 1.5),
    "*": (1.5, 0.5, 4.0),
}

MIN_STAT_SAMPLES = 3
OBS_MIN_CONFIDENCE = 0.5


def actual_hours_by_category(items: Iterable[Item], logs: Iterable[Log]) -> dict[str, list[float]]:
    cat = {i.id: i.category for i in items}
    out: dict[str, list[float]] = {}
    for log in logs:
        if log.kind != "done":
            continue
        actual = log.data.get("actual_h")
        c = cat.get(log.item_id or "")
        if c and isinstance(actual, (int, float)) and actual > 0:
            out.setdefault(c, []).append(float(actual))
    return out


def stat_value(samples: list[float]) -> Value | None:
    if len(samples) < MIN_STAT_SAMPLES:
        return None
    med = statistics.median(samples)
    q = statistics.quantiles(samples, n=4, method="inclusive")
    return Value(v=round(med, 2), range=(round(q[0], 2), round(q[2], 2)), source="stat",
                 basis=f"同類別 {len(samples)} 筆完成紀錄的中位數與四分位距")


def bias_for(category: str | None, observations: Iterable[Observation]) -> tuple[float, str] | None:
    best = None
    for o in observations:
        if o.status == "rejected" or o.confidence < OBS_MIN_CONFIDENCE:
            continue
        if "effort_bias" not in o.params or o.scope not in (category, "*", None):
            continue
        if best is None or (o.scope == category and best.scope != category) or o.confidence > best.confidence:
            best = o
    if best is None:
        return None
    return float(best.params["effort_bias"]), best.id


def default_value(category: str | None, prefs: dict | None = None) -> Value:
    table = dict(DEFAULT_EFFORT)
    table.update({k: tuple(v) for k, v in ((prefs or {}).get("default_effort") or {}).items()})
    key = category if category in table else "*"
    v, lo, hi = table[key]
    return Value(v=v, range=(lo, hi), source="default",
                 basis=f"類別「{key}」預設值" if key != "*" else "通用預設值")


def apply_bias(value: Value, observations: Iterable[Observation], category: str | None) -> Value:
    if value.source not in ("llm", "default"):
        return value
    b = bias_for(category, observations)
    if not b or abs(b[0] - 1.0) < 0.05:
        return value
    ratio, obs_id = b
    return Value(
        v=round(value.v * ratio, 2),
        range=(round(value.lo * ratio, 2), round(value.hi * ratio, 2)),
        source=value.source,
        basis=f"{value.basis or ''}；依觀察 {obs_id} 校正 ×{ratio:.2f}".lstrip("；"),
    )


class Estimator:
    """Precomputed context for resolving effort without touching the DB."""

    def __init__(self, items: list[Item], logs: list[Log], observations: list[Observation], prefs: dict):
        self.samples = actual_hours_by_category(items, logs)
        self.observations = observations
        self.prefs = prefs

    def effort(self, item: Item) -> Value:
        if item.effort is not None:
            return apply_bias(item.effort, self.observations, item.category)
        if item.category and (sv := stat_value(self.samples.get(item.category, []))):
            return sv
        return apply_bias(default_value(item.category, self.prefs), self.observations, item.category)

    def commitment(self, item: Item) -> Commitment:
        c = item.commitment
        level = c.level
        if level is None:
            external = bool(item.meta.get("external_id")) or item.category in ("測驗",)
            if "不計分" in item.text:
                level = Value(v=1, source="default", basis="描述標示不計分")
            else:
                level = Value(v=0 if external else 1, source="default",
                              basis="外部指定的課業通常不能推" if external else "未指定，預設為 try")
        cost = c.cost or Value(v=[8, 4, 1][round(level.v)], range=None, source="default",
                               basis="依承諾等級的預設代價")
        return Commitment(level=level, cost=cost)
