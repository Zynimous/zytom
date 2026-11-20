"""Application layer: everything the MCP tools and CLI call.

Read operations build a :class:`World` snapshot and hand it to the pure
engine. Write operations go through :meth:`Secretary.upsert`,
:meth:`Secretary.log_event` and :meth:`Secretary.apply`, which always append
to the event log.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import engine
from .models import LEVELS, Commitment, Item, Plan, TimeSpec, Value
from .recurrence import materialize
from .store import Store, new_id
from .timeutil import now_utc, parse_dt

STATUS_KINDS = {"started": "active", "done": "done", "dropped": "dropped"}


def to_value(x: Any, default_source: str = "stated") -> Value | None:
    """Accept 4, "4", {"v":4,...} or a Value."""
    if x is None:
        return None
    if isinstance(x, Value):
        return x
    if isinstance(x, (int, float, str)):
        return Value(v=float(x), source=default_source)  # type: ignore[arg-type]
    v = Value.model_validate(x)
    if v.source == "llm" and (v.range is None or not v.basis):
        raise ValueError("llm estimates must include range and basis")
    return v


def to_level(x: Any) -> Value | None:
    if x is None:
        return None
    if isinstance(x, str):
        return Value(v=LEVELS.index(x), source="stated")
    if isinstance(x, dict) and isinstance(x.get("v"), str):
        x = dict(x, v=LEVELS.index(x["v"]))
    return to_value(x)


class Secretary:
    def __init__(self, store: Store, clock=None):
        self.store = store
        self.clock = clock or store.clock
        store.clock = self.clock  # log 時間戳與引擎用同一個時鐘

    # ------------------------------------------------------------ helpers
    @property
    def now(self) -> datetime:
        return self.clock()

    @property
    def prefs(self) -> dict:
        return engine.merged_prefs(self.store.all_prefs())

    @property
    def tz(self) -> str:
        return self.prefs["tz"]

    def world(self, include_closed: bool = True) -> engine.World:
        return engine.World(
            items=self.store.items(None if include_closed else engine.OPEN),
            logs=self.store.logs(),
            observations=self.store.observations(),
            prefs=self.store.all_prefs(),
            now=self.now,
        )

    def _dt(self, v):
        return parse_dt(v, self.tz) if v is not None else None

    def _norm_time(self, t: dict | None) -> dict | None:
        if not t:
            return t
        t = dict(t)
        for k in ("due", "due_earliest", "start", "end", "dtstart"):
            if isinstance(t.get(k), str):
                t[k] = self._dt(t[k])
        return t

    def _norm_plan(self, p: dict | None) -> dict | None:
        if not p:
            return p
        p = dict(p)
        for k in ("start", "end"):
            if isinstance(p.get(k), str):
                p[k] = self._dt(p[k])
        return p

    # ------------------------------------------------------------ items
    def upsert(self, data: dict) -> dict:
        """Create (no id) or patch (id given) an Item. Only provided keys change."""
        now = self.now
        data = dict(data)
        item_id = data.pop("id", None)
        existing = self.store.get_item(item_id) if item_id else None
        if item_id and existing is None:
            raise ValueError(f"item {item_id} not found")
        events: list[tuple[str, dict]] = []

        if existing is None:
            if not data.get("text"):
                raise ValueError("text is required for a new item")
            base: dict[str, Any] = {"id": new_id(), "text": data["text"], "created": now, "updated": now}
        else:
            base = existing.model_dump(by_alias=True)

        if "text" in data:
            base["text"] = data["text"]
        for k in ("parent", "category", "status", "meta"):
            if k in data:
                base[k] = (base.get("meta") or {}) | data[k] if k == "meta" and data[k] is not None else data[k]
        if "time" in data:
            base["time"] = TimeSpec.model_validate(self._norm_time(data["time"]) or {"type": "none"})
        if "plan" in data:
            new_plan = Plan.model_validate(self._norm_plan(data["plan"])) if data["plan"] else None
            if existing is not None:
                events.append(("planned", {"before": existing.plan and existing.plan.model_dump(mode="json"),
                                           "after": new_plan and new_plan.model_dump(mode="json")}))
            base["plan"] = new_plan
        if "effort" in data:
            new_eff = to_value(data["effort"])
            old = existing.effort if existing else None
            if old and new_eff and old.source != "stated" and new_eff.source == "stated":
                events.append(("estimate_corrected", {"field": "effort", "before": old.model_dump(),
                                                      "after": new_eff.model_dump(), "by": "user"}))
            base["effort"] = new_eff
        if "commitment" in data:
            c = data["commitment"] or {}
            cur = existing.commitment if existing else Commitment()
            base["commitment"] = Commitment(
                level=to_level(c["level"]) if "level" in c else cur.level,
                cost=to_value(c["cost"]) if "cost" in c else cur.cost,
            )
        base["updated"] = now
        if base.get("parent") and base["parent"] == base["id"]:
            raise ValueError("item cannot be its own parent")
        item = Item.model_validate(base)
        self.store.save_item(item, commit=False)

        if existing is None:
            self.store.append("created", item.id, {"text": item.text, "said": data.get("said")}, commit=False)
            if item.plan:
                self.store.append("planned", item.id, {"after": item.plan.model_dump(mode="json")}, commit=False)
        else:
            changed = sorted(k for k in data if k not in ("plan", "effort"))
            if changed:
                events.append(("updated", {"fields": changed}))
            if existing.status != item.status and item.status in ("done", "dropped", "active"):
                events.append(({"done": "done", "dropped": "dropped", "active": "started"}[item.status], {}))
        for kind, d in events:
            self.store.append(kind, item.id, d, commit=False)
        self.store.db.commit()
        if item.time.type == "recurring":
            materialize(self.store, now, self.tz)
        return {"item": self.describe(item)}

    def describe(self, item: Item, world: engine.World | None = None) -> dict:
        d = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        est = (world or self.world()).estimator
        if item.status in engine.OPEN and item.time.type not in ("fixed", "recurring"):
            e = est.effort(item)
            d["effort_resolved"] = e.model_dump(exclude_none=True) | {"display": display_value(e, "h")}
        cm = est.commitment(item)
        d["commitment_resolved"] = {"level": cm.level_name, "level_source": cm.level.source if cm.level else "default",
                                    "cost": cm.cost.v if cm.cost else None}
        return d

    def query(self, text: str | None = None, status: list[str] | None = None, category: str | None = None,
              parent: str | None = None, time_type: str | None = None, due_before: str | None = None,
              due_after: str | None = None, ids: list[str] | None = None, source: str | None = None,
              limit: int = 50) -> dict:
        items = self.store.items(status or None)
        w = self.world()
        out = []
        db, da = self._dt(due_before), self._dt(due_after)
        for it in items:
            if ids and it.id not in ids:
                continue
            if text and text.lower() not in it.text.lower():
                continue
            if category and it.category != category:
                continue
            if parent is not None and it.parent != parent:
                continue
            if time_type and it.time.type != time_type:
                continue
            if source and it.meta.get("source") != source:
                continue
            if db or da:
                d = engine.due_range(w, it)
                if not d or (db and d["med"] > db) or (da and d["med"] < da):
                    continue
            out.append(it)
        out.sort(key=lambda i: (engine.due_range(w, i) or {"med": i.created + timedelta(days=3650)})["med"])
        return {"count": len(out), "items": [self.describe(i, w) for i in out[:limit]]}

    # -------------------------------------------------------------- log
    def log_event(self, kind: str, item_id: str | None = None, data: dict | None = None) -> dict:
        data = dict(data or {})
        item = self.store.get_item(item_id) if item_id else None
        if item_id and item is None:
            raise ValueError(f"item {item_id} not found")
        if kind in ("deferred", "dropped"):
            raise ValueError(f"'{kind}' changes a commitment; use apply with confirmed=true")
        log = self.store.append(kind, item_id, data, commit=False)
        extra: dict[str, Any] = {}
        if item is not None and kind in STATUS_KINDS and item.time.type not in ("habit", "recurring"):
            item.status = STATUS_KINDS[kind]  # type: ignore[assignment]
            item.updated = self.now
            self.store.save_item(item, commit=False)
            extra["status"] = item.status
        if item is not None and kind == "done" and isinstance(data.get("actual_h"), (int, float)):
            est = self.world().estimator.effort(item)
            if est.v > 0:
                corr = {"estimated": est.model_dump(), "actual_h": data["actual_h"],
                        "ratio": round(data["actual_h"] / est.v, 3), "category": item.category}
                self.store.append("estimate_corrected", item_id, corr, commit=False)
                extra["estimate_check"] = corr
        self.store.db.commit()
        return {"logged": log.model_dump(mode="json"), **extra}

    # ------------------------------------------------------------ engine
    def signals(self, windows: list[int] | None = None, top: int | None = None) -> dict:
        res = engine.signals(self.world(), windows)
        if top is not None:
            res["items"] = res["items"][:top]
        return res

    def simulate(self, changes: list[dict]) -> dict:
        return engine.simulate(self.world(), [self._norm_change(c) for c in changes])

    def recommend(self, start: str | None = None, end: str | None = None, minutes: int | None = None,
                  n: int = 3) -> dict:
        s = self._dt(start) if start else self.now
        e = self._dt(end) if end else s + timedelta(minutes=minutes or 60)
        cands = engine.candidates(self.world(), s, e)
        return {"slot": {"start": s.isoformat(), "end": e.isoformat(),
                         "hours": round((e - s).total_seconds() / 3600, 2)},
                "recommendations": cands[: max(2, min(n, 3))],
                "more": len(cands) > n}

    def relieve(self, target: float | None = None) -> dict:
        return engine.relieve(self.world(), target)

    def _norm_change(self, c: dict) -> dict:
        c = dict(c)
        if c.get("plan"):
            c["plan"] = self._norm_plan(c["plan"])
        if c.get("fields", {}).get("time"):
            c["fields"] = dict(c["fields"], time=self._norm_time(c["fields"]["time"]))
        return c

    def apply(self, changes: list[dict], confirmed: bool = False, note: str | None = None) -> dict:
        """Write user-confirmed changes. Refuses unless ``confirmed`` is true."""
        if not confirmed:
            raise ValueError("apply requires confirmed=true — only call it after the user agreed to these changes")
        w = self.world()
        results = []
        for ch in (self._norm_change(c) for c in changes):
            before = w.by_id.get(ch.get("item_id", ""))
            before_dump = before.model_dump(mode="json") if before else None
            touched = engine.apply_change(w, ch, new_id=lambda: new_id())
            for iid in touched:
                it = w.by_id.get(iid) or next(i for i in w.items if i.id == iid)
                self.store.save_item(it, commit=False)
            main = w.by_id[ch["item_id"]] if ch["item_id"] in w.by_id else next(i for i in w.items if i.id == ch["item_id"])
            op = ch["op"]
            data = {"op": op, "change": ch, "note": note}
            if op == "defer":
                self.store.append("deferred", main.id, data | {"before": before_dump and before_dump.get("plan"),
                                                               "after": main.plan and main.plan.model_dump(mode="json")}, commit=False)
            elif op == "drop":
                self.store.append("dropped", main.id, data, commit=False)
            elif op == "done":
                self.store.append("done", main.id, data, commit=False)
            elif op == "plan":
                self.store.append("planned", main.id, data | {"before": before_dump and before_dump.get("plan")}, commit=False)
            else:
                self.store.append("updated", main.id, data | {"before": before_dump}, commit=False)
            for iid in touched[1:]:
                self.store.append("created", iid, {"from_split": main.id}, commit=False)
            if ch.get("from_recommendation"):
                self.store.append("rec_accepted", main.id, {"recommendation": ch["from_recommendation"]}, commit=False)
            results.append({"op": op, "item_id": main.id, "touched": touched})
        self.store.db.commit()
        return {"applied": results}

    # ----------------------------------------------------- observations
    def observations(self, scope: str | None = None, include_hypotheses: bool = True,
                     min_confidence: float = 0.0) -> dict:
        obs = self.store.observations(scope)
        obs = [o for o in obs if o.confidence >= min_confidence and (include_hypotheses or o.status == "confirmed")]
        obs.sort(key=lambda o: -o.confidence)
        return {"observations": [o.model_dump(mode="json") for o in obs]}

    # ------------------------------------------------------------ context
    def context(self) -> dict:
        """Per-turn context (Plan.md「Context 組成」): today, pressure, top 5 signals."""
        from .timeutil import tz
        w = self.world()
        sig = engine.signals(w)
        z = tz(self.tz)
        local = self.now.astimezone(z)
        day_s = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_e = day_s + timedelta(days=1)
        today = []
        for it in w.items:
            if it.status == "dropped":
                continue
            if it.time.type == "fixed" and it.time.start < day_e and it.time.end > day_s:  # type: ignore[operator]
                today.append({"id": it.id, "text": it.text, "start": it.time.start.isoformat(), "end": it.time.end.isoformat()})  # type: ignore[union-attr]
            elif it.status in engine.OPEN and (d := engine.due_range(w, it)) and day_s <= d["med"] < day_e:
                today.append({"id": it.id, "text": it.text, "due": d["med"].isoformat()})
            elif it.status in engine.OPEN and it.plan and it.plan.precision in ("slot", "part") and it.plan.start \
                    and day_s <= it.plan.start < day_e:
                today.append({"id": it.id, "text": it.text, "plan": it.plan.model_dump(mode="json")})
        return {
            "now": local.isoformat(),
            "today": today,
            "pressure": {k: {kk: v[kk] for kk in ("opt", "med", "pes", "source", "available_h")}
                         for k, v in sig["pressure"].items()},
            "tension": sig["tension"],
            "top": [i for i in sig["items"] if i["kind"] != "fixed"][:5],
            "notes": [{"name": n["name"], "content": n["content"]} for n in self.store.notes()],
        }


def display_value(v: Value, unit: str = "") -> str:
    """Non-stated values are shown with a leading ``~``."""
    s = f"{v.v:g}{unit}"
    if v.range and v.range[0] != v.range[1]:
        s += f" ({v.range[0]:g}–{v.range[1]:g}{unit})"
    return s if v.source == "stated" else "~" + s
