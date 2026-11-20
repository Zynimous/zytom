from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from zytom import engine
from zytom.alerts import tick
from zytom.ics import import_ics
from zytom.models import Value, weakest
from zytom.observe import daily_stats
from zytom.recurrence import materialize
from zytom.service import Secretary
from zytom.store import Store
from zytom.timeutil import measure, parse_dt, subtract

FIXTURE = Path(__file__).parent / "fixtures" / "tronclass_sample.ics"
NOW = parse_dt("2026-11-21T10:00", "Asia/Taipei")  # Saturday


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def sec(clock):
    s = Store(":memory:")
    # simple, predictable day: sleep 00:00–08:00, no overhead
    s.set_pref("reserved", [["00:00", "08:00"]])
    s.set_pref("overhead_h", 0)
    return Secretary(s, clock)


def add(sec, **kw):
    return sec.upsert(kw)["item"]["id"]


# ------------------------------------------------------------------ model

def test_value_range_and_weakest():
    v = Value(v=4, range=(6, 3), source="llm", basis="x")
    assert (v.lo, v.hi) == (3, 6)
    assert Value(v=2).range is None and Value(v=2).lo == 2
    assert weakest(["stated", "llm", "stat"]) == "llm"
    assert weakest(["stated", "default"]) == "default"


def test_intervals():
    a = parse_dt("2026-11-21T00:00")
    b = a + timedelta(hours=10)
    free = subtract([(a, b)], [(a + timedelta(hours=2), a + timedelta(hours=3)), (a + timedelta(hours=9), b + timedelta(hours=5))])
    assert measure(free) == pytest.approx(8)


# ------------------------------------------------------------------ store

def test_log_is_append_only(sec):
    sec.store.append("said", None, {"text": "好累"})
    with pytest.raises(sqlite3.IntegrityError):
        sec.store.db.execute("UPDATE log SET kind='done'")
    with pytest.raises(sqlite3.IntegrityError):
        sec.store.db.execute("DELETE FROM log")


def test_upsert_creates_logs_and_correction(sec):
    iid = add(sec, text="報告", category="報告", time={"type": "deadline", "due": "2026-11-23T23:59"},
              effort={"v": 6, "range": [4, 9], "source": "llm", "basis": "類似報告"})
    sec.upsert({"id": iid, "effort": 2})  # 使用者隨口修正
    kinds = [log.kind for log in sec.store.logs(item_id=iid)]
    assert kinds == ["created", "estimate_corrected"]
    assert sec.store.get_item(iid).effort.source == "stated"


def test_llm_estimate_requires_range_and_basis(sec):
    with pytest.raises(ValueError):
        add(sec, text="x", effort={"v": 3, "source": "llm"})


def test_default_and_stat_estimates(sec):
    iid = add(sec, text="作業", category="作業", time={"type": "deadline", "due": "2026-11-28T23:59"})
    e = sec.world().estimator.effort(sec.store.get_item(iid))
    assert e.source == "default"
    for h in (1.0, 2.0, 3.0):
        did = add(sec, text="舊作業", category="作業")
        sec.log_event("done", did, {"actual_h": h})
    e = sec.world().estimator.effort(sec.store.get_item(iid))
    assert e.source == "stat" and e.v == 2.0


# ----------------------------------------------------------------- engine

def test_slack_and_pressure(sec):
    # Sat 10:00 → Mon 23:59 with sleep 00–08: Sat 14h, Sun 16h, Mon 16h = 46h
    add(sec, text="A", time={"type": "deadline", "due": "2026-11-23T23:59:59"}, effort=10)
    add(sec, text="B", time={"type": "deadline", "due": "2026-11-22T23:59:59"}, effort=5)
    sig = sec.signals()
    items = {i["text"]: i for i in sig["items"]}
    assert items["B"]["available_h"] == pytest.approx(30, abs=0.01)
    assert items["B"]["slack_h"]["med"] == pytest.approx(25, abs=0.01)
    # EDF: A must share time with B
    assert items["A"]["slack_h"]["med"] == pytest.approx(46 - 15, abs=0.01)
    p3 = sig["pressure"]["3d"]
    assert p3["source"] == "stated" and p3["estimated"] is False
    assert p3["required_h"]["med"] == pytest.approx(15, abs=0.01)


