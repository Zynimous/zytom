"""MCP server: tools and data only, no prompts (Plan.md「MCP 工具介面」).

Core tools from the plan: item_upsert, item_query, log_event, signals,
simulate, recommend, relieve, apply, observations.
Supporting tools: context, alerts, alert_feedback, calendar_sync,
observation_upsert, notes_get, notes_propose, prefs.
"""

from __future__ import annotations

import difflib
import functools
import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .ics import fetch, import_ics, sync_sources
from .models import Observation
from .service import Secretary
from .store import Store, new_id
from .timeutil import now_utc, parse_dt

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
COMMIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

Change = dict[str, Any]
CHANGE_DOC = (
    "Change objects: "
    '{"op":"defer","item_id":..,"until":ISO,"end"?:ISO} | {"op":"drop","item_id":..} | '
    '{"op":"shrink","item_id":..,"factor"?:0.6,"effort"?:hours|Value} | '
    '{"op":"split","item_id":..,"parts":[{"text"?,"fraction"|"effort","plan"?}]} | '
    '{"op":"plan","item_id":..,"plan":Plan|null} | {"op":"set","item_id":..,"fields":{...}} | '
    '{"op":"done","item_id":..}. Naive datetimes are in the user timezone.'
)


def _expected_errors(fn):
    """Bad input (ValueError, validation errors, unknown keys) reaches the agent as a readable ToolError."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError, LookupError) as e:
            raise ToolError(str(e)) from e

    return wrapper


def glossary_text(section: str | None = None) -> str:
    from importlib.resources import files

    text = files("zytom").joinpath("glossary.md").read_text(encoding="utf-8")
    if not section:
        return text
    parts = text.split("\n## ")
    hits = [p for p in parts[1:] if section.lower() in p.split("\n", 1)[0].lower()]
    if not hits:
        heads = [p.split("\n", 1)[0] for p in parts[1:]]
        raise ValueError(f"no section {section!r}; sections: {heads}")
    return "\n\n".join("## " + h for h in hits)


def _clock():
    fixed = os.environ.get("ZYTOM_NOW")
    return (lambda: parse_dt(fixed)) if fixed else now_utc


def build_server(store: Store | None = None, clock=None) -> MCPServer:
    store = store or Store()
    sec = Secretary(store, clock or _clock())
    mcp = MCPServer(name="zytom", title="Zytom 個人秘書", version="0.1.0",
                    description="Items, event log, deterministic pressure engine and observations for a personal secretary agent.")
    _register = mcp.tool

    def tool(**kw):
        return lambda fn: _register(**kw)(_expected_errors(fn))

    mcp.tool = tool  # type: ignore[method-assign]

    # ------------------------------------------------------------- items
    @mcp.tool(annotations=WRITE)
    def item_upsert(
        text: Annotated[str | None, Field(description="原始描述；新增時必填")] = None,
        id: Annotated[str | None, Field(description="既有 Item id；省略則新增")] = None,
        parent: Annotated[str | None, Field(description="父 Item（專案）id")] = None,
        category: Annotated[str | None, Field(description="類別，例如 報告、數學、程式、作業")] = None,
        time: Annotated[dict | None, Field(description=(
            'TimeSpec. {"type":"none"} | {"type":"deadline","due":ISO,"due_earliest"?:ISO,"start"?:ISO} | '
            '{"type":"fixed","start":ISO,"end":ISO} | {"type":"anchored","target":id,"relation":"before"|"after","offset_h":n} | '
            '{"type":"recurring","rrule":"FREQ=WEEKLY;BYDAY=WE","dtstart":ISO,"duration_h":n,"as":"fixed"|"deadline"} | '
            '{"type":"habit","count":3,"per":"week"}'))] = None,
        plan: Annotated[dict | None, Field(description='{"precision":"range"|"part"|"slot","start":ISO,"end":ISO,"label":"今晚"}')] = None,
        effort: Annotated[float | dict | None, Field(description=(
            "剩餘工時（小時）。數字 = 使用者說的（stated）；推估請給 "
            '{"v":4,"range":[3,6],"source":"llm","basis":"..."}'))] = None,
        commitment: Annotated[dict | None, Field(description='{"level":"must"|"try"|"droppable" 或 Value,"cost":0-10 或 Value}')] = None,
        status: Annotated[Literal["open", "active", "done", "dropped"] | None, Field()] = None,
        meta: Annotated[dict | None, Field(description="額外資訊，與既有 meta 合併")] = None,
        said: Annotated[str | None, Field(description="使用者原話（新增時記入 log）")] = None,
    ) -> dict:
        """新增或修改 Item。只更新有提供的欄位。使用者明說的數值用數字（stated），推估的值必須附 range 與 basis。
        改變承諾的動作（延後、推掉、拆分）請用 apply。"""
        data: dict[str, Any] = {k: v for k, v in {
            "id": id, "text": text, "parent": parent, "category": category, "time": time, "plan": plan,
            "effort": effort, "commitment": commitment, "status": status, "meta": meta, "said": said,
        }.items() if v is not None}
        if status == "dropped":
            raise ValueError("dropping changes a commitment; use apply with confirmed=true")
        return sec.upsert(data)

    @mcp.tool(annotations=READ)
    def item_query(
        text: Annotated[str | None, Field(description="描述包含此字串")] = None,
        status: Annotated[list[Literal["open", "active", "done", "dropped"]] | None, Field(description="預設 open+active")] = None,
        category: str | None = None,
        parent: Annotated[str | None, Field(description="只列此 Item 的子項")] = None,
        time_type: Literal["none", "deadline", "fixed", "anchored", "recurring", "habit"] | None = None,
        due_before: Annotated[str | None, Field(description="ISO 時間")] = None,
        due_after: str | None = None,
        ids: list[str] | None = None,
        source: Annotated[str | None, Field(description="例如 ics（行事曆匯入）")] = None,
        limit: int = 50,
    ) -> dict:
        """依條件查詢 Item，依截止時間排序；附上補值後的工時（effort_resolved，非 stated 會以 ~ 顯示）。"""
        return sec.query(text, status or ["open", "active"], category, parent, time_type, due_before, due_after,
                         ids, source, limit)

    @mcp.tool(annotations=WRITE)
    def log_event(
        kind: Literal["started", "done", "skipped", "rec_accepted", "rec_rejected", "said", "planned",
                      "estimate_corrected"],
        item_id: str | None = None,
        data: Annotated[dict | None, Field(description=(
            '例如 done: {"actual_h":2.5}；said: {"text":"好累","signal":"熬夜"}；rec_rejected: {"instead":item_id}'))] = None,
    ) -> dict:
        """寫入事件（只能追加）。started/done 會同步 Item 狀態（習慣與週期規則除外）；done 帶 actual_h 時自動比對預估。
        對話中的狀態訊號（好累、狀態很好）用 said，只記 log 不改 Item。"""
        return sec.log_event(kind, item_id, data)

    @mcp.tool(annotations=READ)
    def log_query(
        item_id: str | None = None,
        kinds: Annotated[list[str] | None, Field(description="事件種類，例如 [\"done\",\"deferred\"]")] = None,
        since: Annotated[str | None, Field(description="ISO 時間；例如一週前")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict:
        """查詢事件 log（最新的在後）。每週反思與 Observation 的 evidence 用。"""
        logs = store.logs(item_id, kinds, parse_dt(since, sec.tz) if since else None)
        return {"count": len(logs), "logs": [l.model_dump(mode="json") for l in logs[-limit:]]}

    # ------------------------------------------------------------ engine
    @mcp.tool(annotations=READ)
    def signals(
        windows: Annotated[list[int] | None, Field(description="窗口天數，預設 [3, 7]")] = None,
        top: Annotated[int | None, Field(description="只回傳最緊的前 N 個 Item")] = None,
    ) -> dict:
        """所有 Item 的訊號（slack、習慣落後、專案停滯、擱置）與各窗口壓力值；每個數值都有 opt/med/pes 與 source。
        tension 依不確定度分級：warn = 直接提醒，ask = 先確認 key_estimate，none = 不動作。"""
        return sec.signals(windows, top)

    @mcp.tool(annotations=READ)
    def simulate(changes: Annotated[list[Change], Field(description=CHANGE_DOC)]) -> dict:
        """在副本上套用變更，回傳壓力差異與 slack 變小或轉負的 Item。不寫入。"""
        return sec.simulate(changes)

    @mcp.tool(annotations=READ)
    def recommend(
        start: Annotated[str | None, Field(description="空檔開始（ISO），預設現在")] = None,
        end: Annotated[str | None, Field(description="空檔結束（ISO）")] = None,
        minutes: Annotated[int | None, Field(description="沒給 end 時的長度，預設 60")] = None,
        n: Annotated[int, Field(ge=2, le=3)] = 3,
    ) -> dict:
        """空檔推薦 2～3 個 Item（塞得進的優先，依緊迫程度與觀察排序）。使用者接受或拒絕後請用 log_event 記錄。"""
        return sec.recommend(start, end, minutes, n)

    @mcp.tool(annotations=READ)
    def relieve(target: Annotated[float | None, Field(description="目標壓力值，預設為學習到的門檻")] = None) -> dict:
        """找出讓壓力回到門檻以下、代價最小的變更組合（延後、拆小、縮小範圍、推掉 droppable），回傳前 3 組。不寫入。"""
        return sec.relieve(target)

    @mcp.tool(annotations=COMMIT)
    def apply(
        changes: Annotated[list[Change], Field(description=CHANGE_DOC + ' 可加 "from_recommendation" 記錄採納。')],
        confirmed: Annotated[bool, Field(description="使用者已明確同意這些變更時才設為 true")] = False,
        note: Annotated[str | None, Field(description="使用者的原話或理由")] = None,
    ) -> dict:
        """套用使用者確認過的變更並寫入 log。系統不會自己延後、推掉或重排；沒有使用者同意不要呼叫。"""
        return sec.apply(changes, confirmed, note)

    # ------------------------------------------------------ observations
    @mcp.tool(annotations=READ)
    def observations(
        scope: Annotated[str | None, Field(description="類別或情境；省略則全部")] = None,
        include_hypotheses: bool = True,
        min_confidence: float = 0.0,
    ) -> dict:
        """查詢觀察到的行為模式（只描述使用者通常怎麼做，不評論）。rejected 不會出現。"""
        return sec.observations(scope, include_hypotheses, min_confidence)

    @mcp.tool(annotations=WRITE)
    def observation_upsert(
        text: str,
        id: Annotated[str | None, Field(description="既有 id；省略則新增")] = None,
        scope: str | None = None,
        params: dict | None = None,
        evidence: Annotated[list[int] | None, Field(description="支持的 log id")] = None,
        confidence: Annotated[float | None, Field(ge=0, le=1)] = None,
        status: Literal["hypothesis", "confirmed", "rejected"] | None = None,
        origin: Literal["reflection", "user"] = "reflection",
    ) -> dict:
        """每週反思或使用者確認時新增、強化、削弱 Observation。"""
        cur = next((o for o in store.observations(include_rejected=True) if o.id == id), None) if id else None
        base = cur.model_dump() if cur else {"id": id or new_id("o"), "origin": origin}
        base.update({k: v for k, v in {"text": text, "scope": scope, "params": params, "evidence": evidence,
                                       "confidence": confidence, "status": status}.items() if v is not None})
        base["last_seen"] = sec.now
        return {"observation": store.save_observation(Observation.model_validate(base)).model_dump(mode="json")}

    # ----------------------------------------------------------- context
    @mcp.tool(annotations=READ)
    def context() -> dict:
        """每輪對話的精簡 context：今天的行程與截止、壓力值、最緊的 5 個 Item、Notes。"""
        return sec.context()

    @mcp.tool(annotations=READ)
    def alerts(undelivered_only: bool = False, limit: int = 20) -> dict:
        """列出主動提醒事件（由排程迴圈產生）。"""
        return {"alerts": [a["body"] | {"delivered": bool(a["delivered"])}
                           for a in store.alerts(undelivered_only, limit)]}

    @mcp.tool(annotations=WRITE)
    def alert_feedback(
        alert_id: str,
        reaction: Literal["accepted", "ignored", "not_needed"],
        note: str | None = None,
    ) -> dict:
        """記錄使用者對提醒的反應，供門檻學習使用。"""
        a = next((x for x in store.alerts(limit=500) if x["id"] == alert_id), None)
        if a is None:
            raise ValueError(f"alert {alert_id} not found")
        log = store.append("alert_feedback", None, {"alert_id": alert_id, "reaction": reaction, "note": note,
                                                    "rule": a["body"]["rule"], "items": a["body"]["items"]})
        return {"logged": log.model_dump(mode="json")}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    def calendar_sync(
        url: Annotated[str | None, Field(description="ICS 訂閱網址；省略則同步已儲存的來源")] = None,
        name: Annotated[str | None, Field(description="來源名稱")] = None,
        save: Annotated[bool, Field(description="把 url 存成之後自動同步的來源")] = False,
        include_types: Annotated[list[str] | None, Field(description="TronClass 類型，預設 homework/exam/questionnaire")] = None,
        google_calendars: Annotated[list[str] | None, Field(description=(
            "Google Calendar 的 calendar id（例如 primary）。需先由使用者在終端機執行 `zytom gcal auth` 授權"))] = None,
    ) -> dict:
        """唯讀匯入外部行事曆：ICS 網址或 Google Calendar API。TronClass 的作業/測驗會變成 deadline Item；
        一般行事曆與 Google Calendar 的行程變成 fixed Item。不會寫回外部行事曆，也不會覆蓋使用者設定的工時、計畫與狀態。"""
        if google_calendars:
            from .gcal import import_gcal

            name = name or "google"
            if save:
                srcs = [s for s in (store.pref("calendar_sources", []) or []) if s["name"] != name]
                srcs.append({"type": "gcal", "name": name, "calendars": google_calendars})
                store.set_pref("calendar_sources", srcs)
            return {"results": [{"name": name, "ok": True,
                                 **import_gcal(store, name, google_calendars, sec.tz, sec.now)}]}
        if url is None:
            return {"results": sync_sources(store, sec.tz, sec.now, only=name)}
        name = name or "calendar"
        if save:
            srcs = [s for s in (store.pref("calendar_sources", []) or []) if s["name"] != name]
            srcs.append({"name": name, "url": url, "include_types": include_types})
            store.set_pref("calendar_sources", srcs)
        return {"results": [{"name": name, "ok": True,
                             **import_ics(store, fetch(url), name, sec.tz, sec.now, include_types)}]}

    # ---------------------------------------------------------- glossary
    @mcp.resource("zytom://glossary", name="glossary", title="zytom 資料與數值說明",
                  description="欄位定義、opt/med/pes、source、壓力值、tension 分級、變更格式等", mime_type="text/markdown")
    def glossary_resource() -> str:
        return glossary_text()

    @mcp.tool(annotations=READ)
    def glossary(
        section: Annotated[str | None, Field(description=(
            "只取某一節，例如 Value、三情境數值、Item、可用時間、signals、壓力值、tension、變更、recommend、log、"
            "Observation、Alert、calendar_view；省略則回傳全文"))] = None,
    ) -> dict:
        """資料與數值說明（與 resource zytom://glossary 相同）：各欄位的定義與計算方式。"""
        return {"text": glossary_text(section)}

    # ---------------------------------------------------------- calendar
    @mcp.tool(annotations=READ)
    def calendar_view(
        view: Literal["day", "week", "month", "list"] = "week",
        date: Annotated[str | None, Field(description="錨點日期 YYYY-MM-DD，預設今天")] = None,
        layers: Annotated[list[Literal["fixed", "plan", "deadline", "recurring"]] | None,
                          Field(description="要顯示的圖層，預設全部")] = None,
        days: Annotated[int, Field(description="list 模式的天數")] = 14,
        text: Annotated[bool, Field(description="true 時回傳可直接顯示的文字版")] = False,
    ) -> dict:
        """秘書日曆：固定行程、規劃（slot/part/range）、截止、週期與習慣四個圖層。
        預估內容 label 帶 ~，未實例化的週期期次 dashed=true。外部行事曆唯讀，不寫回。"""
        from . import calendar_view as cv

        cal = cv.build(sec.world(), view, date, layers, days)
        return {"text": cv.render_text(cal)} if text else cal

    # ------------------------------------------------------------- notes
    @mcp.tool(annotations=READ)
    def notes_get(name: str | None = None, history: bool = False) -> dict:
        """讀取使用者的長期指示（Notes）。history=true 時列出版本與待確認的提案。"""
        if name and history:
            return {"history": store.note_history(name)}
        notes = [n for n in store.notes() if not name or n["name"] == name]
        return {"notes": [{"name": n["name"], "version": n["version"], "content": n["content"]} for n in notes],
                "proposed": [{"name": n["name"], "version": n["version"], "reason": n["reason"]}
                             for n in store.notes("proposed") if not name or n["name"] == name]}

    @mcp.tool(annotations=WRITE)
    def notes_propose(
        name: Annotated[str, Field(description="Note 名稱，例如 main.md")],
        content: Annotated[str, Field(description="修改後的完整內容")],
        reason: str | None = None,
    ) -> dict:
        """提出 Notes 修改（diff）。Agent 不能直接寫入，須由使用者在 App 或 CLI 確認（zytom notes accept）。"""
        cur = store.note(name)
        diff = "".join(difflib.unified_diff((cur["content"] if cur else "").splitlines(True), content.splitlines(True),
                                            f"{name} (current)", f"{name} (proposed)"))
        version = store.add_note_version(name, content, "agent", "proposed", reason)
        return {"name": name, "version": version, "diff": diff, "status": "proposed — awaiting user confirmation"}

    # ------------------------------------------------------------- prefs
    @mcp.tool(annotations=WRITE)
    def prefs(
        set: Annotated[dict | None, Field(description=(
            '要更新的偏好，例如 {"reserved":[["00:30","08:00"]],"overhead_h":3,"daily_review":"21:30","tz":"Asia/Taipei"}'))] = None,
    ) -> dict:
        """讀取或更新偏好（睡眠等保留時間、每日雜項時數、時區、每日回顧時間、預設工時…）。門檻由系統學習，不在此設定。"""
        protected = {"threshold", "learn_cursor", "calendar_sources"}
        for k, v in (set or {}).items():
            if k in protected:
                raise ValueError(f"'{k}' cannot be set here")
            store.set_pref(k, v)
        p = sec.prefs
        p.pop("calendar_sources", None)  # 可能含私人網址
        return {"prefs": p}

    return mcp


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="zytom-mcp")
    ap.add_argument("--db", help="SQLite path (default: $ZYTOM_DB or ~/.local/share/zytom/zytom.db)")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server = build_server(Store(args.db) if args.db else None)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
