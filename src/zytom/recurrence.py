"""Recurring items: lazy instantiation and startup catch-up (Plan.md「週期實例化」)."""

from __future__ import annotations

from datetime import datetime, timedelta

from dateutil.rrule import rrulestr

from .models import Item, TimeSpec
from .store import Store, new_id
from .timeutil import now_utc, tz


def occurrences(rule: Item, start: datetime, end: datetime, tzname: str) -> list[datetime]:
    t = rule.time
    assert t.type == "recurring" and t.rrule and t.dtstart
    dtstart = t.dtstart.astimezone(tz(tzname))
    r = rrulestr(t.rrule, dtstart=dtstart)
    return [o for o in r.between(start, end, inc=True)]


def next_occurrence(rule: Item, after: datetime, tzname: str) -> datetime | None:
    t = rule.time
    r = rrulestr(t.rrule, dtstart=t.dtstart.astimezone(tz(tzname)))  # type: ignore[union-attr]
    return r.after(after, inc=False)


def occurrence_span(rule: Item, occ: datetime) -> tuple[datetime, datetime]:
    dur = rule.time.duration_h or 0.0
    return occ, occ + timedelta(hours=dur)


def instance_time(rule: Item, occ: datetime) -> TimeSpec:
    s, e = occurrence_span(rule, occ)
    if rule.time.as_ == "deadline":
        return TimeSpec(type="deadline", due=e)
    return TimeSpec(type="fixed", start=s, end=e if e > s else s + timedelta(hours=1))


def instances_of(items: list[Item], rule_id: str) -> list[Item]:
    return sorted(
        (i for i in items if i.meta.get("instance_of") == rule_id),
        key=lambda i: i.meta.get("occurrence", ""),
    )


def make_instance(rule: Item, occ: datetime, now: datetime) -> Item:
    return Item(
        id=new_id("r"),
        text=rule.text,
        parent=rule.parent,
        category=rule.category,
        time=instance_time(rule, occ),
        effort=rule.effort,
        commitment=rule.commitment,
        created=now,
        updated=now,
        meta={"instance_of": rule.id, "occurrence": occ.isoformat()},
    )


def materialize(store: Store, now: datetime | None = None, tzname: str = "Asia/Taipei") -> dict:
    """Create due instances and record missed periods.

    Lazy rule: the instance for the next occurrence is created only once the
    latest existing instance's period is over (Wednesday's instance → on
    Thursday the next Wednesday's instance appears). Occurrences that ended
    while the service was down and never got an instance are logged as
    ``skipped`` with ``missed: true``.
    """
    now = now or now_utc()
    items = store.items()
    created, missed = [], []
    for rule in items:
        if rule.time.type != "recurring" or rule.status in ("done", "dropped"):
            continue
        insts = instances_of(items, rule.id)
        if insts:
            last_occ = datetime.fromisoformat(insts[-1].meta["occurrence"])
            if occurrence_span(rule, last_occ)[1] > now:
                continue  # current period still running
            cursor = last_occ
        else:
            # first instance: the occurrence currently running or the next one
            cursor = rule.time.dtstart - timedelta(seconds=1)  # type: ignore[operator]
            cursor = max(cursor, rule.created - timedelta(seconds=1))
        while True:
            occ = next_occurrence(rule, cursor, tzname)
            if occ is None:
                break
            if occurrence_span(rule, occ)[1] <= now and insts:
                store.append("skipped", rule.id, {"missed": True, "occurrence": occ.isoformat()}, commit=False)
                missed.append({"rule": rule.id, "occurrence": occ.isoformat()})
                cursor = occ
                continue
            if occurrence_span(rule, occ)[1] <= now:
                cursor = occ  # before first ever instance: nothing to miss yet
                continue
            inst = make_instance(rule, occ, now)
            store.save_item(inst, commit=False)
            store.append("created", inst.id, {"from_rule": rule.id, "occurrence": occ.isoformat()}, commit=False)
            created.append(inst.id)
            break
    store.db.commit()
    return {"created": created, "missed": missed}


def future_occurrences(rule: Item, items: list[Item], start: datetime, end: datetime, tzname: str) -> list[datetime]:
    """Occurrences in [start, end] that have no instance yet — estimated load."""
    have = {i.meta.get("occurrence") for i in instances_of(items, rule.id)}
    return [o for o in occurrences(rule, start, end, tzname) if o.isoformat() not in have]
