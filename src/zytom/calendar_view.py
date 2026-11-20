"""秘書日曆 (Plan.md「秘書日曆」): private calendar built from Items.

Layers
  fixed      fixed Items (external calendars, user)
  plan       Items with a plan: slot → timed block, part → half-day block, range → band
  deadline   due dates of open Items — a marker, never occupies time
  recurring  instantiated occurrences plus *estimated* future occurrences
             (dashed) and habit targets for the current period

Views: day / week / month / list. Estimated content keeps the ``~`` mark.
External calendars are read-only inputs; nothing here writes anywhere.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, time, timedelta
from typing import Iterable

from . import engine
from .models import Item
from .recurrence import future_occurrences, occurrence_span
from .timeutil import period_bounds, tz

LAYERS = ("fixed", "plan", "deadline", "recurring")
VIEWS = ("day", "week", "month", "list")
PART_HOURS = {"morning": (8, 12), "afternoon": (13, 18), "evening": (18, 23)}


def view_range(view: str, anchor: date, tzname: str, list_days: int = 14) -> tuple[datetime, datetime]:
    z = tz(tzname)
    if view == "day":
        s = anchor
        e = s + timedelta(days=1)
    elif view == "week":
        s = anchor - timedelta(days=anchor.weekday())
        e = s + timedelta(days=7)
    elif view == "month":
        first = anchor.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        s = first - timedelta(days=first.weekday())  # 補滿整週，方便畫月曆格
        e = nxt + timedelta(days=(7 - nxt.weekday()) % 7)
    elif view == "list":
        s = anchor
        e = s + timedelta(days=list_days)
    else:
        raise ValueError(f"view must be one of {VIEWS}")
    return datetime.combine(s, time(), z), datetime.combine(e, time(), z)


def _overlaps(s: datetime, e: datetime, ws: datetime, we: datetime) -> bool:
    return s < we and e > ws


def _entry(layer, it: Item, start, end, display, estimated=False, dashed=False, label=None, **extra) -> dict:
    text = label or it.text
    return {
        "layer": layer,
        "item_id": it.id,
        "text": text,
        "label": ("~" if estimated else "") + text,
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "display": display,  # block / band / marker
        "estimated": estimated,
        "dashed": dashed,
        "status": it.status,
        "category": it.category,
        **extra,
    }


def build(world: engine.World, view: str = "week", anchor: date | str | None = None,
          layers: Iterable[str] | None = None, list_days: int = 14) -> dict:
    tzname = world.tz
    z = tz(tzname)
    if anchor is None:
        anchor = world.now.astimezone(z).date()
    elif isinstance(anchor, str):
        anchor = date.fromisoformat(anchor[:10])
    layers = [layer for layer in (layers or LAYERS) if layer in LAYERS]
    ws, we = view_range(view, anchor, tzname, list_days)
    est = world.estimator
    entries: list[dict] = []

    for it in world.items:
        if it.status == "dropped":
            continue
        t = it.time
        instance = bool(it.meta.get("instance_of"))
        # fixed / recurring instances
        if t.type == "fixed" and t.start and t.end and _overlaps(t.start, t.end, ws, we):
            layer = "recurring" if instance else "fixed"
            if layer in layers:
                entries.append(_entry(layer, it, t.start, t.end, "band" if t.allday else "block",
                                      allday=t.allday, source=it.meta.get("source") or "user"))
        # deadline marker
        if "deadline" in layers or ("recurring" in layers and instance):
            d = engine.due_range(world, it)
            if d and ws <= d["med"] < we:
                layer = "recurring" if instance else "deadline"
                if layer in layers:
                    extra = {}
                    if it.status in engine.OPEN:
                        e = est.effort(it)
                        extra["effort"] = {"v": e.v, "range": e.range, "source": e.source, "basis": e.basis}
                    entries.append(_entry(layer, it, d["med"], None, "marker", **extra,
                                          due_earliest=d["pes"].isoformat() if d["pes"] != d["med"] else None))
        # plan layer
        p = it.plan
        if "plan" in layers and p and p.precision in ("slot", "part", "range") and p.start and it.status != "done":
            pe = p.end or (p.start + timedelta(hours=1))
            if _overlaps(p.start, pe, ws, we):
                e = est.effort(it) if it.status in engine.OPEN else None
                display = {"slot": "block", "part": "block", "range": "band"}[p.precision]
                entries.append(_entry("plan", it, p.start, pe, display, estimated=bool(e and e.estimated),
                                      precision=p.precision, plan_label=p.label,
                                      effort={"v": e.v, "range": e.range, "source": e.source} if e else None))
        # recurring rules: estimated future occurrences (not yet instantiated) → dashed
        if "recurring" in layers and t.type == "recurring" and it.status in engine.OPEN:
            for occ in future_occurrences(it, world.items, max(ws, world.now), we, tzname):
                s, e2 = occurrence_span(it, occ)
                if t.as_ == "deadline":
                    entries.append(_entry("recurring", it, e2, None, "marker", estimated=True, dashed=True,
                                          occurrence=occ.isoformat()))
                else:
                    entries.append(_entry("recurring", it, s, e2 if e2 > s else s + timedelta(hours=1), "block",
                                          estimated=True, dashed=True, occurrence=occ.isoformat()))
        # habits: current-period target as a band
        if "recurring" in layers and t.type == "habit" and it.status in engine.OPEN:
            cursor = ws
            while cursor < we:
                ps, pe2 = period_bounds(cursor, t.per, tzname)  # type: ignore[arg-type]
                done = sum(1 for log in world.logs_for(it.id) if log.kind == "done" and ps <= log.ts < pe2)
                future = ps > world.now
                entries.append(_entry("recurring", it, max(ps, ws), min(pe2, we), "band",
                                      estimated=future, dashed=future,
                                      label=f"{it.text} {done}/{t.count}", habit={"done": done, "target": t.count}))
                cursor = pe2

    entries.sort(key=lambda x: (x["start"], {"band": 0, "block": 1, "marker": 2}[x["display"]]))
    days = []
    d = ws
    while d < we:
        nd = d + timedelta(days=1)
        idx = []
        for i, e in enumerate(entries):
            s = datetime.fromisoformat(e["start"])
            en = datetime.fromisoformat(e["end"]) if e["end"] else s
            if (s < nd and en > d) or (d <= s < nd):
                idx.append(i)
        days.append({"date": d.date().isoformat(), "entries": idx})
        d = nd
    return {
        "view": view,
        "anchor": anchor.isoformat(),
        "start": ws.isoformat(),
        "end": we.isoformat(),
        "tz": tzname,
        "layers": layers,
        "entries": entries,
        "days": days,
        "legend": {"~": "預估值（非使用者所說）", "dashed": "未實例化的週期期次或未來期間"},
    }


# ------------------------------------------------------------------ HTML

_CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--mute:#6b7280;--line:#e5e2da;--fixed:#3b6ea5;--plan:#3f8f5b;--deadline:#b4462f;--recurring:#7a5aa6}
@media (prefers-color-scheme:dark){:root{--bg:#17181a;--fg:#e8e6e1;--mute:#9a9893;--line:#2c2d30}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,"Noto Sans TC",sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0 12px 0 0}header a,header button{color:inherit;text-decoration:none;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:none;cursor:pointer;font:inherit}
header a.on{background:var(--fg);color:var(--bg)}label{display:inline-flex;gap:4px;align-items:center;color:var(--mute)}
.grid{display:grid;gap:1px;background:var(--line);border-bottom:1px solid var(--line)}
.day{background:var(--bg);min-height:110px;padding:6px}.day.out{opacity:.45}.day.today .dn{background:var(--deadline);color:#fff;border-radius:10px;padding:0 6px}
.dn{font-size:12px;color:var(--mute);margin-bottom:4px;display:inline-block}
.e{font-size:12px;border-left:3px solid;padding:1px 6px;margin:2px 0;border-radius:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.e.fixed{border-color:var(--fixed);background:color-mix(in srgb,var(--fixed) 12%,transparent)}
.e.plan{border-color:var(--plan);background:color-mix(in srgb,var(--plan) 12%,transparent)}
.e.deadline{border-color:var(--deadline)}.e.deadline::before{content:"⚑ ";color:var(--deadline)}
.e.recurring{border-color:var(--recurring);background:color-mix(in srgb,var(--recurring) 10%,transparent)}
.e.dashed{border-left-style:dashed;background:none;outline:1px dashed color-mix(in srgb,var(--recurring) 60%,transparent)}
.e.band{font-weight:600}.e.done{text-decoration:line-through;opacity:.6}.t{color:var(--mute);margin-right:4px}
.list{max-width:860px;margin:0 auto;padding:8px 16px}.list h3{font-size:13px;color:var(--mute);margin:16px 0 4px}
.hide-fixed .fixed,.hide-plan .plan,.hide-deadline .deadline,.hide-recurring .recurring{display:none}
.wh{background:var(--bg);text-align:center;font-size:12px;color:var(--mute);padding:4px}
"""

