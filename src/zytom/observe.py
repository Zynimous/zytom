"""Observation layer: log → statistics (here) → reflection (LLM, outside).

The daily batch turns the event log into ``stats``-origin Observations with
machine-usable ``params``. The weekly LLM reflection reads these through the
``observations`` tool and writes back via ``observation_upsert``. Nothing
here describes the user's state; it only describes what they usually do.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import engine
from .models import Observation, Value
from .store import Store
from .timeutil import now_utc, tz

MIN_N = 3
REFLECTION_WARMUP_DAYS = 21
THRESHOLD_STEP = 0.05
THRESHOLD_BOUNDS = (0.4, 1.3)


def _upsert_stat(store: Store, obs_id: str, text: str, scope: str | None, params: dict, evidence: list[int],
                 now: datetime) -> Observation:
    cur = next((o for o in store.observations(include_rejected=True) if o.id == obs_id), None)
    n = len(evidence)
    conf = min(0.9, 0.25 + 0.08 * n)
    if cur and cur.status == "rejected":
        return cur  # rejected 永不使用、也不復活
    status = cur.status if cur else "hypothesis"
    if cur and cur.status == "confirmed":
        conf = max(conf, cur.confidence)
    obs = Observation(id=obs_id, text=text, scope=scope, params=params, evidence=evidence[-50:],
                      confidence=round(conf, 3), status=status, last_seen=now, origin="stats")
    return store.save_observation(obs)


def daily_stats(store: Store, now: datetime | None = None) -> dict:
    now = now or now_utc()
    prefs = engine.merged_prefs(store.all_prefs())
    z = tz(prefs["tz"])
    items = {i.id: i for i in store.items()}
    logs = store.logs()
    cat = lambda iid: (items.get(iid).category if iid in items else None)  # noqa: E731
    written = []

    # 估計偏差：實際 ÷ 預估
    ratios: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for log in logs:
        if log.kind == "estimate_corrected" and "ratio" in log.data:
            c = log.data.get("category") or cat(log.item_id)
            if c:
                ratios[c].append((float(log.data["ratio"]), log.id))
    for c, rs in ratios.items():
        if len(rs) >= MIN_N:
            med = statistics.median(r for r, _ in rs)
            written.append(_upsert_stat(store, f"stat:bias:{c}",
                                        f"「{c}」類實際工時約為預估的 {med:.2f} 倍", c,
                                        {"effort_bias": round(med, 3), "n": len(rs)}, [i for _, i in rs], now))

    # 時段傾向：開始 / 完成的當地小時
    hours: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for log in logs:
        if log.kind in ("started", "done") and (c := cat(log.item_id)):
            hours[c].append((log.ts.astimezone(z).hour, log.id))
    for c, hs in hours.items():
        if len(hs) >= MIN_N:
            cnt = Counter(h for h, _ in hs)
            top = [h for h, k in cnt.most_common(3) if k >= 2]
            if top:
                written.append(_upsert_stat(store, f"stat:hours:{c}",
                                            f"「{c}」類通常在 {', '.join(f'{h}點' for h in sorted(top))} 開始或完成", c,
                                            {"preferred_hours": sorted(top), "n": len(hs)}, [i for _, i in hs], now))

    # 延後模式
    defers: dict[str, Counter] = defaultdict(Counter)
    drop_after: dict[str, list[int]] = defaultdict(list)
    ev_ids: dict[str, list[int]] = defaultdict(list)
    per_item_defers: Counter = Counter()
    for log in logs:
        c = cat(log.item_id)
        if not c:
            continue
        if log.kind == "created":
            defers[c]["items"] += 1
        elif log.kind == "deferred":
            defers[c]["deferred"] += 1
            per_item_defers[log.item_id] += 1
            ev_ids[c].append(log.id)  # type: ignore[arg-type]
        elif log.kind == "dropped":
            drop_after[c].append(per_item_defers[log.item_id])
            ev_ids[c].append(log.id)  # type: ignore[arg-type]
    for c, k in defers.items():
        if k["deferred"] >= MIN_N and k["items"]:
            params = {"defer_rate": round(k["deferred"] / k["items"], 3)}
            if drop_after[c]:
                params["defers_before_drop"] = statistics.median(drop_after[c])
            written.append(_upsert_stat(store, f"stat:defer:{c}",
                                        f"「{c}」類平均每項延後 {params['defer_rate']:.1f} 次", c, params, ev_ids[c], now))

    # 前後關聯：跳過 / 拒絕推薦後兩小時內改做哪類
    after_skip: dict[str, Counter] = defaultdict(Counter)
    skip_ev: dict[str, list[int]] = defaultdict(list)
    for i, log in enumerate(logs):
        if log.kind in ("skipped", "rec_rejected") and (c := cat(log.item_id)):
            for nxt in logs[i + 1:]:
                if nxt.ts - log.ts > timedelta(hours=2):
                    break
                if nxt.kind == "started" and (c2 := cat(nxt.item_id)) and nxt.item_id != log.item_id:
                    after_skip[c][c2] += 1
                    skip_ev[c].append(nxt.id)  # type: ignore[arg-type]
                    break
    for c, cnt in after_skip.items():
        if sum(cnt.values()) >= MIN_N:
            best, k = cnt.most_common(1)[0]
            written.append(_upsert_stat(store, f"stat:after_skip:{c}", f"跳過「{c}」後通常改做「{best}」", c,
                                        {"after_skip_prefers": best, "share": round(k / sum(cnt.values()), 2)},
                                        skip_ev[c], now))

    # 習慣節奏：近 4 期實際次數 ÷ 目標
    for it in items.values():
        if it.time.type != "habit" or it.status not in engine.OPEN:
            continue
        per = it.time.per
        span = {"day": 1, "week": 7, "month": 30}[per]  # type: ignore[index]
        since = now - timedelta(days=4 * span)
        done = [log for log in logs if log.item_id == it.id and log.kind == "done" and log.ts >= since]
        if (now - it.created).days >= span:
            periods = min(4, max(1, (now - it.created).days // span))
            rate = len(done) / periods / (it.time.count or 1)
            written.append(_upsert_stat(store, f"stat:habit:{it.id}",
                                        f"「{it.text}」近 {periods} 期達成率約 {rate:.0%}", it.category or it.id,
                                        {"habit_rate": round(rate, 3), "item_id": it.id},
                                        [log.id for log in done if log.id is not None], now))

    # 完成前置時間：距截止多久才開始
    lead: dict[str, list[tuple[float, int]]] = defaultdict(list)
    first_start: dict[str, datetime] = {}
    for log in logs:
        if log.kind in ("started", "done") and log.item_id and log.item_id not in first_start:
            first_start[log.item_id] = log.ts
            it = items.get(log.item_id)
            if it and it.category and it.time.type == "deadline" and it.time.due:
                lead[it.category].append(((it.time.due - log.ts).total_seconds() / 86400, log.id))  # type: ignore[arg-type]
    for c, ls in lead.items():
        if len(ls) >= MIN_N:
            med = statistics.median(v for v, _ in ls)
            written.append(_upsert_stat(store, f"stat:lead:{c}", f"「{c}」類通常在截止前 {med:.1f} 天才開始", c,
                                        {"lead_days": round(med, 2)}, [i for _, i in ls], now))

    decayed = decay(store, now)
    learned = learn_thresholds(store, now)
    first = logs[0].ts if logs else now
    return {
        "written": [o.id for o in written],
        "decayed": decayed,
        "threshold": learned,
        "reflection_ready": (now - first).days >= REFLECTION_WARMUP_DAYS,
        "log_days": (now - first).days,
    }


def decay(store: Store, now: datetime, stale_days: int = 14) -> list[str]:
    """Lower confidence of observations without fresh evidence."""
    out = []
    for o in store.observations():
        age = (now - o.last_seen).days
        if age > stale_days:
            weeks = (age - stale_days) / 7
            new = round(o.confidence * (0.9 ** weeks), 3)
            last_decay = o.params.get("_decayed_at")
            if new < o.confidence and last_decay != now.date().isoformat():
                o.confidence = new
                o.params["_decayed_at"] = now.date().isoformat()
                store.save_observation(o)
                out.append(o.id)
    return out


def learn_thresholds(store: Store, now: datetime) -> dict:
    """Adjust the tension threshold from reactions (Plan.md「門檻學習」).

    * alert_feedback ``not_needed`` and the related items finished on time → up
    * an item went overdue / was dropped, or the user said they stayed up late,
      with no warn/ask alert in the 3 days before → down
    """
    prefs = engine.merged_prefs(store.all_prefs())
    th = Value.model_validate(prefs["threshold"])
    cursor = int(store.pref("learn_cursor", 0))
    logs = [log for log in store.logs() if (log.id or 0) > cursor]
    items = {i.id: i for i in store.items()}
    alerts = store.alerts(limit=500)
    delta, reasons = 0.0, []

    def alerted_before(ts: datetime) -> bool:
        return any(a["level"] in ("warn", "ask") and ts - timedelta(days=3) <= datetime.fromisoformat(a["ts"]) <= ts
                   for a in alerts)

    for log in logs:
        if log.kind == "alert_feedback" and log.data.get("reaction") == "not_needed":
            ids = log.data.get("items") or []
            on_time = all(
                (it := items.get(i)) and it.status == "done" and not (it.time.due and it.updated > it.time.due)
                for i in ids
            ) if ids else True
            if on_time:
                delta += THRESHOLD_STEP
                reasons.append(f"log {log.id}: 提醒被認為不需要且準時完成")
        elif log.kind == "dropped" or (log.kind == "said" and log.data.get("signal") in ("熬夜", "late_night", "overloaded")):
            if not alerted_before(log.ts):
                delta -= THRESHOLD_STEP
                reasons.append(f"log {log.id}: 未提醒但出現 {log.kind}/{log.data.get('signal')}")
        elif log.kind == "done" and (it := items.get(log.item_id or "")) and it.time.due and log.ts > it.time.due:
            if not alerted_before(log.ts):
                delta -= THRESHOLD_STEP
                reasons.append(f"log {log.id}: 未提醒但逾期完成")
    if logs:
        store.set_pref("learn_cursor", logs[-1].id)
    if delta:
        lo, hi = THRESHOLD_BOUNDS
        new_v = round(min(hi, max(lo, th.v + delta)), 3)
        th = Value(v=new_v, source="stat", basis="; ".join(reasons[-5:]))
        store.set_pref("threshold", th.model_dump())
    return {"v": th.v, "delta": round(delta, 3), "reasons": reasons}
