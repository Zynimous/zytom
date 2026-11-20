"""Proactive alerts (Plan.md「主動提醒引擎」).

``tick`` runs the engine, evaluates rules, de-duplicates against earlier
alerts and hands new ones to Notifiers. Message wording is left to the
client/LLM: an Alert carries the rule, items, level, a signal snapshot and
the relieve/recommend options to talk about.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import engine
from .recurrence import materialize
from .store import Store, new_id
from .timeutil import now_utc, tz

LEVELS = ["info", "ask", "warn"]
# 「惡化」至少要超過多少（各規則 severity 的單位）才再次提醒
DEDUPE_ABS_TOL = {"tension": 0.05, "slack_negative": 2.0, "habit_behind": 1.0, "project_stale": 0.5,
                  "deferred_idle": 1.0}


class Notifier(Protocol):
    def send(self, alerts: list[dict]) -> None: ...


class StdoutNotifier:
    def send(self, alerts: list[dict]) -> None:
        for a in alerts:
            print(json.dumps(a, ensure_ascii=False, default=str), file=sys.stdout, flush=True)


class WebhookNotifier:
    def __init__(self, url: str):
        self.url = url

    def send(self, alerts: list[dict]) -> None:
        body = json.dumps({"alerts": alerts}, ensure_ascii=False, default=str).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()  # noqa: S310 — user-configured URL


class FileNotifier:
    """JSONL file — the desktop app can tail it as a simple IPC channel."""

    def __init__(self, path: str):
        self.path = Path(path).expanduser()

    def send(self, alerts: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for a in alerts:
                f.write(json.dumps(a, ensure_ascii=False, default=str) + "\n")


class UnixSocketNotifier:
    """Desktop IPC: send JSONL to a Unix socket the local app listens on."""

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())

    def send(self, alerts: list[dict]) -> None:
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(self.path)
            for a in alerts:
                s.sendall((json.dumps(a, ensure_ascii=False, default=str) + "\n").encode())


def notifiers_from_prefs(prefs: dict) -> list[Notifier]:
    out: list[Notifier] = []
    for n in prefs.get("notifiers") or []:
        if n.get("type") == "webhook":
            out.append(WebhookNotifier(n["url"]))
        elif n.get("type") == "file":
            out.append(FileNotifier(n["path"]))
        elif n.get("type") == "unix":
            out.append(UnixSocketNotifier(n["path"]))
        elif n.get("type") == "stdout":
            out.append(StdoutNotifier())
    return out


# ----------------------------------------------------------------- rules

def evaluate(world: engine.World, sig: dict | None = None) -> list[dict]:
    """Candidate alerts before de-duplication. Each has a severity number."""
    sig = sig or engine.signals(world)
    p = world.prefs
    out: list[dict] = []
    t = sig["tension"]
    if t["level"] in ("warn", "ask", "info"):
        win = sig["pressure"][t["window"]]
        ids = [c["item_id"] for c in win["contributors"][:5]]
        out.append({"rule": "tension", "items": ids, "level": t["level"],
                    "severity": round(t["pressure"] if t["level"] == "warn" else t["pessimistic"], 3),
                    "signals": {"pressure": {k: {kk: v[kk] for kk in ("opt", "med", "pes", "source")}
                                             for k, v in sig["pressure"].items()}, "tension": t}})
    neg = [i for i in sig["items"] if i["kind"] == "deadline" and i["slack_h"]["med"] < 0]
    if neg:
        out.append({"rule": "slack_negative", "items": [i["id"] for i in neg], "level": "warn",
                    "severity": round(-sum(i["slack_h"]["med"] for i in neg), 2),
                    "signals": {"items": [{k: i[k] for k in ("id", "text", "due", "slack_h", "overdue")} for i in neg]}})
    for i in sig["items"]:
        if i["kind"] == "habit" and i["behind"] and not i["catchable"]:
            out.append({"rule": "habit_behind", "items": [i["id"]], "level": "info", "severity": i["behind"],
                        "signals": {"habit": i}})
        if i["kind"] == "project" and i["stagnant_days"] > p["project_stale_days"]:
            out.append({"rule": "project_stale", "items": [i["id"]],
                        "level": "ask" if i["children_open"] == 0 else "info",
                        "severity": round(i["stagnant_days"] / p["project_stale_days"], 2),
                        "signals": {"project": i}})
    # 延後未動
    for it in world.items:
        if it.status not in engine.OPEN:
            continue
        logs = world.logs_for(it.id)
        last_defer = max((log.ts for log in logs if log.kind == "deferred"), default=None)
        if last_defer is None:
            continue
        last_any = max(log.ts for log in logs if log.kind not in ("said",))
        idle = (world.now - last_any).days
        if last_any == last_defer and idle >= p["deferred_idle_days"]:
            out.append({"rule": "deferred_idle", "items": [it.id], "level": "info",
                        "severity": round(idle / p["deferred_idle_days"], 2),
                        "signals": {"idle_days": idle, "text": it.text}})
    # 每日回顧
    review = p.get("daily_review")
    if review:
        local = world.now.astimezone(tz(world.tz))
        hh, mm = map(int, review.split(":"))
        if (local.hour, local.minute) >= (hh, mm):
            out.append({"rule": "daily_review", "items": [], "level": "info", "severity": 0,
                        "key_suffix": local.date().isoformat(),
                        "signals": {"pressure": {k: v["med"] for k, v in sig["pressure"].items()},
                                    "top": [{k: i.get(k) for k in ("id", "text", "kind", "urgency")}
                                            for i in sig["items"][:5]]}})
    return out


def _key(a: dict) -> str:
    return f"{a['rule']}:{','.join(sorted(a['items']))}:{a.get('key_suffix', '')}"


def tick(store: Store, now: datetime | None = None, notifiers: list[Notifier] | None = None,
         summarize: bool = False) -> list[dict]:
    """One loop iteration. Returns the alerts that were emitted."""
    now = now or now_utc()
    saved_clock, store.clock = store.clock, (lambda: now)
    try:
        return _tick(store, now, notifiers, summarize)
    finally:
        store.clock = saved_clock


def _tick(store: Store, now: datetime, notifiers, summarize: bool) -> list[dict]:
    prefs = engine.merged_prefs(store.all_prefs())
    materialize(store, now, prefs["tz"])
    world = engine.World(store.items(), store.logs(), store.observations(), store.all_prefs(), now)
    sig = engine.signals(world)
    emitted = []
    for a in evaluate(world, sig):
        key = _key(a)
        last = store.last_alert(key)
        level = a["level"]
        if last:
            tol = max(DEDUPE_ABS_TOL.get(a["rule"], 0.05), 0.1 * abs(last["severity"]))
            if a["severity"] <= last["severity"] + tol:
                continue  # 訊號沒有惡化：不重複提醒
            # 持續惡化：info → ask → warn
            level = LEVELS[max(LEVELS.index(level), min(2, LEVELS.index(last["level"]) + 1))]
        options = None
        if a["rule"] in ("tension", "slack_negative") and level == "warn":
            options = {"relieve": engine.relieve(world)["options"]}
        elif a["rule"] == "tension" and level == "ask":
            options = {"confirm_estimate": a["signals"]["tension"].get("key_estimate")}
        elif a["rule"] == "daily_review":
            local = now.astimezone(tz(prefs["tz"]))
            tomorrow = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            options = {"recommend_tomorrow": engine.candidates(world, tomorrow, tomorrow + timedelta(hours=2))[:3]}
        alert = {"id": new_id("a"), "ts": now.isoformat(), "rule": a["rule"], "items": a["items"],
                 "level": level, "signals": a["signals"], "options": options}
        store.save_alert(alert["id"], key, level, a["severity"], alert)
        emitted.append(alert)
    if summarize and len(emitted) > 1:
        summary = {"id": new_id("a"), "ts": now.isoformat(), "rule": "startup_summary",
                   "items": sorted({i for a in emitted for i in a["items"]}),
                   "level": max((a["level"] for a in emitted), key=LEVELS.index),
                   "signals": {"alerts": [{k: a[k] for k in ("id", "rule", "level", "items")} for a in emitted]},
                   "options": None}
        to_send = [summary]
    else:
        to_send = emitted
    if to_send:
        for n in notifiers if notifiers is not None else notifiers_from_prefs(prefs):
            try:
                n.send(to_send)
            except Exception as e:  # a broken channel must not stop the loop
                print(f"notifier error: {e}", file=sys.stderr)
        store.mark_delivered(a["id"] for a in emitted)
    return to_send


def startup(store: Store, now: datetime | None = None, notifiers: list[Notifier] | None = None) -> dict:
    """Startup check: catch up recurring instances, recompute, send one summary."""
    from .ics import sync_sources

    now = now or now_utc()
    prefs = engine.merged_prefs(store.all_prefs())
    synced = sync_sources(store, prefs["tz"], now) if store.pref("calendar_sources") else []
    mat = materialize(store, now, prefs["tz"])
    sent = tick(store, now, notifiers, summarize=True)
    return {"calendars": synced, "materialized": mat, "alerts": sent}


def run_daemon(store: Store, interval_min: float = 30, notifiers: list[Notifier] | None = None) -> None:
    """Blocking loop: tick every ``interval_min``, stats once a day, calendar sync every few hours."""
    from .ics import sync_sources
    from .observe import daily_stats

    print(json.dumps(startup(store, notifiers=notifiers), ensure_ascii=False, default=str), flush=True)
    last_stats = store.pref("last_stats_date")
    last_sync = time.monotonic()
    while True:
        time.sleep(interval_min * 60)
        now = now_utc()
        prefs = engine.merged_prefs(store.all_prefs())
        if time.monotonic() - last_sync > float(prefs.get("calendar_sync_h", 6)) * 3600:
            sync_sources(store, prefs["tz"], now)
            last_sync = time.monotonic()
        tick(store, now, notifiers)
        today = now.astimezone(tz(prefs["tz"])).date().isoformat()
        if today != last_stats:
            daily_stats(store, now)
            store.set_pref("last_stats_date", today)
            last_stats = today
