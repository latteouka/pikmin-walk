**Created**: 2026-05-17

# 原地繞單朵花（種花模式）

## 目的

在 `/flower-cruise` 的「種花」模式下，支援使用者在原地對**單一座標**無限繞圈，
持續種花。使用者明確表示不在乎花瓣浪費（同一片地反覆種花、petal 重疊），
要的就是「待在原地一直繞同一朵花」。

## 背景

現況：花朵巡航的種花模式**強制需要 ≥2 朵花** —— UI 的 `預覽路線`／`按照順序`
按鈕與 `開始` 都有 `length < 2` 防呆，伺服器 `_handle_start_loop_walk` 也對
`len(route) < 2` 直接報錯。

繞花引擎其實已經存在：`server.py` 的 `orbit_at_flower()` 呼叫
`pikmin_walk.circle_walk()`，會在一個中心點周圍走帶角度抖動的小圓圈。
目前它只在「到達某朵花」時被呼叫固定 `dwell` 秒數。本功能等於把
`orbit_at_flower` 拉出來、對單一座標無限執行。

## 關鍵設計約束：速度

`circle` profile 的速度刻意設為 **4.5 km/h**，原始碼註解：
`# actual walking pace; Niantic's ceiling is ~6`。Pikmin Bloom 只把
**~6 km/h 以下**的移動算成走路並種花。

花朵巡航頁的速度滑桿（3–25 km/h，預設 19）是給**花與花之間移動**用的。
原地繞花若套用滑桿，Pikmin Bloom 會不算步數、不種花 —— 功能形同失效。

**決議**：原地繞花一律使用 `circle` profile 的步行速度（~4.5 km/h），
速度滑桿對此子模式不生效。這與現有多朵種花的「每朵繞圈」行為一致
（滑桿本來就只影響花間移動，不影響 orbit）。

## 行為規格

### 觸發
種花模式下，座標框解析出**剛好 1 個座標**時，頁面自動進入「原地繞花」子模式
（自動偵測，無新增開關）。

### 繞圈參數
- 半徑：**固定 10m**（使用者指定）。
- 速度：~4.5 km/h（`circle` profile，見上）。
- 時長：**無限**，直到使用者按「停止」。「跑完後循環」勾選框對此模式無關緊要。

### UI 狀態
- 單朵 + 種花 → 啟用「開始」；`預覽路線`／`按照順序` 維持停用（單朵無路線可規劃）。
- `previewInfo` 顯示說明：原地繞花 · 10m 圈 · ~4.5 km/h 步行。
- 地圖畫一個 10m 半透明圈標示繞圈範圍，圈在該座標上。
- 種花模式下若是單朵 → 隱藏「每朵繞圈秒數／最後一朵久留秒數」欄位
  （對單朵無意義）。

## 元件與改動

### Server — `server.py`

**`_handle_start_loop_walk`**
- 放寬 `len(raw_route) < 2` 防呆：1 個座標**只有 `mode == "plant"`** 時允許，
  其他模式（harvest／quick_harvest）仍回 `error`：「採花模式至少需要 2 朵花」。
- `runner()` 加分支：`len(route) == 1` →
  1. 瞬移到該座標
  2. 送 `loop_walk_started`（`loop_route` = 單點，`loop_distance_km` = 0）
  3. 以**無限時長**呼叫 `orbit_at_flower`
  4. 停止時走既有 `asyncio.CancelledError` → `stopped` 路徑

**`orbit_at_flower`**（巢狀函式）
- 新增選用參數 `report_steps`（預設 `False`，多朵路徑行為完全不變）。
- `report_steps=True` 時：每個 `tick` 帶真實 `step_m`（相鄰繞圈點的 haversine
  距離），讓前端統計列的「km」會累加。對原地種花來說「走了多少距離」才是
  有意義的數字。
- 半徑沿用既有 `get_radius` 機制：client 在此請求送 `dwell_radius_m: 10`。

### UI — `static/flower-cruise.html`

- 新增 `isSingleFlowerPlant()` = `getCruiseMode() === 'plant' && parsedFlowers.length === 1`。
- `updateParseInfo()`：若 `isSingleFlowerPlant()` → 啟用「開始」、設定 `previewInfo`
  說明文字、地圖畫 10m 圈。
- 模式 radio `change` 事件 → 重新評估啟用狀態（先貼好 1 朵再切到種花也能正確啟用）。
- `syncDwellVisibility()`：單朵種花時隱藏 dwell-row。
- `$btnStart` click：若 `isSingleFlowerPlant()` → 送
  `start_loop_walk { route:[parsedFlowers[0]], mode:'plant', dwell_radius_m:10, speed_kmh, loop }`；
  否則走既有 `previewedRoute` 流程。
- **軌跡上限**：原地繞花會跑數小時，把 `trailLine` 限制在最近約 600 點，
  避免 Leaflet polyline 無限增長導致瀏覽器卡頓。

## 資料流

```
貼 1 個座標 → parseFlowers → (種花模式) 自動進入原地繞花
  → 按「開始」
  → WS: start_loop_walk { route:[c], mode:'plant', dwell_radius_m:10 }
  → server 瞬移到 c → 無限 orbit_at_flower(report_steps=True)
  → tick 串流（帶真實 step_m）
  → 按「停止」→ CancelledError → stopped
```

## 錯誤處理

| 情況 | 行為 |
|---|---|
| 1 朵 + 採花／快速採花 | 「開始」維持停用；`previewInfo`：「採花模式至少需要 2 朵花」 |
| 0 朵 | 行為不變 |
| Server 收到 1 元素 route 但 mode 非 plant | 回 `error`：「採花模式至少需要 2 朵花」 |

## 測試

本專案無 test runner，依 `.claude/dev/e2e.md` 慣例做 Playwright-MCP 煙霧測試並記錄：

1. 種花模式貼 1 個座標 → 「開始」啟用、`預覽/按順序` 停用、地圖出現 10m 圈。
2. 切到採花模式 → 「開始」停用、顯示「至少需要 2 朵花」；切回種花 → 重新啟用。
3. 按「開始」→ 繞圈啟動、收到 `loop_walk_started` 與連續 `tick`、統計「km」累加。
4. 按「停止」→ 收到 `stopped`、按鈕狀態復原。
5. 多朵種花（≥2）流程不受影響（迴歸）。

繞圈數學（`circle_walk`）不在本次改動範圍、已驗證過，不需新單元測試。
