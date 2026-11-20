# zytom 資料與數值說明

本文件只描述欄位定義與計算方式，不含行為指示。時間皆為 ISO 8601；未帶時區的輸入視為使用者時區（prefs.tz，預設 Asia/Taipei）。工時單位一律為小時。

## Value（帶來源的數值）

| 欄位 | 意義 |
| --- | --- |
| `v` | 數值（中位估計） |
| `range` | `[低, 高]` 區間；沒有時視為 `[v, v]` |
| `source` | 來源，由強到弱：`stated`（使用者說的）> `stat`（同類別 ≥3 筆完成紀錄的中位數與四分位距）> `llm`（模型推估）> `default`（類別預設值） |
| `basis` | 估算依據的文字 |

- 衍生數值的 `source` 取所有輸入中最弱的一個。
- `estimated: true` 表示 `source` 不是 `stated`。
- 顯示文字中前綴 `~` 表示預估值（例：`~2h (1–5h)`）。
- `effort_resolved`：Item 自己沒有 effort 時，系統依上述順序補出的值；`commitment_resolved` 同理。

## 三情境數值（opt / med / pes）

每個訊號都有三個值，分別用輸入區間的對應端點計算：

| 鍵 | 意義 |
| --- | --- |
| `opt` | 樂觀：工時取區間低端、模糊截止取最晚 |
| `med` | 中位：工時取 `v` |
| `pes` | 悲觀：工時取區間高端、模糊截止取最早 |

對 slack 而言 `opt` ≥ `med` ≥ `pes`；對壓力值而言 `opt` ≤ `med` ≤ `pes`。三者相同代表輸入沒有不確定度。

## Item

| 欄位 | 意義 |
| --- | --- |
| `text` | 原始描述（唯一必填） |
| `parent` | 父 Item；有子項或 `meta.project=true` 的 Item 視為專案 |
| `category` | 類別，用於統計與預設工時 |
| `time.type` | `none` 無期限 / `deadline` 截止（`due`，可有 `due_earliest`、最早開始 `start`）/ `fixed` 固定時段 / `anchored` 依附另一 Item（`target`、`relation`、`offset_h`）/ `recurring` 週期規則（`rrule`、`dtstart`、`duration_h`、`as`）/ `habit` 頻率目標（`count` 次每 `per`） |
| `plan` | 打算何時做；`precision`：`slot` 精確時段、`part` 半天或晚上、`range` 一段期間 |
| `effort` | 剩餘工時（Value） |
| `commitment.level` | `must` / `try` / `droppable`（Value，v 為 0/1/2） |
| `commitment.cost` | 延後或推掉的代價，0–10 |
| `status` | `open` / `active` / `done` / `dropped` |
| `meta` | 外部來源資訊：`source`（`ics`、`gcal`）、`external_id`、`course`、`url`、`instance_of`（週期實例所屬規則）、`missing_from_feed`（來源中已消失） |

匯入的課業預設 `must`；標題含「不計分」者預設 `try`；其餘預設 `try`。這些預設的 `source` 都是 `default`。

## 可用時間

可用時間 = 時段總長 − 每日保留時段（`prefs.reserved`，如睡眠）− `fixed` Item（整天或標為空閒者除外）− 尚未實例化的週期固定行程，再乘上 `1 − overhead_h ÷ 每日清醒時數`。

## signals 的 Item 訊號（`items[]`，依 `urgency` 由高到低）

| `kind` | 欄位 | 意義 |
| --- | --- | --- |
| `deadline` | `slack_h` | 截止前可用時間 − 所有截止不晚於它的 Item 的工時總和（EDF）；也不超過「自身最早開始到截止的可用時間 − 自身工時」。負值 = 依目前預估做不完 |
| | `available_h` | 現在到截止的可用時間 |
| | `effort` | 使用中的工時三情境值與來源 |
| | `overdue` | 截止已過 |
| `habit` | `target` / `done` / `behind` | 本期目標（期中才建立的會按比例折算）、已完成、還差幾次 |
| | `catchable` | 剩餘天數 × `prefs.habit_max_per_day` 是否還補得回 |
| `project` | `stagnant_days` | 距離本身或任一子項最後一次進展的天數 |
| | `children_open` | 未完成子項數 |
| `none` | `idle_days` | 距離建立或最後一次動作的天數 |
| `fixed` | `start` / `end` | 尚未結束的固定行程 |

`urgency`（0–2）是排序用的綜合分數：deadline 為「1 − slack ÷ 可用時間」加上截止接近度；habit 為落後次數 ÷ 剩餘可補次數；project 與 none 依停滯或擱置天數。只用於同一次結果內的相對排序，不同次之間不宜直接比較。

## 壓力值（`pressure["3d"]`、`pressure["7d"]`）