_LAYER_NAMES = {"fixed": "固定行程", "plan": "規劃", "deadline": "截止", "recurring": "週期與習慣"}
_WEEKDAYS = "一二三四五六日"


def _hm(iso: str, z) -> str:
    return datetime.fromisoformat(iso).astimezone(z).strftime("%H:%M")


def _entry_html(e: dict, z, show_date: bool = False) -> str:
    cls = ["e", e["layer"], e["display"]]
    if e["dashed"]:
        cls.append("dashed")
    if e["status"] == "done":
        cls.append("done")
    when = ""
    if e["display"] == "block":
        when = _hm(e["start"], z) + ("–" + _hm(e["end"], z) if e["end"] else "")
    elif e["display"] == "marker":
        when = _hm(e["start"], z)
    if show_date:
        when = datetime.fromisoformat(e["start"]).astimezone(z).strftime("%m/%d ") + when
    tip = e["label"]
    if e.get("effort"):
        ef = e["effort"]
        rng = f" ({ef['range'][0]:g}–{ef['range'][1]:g})" if ef.get("range") else ""
        tip += f"\n工時 {'~' if ef['source'] != 'stated' else ''}{ef['v']:g}h{rng} · {ef['source']}"
        if ef.get("basis"):
            tip += f"\n依據：{ef['basis']}"
    t = f'<span class="t">{html.escape(when)}</span>' if when else ""
    return f'<div class="{" ".join(cls)}" title="{html.escape(tip)}">{t}{html.escape(e["label"])}</div>'