def test_estimated_inputs_propagate_and_ask(sec):
    add(sec, text="大報告", category="報告", time={"type": "deadline", "due": "2026-11-22T23:59"},
        effort={"v": 14, "range": [10, 30], "source": "llm", "basis": "猜的"})
    sig = sec.signals()
    assert sig["pressure"]["3d"]["source"] == "llm"
    t = sig["tension"]
    assert t["level"] == "ask"
    assert t["key_estimate"]["text"] == "大報告"


def test_warn_when_stated(sec):
    add(sec, text="趕工", time={"type": "deadline", "due": "2026-11-22T23:59"}, effort=28)
    assert sec.signals()["tension"]["level"] == "warn"


def test_fixed_reduces_availability(sec):
    before = sec.signals()["pressure"]["3d"]["available_h"]
    add(sec, text="打工", time={"type": "fixed", "start": "2026-11-21T13:00", "end": "2026-11-21T18:00"})
    assert sec.signals()["pressure"]["3d"]["available_h"] == pytest.approx(before - 5, abs=0.01)


def test_habit_and_project_signals(sec, clock):
    clock.t = parse_dt("2026-11-16T09:00")  # Monday: habit created at period start
    h = add(sec, text="跑步", time={"type": "habit", "count": 3, "per": "week"}, effort=0.5)
    proj = add(sec, text="畢業專題", meta={"project": True})
    clock.t = NOW
    sec.log_event("done", h)
    items = {i["id"]: i for i in sec.signals()["items"]}
    assert items[h]["behind"] == 2 and items[h]["catchable"] is True
    assert sec.store.get_item(h).status == "open"  # 習慣不會被 done 關掉
    assert items[proj]["kind"] == "project" and items[proj]["stagnant_days"] > 4


def test_anchored(sec):
    exam = add(sec, text="期中考", time={"type": "fixed", "start": "2026-11-24T10:00", "end": "2026-11-24T12:00"})
    rev = add(sec, text="複習", time={"type": "anchored", "target": exam, "relation": "before", "offset_h": 12}, effort=4)
    items = {i["id"]: i for i in sec.signals()["items"]}
    assert parse_dt(items[rev]["due"]) == parse_dt("2026-11-23T22:00")


def test_simulate_does_not_write(sec):
    a = add(sec, text="A", time={"type": "deadline", "due": "2026-11-22T20:00"}, effort=6)
    add(sec, text="B", time={"type": "deadline", "due": "2026-11-22T22:00"}, effort=6)
    before_logs = len(sec.store.logs())
    res = sec.simulate([{"op": "defer", "item_id": a, "until": "2026-11-22T12:00"}])
    assert len(sec.store.logs()) == before_logs
    assert sec.store.get_item(a).plan is None
    assert any(w["id"] == a for w in res["slack_worse"])


def test_candidates_fit(sec):
    small = add(sec, text="小作業", time={"type": "deadline", "due": "2026-11-23T23:59"}, effort=0.5)
    big = add(sec, text="大作業", time={"type": "deadline", "due": "2026-11-23T23:59"}, effort=5)
    rec = sec.recommend("2026-11-21T14:00", "2026-11-21T15:00")["recommendations"]
    fits = {r["id"]: r["fit"] for r in rec}
    assert fits[small] == "full" and fits.get(big) == "partial"


