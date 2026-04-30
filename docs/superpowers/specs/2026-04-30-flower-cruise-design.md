# 花朵巡航（Flower Cruise）設計

**Created:** 2026-04-30

## Overview

新增「花朵巡航」功能：使用者貼上一組花朵 GPS 座標（Pikmin Bloom 種花地點），系統用 OSRM TSP 算出最佳拜訪順序，然後在這些花之間以直線（great-circle）無限繞圈走，模擬種花路線。

獨立 UI 頁面 `/flower-cruise`，跟現有 `/walk`（loop walk）平行存在，不影響主頁。

## Decisions（已釐清）

| 項目 | 決定 |
|---|---|
| 偏移每朵花 | **不偏移**，使用原座標 |
| 拜訪順序 | OSRM Trip API（TSP）— 只取順序，不取幾何 |
| 花跟花之間的路徑 | **直線（great-circle）**，不沿步行路網 |
| 路徑模式 | **無限 loop**（走完最後一朵接回第一朵） |
| 預設速度 | 19 km/h（slider 範圍 3–25 km/h，與既有 walk.html 一致） |
| 輸入 UX | textarea 貼上，分隔符寬鬆（換行/逗號/空白/tab 全吃） |
| UI 位置 | **獨立頁面** `/flower-cruise`，新 HTML 檔 |
| 走路階段 | 100% 複用 `_handle_start_loop_walk`，server 端不需要新 runner |

## Architecture

### 資料流

```
[使用者貼座標 → textarea]
       │ 前端 regex 解析（split 數字 → pair lat/lon）
       ▼
   花朵清單 [(lat, lon), ...]，地圖上顯示為粉色 markers
       │
       │ 點「解析並預覽」
       │   POST /api/preview-flower-cruise { flowers: [[lat, lon], ...] }
       ▼
   server: OSRM Trip API
       │   - 拿 data.waypoints[*].waypoint_index 取 TSP 順序
       │   - 重排 input flowers
       │   - 回傳 { ordered, distance_km, lap_eta_min, count }
       ▼
   前端畫:
       - markers（粉色 ●，含順序編號 1, 2, 3, ...）
       - polyline（直線連接 ordered + 回到 ordered[0]）
       - 顯示「N 朵花、一圈 X km、~Y 分鐘」
       │
       │ 點「開始巡航」
       │   WS { type: "start_loop_walk",
       │        route: [...ordered, ordered[0]],
       │        speed_kmh: 19 }
       ▼
   現有 _handle_start_loop_walk 接手
       - 直線連點走（great-circle）
       - 1 Hz tick + 1m position jitter
       - live speed slider 即時生效
       - 無限 loop
```

### 為什麼複用 `_handle_start_loop_walk`

現有 server.py:1120 的 `_handle_start_loop_walk` runner 已經實作了：
- 拿一個 ordered route → great-circle 連點走
- live speed control（讀 `session.live_speed_kmh` 每個 tick）
- pause / stop
- 1 Hz tick、1m GPS jitter
- 圈數累計（`type: "loop_lap"` event）

這正是花朵巡航需要的執行階段。**唯一新增的是「前置處理」**（接收 textarea、TSP 排序、preview 顯示）。

## File Changes

### 新增

| 檔案 | 內容 |
|---|---|
| `static/flower-cruise.html` | 拷貝 `walk.html` 結構，把「形狀按鈕 + 距離 slider」換成「textarea + 解析按鈕 + 預覽資訊」。WS 互動、地圖、speed slider 全部照搬 |

### 修改 `server.py`

加入：

1. **新 handler** `flower_cruise_page(request)` — 一行 FileResponse
2. **新 endpoint** `preview_flower_cruise(request)`：
   ```python
   # POST body: { flowers: [[lat, lon], ...] }
   # 1. validate (≥2 points, lat in [-90,90], lon in [-180,180])
   # 2. call OSRM trip API → 取 data["waypoints"][*]["waypoint_index"]
   # 3. 用 waypoint_index 重排 input
   # 4. 算 great-circle 一圈總距離
   # 5. return { ordered, distance_km, lap_eta_min, count }
   ```
3. **2 行 Route**：
   ```python
   Route("/flower-cruise", flower_cruise_page),
   Route("/api/preview-flower-cruise", preview_flower_cruise, methods=["POST"]),
   ```

OSRM 既有 helper `_osrm_trip_route` 回傳的是「routed geometry」，不是「TSP 順序」。需要寫個新 helper：

```python
async def _osrm_trip_order(waypoints):
    """Return TSP-optimized visit order (indices into input list)."""
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    data = await _osrm_fetch(
        f"/trip/v1/foot/{coords_str}?roundtrip=true&source=first"
    )
    if data is None or not data.get("waypoints"):
        return None
    return sorted(range(len(waypoints)),
                  key=lambda i: data["waypoints"][i]["waypoint_index"])
```

注意：`source=first` 確保起點固定，否則 OSRM 可能回任意起點。

### 不變

- `pikmin_walk.py` — 零改動
- `_handle_start_loop_walk` — 零改動
- WS protocol — 完全沿用 `start_loop_walk` / `loop_walk_started` / `loop_lap` / `tick` / `stopped` 等 message
- 主頁 `/` 與 `/walk` — 不動

