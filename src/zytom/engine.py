"""Deterministic engine (Plan.md「數值引擎」).

Pure functions over a :class:`World` snapshot. Nothing here writes to the
database or calls an LLM. Every numeric signal is a triple — optimistic /
median / pessimistic — computed from the matching ends of the input ranges,
and carries the weakest source among its inputs.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .estimate import Estimator
from .models import SOURCE_RANK, Item, Log, Observation, Plan, TimeSpec, Value, weakest
from .recurrence import future_occurrences, occurrence_span
from .timeutil import (
    Interval,
    clip,
    daily_blocks,
    hours,
    measure,
    merge,
    overlap_h,
    parse_dt,
    period_bounds,
    subtract,
    tz,
)

SCEN = ("opt", "med", "pes")
PEAK_FLOOR_H = 12.0
OPEN = ("open", "active")
PROGRESS_KINDS = {"created", "planned", "started", "done", "deferred", "updated", "estimate_corrected"}

DEFAULT_PREFS: dict[str, Any] = {
    "tz": "Asia/Taipei",
    "reserved": [["00:30", "08:00"]],  # 睡眠等保留時間（每日、當地時間）
    "overhead_h": 3.0,  # 每日吃飯、通勤等雜項
    "windows": [3, 7],
    "threshold": {"v": 0.7, "source": "default", "basis": "初始門檻"},
    "habit_max_per_day": 1,
    "project_stale_days": 14,
    "deferred_idle_days": 7,
    "horizon_days": 120,
}


def merged_prefs(prefs: dict | None) -> dict:
    out = dict(DEFAULT_PREFS)
    out.update(prefs or {})
    return out


def triple(value: Value) -> dict[str, float]:
    return {"opt": value.lo, "med": value.v, "pes": value.hi}


def tv(t: dict[str, float], source: str, nd: int = 2, **extra) -> dict:
    out = {k: (round(t[k], nd) if math.isfinite(t[k]) else t[k]) for k in SCEN}
    out["source"] = source
    out["estimated"] = source != "stated"
    out.update(extra)
    return out


@dataclass
class World:
    items: list[Item]
    logs: list[Log]
    observations: list[Observation]
    prefs: dict
    now: datetime
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.prefs = merged_prefs(self.prefs)

    def copy(self) -> "World":
        return World([i.model_copy(deep=True) for i in self.items], self.logs, self.observations,
                     self.prefs, self.now)

    @property
    def tz(self) -> str:
        return self.prefs["tz"]

    @property
    def by_id(self) -> dict[str, Item]:
        if "by_id" not in self._cache:
            self._cache["by_id"] = {i.id: i for i in self.items}
        return self._cache["by_id"]

    @property
    def children(self) -> dict[str, list[Item]]:
        if "children" not in self._cache:
            ch: dict[str, list[Item]] = {}
            for i in self.items:
                if i.parent:
                    ch.setdefault(i.parent, []).append(i)
            self._cache["children"] = ch
        return self._cache["children"]

    @property
    def estimator(self) -> Estimator:
        if "est" not in self._cache:
            self._cache["est"] = Estimator(self.items, self.logs, self.observations, self.prefs)
        return self._cache["est"]

    def logs_for(self, item_id: str) -> list[Log]:
        if "logs_by" not in self._cache:
            lb: dict[str, list[Log]] = {}
            for log in self.logs:
                if log.item_id:
                    lb.setdefault(log.item_id, []).append(log)
            self._cache["logs_by"] = lb
        return self._cache["logs_by"].get(item_id, [])


# ----------------------------------------------------------------- calendar

class Availability:
    """Free working time: window − reserved blocks − fixed items − overhead."""

    def __init__(self, world: World, horizon: datetime):
        p = world.prefs
        self.start = world.now
        self.end = max(horizon, world.now + timedelta(days=max(p["windows"]) + 1))
        reserved = daily_blocks(self.start, self.end, [tuple(x) for x in p["reserved"]], world.tz)
        busy = list(reserved)
        self.fixed: list[tuple[Item | None, Interval, str]] = []
        for it in world.items:
            if it.status == "dropped" or it.time.type != "fixed" or it.time.allday:
                continue
            iv = (it.time.start, it.time.end)
            self.fixed.append((it, iv, "stated"))
            busy.append(iv)
        for rule in world.items:
            if rule.time.type == "recurring" and rule.time.as_ == "fixed" and rule.status in OPEN:
                for occ in future_occurrences(rule, world.items, self.start, self.end, world.tz):
                    iv = occurrence_span(rule, occ)
                    self.fixed.append((rule, iv, "default"))
                    busy.append(iv)
        self.free = subtract([(self.start, self.end)], busy)
        reserved_per_day = sum(
            hours(datetime.fromisoformat(f"2000-01-0{2 if b <= a else 1}T{b}") - datetime.fromisoformat(f"2000-01-01T{a}"))
            for a, b in p["reserved"]
        )
        awake = max(1.0, 24.0 - reserved_per_day)
        self.factor = max(0.0, 1.0 - float(p["overhead_h"]) / awake)

    def hours(self, t0: datetime, t1: datetime) -> float:
        if t1 <= t0:
            return 0.0
        return measure(clip(self.free, max(t0, self.start), min(t1, self.end))) * self.factor

    def free_intervals(self, t0: datetime, t1: datetime) -> list[Interval]:
        return clip(self.free, t0, t1)


# ----------------------------------------------------------- item helpers

def is_rule(item: Item) -> bool:
    return item.time.type == "recurring"


def is_project(world: World, item: Item) -> bool:
    return bool(world.children.get(item.id)) or bool(item.meta.get("project"))


def is_workload(world: World, item: Item) -> bool:
    """Items whose effort counts toward pressure / slack."""
    if item.status not in OPEN or is_rule(item) or item.time.type == "fixed":
        return False
    if world.children.get(item.id):  # project with children: children carry the work
        return False
    return True


def resolved_time(world: World, item: Item) -> TimeSpec:
    """Turn anchored relations into concrete deadline / release times."""
    t = item.time
    if t.type != "anchored":
        return t
    target = world.by_id.get(t.target or "")
    if target is None:
        return TimeSpec(type="none")
    tt = resolved_time(world, target) if target.time.type == "anchored" else target.time
    if t.relation == "before":
        anchor = tt.start if tt.type == "fixed" else tt.due
        if anchor is None and target.plan and target.plan.start:
            anchor = target.plan.start
        if anchor is None:
            return TimeSpec(type="none")
        return TimeSpec(type="deadline", due=anchor - timedelta(hours=t.offset_h))
    anchor = tt.end if tt.type == "fixed" else tt.due
    if anchor is None and target.plan and target.plan.end:
        anchor = target.plan.end
    if anchor is None:
        return TimeSpec(type="none")
    return TimeSpec(type="none", start=anchor + timedelta(hours=t.offset_h))


def due_range(world: World, item: Item) -> dict[str, datetime] | None:
    t = resolved_time(world, item)
    if t.type != "deadline" or t.due is None:
        return None
    early = t.due_earliest or t.due
    # 樂觀取最晚的截止，悲觀取最早的
    return {"opt": t.due, "med": t.due, "pes": early}


def release(world: World, item: Item) -> datetime | None:
    t = resolved_time(world, item)
    r = t.start if t.type in ("deadline", "none") else None
    if item.plan and item.plan.precision in ("range", "part", "slot") and item.plan.start:
        r = max(r, item.plan.start) if r else item.plan.start
    return r


def last_progress(world: World, item: Item) -> datetime:
    ts = [log.ts for log in world.logs_for(item.id) if log.kind in PROGRESS_KINDS]
    return max(ts) if ts else item.created


# --------------------------------------------------------------- signals

def _deadline_signals(world: World, avail: Availability) -> dict[str, dict]:
    """EDF slack: time until due − cumulative effort of everything due no later."""
    est = world.estimator
    entries = []
    for it in world.items:
        if not is_workload(world, it):
            continue
        d = due_range(world, it)
        if d:
            entries.append((it, d, triple(est.effort(it)), est.effort(it)))
    # future, not-yet-instantiated recurring deadline instances
    for rule in world.items:
        if is_rule(rule) and rule.time.as_ == "deadline" and rule.status in OPEN:
            for occ in future_occurrences(rule, world.items, world.now, avail.end, world.tz):
                due = occurrence_span(rule, occ)[1]
                e = est.effort(rule)
                pseudo = rule.model_copy(update={"id": f"{rule.id}@{occ.isoformat()}"})
                entries.append((pseudo, {"opt": due, "med": due, "pes": due}, triple(e), e))

    out: dict[str, dict] = {}
    per_scen: dict[str, dict[str, float]] = {s: {} for s in SCEN}
    for s in SCEN:
        cum = 0.0
        for it, d, eff, _ in sorted(entries, key=lambda x: x[1][s]):
            cum += eff[s]
            due = d[s]
            edf = avail.hours(world.now, due) - cum
            rel = release(world, it)
            own = avail.hours(max(world.now, rel or world.now), due) - eff[s]
            per_scen[s][it.id] = min(edf, own)
    for it, d, eff, val in entries:
        if "@" in it.id:
            continue
        slack = {s: per_scen[s][it.id] for s in SCEN}
        until = avail.hours(world.now, d["med"])
        days_left = max(0.0, hours(d["med"] - world.now) / 24.0)
        # 負載比（1 − slack/可用）＋ 截止越近越緊
        load = 2.0 if until <= 0 else max(0.0, 1.0 - slack["med"] / until)
        urgency = min(2.0, load + 0.5 / (1.0 + days_left / 2.0))
        out[it.id] = {
            "kind": "deadline",
            "due": d["med"].isoformat(),
            "overdue": d["med"] < world.now,
            "effort": tv(eff, val.source, basis=val.basis),
            "available_h": round(until, 2),
            "slack_h": tv(slack, val.source),
            "urgency": round(urgency, 3),
        }
    return out


def _habit_signal(world: World, it: Item) -> dict:
    t = it.time
    ps, pe = period_bounds(world.now, t.per, world.tz)  # type: ignore[arg-type]
    done = sum(1 for log in world.logs_for(it.id) if log.kind == "done" and ps <= log.ts < pe)
    target = t.count or 0
    if ps < it.created < pe:  # 期中才建立：本期目標按剩餘比例折算
        target = math.ceil(target * hours(pe - it.created) / hours(pe - ps))
    behind = max(0, target - done)
    remaining_days = max(0.0, hours(pe - world.now) / 24.0)
    capacity = math.ceil(remaining_days) * int(world.prefs["habit_max_per_day"])
    urgency = 0.0 if behind == 0 else min(2.0, behind / max(capacity, 0.1))
    return {
        "kind": "habit",
        "target": target,
        "per": t.per,
        "done": done,
        "behind": behind,
        "remaining_days": round(remaining_days, 2),
        "catchable": behind <= capacity,
        "urgency": round(urgency, 3),
    }


def _stale_signal(world: World, it: Item, project: bool) -> dict:
    if project:
        stamps = [last_progress(world, it)] + [last_progress(world, c) for c in world.children.get(it.id, [])]
        last = max(stamps)
        days = hours(world.now - last) / 24.0
        th = float(world.prefs["project_stale_days"])
        return {"kind": "project", "stagnant_days": round(days, 1),
                "children_open": sum(1 for c in world.children.get(it.id, []) if c.status in OPEN),
                "urgency": round(min(1.5, 0.5 * days / th), 3)}
    days = hours(world.now - last_progress(world, it)) / 24.0
    return {"kind": "none", "idle_days": round(days, 1), "urgency": round(min(0.6, 0.3 * days / 30), 3)}


def _required_hours(world: World, avail: Availability, w_end: datetime) -> tuple[dict[str, float], list[dict]]:
    """Work hours that must happen inside [now, w_end], per scenario."""
    est = world.estimator
    req = {s: 0.0 for s in SCEN}
    contrib: list[dict] = []
    now = world.now

    def add(item_id: str, text: str, fractions: dict[str, float], value: Value,
            by: dict[str, datetime] | None = None):
        """``by``: per scenario, the time by which this work has to be done (≤ w_end)."""
        eff = triple(value)
        amount = {s: eff[s] * fractions[s] for s in SCEN}
        if amount["pes"] <= 1e-9:
            return
        for s in SCEN:
            req[s] += amount[s]
        contrib.append({"item_id": item_id, "text": text, "hours": tv(amount, value.source),
                        "effort": tv(eff, value.source, basis=value.basis),
                        "_by": {s: min((by or {}).get(s, w_end), w_end) for s in SCEN}})

    for it in world.items:
        if not is_workload(world, it):
            continue
        if it.time.type == "habit":
            sig = _habit_signal(world, it)
            if sig["behind"]:
                ps, pe = period_bounds(now, it.time.per, world.tz)  # type: ignore[arg-type]
                frac = min(1.0, avail.hours(now, min(pe, w_end)) / max(avail.hours(now, pe), 1e-6))
                per = est.effort(it)
                scaled = Value(v=per.v * sig["behind"], range=(per.lo * sig["behind"], per.hi * sig["behind"]),
                               source=per.source, basis=per.basis)
                add(it.id, it.text, {s: frac for s in SCEN}, scaled)
            continue
        value = est.effort(it)
        plan = it.plan
        if plan and plan.precision in ("range", "part", "slot") and plan.start and plan.end and plan.end > plan.start:
            f = overlap_h((plan.start, plan.end), (now, w_end)) / hours(plan.end - plan.start)
            if plan.end <= now:  # 計畫時段已過但沒完成：仍視為窗口內的工作
                f = 1.0
            add(it.id, it.text, {s: f for s in SCEN}, value, {s: plan.end for s in SCEN})
            continue
        d = due_range(world, it)
        if not d:
            continue
        rel = max(now, release(world, it) or now)
        fr = {}
        for s in SCEN:
            if d[s] <= w_end:
                fr[s] = 1.0
            else:
                total = avail.hours(rel, d[s])
                fr[s] = min(1.0, avail.hours(rel, w_end) / total) if total > 0 else 1.0
        add(it.id, it.text, fr, value, d)

    for rule in world.items:
        if is_rule(rule) and rule.time.as_ == "deadline" and rule.status in OPEN:
            occs = future_occurrences(rule, world.items, now, w_end, world.tz)
            if occs:
                e = est.effort(rule)
                n = len(occs)
                add(rule.id, f"{rule.text}（未實例化 ×{n}）", {s: 1.0 for s in SCEN},
                    Value(v=e.v * n, range=(e.lo * n, e.hi * n), source=e.source if e.source != "stated" else "stat",
                          basis="未實例化的未來期次"))
    contrib.sort(key=lambda c: -c["hours"]["med"])
    return req, contrib


def threshold(world: World, category: str = "*") -> Value:
    th = world.prefs.get("thresholds", {}).get(category) or world.prefs["threshold"]
    return th if isinstance(th, Value) else Value.model_validate(th)


def pressure(world: World, avail: Availability, days: int) -> dict:
    w_end = world.now + timedelta(days=days)
    available = avail.hours(world.now, w_end)
    req, contrib = _required_hours(world, avail, w_end)
    sources = [c["hours"]["source"] for c in contrib]
    src = weakest(sources)
    flat = {s: (req[s] / available if available > 0 else math.inf) for s in SCEN}
    # 峰值壓力：在窗口內每個截止點檢查「到該點為止需要的工時 ÷ 到該點為止的可用時間」，
    # 避免明天的趕工被 3 天窗口稀釋。檢查點至少距現在 PEAK_FLOOR_H，避免除以接近 0。
    p, peak_at = dict(flat), {s: w_end for s in SCEN}
    floor = world.now + timedelta(hours=PEAK_FLOOR_H)
    for s in SCEN:
        points = sorted({max(c["_by"][s], floor) for c in contrib if c["_by"][s] < w_end})
        for pt in points:
            need = sum(c["hours"][s] for c in contrib if max(c["_by"][s], floor) <= pt)
            have = avail.hours(world.now, pt)
            ratio = need / have if have > 0 else math.inf
            if ratio > p[s]:
                p[s], peak_at[s] = ratio, pt
    for c in contrib:
        c.pop("_by", None)
    strong_h = sum(c["hours"]["med"] for c in contrib if SOURCE_RANK[c["hours"]["source"]] <= 1)
    return tv(p, src, nd=3,
              window_days=days,
              flat=tv(flat, src, nd=3),
              peak_at=peak_at["med"].isoformat(),
              available_h=round(available, 2),
              required_h=tv(req, src),
              strong_share=round(strong_h / req["med"], 3) if req["med"] > 0 else 1.0,
              contributors=contrib[:8])


def tension(world: World, pressures: dict[str, dict]) -> dict:
    """Plan.md「依不確定度分級」."""
    th = threshold(world)
    worst = None
    for key, p in pressures.items():
        strong = p["strong_share"] >= 0.5
        if p["med"] > th.v and strong:
            level, why = "warn", "中位壓力超過門檻，主要輸入為 stated/stat"
        elif p["pes"] > th.v:
            level = "ask" if not strong else "info"
            why = "悲觀壓力超過門檻，主要輸入為預估" if not strong else "只有悲觀情境超過門檻"
        else:
            level, why = "none", "悲觀壓力也未超過門檻"
        rank = {"none": 0, "info": 1, "ask": 2, "warn": 3}[level]
        if worst is None or rank > worst["_rank"] or (rank == worst["_rank"] and p["med"] > worst["pressure"]):
            key_estimate = None
            if level in ("ask", "info") and not strong:
                weak = [c for c in p["contributors"] if SOURCE_RANK[c["effort"]["source"]] >= 2]
                if weak:
                    key_estimate = weak[0]
            worst = {"_rank": rank, "level": level, "window": key, "pressure": p["med"],
                     "pessimistic": p["pes"], "threshold": th.v, "reason": why, "key_estimate": key_estimate}
    worst = worst or {"_rank": 0, "level": "none"}
    worst.pop("_rank")
    return worst


def horizon(world: World) -> datetime:
    cap = world.now + timedelta(days=int(world.prefs["horizon_days"]))
    dues = [d["opt"] for it in world.items if (d := due_range(world, it)) and it.status in OPEN]
    plans = [it.plan.end for it in world.items if it.plan and it.plan.end and it.status in OPEN]
    latest = max(dues + plans, default=world.now)
    return min(max(latest, world.now) + timedelta(days=1), cap)


def signals(world: World, windows: list[int] | None = None, include_items: bool = True) -> dict:
    """All item signals plus pressure for each window (default 3 and 7 days)."""
    windows = windows or list(world.prefs["windows"])
    avail = Availability(world, horizon(world))
    pressures = {f"{d}d": pressure(world, avail, d) for d in windows}
    result: dict[str, Any] = {
        "now": world.now.isoformat(),
        "pressure": pressures,
        "tension": tension(world, pressures),
    }
    if not include_items:
        return result
    items: list[dict] = []
    dl = _deadline_signals(world, avail)
    for it in world.items:
        if it.status not in OPEN or is_rule(it):
            continue
        if it.id in dl:
            sig = dl[it.id]
        elif it.time.type == "habit":
            sig = _habit_signal(world, it)
        elif is_project(world, it):
            sig = _stale_signal(world, it, project=True)
        elif it.time.type == "fixed":
            if it.time.end and it.time.end < world.now:
                continue
            sig = {"kind": "fixed", "start": it.time.start.isoformat(), "end": it.time.end.isoformat(), "urgency": 0.0}  # type: ignore[union-attr]
        else:
            sig = _stale_signal(world, it, project=False)
        items.append({"id": it.id, "text": it.text, "category": it.category, "status": it.status, **sig})
    items.sort(key=lambda x: -x["urgency"])
    result["items"] = items
    return result


# -------------------------------------------------------------- changes

CHANGE_OPS = ("defer", "drop", "shrink", "split", "plan", "set", "done")


def apply_change(world: World, change: dict, new_id=None) -> list[str]:
    """Mutate ``world`` in place. Returns ids touched. Used by simulate and apply."""
    op = change.get("op")
    it = world.by_id.get(change.get("item_id", ""))
    if it is None:
        raise ValueError(f"unknown item {change.get('item_id')!r}")
    touched = [it.id]
    if op == "defer":
        until = parse_dt(change["until"], world.tz)
        d = due_range(world, it)
        end = parse_dt(change["end"], world.tz) if change.get("end") else (d["med"] if d and d["med"] > until else until + timedelta(days=7))
        it.plan = Plan(precision="range", start=until, end=end, label=change.get("label") or "延後")
    elif op == "plan":
        it.plan = Plan.model_validate(change["plan"]) if change.get("plan") else None
    elif op == "drop":
        it.status = "dropped"
    elif op == "done":
        it.status = "done"
    elif op == "shrink":
        e = world.estimator.effort(it)
        if "effort" in change:
            it.effort = Value.model_validate(change["effort"]) if isinstance(change["effort"], dict) else Value(v=float(change["effort"]), source="stated")
        else:
            f = float(change.get("factor", 0.6))
            it.effort = Value(v=round(e.v * f, 2), range=(round(e.lo * f, 2), round(e.hi * f, 2)),
                              source=e.source, basis=f"縮小範圍 ×{f}（原 {e.v}h）")
    elif op == "split":
        parts = change.get("parts") or []
        if not parts:
            raise ValueError("split needs parts")
        e = world.estimator.effort(it)
        make = new_id or (lambda: f"tmp_{len(world.items)}")
        for p in parts:
            frac = p.get("fraction")
            eff = (Value.model_validate(p["effort"]) if isinstance(p.get("effort"), dict)
                   else Value(v=float(p["effort"]), source="stated") if p.get("effort") is not None
                   else Value(v=round(e.v * frac, 2), range=(round(e.lo * frac, 2), round(e.hi * frac, 2)),
                              source=e.source, basis=f"由 {it.id} 拆出 {frac:.0%}"))
            child = Item(
                id=make(), text=p.get("text") or f"{it.text}（部分）", parent=it.id, category=it.category,
                time=it.time.model_copy(), effort=eff, commitment=it.commitment,
                plan=Plan.model_validate(p["plan"]) if p.get("plan") else None,
                created=world.now, updated=world.now,
            )
            world.items.append(child)
            touched.append(child.id)
        world._cache.clear()
    elif op == "set":
        data = it.model_dump(by_alias=True)
        fields = dict(change.get("fields") or {})
        if "id" in fields or "created" in fields:
            raise ValueError("set cannot change id or created")
        data.update(fields)
        new = Item.model_validate(data)
        world.items[world.items.index(it)] = new
        world._cache.clear()
        it = new
    else:
        raise ValueError(f"unknown op {op!r}; expected one of {CHANGE_OPS}")
    it.updated = world.now
    world._cache.pop("est", None)
    return touched


def _slack_map(sig: dict) -> dict[str, dict]:
    return {i["id"]: i["slack_h"] for i in sig.get("items", []) if "slack_h" in i}


def simulate(world: World, changes: list[dict]) -> dict:
    before = signals(world)
    w2 = world.copy()
    for ch in changes:
        apply_change(w2, ch)
    after = signals(w2)
    sb, sa = _slack_map(before), _slack_map(after)
    worse = []
    for iid, a in sa.items():
        b = sb.get(iid)
        if b is None:
            continue
        if a["med"] < b["med"] - 0.01:
            worse.append({"id": iid, "text": w2.by_id[iid].text, "slack_before": b["med"], "slack_after": a["med"],
                          "turned_negative": b["med"] >= 0 > a["med"]})
    return {
        "changes": changes,
        "pressure": {k: {"before": before["pressure"][k]["med"], "after": after["pressure"][k]["med"],
                         "before_pes": before["pressure"][k]["pes"], "after_pes": after["pressure"][k]["pes"],
                         "source": after["pressure"][k]["source"]}
                     for k in before["pressure"]},
        "tension": {"before": before["tension"]["level"], "after": after["tension"]["level"]},
        "slack_worse": sorted(worse, key=lambda x: x["slack_after"]),
    }


# ------------------------------------------------------------ candidates

def candidates(world: World, start: datetime, end: datetime, include_partial: bool = True) -> list[dict]:
    """Items whose effort fits into the gap, most urgent first."""
    length = hours(end - start)
    sig = {i["id"]: i for i in signals(world).get("items", [])}
    out = []
    recently_rejected = {
        log.item_id for log in world.logs
        if log.kind == "rec_rejected" and world.now - log.ts < timedelta(hours=3)
    }
    for it in world.items:
        if not is_workload(world, it) or it.id not in sig:
            continue
        rel = release(world, it)
        if rel and rel > end:
            continue
        s = sig[it.id]
        if it.time.type == "habit" and not s.get("behind"):
            continue
        e = world.estimator.effort(it)
        fit = "full" if e.v <= length + 1e-9 else "partial"
        if fit == "partial" and (not include_partial or s["kind"] != "deadline"):
            continue
        score = s["urgency"] + (0.15 if fit == "full" else 0.0)
        reasons = []
        if s["kind"] == "deadline":
            reasons.append(f"中位 slack {s['slack_h']['med']}h")
        elif s["kind"] == "habit":
            reasons.append(f"本{ {'day': '日', 'week': '週', 'month': '月'}[s['per']] }還差 {s['behind']} 次")
        elif s["kind"] in ("none", "project"):
            reasons.append(f"擱置 {s.get('idle_days', s.get('stagnant_days'))} 天")
        for o in world.observations:
            if o.status == "rejected" or o.confidence < 0.5 or o.scope not in (it.category, "*", None):
                continue
            local_h = start.astimezone(tz(world.tz)).hour
            if local_h in (o.params.get("preferred_hours") or []):
                score += 0.2 * o.confidence
                reasons.append(f"符合觀察：{o.text}")
            if local_h in (o.params.get("avoid_hours") or []):
                score -= 0.2 * o.confidence
        if it.id in recently_rejected:
            score -= 0.5
            reasons.append("剛被拒絕過")
        out.append({"id": it.id, "text": it.text, "category": it.category, "fit": fit,
                    "effort": tv(triple(e), e.source, basis=e.basis), "urgency": s["urgency"],
                    "score": round(score, 3), "reasons": reasons})
    out.sort(key=lambda c: (-c["score"]))
    return out


# --------------------------------------------------------------- relieve

def _relief_options(world: World, window_end: datetime) -> list[tuple[dict, float, str]]:
    est = world.estimator
    sig = signals(world)
    contrib_ids: list[str] = []
    for p in sig["pressure"].values():
        for c in p["contributors"]:
            if c["item_id"] not in contrib_ids and c["item_id"] in world.by_id:
                contrib_ids.append(c["item_id"])
    opts = []
    for iid in contrib_ids:
        it = world.by_id[iid]
        if not is_workload(world, it):
            continue
        cm = est.commitment(it)
        cost = cm.cost.v if cm.cost else 4.0
        level = cm.level_name
        d = due_range(world, it)
        if it.time.type != "habit" and (d is None or d["pes"] > window_end + timedelta(days=1)):
            opts.append(({"op": "defer", "item_id": iid, "until": (window_end + timedelta(hours=1)).isoformat()},
                         0.5 + 0.2 * cost, f"把「{it.text}」延到窗口之後"))
        if d is not None and d["pes"] > window_end:
            opts.append(({"op": "split", "item_id": iid, "parts": [
                {"text": f"{it.text}（先做一半）", "fraction": 0.5},
                {"text": f"{it.text}（後半）", "fraction": 0.5,
                 "plan": {"precision": "range", "start": window_end.isoformat(), "end": d["pes"].isoformat()}},
            ]}, 0.5 + 0.3 * cost, f"把「{it.text}」拆成兩半，後半排到窗口之後"))
        if it.time.type != "habit":
            opts.append(({"op": "shrink", "item_id": iid, "factor": 0.6}, 1.0 + 0.5 * cost,
                         f"縮小「{it.text}」的範圍到約 60%"))
        if level == "droppable":  # 只有 droppable 才會被提議推掉
            opts.append(({"op": "drop", "item_id": iid}, cost, f"推掉「{it.text}」"))
    return opts


def relieve(world: World, target: float | None = None, max_changes: int = 3, top: int = 3) -> dict:
    """Cheapest change sets that bring every window's median pressure ≤ target."""
    target = target if target is not None else threshold(world).v
    base = signals(world, include_items=False)
    longest = max(int(k[:-1]) for k in base["pressure"])
    window_end = world.now + timedelta(days=longest)
    if all(v["med"] <= target for v in base["pressure"].values()):
        return {"target": target, "pressure_now": {k: v["med"] for k, v in base["pressure"].items()},
                "already_ok": True, "options": [], "note": "壓力已在門檻以下，不需要減壓。"}
    opts = _relief_options(world, window_end)[:14]
    base_slack = _slack_map(signals(world))
    plans = []

    def evaluate(combo):
        ids = [c[0]["item_id"] for c in combo]
        if len(set(ids)) != len(ids):
            return None
        w2 = world.copy()
        for ch, _, _ in combo:
            apply_change(w2, ch)
        s2 = signals(w2)
        new_negative = [
            i["id"] for i in s2.get("items", [])
            if "slack_h" in i and i["slack_h"]["med"] < 0 <= base_slack.get(i["id"], {"med": -1})["med"]
        ]
        if new_negative:
            return None
        pm = {k: v["med"] for k, v in s2["pressure"].items()}
        if all(pm[k] >= base["pressure"][k]["med"] - 1e-6 for k in pm):
            return None  # 沒有降低任何窗口的壓力
        return {
            "changes": [c[0] for c in combo],
            "summary": [c[2] for c in combo],
            "cost": round(sum(c[1] for c in combo), 2),
            "pressure_after": {k: round(v, 3) for k, v in pm.items()},
            "reaches_target": all(v <= target for v in pm.values()),
        }

    for n in range(1, max_changes + 1):
        for combo in itertools.combinations(opts, n):
            r = evaluate(combo)
            if r:
                plans.append(r)
        if sum(p["reaches_target"] for p in plans) >= top:
            break
    plans.sort(key=lambda p: (not p["reaches_target"], p["cost"], max(p["pressure_after"].values())))
    seen, uniq = set(), []
    for p in plans:
        key = tuple(sorted((c["op"], c["item_id"]) for c in p["changes"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
        if len(uniq) == top:
            break
    return {
        "target": target,
        "pressure_now": {k: v["med"] for k, v in base["pressure"].items()},
        "already_ok": all(v["med"] <= target for v in base["pressure"].values()),
        "options": uniq,
        "note": "只回傳方案，不會寫入；使用者確認後再用 apply 套用。",
    }