def test_relieve_and_apply(sec):
    must = add(sec, text="期末報告", time={"type": "deadline", "due": "2026-11-23T23:59"}, effort=30)
    opt = add(sec, text="社團佈置", time={"type": "deadline", "due": "2026-11-22T18:00"}, effort=6,
              commitment={"level": "droppable", "cost": 1})
    res = sec.relieve()
    assert res["options"], res
    ops = [(c["op"], c["item_id"]) for o in res["options"] for c in o["changes"]]
    assert ("drop", must) not in ops  # must 不會被提議推掉
    assert any(op == "drop" and iid == opt for op, iid in ops)
    with pytest.raises(ValueError):
        sec.apply([{"op": "drop", "item_id": opt}])
    sec.apply([{"op": "drop", "item_id": opt}], confirmed=True)
    assert sec.store.get_item(opt).status == "dropped"
    assert sec.store.logs(item_id=opt, kinds=["dropped"])


def test_apply_split_and_set(sec):
    a = add(sec, text="論文", time={"type": "deadline", "due": "2026-12-20T23:59"}, effort=10)
    out = sec.apply([{"op": "split", "item_id": a, "parts": [{"fraction": 0.5}, {"fraction": 0.5}]}], confirmed=True)
    kids = out["applied"][0]["touched"][1:]
    assert len(kids) == 2 and all(sec.store.get_item(k).parent == a for k in kids)
    sec.apply([{"op": "set", "item_id": kids[0], "fields": {"effort": {"v": 7, "source": "stated"}}}], confirmed=True)
    assert sec.store.get_item(kids[0]).effort.v == 7


def test_log_event_refuses_commitment_changes(sec):
    a = add(sec, text="A")
    with pytest.raises(ValueError):
        sec.log_event("dropped", a)


# ------------------------------------------------------------- recurrence

def test_recurrence_lazy_and_missed(sec, clock):
    rule = add(sec, text="週會", time={"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=WE",
                                       "dtstart": "2026-11-04T19:00", "duration_h": 1})
    insts = [i for i in sec.store.items() if i.meta.get("instance_of") == rule]
    assert len(insts) == 1 and insts[0].time.start == parse_dt("2026-11-25T19:00")
    # same period: nothing new
    assert materialize(sec.store, NOW)["created"] == []
    # Thursday after: next Wednesday appears
    r = materialize(sec.store, parse_dt("2026-11-26T09:00"))
    assert len(r["created"]) == 1 and r["missed"] == []
    # long downtime: missed periods are logged
    r = materialize(sec.store, parse_dt("2026-12-18T09:00"))
    assert len(r["missed"]) == 2 and len(r["created"]) == 1
    # future occurrences count as estimated busy time
    p = sec.signals(windows=[7])["pressure"]["7d"]
    assert p["available_h"] < 7 * 16


# --------------------------------------------------------------------- ics

def test_ics_import_tronclass(sec):
    data = FIXTURE.read_bytes()
    r = import_ics(sec.store, data, "tc", "Asia/Taipei", NOW)
    assert r["adapter"] == "tronclass"
    assert r["created"] == 2 and r["skipped_past"] == 1  # material skipped, old homework skipped
    items = {i.text: i for i in sec.store.items()}
    hw = items["作業一（範例課程）"]
    assert hw.category == "作業" and hw.time.type == "deadline"
    assert hw.time.due == parse_dt("2026-11-25T15:59:00Z")
    assert "小考（範例課程）" in items
    # user plans it; re-import keeps that
    sec.upsert({"id": hw.id, "effort": 3})
    r2 = import_ics(sec.store, data, "tc", "Asia/Taipei", NOW)
    assert r2["created"] == 0 and r2["unchanged"] == 2
    assert sec.store.get_item(hw.id).effort.v == 3
    # imported coursework defaults to must
    assert sec.world().estimator.commitment(hw).level_name == "must"


# ----------------------------------------------------------------- alerts