## UI Layout

```
┌────────────────────────────────────────────────────┐
│  🌸 花朵巡航              [回主頁]                  │
├──────────────────────┬─────────────────────────────┤
│                      │ 花朵座標（一行一個）         │
│                      │ ┌─────────────────────────┐ │
│      Leaflet         │ │ 41.450779,31.795279     │ │
│      地圖            │ │ 41.449948,31.794372     │ │
│                      │ │ ...                     │ │
│   花朵 markers       │ └─────────────────────────┘ │
│   (粉色 ●)           │ [清空] [解析並預覽]         │
│                      │                             │
│   TSP 路線 polyline  │ ─────────────────────       │
│   (含順序數字)       │ 速度: [====●==] 19 km/h     │
│                      │                             │
│                      │ 預覽結果:                   │
│                      │   31 朵花                   │
│                      │   一圈 1.8 km / ~5.7 min    │
│                      │                             │
│                      │ [▶ 開始巡航] [⏸ 停止]       │
│                      │                             │
│                      │ 已跑: 第 3 圈 / 0:18:45     │
└──────────────────────┴─────────────────────────────┘
```

### Frontend 解析邏輯

```javascript
function parseFlowers(text) {
  const nums = text.match(/-?\d+\.?\d*/g) || [];
  const pairs = [];
  for (let i = 0; i + 1 < nums.length; i += 2) {
    const lat = parseFloat(nums[i]);
    const lon = parseFloat(nums[i + 1]);
    if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
      pairs.push([lat, lon]);
    }
  }
  return pairs;
}
```

特性：
- 一行 `41.450779,31.795279` ✔
- 一行 `41.450779 31.795279` ✔
- 多行混合空白 / tab ✔
- 連續貼上沒換行 `41.4 31.7 41.5 31.8` ✔
- 奇數個數字 → 丟掉最後一個（容錯）
- lat/lon 範圍外 → 該 pair 整個丟掉

### 地圖顯示

- 花朵 markers：粉色圓點，hover 顯示「花 #N」
- 預覽完成後加上 polyline：直線連 ordered[0] → ordered[1] → ... → ordered[-1] → ordered[0]
- 走路時：藍色小點即時跟著當前位置（既有 `tick` event）

## Error Handling

| 狀況 | 處理 |
|---|---|
| textarea 空 / 數字數量 < 4（不到 2 個 pair） | 前端按鈕停用，顯示「至少需要 2 朵花」 |
| OSRM API 連不上 / 超時 | server 回 500，前端 toast「OSRM 無法規劃路線」 |
| 花太多（OSRM Trip 上限通常 100 個） | server 檢查 `len(flowers) > 100` → 回 400 |
| 花全部很近（< 5m） | 不特別處理，OSRM 會自然處理；前端 polyline 看起來會很擠 |
| 沒連到手機 | 走「開始巡航」時走原本的 WS error path（`session.loc_sim is None`） |

## Caveats

- **沒有偏移**：使用者明確選擇不要偏移，所以踩到花上是預期行為。如果之後想加，整個 design 已經預留好（在 server 端 ordered list 出去之前對每個點加 offset 即可）。
- **TSP 起點固定 first**：OSRM 用 `source=first` 確保第一朵花是起點，否則每次預覽順序可能不一樣。
- **沒有經過裝置目前位置**：使用者按「開始巡航」時，`_handle_start_loop_walk` 會 teleport 到 ordered[0]（route 的第一個點），不從手機目前位置接過去。這跟現有 loop_walk 行為一致。
- **`/flower-cruise` 是新頁面**：使用者要從 `/` 主頁手動切過去（或在主頁加個連結）。是否在主頁加 nav 由實作決定，不在這個 spec 範圍。
- **OSRM TSP 在「散在公園/廣場」的花點上可能順序奇怪**：因為 TSP 是基於步行路網距離排序，但走的是直線。這是已知 trade-off，使用者可以接受。

## Out of Scope

- 偏移每朵花（已決定不做）
- 編輯花朵清單（移除 / 重新排序）— v1 簡化為「貼上 → 預覽 → 走」，不支援增刪
- 儲存花朵清單到 bookmarks / state.json — v1 不做
- 多個 flower set 切換 — v1 不做
- 跳過某些花 / 暫時忽略 — v1 不做

## Resolved

- 頁面 URL：**`/flower-cruise`**
- 主頁 `/` **加上「→ 花朵巡航」入口連結**（與既有 `/walk` 連結並列）

## Implementation Plan Hint（給下一步 writing-plans）

預估工作量：
- `static/flower-cruise.html` — 拷貝 walk.html 後改 ~80 行（換掉 shape 控制區、加 textarea + 解析）
- `server.py` — 加 ~50 行（page handler、preview endpoint、osrm_trip_order helper、Route 註冊）
- 沒有新 runner，沒有改 pikmin_walk.py
- 整體約 130 行 net add

測試：
- 手動 — 貼用戶提供的 31 個座標，確認 OSRM TSP 順序合理、polyline 畫對、開始巡航走得起來
- 不需要 unit test（純 glue code，OSRM 已被 trust）
