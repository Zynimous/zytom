"""Read-only import of external calendars (ICS) into Items.

External calendars are never written back. Re-importing updates only the
fields the source owns (text, time, source meta); anything the user or agent
set — effort, plan, status, commitment — is kept.

Two adapters:

* **TronClass** feeds (``PRODID`` mentions TronClass): each activity is split
  into ``…-start`` / ``…-deadline`` / ``…-end`` marker events. They are
  grouped back into one ``deadline`` Item per activity (homework, exam,
  questionnaire by default; material / page are reading material, skipped
  unless asked for).
* **Generic** calendars: timed events → ``fixed`` Items, all-day events →
  all-day ``fixed`` (shown, but not subtracted from free time), RRULEs
  expanded inside the import horizon.
"""

from __future__ import annotations

import re
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

from dateutil.rrule import rrulestr
from icalendar import Calendar

from .models import Item, TimeSpec
from .store import Store, new_id
from .timeutil import now_utc, parse_dt

TRONCLASS_TYPES = {
    "homework": "作業",
    "exam": "測驗",
    "questionnaire": "問卷",
    "material": "教材",
    "page": "頁面",
}
TRONCLASS_DEFAULT_TYPES = ("homework", "exam", "questionnaire")
_MARKER = re.compile(r"^(?P<base>.+)-(?P<marker>start|end|deadline)$")
_FIELD = re.compile(r"^(?P<k>[^：:\n]{1,8})[：:](?P<v>.*)$")
_TITLE_SUFFIX = re.compile(r"\s*·\s*(開始|截止|結束)\s*$")
_COURSE_PREFIX = re.compile(r"^\[(?P<course>[^\]]+)\]\s*")


