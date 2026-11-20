# zytom — 個人 AI 秘書（MCP server）

依 [Plan.md](Plan.md) 實作的資料層、數值引擎、觀察層、主動提醒、秘書日曆、FastAPI 服務與 MCP 工具。
MCP server 只提供工具與資料，**不含提示詞**；Agent 的系統提示詞與 skill 另外設計。

## 安裝與啟動

```bash
uv sync
uv run zytom serve                 # MCP（stdio）
uv run zytom serve --transport streamable-http --port 8765
uv run zytom api                   # FastAPI 服務：REST、/calendar、/ws/alerts、/mcp、排程迴圈
```

### FastAPI 服務（`zytom api`，預設 127.0.0.1:8780）

| 路徑 | 內容 |
| --- | --- |
| `GET /calendar?view=month\|week\|day\|list&date=&layers=` | 秘書日曆網頁（圖層可個別開關） |
| `GET /api/calendar` | 同上，JSON |
| `WS /ws/alerts` | 訂閱提醒；連線時先送出尚未送達的提醒 |
| `POST /subscriptions/webhook` | 註冊 webhook 推播 |
| `POST /mcp` | MCP（streamable HTTP），與 stdio 版相同的工具 |
| `GET/POST /items`、`/log`、`/signals`、`/simulate`、`/recommend`、`/relieve`、`/apply`、`/observations`、`/context`、`/alerts`、`/alerts/{id}/feedback`、`/tick`、`/calendar/sync` | 與 MCP 工具對應的 REST |

啟動時先做補檢查（同步行事曆、補產生週期實例、停機期間的提醒合併成一則摘要），之後每 30 分鐘 tick、每日跑一次統計與門檻學習、每 6 小時同步行事曆。
設定 `ZYTOM_TOKEN` 後所有請求需帶 `Authorization: Bearer …`（WebSocket 用 `?token=`）。

推播走 Notifier 介面：WebSocket（服務版）、webhook、Unix socket（desktop IPC，`prefs notifiers='[{"type":"unix","path":"…"}]'`）、JSONL 檔、stdout。

資料庫預設在 `~/.local/share/zytom/zytom.db`，可用 `--db` 或 `ZYTOM_DB` 改位置。
本 repo 的 `.mcp.json` 讓 Claude Code 在專案目錄下直接接上 `zytom`。

其他 MCP 客戶端：

```json
{ "mcpServers": { "zytom": { "command": "uv", "args": ["run", "--project", "/path/to/zytom", "zytom", "serve"] } } }
```

## MCP 工具

| 工具 | 用途 | 寫入 |
| --- | --- | --- |
| `item_upsert` | 新增或修改 Item（數字 = stated；推估須附 `range`、`basis`） | 是 |
| `item_query` | 依條件查詢，附上補值後的工時（非 stated 顯示 `~`） | 否 |
| `log_event` | 寫入事件；started/done 同步狀態，done 帶 `actual_h` 會比對預估 | 是 |
| `signals` | slack、習慣落後、專案停滯、擱置；3/7 天壓力值與提醒分級 | 否 |
| `simulate` | 在副本上套用變更，回傳壓力差異與 slack 變差的 Item | 否 |
| `recommend` | 空檔推薦 2～3 個 Item | 否 |
| `relieve` | 代價最小的前 3 組減壓方案 | 否 |
| `apply` | 套用使用者確認過的變更；沒有 `confirmed=true` 一律拒絕 | 是 |
| `observations` | 查詢 Observation | 否 |
| `context` | 每輪 context：今天、壓力、最緊 5 項、Notes | 否 |
| `alerts` / `alert_feedback` | 提醒事件與使用者反應（供門檻學習） | 否 / 是 |
| `calendar_sync` | 唯讀匯入 ICS（TronClass 作業/測驗 → deadline） | 是 |
| `observation_upsert` | 每週反思寫回 Observation | 是 |
| `notes_get` / `notes_propose` | 讀 Notes；Agent 只能提案，使用者以 `zytom notes accept` 確認 | 否 / 提案 |
| `prefs` | 睡眠等保留時段、每日雜項時數、時區、每日回顧時間 | 是 |
| `calendar_view` | 秘書日曆：day / week / month / list，四個圖層 | 否 |
| `glossary` | 欄位與數值說明（同 resource `zytom://glossary`），可只取一節 | 否 |
| `log_query` | 查詢事件 log（反思與 evidence 用） | 否 |

變更（`simulate` / `apply`）格式：

```json
{"op": "defer",  "item_id": "…", "until": "2026-11-25T09:00"}
{"op": "drop",   "item_id": "…"}
{"op": "shrink", "item_id": "…", "factor": 0.6}
{"op": "split",  "item_id": "…", "parts": [{"fraction": 0.5}, {"fraction": 0.5, "plan": {…}}]}
{"op": "plan",   "item_id": "…", "plan": {"precision": "part", "start": "…", "end": "…", "label": "今晚"}}
{"op": "set",    "item_id": "…", "fields": {…}}
{"op": "done",   "item_id": "…"}
```