def render_html(cal: dict, base_url: str = "/calendar", now: datetime | None = None) -> str:
    z = tz(cal["tz"])
    view = cal["view"]
    anchor = date.fromisoformat(cal["anchor"])
    step = {"day": 1, "week": 7, "month": 31, "list": 14}[view]
    prev_a = (anchor - timedelta(days=step)).replace(day=1) if view == "month" else anchor - timedelta(days=step)
    next_a = (anchor.replace(day=1) + timedelta(days=32)).replace(day=1) if view == "month" else anchor + timedelta(days=step)
    today = (now or datetime.now(z)).astimezone(z).date()
    layers_q = ",".join(cal["layers"])
    nav = "".join(
        f'<a class="{"on" if v == view else ""}" href="{base_url}?view={v}&date={anchor}&layers={layers_q}">'
        f'{ {"day": "日", "week": "週", "month": "月", "list": "列表"}[v] }</a>' for v in VIEWS)
    toggles = "".join(
        f'<label><input type="checkbox" data-layer="{k}" checked>{v}</label>' for k, v in _LAYER_NAMES.items()
        if k in cal["layers"])
    title = {"day": f"{anchor:%Y-%m-%d}", "week": f"{anchor:%Y} 第 {anchor.isocalendar()[1]} 週",
             "month": f"{anchor:%Y 年 %m 月}", "list": f"{anchor:%Y-%m-%d} 起"}[view]
    entries = cal["entries"]
    if view == "list":
        parts = []
        for d in cal["days"]:
            if not d["entries"]:
                continue
            dd = date.fromisoformat(d["date"])
            parts.append(f"<h3>{dd:%m/%d}（{_WEEKDAYS[dd.weekday()]}）</h3>")
            parts += [_entry_html(entries[i], z) for i in d["entries"]]
        body = f'<div class="list">{"".join(parts) or "<p>沒有項目</p>"}</div>'
    else:
        cols = 1 if view == "day" else 7
        head = "" if view == "day" else "".join(f'<div class="wh">{w}</div>' for w in _WEEKDAYS)
        cells = []
        for d in cal["days"]:
            dd = date.fromisoformat(d["date"])
            cls = ["day"]
            if view == "month" and dd.month != anchor.month:
                cls.append("out")
            if dd == today:
                cls.append("today")
            label = f"{dd:%m/%d}" if view != "month" else str(dd.day)
            cells.append(f'<div class="{" ".join(cls)}"><span class="dn">{label}</span>'
                         + "".join(_entry_html(entries[i], z) for i in d["entries"]) + "</div>")
        body = f'<div class="grid" style="grid-template-columns:repeat({cols},1fr)">{head}{"".join(cells)}</div>'
    script = """
document.querySelectorAll('input[data-layer]').forEach(cb=>{
  const k='zytom-layer-'+cb.dataset.layer; if(localStorage.getItem(k)==='0'){cb.checked=false;document.body.classList.add('hide-'+cb.dataset.layer)}
  cb.addEventListener('change',()=>{document.body.classList.toggle('hide-'+cb.dataset.layer,!cb.checked);localStorage.setItem(k,cb.checked?'1':'0')})});
"""
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>秘書日曆 · {html.escape(title)}</title>
<style>{_CSS}</style></head><body>
<header><h1>秘書日曆</h1>
<a href="{base_url}?view={view}&date={prev_a}&layers={layers_q}">‹</a>
<strong>{html.escape(title)}</strong>
<a href="{base_url}?view={view}&date={next_a}&layers={layers_q}">›</a>
<a href="{base_url}?view={view}&layers={layers_q}">今天</a>
<span style="flex:1"></span>{nav}<span style="width:12px"></span>{toggles}
</header>{body}
<p style="color:var(--mute);font-size:12px;padding:0 16px">~ 為預估值（滑鼠停留可看依據）；虛線為未實例化的週期期次。外部行事曆唯讀匯入，不會寫回。</p>
<script>{script}</script>
<script type="application/json" id="data">{html.escape(json.dumps({"start": cal["start"], "end": cal["end"]}))}</script>
</body></html>"""


def render_text(cal: dict) -> str:
    z = tz(cal["tz"])
    lines = [f"{cal['view']} {cal['start'][:10]} → {cal['end'][:10]}  圖層: {', '.join(cal['layers'])}"]
    for d in cal["days"]:
        if not d["entries"]:
            continue
        dd = date.fromisoformat(d["date"])
        lines.append(f"\n{dd:%m/%d}（{_WEEKDAYS[dd.weekday()]}）")
        for i in d["entries"]:
            e = cal["entries"][i]
            if e["display"] == "block":
                when = f"{_hm(e['start'], z)}–{_hm(e['end'], z)}"
            elif e["display"] == "marker":
                when = f"⚑ {_hm(e['start'], z)}"
            else:
                when = "整段"
            lines.append(f"  {when:<13} [{_LAYER_NAMES[e['layer']]}]{' ┄' if e['dashed'] else ''} {e['label']}")
    return "\n".join(lines)
