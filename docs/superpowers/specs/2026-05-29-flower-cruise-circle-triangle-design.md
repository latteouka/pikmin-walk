# 種花巡航：圓＋大三角形跨圈拼花 — 設計文件

**Created**: 2026-05-29

## 背景與問題

花朵巡航的「多朵 plant 模式」目前在每朵花用 `dwell_radius_m: 2.5` 繞小圈，並在朵與朵之間「走路」（great-circle）。2.5m 的半徑整個埋在 iOS simulate-location 的 **65m `horizontalAccuracy` 雜訊地板**裡，Pikmin Bloom 分不出是移動還是雜訊 → **實質上沒在種花**（使用者回報「趨近於無效」）。

單朵 plant 模式已用 70m 圓繞圈避開這道牆，但多朵模式沒有。

## 目標

把多朵 plant 模式改成「**每朵花原地畫一個圓＋大三角形的圖**、朵與朵之間用 teleport 飛、無限循環」，並讓每次造訪都跳出 65m 雜訊、確實種花。

## 核心限制（為什麼這樣設計）

- **要有效 → 移動範圍必須 > 65m**（跳出 GPS 雜訊地板）。
- **速度必須壓在步行 ~4.5 km/h**（Pikmin Bloom 只計 <6 km/h 的移動，太快不算步、不種花）。
- 由上兩點：走完一個 >65m 的封閉圖形需要數分鐘，**60s/朵畫不完**。
- **解法**：每朵花維護一條固定的「圓＋三角形」polyline 與「目前進度 offset」；每次造訪只走 60s（≈75m）的一段、累加 offset，**圖跨多圈慢慢拼出來**（約 12–13 圈畫滿一輪後重疊續種）。每一段 75m 的位移 > 65m，故每次造訪都有效。

## 圖形幾何

固定相對每朵花中心的一條連續 polyline：

- **圓**：半徑 **70 m**（沿用已驗證會種花的尺寸）。
- **大三角形**：正三角形，外接半徑（角到中心）**95 m**；邊離中心最近 ~47 m（= 95/2），切過內部覆蓋中央。三個角伸出 65m 雜訊圈外。
- **組合**：圓（angle 0→360，取樣為點序列）→ 連接段 → 三角形（v1→v2→v3→v1）為一條連續路徑。
- 總長 ≈ 圓 440 m + 三角形 493 m + 連接段 ≈ **~940 m**。
- 每次造訪走 `dwell_each_s × walking_mps`（60 s × ~1.25 m/s ≈ **75 m**）。畫滿一輪 ≈ 940/75 ≈ **13 次造訪**。

半徑數字（70 / 95 / dwell 60s）可由 client 參數覆寫，後端帶預設。

## 行為規格

### 多朵 plant（取代現有走路邏輯）

```
teleport 到 route[0] 的圖形進度點（loop_walk_started 後）
無限迴圈 lap:
  for flower in route:
    offset = progress[flower]                      # 該朵目前進度（公尺，初始 0）
    teleport 到 figure_point(flower, offset)       # tick note="teleport", step_m=0
    沿 figure 走 dwell_each_s 秒（每 tick set_location + jitter）
        tick note="figure", step_m=相鄰點 haversine
    progress[flower] = (offset + 實走距離) mod L    # 累加、超過總長回繞
  # loop 永遠為真 → 不 break
```

- `progress` 為 runner 區域內的 dict，每次 start 重置；**不寫入磁碟**（runtime 狀態）。
- 暫停/恢復/停止沿用既有機制（`while session.paused` 迴圈、`CancelledError` → `stopped`）。

### 單朵 plant

route 長度為 1 時：原地**無限**沿 figure 走（offset 持續累加、回繞），不 teleport、不分段。等價於把整個圖一直重複畫。

### 採花 / 快速採花（quick_harvest / harvest）

**完全不變**。

## 後端變更

### `pikmin_walk.py`

新增純幾何產生器（**先寫單元測試，TDD**）：

```python
def figure_points(center, circle_r_m, tri_r_m, step_m=2.0) -> list[tuple[float,float]]:
    """回傳「圓＋大三角形」的連續經緯度點序列（閉合）。"""

def figure_walk(center, circle_r_m, tri_r_m, *, rng, start_offset_m=0.0,
                speed_kmh, ...):
    """從 start_offset_m 起，沿 figure_points 以 speed 步行，帶 GPS jitter，
    yield (position, cumulative_offset_m)。"""
```

- 半徑、取樣間距以公尺計，用既有 `destination_point` / `haversine_m` 做大地換算。
- jitter 沿用既有 `jitter_position`（σ≈1.0m）風格。

### `server.py` `_handle_start_loop_walk`

- plant 分支（route ≥ 2）改寫為上面的「teleport-between + per-flower offset + figure_walk」邏輯。
- 單朵分支（route == 1）改用 `figure_walk` 無限版（取代現有 `orbit_at_flower(inf)` 的純圓）。
- 接收新參數：`circle_r_m`(預設70)、`tri_r_m`(預設95)、`dwell_each_s`(預設60)。plant 模式 `loop` 視為恆真。
- 既有 `orbit_at_flower`（圓繞圈）若不再被任何分支使用則移除；若採花分支仍用則保留。

## 前端變更 `static/flower-cruise.html`

- **plant 模式送出**：`mode:'plant'`、`route`、`loop:true`、`dwell_each_s`(預設60)、`circle_r_m:70`、`tri_r_m:95`。**移除** `dwell_radius_m:2.5`、`dwell_last_s`。
- plant 模式 UI：
  - **速度滑桿隱藏**（鎖步行；單朵本來就不適用，多朵現在也是）。
  - **loop checkbox 強制開／隱藏**（figure 仰賴循環拼花）。
  - dwell 欄位改成單一「**每朵停留秒數**」(預設 60)，移除「久留最後」。
- quick_harvest 模式 UI 維持原樣（loop、速度、無 dwell 一切照舊）。

## 測試

### 單元（TDD，`pikmin_walk`）

- `figure_points`：點落在圓上的距中心 ≈ circle_r、三角形角距中心 ≈ tri_r；序列連續（相鄰點距 ≈ step_m）；閉合。
- `figure_walk`：從 offset=0 與 offset=K 起步位置正確；走 T 秒的累積距離 ≈ speed×T；offset 超過總長正確回繞。

### E2E（Playwright，CLAUDE.md 強制）

寫進 `.claude/dev/e2e.md`：
- 多朵 plant：貼 ≥2 朵 → 預覽 → 開始 → 收到 `loop_walk_started`、交替出現 `note:"teleport"` 與 `note:"figure"` 的 tick、`loop_lap` 遞增 → 停止收到 `stopped`。
- 單朵 plant：貼 1 朵 → 開始 → 持續收到 `note:"figure"` tick、無 teleport。
- plant 模式速度滑桿隱藏、loop 強制開。

## 非目標（Out of scope）

- 採花 / 快速採花行為。
- 圖形尺寸的進階 UI（先用預設 70/95，必要時才加）。
- WiFi/VPN tunnel 問題（另案）。
```