def test_alert_dedupe_and_escalation(sec, clock):
    a = add(sec, text="急件", time={"type": "deadline", "due": "2026-11-22T12:00"}, effort=20)
    first = tick(sec.store, clock.t, notifiers=[])
    rules = {x["rule"]: x for x in first}
    assert "slack_negative" in rules and rules["tension"]["level"] == "warn"
    assert rules["tension"]["options"]["relieve"] is not None
    assert tick(sec.store, clock.t + timedelta(minutes=30), notifiers=[]) == []
    sec.upsert({"id": a, "effort": 26})
    again = tick(sec.store, clock.t + timedelta(minutes=60), notifiers=[])
    assert any(x["rule"] == "slack_negative" for x in again)


def test_threshold_learning(sec, clock):
    add(sec, text="趕工", time={"type": "deadline", "due": "2026-11-22T23:59"}, effort=28)
    alerts = tick(sec.store, clock.t, notifiers=[])
    a = next(x for x in alerts if x["rule"] == "tension")
    i = sec.store.items()[0]
    sec.log_event("done", i.id)
    sec.store.append("alert_feedback", None, {"alert_id": a["id"], "reaction": "not_needed", "items": [i.id]})
    res = daily_stats(sec.store, clock.t)
    assert res["threshold"]["v"] == pytest.approx(0.75)


def test_stats_bias_observation(sec):
    for ratio in (1.5, 1.4, 1.6):
        iid = add(sec, text="報告", category="報告", effort={"v": 2, "range": [1, 3], "source": "llm", "basis": "b"})
        sec.log_event("done", iid, {"actual_h": 2 * ratio})
    daily_stats(sec.store, NOW)
    obs = {o.id: o for o in sec.store.observations()}
    assert obs["stat:bias:報告"].params["effort_bias"] == pytest.approx(1.5)
    # once confident, llm/default estimates get corrected
    o = obs["stat:bias:報告"]
    o.confidence = 0.8
    sec.store.save_observation(o)
    new = add(sec, text="新報告", category="報告", effort={"v": 2, "range": [1, 3], "source": "llm", "basis": "b"})
    assert sec.world().estimator.effort(sec.store.get_item(new)).v == pytest.approx(3.0)


# -------------------------------------------------------------------- mcp

def test_mcp_tools_in_process(sec):
    from mcp import Client

    from zytom.server import build_server

    server = build_server(sec.store, sec.clock)

    def data(r):
        import json
        return r.structured_content if r.structured_content is not None else json.loads(r.content[0].text)

    async def run():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"item_upsert", "item_query", "log_event", "signals", "simulate", "recommend", "relieve",
                    "apply", "observations"} <= names
            r = await c.call_tool("item_upsert", {"text": "寫報告", "effort": 3,
                                                  "time": {"type": "deadline", "due": "2026-11-23T12:00"}})
            assert not r.is_error
            iid = data(r)["item"]["id"]
            r = await c.call_tool("apply", {"changes": [{"op": "drop", "item_id": iid}]})
            assert r.is_error and "confirmed" in r.content[0].text
            r = await c.call_tool("signals", {"top": 1})
            assert data(r)["items"][0]["id"] == iid
            r = await c.call_tool("notes_propose", {"name": "main.md", "content": "hi\n"})
            assert "+hi" in data(r)["diff"]
            assert sec.store.note("main.md") is None  # 只是提案，沒有寫入

    asyncio.run(run())


# ---------------------------------------------------------------- calendar