| 欄位 | 意義 |
| --- | --- |
| `opt` / `med` / `pes` | 峰值壓力：在窗口內每個截止點計算「到該點為止需要的工時 ÷ 到該點為止的可用時間」，取最大值（檢查點至少距現在 12 小時） |
| `flat` | 窗口平均：窗口內需要工時 ÷ 窗口可用時間 |
| `peak_at` | 峰值出現的檢查點 |
| `required_h` / `available_h` | 窗口內需要與可用的工時 |
| `strong_share` | 需要工時中來源為 `stated` 或 `stat` 的比例 |
| `contributors` | 貢獻最多工時的前 8 項，含各自的 `hours` 與 `effort` |

窗口內需要的工時：窗口內截止的 Item 計全部；窗口後截止的按可用時間比例分攤；有 `plan` 的依計畫時段與窗口重疊比例；落後的習慣與未實例化的週期截止也計入。

壓力 1.0 代表所需工時剛好填滿可用時間；超過 1.0 代表依目前預估排不下。

## tension（提醒分級）

| `level` | 判定 |
| --- | --- |
| `warn` | 某窗口中位壓力 > 門檻，且 `strong_share` ≥ 0.5 |
| `ask` | 悲觀壓力 > 門檻，且主要輸入為 `llm` / `default`；`key_estimate` 為貢獻最大的預估項 |
| `info` | 只有悲觀壓力 > 門檻，主要輸入為 `stated` / `stat` |
| `none` | 悲觀壓力也未超過門檻 |

`threshold` 是學習中的門檻（初始 0.7，依使用者對提醒的反應每次 ±0.05，範圍 0.4–1.3）。`window` 為判定所依據的窗口，`reason` 為文字說明。

## 變更（simulate / apply）

| `op` | 效果 |
| --- | --- |
| `defer` | 把 plan 設為從 `until` 開始的 range |
| `drop` | 狀態改為 dropped |
| `shrink` | 工時乘以 `factor`（預設 0.6）或改為指定 `effort` |
| `split` | 拆成子 Item（`parts`：`fraction` 或 `effort`，可帶 `plan`） |
| `plan` | 設定或清除 plan |
| `set` | 直接修改欄位 |
| `done` | 標為完成 |

`simulate` 回傳各窗口壓力前後值、tension 前後等級、`slack_worse`（slack 變小的 Item，`turned_negative` 表示由正轉負）。`relieve` 回傳最多 3 組 `options`，各有 `changes`、`summary`、`cost`（越低越好）、`pressure_after`、`reaches_target`；`already_ok` 為 true 時不提供方案。`simulate` 與 `relieve` 不寫入；`apply` 需 `confirmed=true`。

## recommend

`recommendations[]`：`fit` 為 `full`（工時塞得進空檔）或 `partial`（塞不下，只能做一部分）；`score` 為排序分數（緊迫度、是否塞得進、觀察到的時段偏好；3 小時內被拒絕過的會降分）；`reasons` 為文字理由。

## log 事件種類

`created`、`planned`、`started`、`done`（`data.actual_h` 為實際工時）、`deferred`、`skipped`（`data.missed` 為停機期間錯過的週期）、`dropped`、`rec_accepted`、`rec_rejected`（`data.instead` 為改做的 Item）、`estimate_corrected`（預估與實際的比值，或使用者修正）、`said`（對話中的狀態訊號，`data.signal`）、`alert_feedback`（`reaction`：accepted / ignored / not_needed）、`updated`。log 只能追加。

`started` / `done` 會同步 Item 狀態，習慣與週期規則除外（習慣的 done 只計次）。

## Observation

| 欄位 | 意義 |
| --- | --- |
| `text` | 行為模式描述 |
| `scope` | 適用類別或情境 |
| `params` | 引擎可用的數值：`effort_bias`（實際 ÷ 預估）、`preferred_hours`、`lead_days`、`defer_rate`、`after_skip_prefers`、`habit_rate` |
| `confidence` | 0–1；低於 0.5 的不影響估算與推薦 |
| `status` | `hypothesis` / `confirmed` / `rejected`（rejected 永不使用） |
| `origin` | `stats`（每日統計）/ `reflection`（每週反思）/ `user` |

## Alert

| 欄位 | 意義 |
| --- | --- |
| `rule` | `tension`、`slack_negative`、`habit_behind`、`project_stale`、`deferred_idle`、`daily_review`、`startup_summary` |
| `level` | `info` / `ask` / `warn`；同一規則與 Item 組合在訊號持續惡化時依序升級 |
| `signals` | 觸發當下的訊號快照 |
| `options` | `relieve`（減壓方案）、`confirm_estimate`（要確認的估算）、`recommend_tomorrow` |

## calendar_view

圖層：`fixed`、`plan`、`deadline`、`recurring`。`display`：`block` 時段、`band` 整段帶狀、`marker` 時間點。`dashed: true` 為尚未實例化的週期期次或未來期間；`label` 帶 `~` 表示預估。