def fetch(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "zytom-ics/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — user-configured URL
        return r.read()


def _dt(value, tzname: str) -> datetime:
    if isinstance(value, datetime):
        return parse_dt(value, tzname)
    if isinstance(value, date):
        return parse_dt(datetime(value.year, value.month, value.day), tzname)
    return parse_dt(str(value), tzname)


def _desc_fields(desc: str) -> dict[str, str]:
    out = {}
    for line in desc.splitlines():
        m = _FIELD.match(line.strip())
        if m and m["k"] not in out:
            out[m["k"].strip()] = m["v"].strip()
    return out


def _opt_dt(s: str | None, tzname: str) -> datetime | None:
    if not s or s in ("—", "-"):
        return None
    try:
        return parse_dt(s, tzname)
    except ValueError:
        return None


# --------------------------------------------------------------- adapters

def parse_tronclass(cal: Calendar, tzname: str, include_types=TRONCLASS_DEFAULT_TYPES) -> list[dict]:
    groups: dict[str, dict[str, Any]] = {}
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", "")).split("@")[0]
        m = _MARKER.match(uid)
        base, marker = (m["base"], m["marker"]) if m else (uid, "start")
        g = groups.setdefault(base, {"markers": {}})
        g["markers"][marker] = _dt(ev.decoded("DTSTART"), tzname)
        desc = str(ev.get("DESCRIPTION", ""))
        g.setdefault("fields", _desc_fields(desc))
        g.setdefault("summary", str(ev.get("SUMMARY", "")))
        g.setdefault("url", str(ev.get("URL", "")) or None)
    records = []
    for base, g in groups.items():
        f = g["fields"]
        kind = re.search(r"\((\w+)\)", f.get("類型", ""))
        kind = kind.group(1) if kind else "unknown"
        if include_types and kind not in include_types:
            continue
        title = _TITLE_SUFFIX.sub("", g["summary"])
        cm = _COURSE_PREFIX.match(title)
        course = f.get("課程") or (cm["course"] if cm else None)
        name = _COURSE_PREFIX.sub("", title)
        start = _opt_dt(f.get("開始"), tzname) or g["markers"].get("start")
        due = (_opt_dt(f.get("截止"), tzname) or g["markers"].get("deadline")
               or _opt_dt(f.get("結束"), tzname) or g["markers"].get("end"))
        if due is None:
            continue  # nothing to plan against
        records.append({
            "external_id": f"ics:{base}",
            "text": f"{name}（{course}）" if course else name,
            "category": TRONCLASS_TYPES.get(kind, kind),
            "time": TimeSpec(type="deadline", due=due, start=start if start and start < due else None),
            "meta": {"source": "ics", "adapter": "tronclass", "course": course, "ics_type": kind,
                     "url": g["url"], "external_id": f"ics:{base}"},
        })
    return records


def parse_generic(cal: Calendar, tzname: str, now: datetime, horizon_days: int = 120) -> list[dict]:
    records = []
    lo, hi = now - timedelta(days=1), now + timedelta(days=horizon_days)
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", "")) or new_id("ev")
        raw_start = ev.decoded("DTSTART")
        allday = not isinstance(raw_start, datetime)
        start = _dt(raw_start, tzname)
        if allday:
            start = start.replace(hour=0, minute=0)
        if ev.get("DTEND") is not None:
            end = _dt(ev.decoded("DTEND"), tzname)
            if allday:
                end = end.replace(hour=0, minute=0)
        elif ev.get("DURATION") is not None:
            end = start + ev.decoded("DURATION")
        else:
            end = start + (timedelta(days=1) if allday else timedelta(hours=1))
        if end <= start:
            end = start + timedelta(minutes=1)
        transparent = str(ev.get("TRANSP", "")).upper() == "TRANSPARENT"
        dur = end - start
        starts = [start]
        if ev.get("RRULE") is not None:
            rule = ev.get("RRULE").to_ical().decode()
            starts = list(rrulestr(rule, dtstart=start).between(lo, hi, inc=True))
        for s in starts:
            occ_id = uid if len(starts) == 1 and ev.get("RRULE") is None else f"{uid}@{s.isoformat()}"
            e = s + dur
            if transparent and not allday and dur <= timedelta(minutes=1):
                time = TimeSpec(type="deadline", due=s)
            else:
                time = TimeSpec(type="fixed", start=s, end=e, allday=allday or transparent)
            records.append({
                "external_id": f"ics:{occ_id}",
                "text": str(ev.get("SUMMARY", "(無標題)")),
                "category": None,
                "time": time,
                "meta": {"source": "ics", "adapter": "generic", "location": str(ev.get("LOCATION", "")) or None,
                         "external_id": f"ics:{occ_id}"},
            })
    return records


def parse(data: bytes, tzname: str, now: datetime, include_types=None) -> tuple[str, list[dict]]:
    cal = Calendar.from_ical(data)
    prodid = str(cal.get("PRODID", ""))
    if "tronclass" in prodid.lower():
        return "tronclass", parse_tronclass(cal, tzname, include_types or TRONCLASS_DEFAULT_TYPES)
    return "generic", parse_generic(cal, tzname, now)


# ----------------------------------------------------------------- import

def _time_end(t: TimeSpec) -> datetime | None:
    return t.due if t.type == "deadline" else t.end


def import_ics(store: Store, data: bytes, name: str, tzname: str, now: datetime | None = None,
               include_types=None, include_past: bool = False, past_grace_days: float = 1.0) -> dict:
    now = now or now_utc()
    adapter, records = parse(data, tzname, now, include_types)
    return import_records(store, records, name, now, adapter, include_past, past_grace_days)


def import_records(store: Store, records: list[dict], name: str, now: datetime, adapter: str,
                   include_past: bool = False, past_grace_days: float = 1.0,
                   window_end: datetime | None = None) -> dict:
    """Upsert normalized external records (shared by ICS and Google Calendar).

    ``window_end``: the source only covered events up to this time, so items
    after it are not flagged as missing.
    """
    source = records[0]["meta"]["source"] if records else None
    stats = {"adapter": adapter, "events": len(records), "created": 0, "updated": 0, "unchanged": 0,
             "skipped_past": 0, "missing_from_feed": 0}
    seen = set()
    for rec in records:
        ext = rec["external_id"]
        seen.add(ext)
        existing = store.get_by_external(ext)
        end = _time_end(rec["time"])
        if existing is None:
            if not include_past and end is not None and end < now - timedelta(days=past_grace_days):
                stats["skipped_past"] += 1
                continue
            item = Item(id=new_id(), text=rec["text"], category=rec["category"], time=rec["time"],
                        created=now, updated=now, meta=rec["meta"] | {"calendar": name})
            store.save_item(item, commit=False)
            store.append("created", item.id, {"source": rec["meta"]["source"], "calendar": name, "external_id": ext},
                         commit=False)
            stats["created"] += 1
            continue
        changes = {}
        if existing.text != rec["text"]:
            changes["text"] = rec["text"]
        if existing.time.model_dump() != rec["time"].model_dump():
            changes["time"] = rec["time"]
        if existing.category is None and rec["category"]:
            changes["category"] = rec["category"]
        meta = existing.meta | rec["meta"] | {"calendar": name}
        meta.pop("missing_from_feed", None)
        if not changes and meta == existing.meta:
            stats["unchanged"] += 1
            continue
        updated = existing.model_copy(update=changes | {"meta": meta, "updated": now})
        store.save_item(updated, commit=False)
        if changes:
            store.append("updated", existing.id, {"source": rec["meta"]["source"], "fields": sorted(changes),
                                                   "before": {k: getattr(existing, k).model_dump(mode="json") if hasattr(getattr(existing, k), "model_dump") else getattr(existing, k) for k in changes}},
                         commit=False)
        stats["updated"] += 1
    # events that vanished from the feed are flagged, never dropped automatically
    for it in store.items(["open", "active"]):
        if it.meta.get("source") in (source, "ics") and it.meta.get("calendar") == name \
                and it.meta.get("external_id") not in seen:
            end = _time_end(it.time)
            if end and end > now and (window_end is None or end <= window_end) and not it.meta.get("missing_from_feed"):
                store.save_item(it.model_copy(update={"meta": it.meta | {"missing_from_feed": True}}), commit=False)
                stats["missing_from_feed"] += 1
    store.db.commit()
    return stats


def sync_sources(store: Store, tzname: str, now: datetime | None = None, only: str | None = None) -> list[dict]:
    """Fetch and import every source saved in the ``calendar_sources`` pref."""
    out = []
    for src in store.pref("calendar_sources", []) or []:
        if only and src["name"] != only:
            continue
        try:
            if src.get("type") == "gcal":
                from .gcal import import_gcal

                res = import_gcal(store, src["name"], src.get("calendars") or ["primary"], tzname, now)
            else:
                data = fetch(src["url"])
                res = import_ics(store, data, src["name"], tzname, now, src.get("include_types"),
                                 src.get("include_past", False))
            out.append({"name": src["name"], "ok": True, **res})
        except Exception as e:  # network / parse errors are reported, not raised
            out.append({"name": src["name"], "ok": False, "error": f"{type(e).__name__}: {e}"})
    return out