## CLI

```bash
zytom add "期末報告" --due 2026-12-20T23:59 --effort 12 --category 報告
zytom add "跑步" --habit 3/week
zytom add "週會" --rrule "FREQ=WEEKLY;BYDAY=WE" --start 2026-11-04T19:00 --duration 1
zytom list | signals | recommend --minutes 90 | relieve
zytom log done <id> --actual 2.5
zytom import-ics <url|file> --name tronclass --save   # 之後 `zytom sync` 或 daemon 自動同步
zytom prefs reserved='[["00:30","08:00"]]' overhead_h=3 daily_review='"21:30"'
zytom notes show | notes accept main.md 2
zytom calendar --view week | --view month --html cal.html
zytom daemon --stdout                                   # 不開 API 時的提醒迴圈
```

`--now 2026-11-21T10:00`（或 `ZYTOM_NOW`）可以模擬時間。

## 計算方式摘要

- **可用時間** = 窗口 − 每日保留時段（睡眠）− fixed 行程 − 未實例化的週期行程，再扣每日雜項比例。
- **slack** 採 EDF：截止前可用時間 − 所有截止不晚於它的工作總量（也檢查本身的最早開始時間）。
- **壓力值** 回傳峰值：在窗口內每個截止點計算「到該點為止的需要 ÷ 可用」，取最大值（檢查點至少距現在 12 小時）；Plan 原本的窗口平均比值放在 `flat`。這樣明天的趕工不會被 3 天窗口稀釋。
- 每個數值都算 opt / med / pes，`source` 取輸入中最弱的一個。
- **提醒分級**：中位壓力超過門檻且主要輸入為 stated/stat → `warn`（附 `relieve` 方案）；只有悲觀超過且主要為預估 → `ask`（附 `key_estimate`）；否則不動作。
- **門檻** 初始 0.7，`zytom stats`（daemon 每日一次）依 `alert_feedback`、逾期、推掉、熬夜訊號 ±0.05。

## 行事曆匯入

### Google Calendar API（唯讀）

```bash
# 1. Google Cloud Console 建立 OAuth client（類型 Desktop app），下載 credentials.json，啟用 Calendar API
uv run zytom gcal auth --client-secret credentials.json   # 瀏覽器授權，只要求 calendar.readonly
uv run zytom gcal calendars                               # 列出可匯入的日曆
uv run zytom gcal import --calendar primary --save        # 匯入未來 60 天並存為自動同步來源
```

- 行程 → fixed Item（占用可用時間）；整天或標為「空閒」的事件只顯示、不占時間；取消或自己婉拒的跳過。
- 週期事件由 API 展開（`singleEvents=true`），每次各為一個 Item。
- Token 存在資料庫旁的 `gcal_token.json`（權限 600），可用 `ZYTOM_GCAL_TOKEN` 改位置。
- 授權後 MCP `calendar_sync(google_calendars=["primary"])` 與 API 的排程同步也能使用。

### ICS

- 外部行事曆只讀不寫回。重新匯入只更新來源擁有的欄位（標題、時間），保留使用者設定的工時、計畫、狀態。
- TronClass：把同一活動的「開始 / 截止 / 結束」標記合併成一個 deadline Item；預設只匯入 homework / exam / questionnaire，已過期的跳過（`--include-past` 可改）。標示「不計分」的預設為 `try`，其他課業預設 `must`（都標為預估）。
- 從來源消失的未來事件只標記 `missing_from_feed`，不會自動推掉。

## 秘書日曆

| 圖層 | 內容 |
| --- | --- |
| `fixed` | fixed Item（外部行事曆、使用者） |
| `plan` | 有 plan 的 Item：slot / part 為時段，range 為整段帶狀 |
| `deadline` | 截止，只標時間點不佔時段 |
| `recurring` | 已實例化的期次（實線）、預估的未來期次（虛線、`~`）、習慣本期進度 |

預估內容沿用 `~`，網頁上滑鼠停留可看工時來源與依據。

## Agent 怎麼知道怎麼用

分兩層，對應 Plan.md「MCP server 只提供工具與資料，提示詞與 skill 另成模組」：

1. **資料說明**（server 內，`src/zytom/glossary.md`）：只描述欄位定義與計算方式，不含行為指示。透過 resource `zytom://glossary`、工具 `glossary(section=…)`、`GET /glossary` 取得，任何 MCP 客戶端都能用。
2. **秘書 skill**（`.claude/skills/zytom-secretary/SKILL.md`）：每輪流程、對話怎麼寫成 Item、怎麼讀數值、依 tension 等級怎麼回應、減壓確認流程、什麼時候才能問問題、記錄進度、Notes、每週反思、回應風格。在此專案目錄開 Claude Code 會自動載入；換模型或改語氣只改這份，不動工具。

要在別的目錄使用 skill，把資料夾複製到 `~/.claude/skills/`。

## 測試

```bash
uv run pytest
```