def test_calendar_layers_and_views(sec, clock):
    add(sec, text="打工", time={"type": "fixed", "start": "2026-11-22T13:00", "end": "2026-11-22T17:00"})
    hw = add(sec, text="作業", category="作業", time={"type": "deadline", "due": "2026-11-24T23:59"})
    sec.apply([{"op": "plan", "item_id": hw, "plan": {"precision": "part", "start": "2026-11-23T19:00",
                                                       "end": "2026-11-23T23:00", "label": "週一晚上"}}],
              confirmed=True)
    add(sec, text="週會", time={"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=WE",
                                "dtstart": "2026-11-04T19:00", "duration_h": 1})
    from zytom.calendar_view import build, render_html, render_text

    cal = build(sec.world(), "week", "2026-11-30")
    by_layer = {}
    for e in cal["entries"]:
        by_layer.setdefault(e["layer"], []).append(e)
    # 12/2 occurrence is not instantiated yet → estimated + dashed
    meet = [e for e in by_layer["recurring"] if e["text"] == "週會"]
    assert meet and all(e["dashed"] and e["label"].startswith("~") for e in meet)

    cal = build(sec.world(), "week", "2026-11-23")
    layers = {e["layer"] for e in cal["entries"]}
    assert {"plan", "deadline", "recurring"} <= layers
    dl = next(e for e in cal["entries"] if e["layer"] == "deadline")
    assert dl["display"] == "marker" and dl["effort"]["source"] == "default"
    only = build(sec.world(), "week", "2026-11-23", ["deadline"])
    assert {e["layer"] for e in only["entries"]} == {"deadline"}
    month = build(sec.world(), "month", "2026-11-10")
    assert len(month["days"]) % 7 == 0 and month["days"][0]["date"] == "2026-10-26"
    assert "週一晚上" not in render_text(cal) and "作業" in render_text(cal)
    assert "<html" in render_html(month, "/calendar") and "dashed" in render_html(build(sec.world(), "month", "2026-12-01"))


# --------------------------------------------------------------------- api

def test_api_rest_ws_calendar(sec, clock):
    from fastapi.testclient import TestClient

    from zytom.api import create_app

    app = create_app(sec.store, sec.clock, scheduler=False, mount_mcp=True)
    with TestClient(app, base_url="http://127.0.0.1:8780") as c:
        r = c.post("/items", json={"text": "急件", "effort": 20, "time": {"type": "deadline", "due": "2026-11-22T12:00"}})
        assert r.status_code == 200
        iid = r.json()["item"]["id"]
        assert c.post("/apply", json={"changes": [{"op": "drop", "item_id": iid}]}).status_code == 400
        assert c.get("/signals").json()["tension"]["level"] == "warn"
        assert c.get("/api/calendar", params={"view": "list"}).json()["entries"]
        assert "秘書日曆" in c.get("/calendar", params={"view": "month"}).text
        with c.websocket_connect("/ws/alerts") as ws:
            assert ws.receive_json()["type"] == "hello"
            sent = c.post("/tick").json()["alerts"]
            assert sent
            msg = ws.receive_json()
            assert msg["type"] == "alerts" and {a["rule"] for a in msg["alerts"]} >= {"slack_negative"}
        aid = sent[0]["id"]
        assert c.post(f"/alerts/{aid}/feedback", json={"reaction": "not_needed"}).status_code == 200
        h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        init = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        assert init.status_code == 200, init.text
        if sid := init.headers.get("mcp-session-id"):
            h["mcp-session-id"] = sid
        c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200 and "item_upsert" in r.text


# ------------------------------------------------------------ google cal

