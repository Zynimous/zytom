"""Time helpers: tz-aware parsing and interval arithmetic."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]

DEFAULT_TZ = "Asia/Taipei"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or DEFAULT_TZ)


def parse_dt(s: str | datetime | date, tzname: str | None = None) -> datetime:
    """Parse ISO text; naive values are interpreted in the user's timezone."""
    if isinstance(s, datetime):
        dt = s
    elif isinstance(s, date):
        dt = datetime.combine(s, time(23, 59))
    else:
        s = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz(tzname))
    return dt


def hours(td: timedelta) -> float:
    return td.total_seconds() / 3600.0


def merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(base: list[Interval], cut: list[Interval]) -> list[Interval]:
    """base − cut, both arbitrary interval lists."""
    result = merge(base)
    for cs, ce in merge(cut):
        nxt: list[Interval] = []
        for s, e in result:
            if ce <= s or cs >= e:
                nxt.append((s, e))
                continue
            if cs > s:
                nxt.append((s, cs))
            if ce < e:
                nxt.append((ce, e))
        result = nxt
    return result


def clip(intervals: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    return [(max(s, start), min(e, end)) for s, e in intervals if e > start and s < end]


def measure(intervals: list[Interval]) -> float:
    return sum(hours(e - s) for s, e in merge(intervals))


def overlap_h(a: Interval, b: Interval) -> float:
    s, e = max(a[0], b[0]), min(a[1], b[1])
    return max(0.0, hours(e - s))


def daily_blocks(start: datetime, end: datetime, spans: list[tuple[str, str]], tzname: str) -> list[Interval]:
    """Expand daily local-time spans like ("23:30","07:30") over [start, end]."""
    z = tz(tzname)
    out: list[Interval] = []
    day = start.astimezone(z).date() - timedelta(days=1)
    last = end.astimezone(z).date()
    while day <= last:
        for a, b in spans:
            ta, tb = time.fromisoformat(a), time.fromisoformat(b)
            s = datetime.combine(day, ta, z)
            e = datetime.combine(day if tb > ta else day + timedelta(days=1), tb, z)
            out.append((s, e))
        day += timedelta(days=1)
    return clip(merge(out), start, end)


def period_bounds(at: datetime, per: str, tzname: str) -> Interval:
    """Calendar period (day / ISO week from Monday / month) containing ``at``."""
    z = tz(tzname)
    local = at.astimezone(z)
    d = local.date()
    if per == "day":
        s = d
        e = d + timedelta(days=1)
    elif per == "week":
        s = d - timedelta(days=d.weekday())
        e = s + timedelta(days=7)
    elif per == "month":
        s = d.replace(day=1)
        e = (s + timedelta(days=32)).replace(day=1)
    else:
        raise ValueError(per)
    return datetime.combine(s, time(), z), datetime.combine(e, time(), z)
