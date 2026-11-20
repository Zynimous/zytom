"""Command line: data entry, engine views, calendar sync, daemon, notes, MCP server."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from datetime import timedelta

from . import engine
from .store import Store
from .service import Secretary
from .timeutil import parse_dt, tz


def _j(x) -> None:
    print(json.dumps(x, ensure_ascii=False, indent=2, default=str))


def _fmt_t(t: dict) -> str:
    s = f"{t['med']:g}"
    if t["opt"] != t["pes"]:
        s += f" [{t['opt']:g}…{t['pes']:g}]"
    return s if not t.get("estimated") else "~" + s


def print_signals(sig: dict, tzname: str) -> None:
    z = tz(tzname)
    print(f"現在 {parse_dt(sig['now']).astimezone(z):%Y-%m-%d %H:%M}")
    for k, p in sig["pressure"].items():
        print(f"壓力 {k}: {_fmt_t(p)}   需要 {_fmt_t(p['required_h'])}h / 可用 {p['available_h']}h   ({p['source']})")
    t = sig["tension"]
    print(f"判定: {t['level']}" + (f" — {t.get('reason')}" if t.get("reason") else ""))
    if t.get("key_estimate"):
        ke = t["key_estimate"]
        print(f"  建議確認：「{ke['text']}」估 {_fmt_t(ke['effort'])}h（{ke['effort'].get('basis')}）")
    print()
    for i in sig.get("items", []):
        if i["kind"] == "deadline":
            due = parse_dt(i["due"]).astimezone(z)
            print(f"  {i['urgency']:.2f}  slack {_fmt_t(i['slack_h']):>18}h  截止 {due:%m-%d %H:%M}"
                  f"  工時 {_fmt_t(i['effort'])}h  {i['text']}  [{i['id']}]")
        elif i["kind"] == "habit":
            print(f"  {i['urgency']:.2f}  習慣 {i['done']}/{i['target']} 落後 {i['behind']}"
                  f"{'' if i['catchable'] else '（補不回）'}  {i['text']}  [{i['id']}]")
        elif i["kind"] == "project":
            print(f"  {i['urgency']:.2f}  專案停滯 {i['stagnant_days']} 天  {i['text']}  [{i['id']}]")
        elif i["kind"] == "none":
            print(f"  {i['urgency']:.2f}  擱置 {i['idle_days']} 天  {i['text']}  [{i['id']}]")


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    except ValueError as e:
        sys.exit(f"zytom: {e}")


def _main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="zytom", description="個人 AI 秘書（資料層、引擎、MCP server）")
    ap.add_argument("--db", help="SQLite path (default: $ZYTOM_DB or ~/.local/share/zytom/zytom.db)")
    ap.add_argument("--now", help="pretend current time (ISO) — for testing")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="新增 Item")
    a.add_argument("text")
    a.add_argument("--due")
    a.add_argument("--start")
    a.add_argument("--end", help="與 --start 一起使用 → fixed")
    a.add_argument("--effort", type=float, help="小時（stated）")
    a.add_argument("--category")
    a.add_argument("--parent")
    a.add_argument("--habit", help="例如 3/week")
    a.add_argument("--rrule", help="例如 FREQ=WEEKLY;BYDAY=WE（搭配 --start 與 --duration）")
    a.add_argument("--duration", type=float, help="週期實例長度（小時）")
    a.add_argument("--level", choices=["must", "try", "droppable"])
    a.add_argument("--project", action="store_true")

    ls = sub.add_parser("list", help="列出 Item")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true")

    lg = sub.add_parser("log", help="寫入事件")
    lg.add_argument("kind")
    lg.add_argument("item_id", nargs="?")
    lg.add_argument("--actual", type=float, help="done 的實際工時")
    lg.add_argument("--data", help="JSON")

    s = sub.add_parser("signals", help="訊號與壓力值")
    s.add_argument("--json", action="store_true")
    s.add_argument("--top", type=int)

    r = sub.add_parser("recommend", help="空檔推薦")
    r.add_argument("--minutes", type=int, default=60)
    r.add_argument("--start")

    rl = sub.add_parser("relieve", help="減壓方案")
    rl.add_argument("--target", type=float)

    sm = sub.add_parser("simulate", help="模擬變更（JSON list）")
    sm.add_argument("changes")

    ap_ = sub.add_parser("apply", help="套用變更（JSON list）— 等同使用者確認")
    ap_.add_argument("changes")

    ic = sub.add_parser("import-ics", help="唯讀匯入 ICS（網址或檔案）")
    ic.add_argument("source")
    ic.add_argument("--name", default="calendar")
    ic.add_argument("--save", action="store_true", help="存為自動同步來源")
    ic.add_argument("--types", help="TronClass 類型，逗號分隔")
    ic.add_argument("--include-past", action="store_true")

    sub.add_parser("sync", help="同步已儲存的行事曆來源")
    g = sub.add_parser("gcal", help="Google Calendar 唯讀匯入：auth / calendars / import")
    g.add_argument("action", choices=["auth", "calendars", "import"])
    g.add_argument("--client-secret", help="Google Cloud OAuth client（Desktop app）的 credentials.json")
    g.add_argument("--calendar", action="append", help="calendar id，可重複；預設 primary")
    g.add_argument("--name", default="google")
    g.add_argument("--days", type=int, default=60, help="匯入未來幾天")
    g.add_argument("--save", action="store_true", help="存為自動同步來源")
    g.add_argument("--no-browser", action="store_true")
    sub.add_parser("tick", help="跑一次提醒規則")
    sub.add_parser("startup", help="啟動補檢查")
    sub.add_parser("stats", help="跑每日統計批次與門檻學習")
    sub.add_parser("context", help="每輪對話的 context")
    d = sub.add_parser("daemon", help="定時提醒迴圈")
    d.add_argument("--interval", type=float, default=30, help="分鐘")
    d.add_argument("--stdout", action="store_true", help="同時把提醒印到 stdout")

    pr = sub.add_parser("prefs", help="讀取或設定偏好")
    pr.add_argument("set", nargs="*", help="key=JSON")

    n = sub.add_parser("notes", help="Notes：show / set / accept / reject / history / revert")
    n.add_argument("action", choices=["show", "set", "accept", "reject", "history", "revert"])
    n.add_argument("name", nargs="?", default="main.md")
    n.add_argument("arg", nargs="?", help="set: 檔案路徑（- 為 stdin）；accept/reject/revert: 版本")

    cv = sub.add_parser("calendar", help="秘書日曆（day / week / month / list）")
    cv.add_argument("--view", default="week", choices=["day", "week", "month", "list"])
    cv.add_argument("--date")
    cv.add_argument("--layers", help="fixed,plan,deadline,recurring")
    cv.add_argument("--days", type=int, default=14)
    cv.add_argument("--json", action="store_true")
    cv.add_argument("--html", help="輸出 HTML 檔")

    apis = sub.add_parser("api", help="啟動 FastAPI 服務（REST、/calendar、/ws/alerts、/mcp、排程）")
    apis.add_argument("--host", default="127.0.0.1")
    apis.add_argument("--port", type=int, default=8780)
    apis.add_argument("--interval", type=float, default=30)
    apis.add_argument("--no-scheduler", action="store_true")

    sv = sub.add_parser("serve", help="啟動 MCP server")
    sv.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    store = Store(args.db)
    now_s = args.now or os.environ.get("ZYTOM_NOW")
    clock = (lambda: parse_dt(now_s)) if now_s else None
    sec = Secretary(store, clock)
    now = sec.now

    if args.cmd == "add":
        data: dict = {"text": args.text}
        if args.due:
            data["time"] = {"type": "deadline", "due": args.due, **({"start": args.start} if args.start else {})}
        elif args.rrule:
            data["time"] = {"type": "recurring", "rrule": args.rrule, "dtstart": args.start,
                            "duration_h": args.duration or 1}
        elif args.start and args.end:
            data["time"] = {"type": "fixed", "start": args.start, "end": args.end}
        elif args.habit:
            c, per = args.habit.split("/")
            data["time"] = {"type": "habit", "count": int(c), "per": per}
        for k in ("effort", "category", "parent"):
            if getattr(args, k) is not None:
                data[k] = getattr(args, k)
        if args.level:
            data["commitment"] = {"level": args.level}
        if args.project:
            data["meta"] = {"project": True}
        _j(sec.upsert(data)["item"])
    elif args.cmd == "list":
        res = sec.query(status=None if args.all else ["open", "active"], limit=1000)
        if args.json:
            _j(res)
        else:
            z = tz(sec.tz)
            for it in res["items"]:
                t = it["time"]
                when = ""
                if t.get("due"):
                    when = "截止 " + parse_dt(t["due"]).astimezone(z).strftime("%m-%d %H:%M")
                elif t.get("start") and t["type"] == "fixed":
                    when = parse_dt(t["start"]).astimezone(z).strftime("%m-%d %H:%M")
                eff = it.get("effort_resolved", {}).get("display", "")
                print(f"{it['id']}  {it['status']:<6} {t['type']:<9} {when:<17} {eff:<18} {it['text']}")
            print(f"({res['count']} items)")
    elif args.cmd == "log":
        data = json.loads(args.data) if args.data else {}
        if args.actual is not None:
            data["actual_h"] = args.actual
        _j(sec.log_event(args.kind, args.item_id, data))
    elif args.cmd == "signals":
        sig = sec.signals(top=args.top)
        _j(sig) if args.json else print_signals(sig, sec.tz)
    elif args.cmd == "recommend":
        _j(sec.recommend(args.start, None, args.minutes))
    elif args.cmd == "relieve":
        _j(sec.relieve(args.target))
    elif args.cmd == "simulate":
        _j(sec.simulate(json.loads(args.changes)))
    elif args.cmd == "apply":
        _j(sec.apply(json.loads(args.changes), confirmed=True, note="cli"))
    elif args.cmd == "import-ics":
        from .ics import fetch, import_ics

        src = args.source
        data_b = fetch(src) if src.startswith(("http://", "https://")) else open(src, "rb").read()
        types = args.types.split(",") if args.types else None
        _j(import_ics(store, data_b, args.name, sec.tz, now, types, args.include_past))
        if args.save:
            srcs = [x for x in (store.pref("calendar_sources", []) or []) if x["name"] != args.name]
            srcs.append({"name": args.name, "url": src, "include_types": types})
            store.set_pref("calendar_sources", srcs)
    elif args.cmd == "sync":
        from .ics import sync_sources

        _j(sync_sources(store, sec.tz, now))
    elif args.cmd == "gcal":
        from .gcal import GoogleCalendar, import_gcal

        gc = GoogleCalendar(args.client_secret)
        if args.action == "auth":
            _j(gc.authorize(open_browser=not args.no_browser))
        elif args.action == "calendars":
            _j(gc.calendars())
        else:
            cals = args.calendar or ["primary"]
            _j(import_gcal(store, args.name, cals, sec.tz, now, args.days, gc))
            if args.save:
                srcs = [x for x in (store.pref("calendar_sources", []) or []) if x["name"] != args.name]
                srcs.append({"type": "gcal", "name": args.name, "calendars": cals})
                store.set_pref("calendar_sources", srcs)
    elif args.cmd == "tick":
        from .alerts import tick

        _j(tick(store, now))
    elif args.cmd == "startup":
        from .alerts import startup

        _j(startup(store, now))
    elif args.cmd == "stats":
        from .observe import daily_stats

        _j(daily_stats(store, now))
    elif args.cmd == "context":
        _j(sec.context())
    elif args.cmd == "daemon":
        from .alerts import StdoutNotifier, notifiers_from_prefs, run_daemon

        ns = notifiers_from_prefs(sec.prefs) + ([StdoutNotifier()] if args.stdout else [])
        run_daemon(store, args.interval, ns)
    elif args.cmd == "prefs":
        for kv in args.set:
            k, v = kv.split("=", 1)
            try:
                store.set_pref(k, json.loads(v))
            except json.JSONDecodeError:
                store.set_pref(k, v)
        _j(sec.prefs)
    elif args.cmd == "notes":
        name = args.name
        if args.action == "show":
            cur = store.note(name)
            print(cur["content"] if cur else f"(no note {name})")
            for p in store.notes("proposed"):
                if p["name"] == name:
                    print(f"\n--- 待確認提案 v{p['version']}: {p['reason'] or ''}", file=sys.stderr)
                    old = cur["content"] if cur else ""
                    sys.stderr.writelines(difflib.unified_diff(old.splitlines(True), p["content"].splitlines(True)))
        elif args.action == "set":
            content = sys.stdin.read() if args.arg in (None, "-") else open(args.arg, encoding="utf-8").read()
            print(f"{name} v{store.add_note_version(name, content, 'user', 'current')}")
        elif args.action in ("accept", "reject"):
            if not args.arg:
                sys.exit("version required")
            row = next((x for x in store.note_history(name) if x["version"] == int(args.arg)), None)
            if not row or row["state"] != "proposed":
                sys.exit(f"{name} v{args.arg} is not a pending proposal")
            store.set_note_state(name, int(args.arg), "current" if args.action == "accept" else "rejected")
            print(f"{name} v{args.arg} {args.action}ed")
        elif args.action == "history":
            for h in store.note_history(name):
                print(f"v{h['version']}  {h['ts'][:16]}  {h['author']:<5} {h['state']:<10} {h['reason'] or ''}")
        elif args.action == "revert":
            row = next((x for x in store.note_history(name) if x["version"] == int(args.arg)), None)
            if not row:
                sys.exit("no such version")
            print(f"{name} v{store.add_note_version(name, row['content'], 'user', 'current', f'revert to v{args.arg}')}")
    elif args.cmd == "calendar":
        from . import calendar_view as cvm

        cal = cvm.build(sec.world(), args.view, args.date, args.layers.split(",") if args.layers else None, args.days)
        if args.html:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(cvm.render_html(cal, "", now))
            print(args.html)
        elif args.json:
            _j(cal)
        else:
            print(cvm.render_text(cal))
    elif args.cmd == "api":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(store, clock, not args.no_scheduler, args.interval), host=args.host, port=args.port)
    elif args.cmd == "serve":
        from .server import build_server

        server = build_server(store, clock)
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run(args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