def test_gcal_import_readonly(sec, tmp_path):
    import json as _json
    import urllib.parse

    from zytom.gcal import SCOPE, GoogleCalendar, import_gcal

    tok = tmp_path / "tok.json"
    tok.write_text(_json.dumps({"access_token": "old", "refresh_token": "r", "expires_at": 0,
                                "client_id": "cid", "client_secret": "s"}))
    calls = []
    events = [
        {"id": "e1", "status": "confirmed", "summary": "打工",
         "start": {"dateTime": "2026-11-22T13:00:00+08:00"}, "end": {"dateTime": "2026-11-22T17:00:00+08:00"}},
        {"id": "e2", "status": "confirmed", "summary": "放假", "start": {"date": "2026-11-23"}, "end": {"date": "2026-11-24"}},
        {"id": "e3", "status": "cancelled", "summary": "取消的"},
        {"id": "e4", "status": "confirmed", "summary": "婉拒的會議",
         "start": {"dateTime": "2026-11-22T09:00:00+08:00"}, "end": {"dateTime": "2026-11-22T10:00:00+08:00"},
         "attendees": [{"email": "me@example.com", "self": True, "responseStatus": "declined"}]},
        {"id": "e5_20261126", "recurringEventId": "e5", "summary": "週會",
         "start": {"dateTime": "2026-11-26T19:00:00", "timeZone": "Asia/Taipei"},
         "end": {"dateTime": "2026-11-26T20:00:00", "timeZone": "Asia/Taipei"}},
    ]

    def fake_http(method, url, headers, form):
        calls.append((method, url, headers, form))
        if url.startswith("https://oauth2.googleapis.com/token"):
            assert form["grant_type"] == "refresh_token"
            return {"access_token": "new", "expires_in": 3600}
        assert method == "GET" and headers["Authorization"] == "Bearer new"
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["singleEvents"] == "true"
        if "pageToken" not in q:
            return {"items": events[:3], "nextPageToken": "p2"}
        return {"items": events[3:]}

    gc = GoogleCalendar(token_path=tok, http=fake_http)
    assert "readonly" in SCOPE
    r = import_gcal(sec.store, "google", ["primary"], "Asia/Taipei", NOW, client=gc)
    assert r["created"] == 3  # cancelled and declined skipped
    assert all(m == "GET" or "oauth2" in u for m, u, *_ in calls)  # 只讀取，不寫回
    items = {i.text: i for i in sec.store.items()}
    assert items["打工"].time.type == "fixed" and not items["打工"].time.allday
    assert items["放假"].time.allday
    assert items["週會"].time.start == parse_dt("2026-11-26T19:00", "Asia/Taipei")
    # busy time from Google reduces availability; all-day does not
    sig = sec.signals(windows=[3])["pressure"]["3d"]
    sec2 = Secretary(Store(":memory:"), sec.clock)
    sec2.store.set_pref("reserved", [["00:00", "08:00"]])
    sec2.store.set_pref("overhead_h", 0)
    assert sig["available_h"] == pytest.approx(sec2.signals(windows=[3])["pressure"]["3d"]["available_h"] - 4)
    # re-import keeps user edits; event gone from window gets flagged, not dropped
    sec.upsert({"id": items["打工"].id, "category": "打工"})
    events.pop(0)
    r2 = import_gcal(sec.store, "google", ["primary"], "Asia/Taipei", NOW, client=gc)
    assert r2["missing_from_feed"] == 1
    gone = sec.store.get_item(items["打工"].id)
    assert gone.status == "open" and gone.meta["missing_from_feed"] and gone.category == "打工"


def test_gcal_oauth_loopback_pkce(tmp_path, capsys):
    import base64
    import hashlib
    import json as _json
    import threading
    import time as _time
    import urllib.parse
    import urllib.request

    from zytom.gcal import SCOPE, GoogleCalendar

    secret = tmp_path / "credentials.json"
    secret.write_text(_json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    seen = {}

    def fake_http(method, url, headers, form):
        seen.update(form)
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": SCOPE}

    gc = GoogleCalendar(secret, tmp_path / "tok.json", http=fake_http)
    out = {}
    th = threading.Thread(target=lambda: out.update(gc.authorize(open_browser=False, timeout=20)))
    th.start()
    for _ in range(100):
        printed = capsys.readouterr().out
        if "https://accounts.google.com" in printed:
            break
        _time.sleep(0.05)
    url = next(line for line in printed.splitlines() if line.startswith("https://"))
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["scope"] == SCOPE and q["code_challenge_method"] == "S256"
    urllib.request.urlopen(f"{q['redirect_uri']}?code=abc&state={q['state']}").read()
    th.join(10)
    assert out == {"authorized": True, "scope": SCOPE}
    assert seen["code"] == "abc"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(seen["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"]
    assert (tmp_path / "tok.json").stat().st_mode & 0o777 == 0o600
    assert gc.authorized
