# 種花巡航：圓＋大三角形跨圈拼花 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把花朵巡航「多朵 plant 模式」改成每朵花原地畫「圓(70m)＋大三角形(95m)」圖、朵間 teleport、無限循環跨圈拼花，每次造訪都跳出 65m GPS 雜訊地板。

**Architecture:** 純幾何放 `pikmin_walk.py`（`figure_points` 產生閉合 polyline、`point_at_offset` 依弧長取點，皆可單元測試）。`server.py` 的 plant 分支用「per-flower offset + teleport-between + 走 60s 弧段」驅動；單朵則無限走整個圖。前端 `flower-cruise.html` 改送圖形參數、鎖速度、強制 loop。

**Tech Stack:** Python 3.11+ / Starlette WebSocket / 既有大地幾何 helper（`destination_point`/`haversine_m`/`step_toward`）；測試用 pytest（`uv run --with pytest`）；前端純 JS + Leaflet；E2E 用 Playwright。

**設計來源：** `docs/superpowers/specs/2026-05-29-flower-cruise-circle-triangle-design.md`

---

## File Structure

- `pikmin_walk.py` — 新增 `_segment_points`、`figure_points`、`point_at_offset`（純幾何，緊接在 `jitter_position` 之後，約 line 286）。
- `tests/test_figure.py` — 新建，幾何單元測試。
- `server.py` — 改寫 `_handle_start_loop_walk` 的 plant 行為（單朵 + 多朵），約 line 1866–2020。
- `static/flower-cruise.html` — plant 模式送出參數 + UI（速度隱藏、loop 強制、dwell 單欄）。
- `.claude/dev/e2e.md` — 新增 E2E test stories 與結果。
- `CLAUDE.md`（專案根）— 更新「花朵巡航」設計決策段落。

---

## Task 1: 幾何 — `_segment_points` + `figure_points`

**Files:**
- Modify: `pikmin_walk.py`（在 `jitter_position` 後，約 line 286 插入）
- Test: `tests/test_figure.py`

- [ ] **Step 1: Write the failing test**

建立 `tests/test_figure.py`：

```python
import math
from pikmin_walk import figure_points, haversine_m

CENTER = (43.0686, 141.3507)  # 札幌駅

def _dist(p):
    return haversine_m(CENTER, p)

def test_figure_points_closed_loop():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    assert len(pts) > 50
    # 閉合：頭尾同點
    assert haversine_m(pts[0], pts[-1]) < 0.5

def test_figure_points_radii():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    dists = [_dist(p) for p in pts]
    # 三角形角 ~95m 是最遠點
    assert max(dists) == max(dists) and 93.0 <= max(dists) <= 97.0
    # 圓上的點 ~70m 存在
    assert any(abs(d - 70.0) < 1.5 for d in dists)
    # 起點在正北、圓半徑上
    assert abs(_dist(pts[0]) - 70.0) < 1.0

def test_figure_points_continuous():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    gaps = [haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    # 相鄰點間距不超過取樣步距太多
    assert max(gaps) <= 4.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest tests/test_figure.py -v`
Expected: FAIL with `ImportError: cannot import name 'figure_points'`

- [ ] **Step 3: Write minimal implementation**

在 `pikmin_walk.py` 的 `jitter_position` 函式後（約 line 286）插入：

```python
def _segment_points(a: Waypoint, b: Waypoint, step_m: float) -> list[Waypoint]:
    """Sample a great-circle segment from `a` (exclusive) to `b` (inclusive).

    The first point returned is `step_m` past `a`; the last is exactly `b`
    (step_toward snaps to the target on overshoot). `a` itself is omitted so
    callers can chain segments without duplicating the shared vertex.
    """
    dist = haversine_m(a, b)
    n = max(1, int(math.ceil(dist / step_m)))
    return [step_toward(a, b, step_m * k) for k in range(1, n + 1)]


def figure_points(
    center: Waypoint,
    circle_r_m: float,
    tri_r_m: float,
    step_m: float = 3.0,
) -> list[Waypoint]:
    """Closed polyline of a circle (radius `circle_r_m`) overlaid with a large
    equilateral triangle (circumradius `tri_r_m`), centered on `center`.

    Path order (one continuous closed loop):
      circle (north → full turn) → radial connector out (circle_r → tri_r)
      → triangle v1→v2→v3→v1 → radial connector back in → start.

    Returned as (lat, lon) points spaced ~`step_m` apart; pts[0] == pts[-1].
    """
    pts: list[Waypoint] = []
    # 1. circle, angle 0..2π (north = bearing 0)
    circ_n = max(12, int(round(2 * math.pi * circle_r_m / step_m)))
    for k in range(circ_n + 1):
        ang = 2 * math.pi * k / circ_n
        pts.append(destination_point(center, ang, circle_r_m))
    # pts[-1] now == pts[0] (north @ circle_r); start the chain from there.
    north_in = destination_point(center, 0.0, circle_r_m)
    v1 = destination_point(center, 0.0, tri_r_m)
    v2 = destination_point(center, 2 * math.pi / 3, tri_r_m)
    v3 = destination_point(center, 4 * math.pi / 3, tri_r_m)
    # 2. connector out (circle_r → tri_r, due north)
    pts += _segment_points(north_in, v1, step_m)
    # 3. triangle
    pts += _segment_points(v1, v2, step_m)
    pts += _segment_points(v2, v3, step_m)
    pts += _segment_points(v3, v1, step_m)
    # 4. connector back in → closes loop at the circle start
    pts += _segment_points(v1, north_in, step_m)
    return pts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest pytest tests/test_figure.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add pikmin_walk.py tests/test_figure.py
git commit -m "feat(geo): figure_points — closed circle+triangle planting polyline"
```

---

## Task 2: 幾何 — `point_at_offset`

**Files:**
- Modify: `pikmin_walk.py`（緊接 `figure_points` 之後）
- Test: `tests/test_figure.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_figure.py` 末尾追加：

```python
from pikmin_walk import point_at_offset

def _total_len(pts):
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

def test_point_at_offset_zero_is_start():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    assert haversine_m(point_at_offset(pts, 0.0), pts[0]) < 0.01

def test_point_at_offset_wraps():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    L = _total_len(pts)
    # offset == L 回到起點
    assert haversine_m(point_at_offset(pts, L), pts[0]) < 0.5
    # 超過一圈等於繞回去
    a = point_at_offset(pts, 5.0)
    b = point_at_offset(pts, L + 5.0)
    assert haversine_m(a, b) < 0.5

def test_point_at_offset_advances_by_distance():
    pts = figure_points(CENTER, 70.0, 95.0, step_m=3.0)
    p0 = point_at_offset(pts, 100.0)
    p1 = point_at_offset(pts, 110.0)
    # 沿線推進 10m，兩點直線距離 ≤ 10m（弧線略小）且 > 8m
    d = haversine_m(p0, p1)
    assert 8.0 <= d <= 10.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest tests/test_figure.py -v`
Expected: FAIL with `ImportError: cannot import name 'point_at_offset'`

- [ ] **Step 3: Write minimal implementation**

在 `pikmin_walk.py` 的 `figure_points` 之後插入：

```python
def point_at_offset(points: list[Waypoint], offset_m: float) -> Waypoint:
    """Point at arc-length `offset_m` along the closed polyline `points`.

    `offset_m` is taken modulo the total length, so it wraps seamlessly —
    callers advance offset every tick and never need to reset it.
    """
    if len(points) < 2:
        return points[0]
    seg = [haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(seg)
    if total <= 0:
        return points[0]
    s = offset_m % total
    for i, d in enumerate(seg):
        if s <= d:
            return points[i] if d <= 0 else step_toward(points[i], points[i + 1], s)
        s -= d
    return points[-1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest pytest tests/test_figure.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add pikmin_walk.py tests/test_figure.py
git commit -m "feat(geo): point_at_offset — arc-length lookup with seamless wrap"
```

---

## Task 3: server.py — plant 分支改用 figure（單朵 + 多朵 teleport）

**Files:**
- Modify: `server.py:1899`（import 行）、`server.py:1879-1880`（參數）、`server.py:1897-2020`（runner 主體）

> **背景：** 現有 runner 在 `len(route)==1` 走純圓 `orbit_at_flower(inf)`；多朵時 plant 與 harvest 共用「走路 + 小圈」。本任務把 **plant** 抽出來改用 figure，**harvest / quick_harvest 維持不變**（仍用 `orbit_at_flower`，故該 nested 函式保留）。

- [ ] **Step 1: 擴充 import（line 1899）**

把：
```python
        from pikmin_walk import haversine_m, step_toward, jitter_position, circle_walk, PROFILES
```
改為：
```python
        from pikmin_walk import (haversine_m, step_toward, jitter_position,
                                 circle_walk, figure_points, point_at_offset, PROFILES)
```

- [ ] **Step 2: 新增圖形參數（在 line 1880 `quick_dwell_s = ...` 後插入）**

```python
    circle_r_m = float(msg.get("circle_r_m", 70.0))
    tri_r_m = float(msg.get("tri_r_m", 95.0))
```

- [ ] **Step 3: 在 `orbit_at_flower` 定義後（line 1942 之後、`try:` 之前）新增 `walk_figure` nested coroutine**

```python
        WALK_KMH = PROFILES["circle"].nominal_kmh   # ~4.5 km/h，鎖步行
        FIG_MPS = WALK_KMH * 1000.0 / 3600.0
        FIG_JITTER_M = 1.0

        async def walk_figure(points, offset: float, seconds: float) -> float:
            """沿閉合 polyline `points` 從 `offset`(公尺) 走 `seconds` 秒。
            回傳新的 offset。seconds=inf 時無限走（按停止才離開）。"""
            elapsed = 0.0
            prev = None
            while elapsed < seconds:
                offset += FIG_MPS * tick_s
                pos = point_at_offset(points, offset)
                noisy = jitter_position(pos, FIG_JITTER_M, rng)
                await session.set_location(*noisy)
                step_m = haversine_m(prev, noisy) if prev is not None else 0.0
                prev = noisy
                await ws.send_json({
                    "type": "tick", "lat": noisy[0], "lon": noisy[1],
                    "step_m": step_m, "note": "figure",
                })
                await asyncio.sleep(tick_s)
                elapsed += tick_s
                while session.paused:
                    await asyncio.sleep(0.5)
            return offset
```

- [ ] **Step 4: 改寫單朵分支（line 1958-1960）**

把：
```python
            if len(route) == 1:
                await orbit_at_flower(route[0], float("inf"), report_steps=True)
                return
```
改為：
```python
            if len(route) == 1:
                # 種花：原地無限畫整個「圓＋大三角形」圖。
                pts = figure_points(route[0], circle_r_m, tri_r_m)
                await walk_figure(pts, 0.0, float("inf"))
                return
```

- [ ] **Step 5: 在 `lap = 0`（line 1962）之前，預先算好多朵 plant 的每朵圖與進度**

在 `lap = 0` 上方插入：
```python
            # plant 多朵：每朵一條固定 figure + 各自的弧長進度（offset），
            # 跨圈累加，把整個圖一段一段拼出來。
            figpts: dict[int, list] = {}
            progress: dict[int, float] = {}
            if mode == "plant":
                figpts = {i: figure_points(f, circle_r_m, tri_r_m)
                          for i, f in enumerate(route)}
                progress = {i: 0.0 for i in range(len(route))}
```

- [ ] **Step 6: 在 lap 迴圈裡，於 quick_harvest 分支之後、現有 plant/harvest 走路邏輯（line 1985 `pos = route[0]`）之前，插入 plant 分支**

在 `continue`（quick_harvest 分支結尾，line 1983）之後插入：
```python
                if mode == "plant":
                    for i, flower in enumerate(route):
                        pts = figpts[i]
                        off = progress[i]
                        start = point_at_offset(pts, off)
                        await session.set_location(*start)
                        await ws.send_json({
                            "type": "tick", "lat": start[0], "lon": start[1],
                            "step_m": 0.0, "note": "teleport",
                        })
                        progress[i] = await walk_figure(pts, off, dwell_each_s)
                    continue  # plant 永遠循環
```

> 之後現有的 `pos = route[0] ... orbit_at_flower ...`（line 1985-2018）成為 **harvest 專用**的走路分支，原樣保留。`if not loop_mode: break` 也保留（harvest 用）。

- [ ] **Step 7: 手動冒煙驗證（無裝置也能跑邏輯）**

建立暫時腳本 `/tmp/smoke_plant.py` 驗證 runner 會送出 teleport/figure/loop_lap（mock 掉裝置）：

```python
import asyncio, types, server
from unittest.mock import AsyncMock

async def main():
    msgs = []
    ws = types.SimpleNamespace(send_json=AsyncMock(side_effect=lambda m: msgs.append(m)))
    server.session.set_location = AsyncMock()
    server.session.running_task = None
    server.session.paused = False
    await server._handle_start_loop_walk(ws, {
        "type": "start_loop_walk", "mode": "plant", "loop": True,
        "route": [[43.0686, 141.3507], [43.07, 141.36]],
        "dwell_each_s": 2, "circle_r_m": 70, "tri_r_m": 95,
    })
    await asyncio.sleep(6)
    server.session.running_task.cancel()
    notes = [m.get("note") for m in msgs if m.get("type") == "tick"]
    print("has teleport:", "teleport" in notes)
    print("has figure:", "figure" in notes)
    print("loop_lap count:", sum(1 for m in msgs if m.get("type") == "loop_lap"))

asyncio.run(main())
```

Run: `uv run python /tmp/smoke_plant.py`
Expected: `has teleport: True` / `has figure: True` / `loop_lap count: >= 1`

- [ ] **Step 8: Commit**

```bash
git add server.py
git commit -m "feat(server): plant mode draws circle+triangle figure, teleports between flowers, loops"
```

---

## Task 4: 前端 `flower-cruise.html` — 參數與 UI

**Files:**
- Modify: `static/flower-cruise.html`（速度列 ~428、loop 列 ~457、dwell 列 ~462-471、`$dwellLast` ~546、`syncDwellVisibility` ~563-571、$btnStart handler ~898-929）

- [ ] **Step 1: 速度列加 id，方便 plant 時隱藏（line 428）**

把：
```html
    <div class="control-row">
      <label>速度</label>
```
改為：
```html
    <div class="control-row" id="speedRow">
      <label>速度</label>
```

- [ ] **Step 2: dwell 列改成單一「每朵停留秒數」(預設 60)，移除「久留最後」(line 462-471)**

把整個 `<div class="dwell-row" ...> ... </div>` 換成：
```html
    <div class="dwell-row" id="dwellRow" style="display:none;">
      <div class="field">
        <span class="field-label">每朵停留秒數</span>
        <input type="number" id="dwellEach" min="10" max="300" value="60" step="5">
      </div>
    </div>
```

- [ ] **Step 3: 移除 `$dwellLast` 宣告（line 546）**

刪掉這一行：
```javascript
const $dwellLast = document.getElementById('dwellLast');
```

- [ ] **Step 4: 擴充 `syncDwellVisibility` → plant 時隱藏速度、強制 loop（line 563-571）**

把：
```javascript
function syncDwellVisibility() {
  const show = getCruiseMode() === 'plant' && parsedFlowers.length >= 2;
  $dwellRow.style.display = show ? 'grid' : 'none';
}
$modeRadios.forEach(r => r.addEventListener('change', () => {
  syncDwellVisibility();
  applySingleFlowerPlant();
}));
syncDwellVisibility();
```
改為：
```javascript
const $speedRow = document.getElementById('speedRow');
const $loopRow = document.querySelector('.loop-row');
function syncModeUI() {
  const isPlant = getCruiseMode() === 'plant';
  // dwell 欄位：plant 多朵才顯示
  $dwellRow.style.display = (isPlant && parsedFlowers.length >= 2) ? 'grid' : 'none';
  // plant 鎖步行速度 → 隱藏速度滑桿
  $speedRow.style.display = isPlant ? 'none' : 'flex';
  // plant 仰賴循環拼花 → 強制開、隱藏 loop 開關
  if (isPlant) { $loopChk.checked = true; }
  $loopRow.style.display = isPlant ? 'none' : 'flex';
}
$modeRadios.forEach(r => r.addEventListener('change', () => {
  syncModeUI();
  applySingleFlowerPlant();
}));
syncModeUI();
```

> 註：`.control-row` 與 `.loop-row` 預設為 flex（見 CSS）；若不是 flex 請改用 `''` 還原預設。`syncDwellVisibility()` 在別處若有被呼叫，一併改成 `syncModeUI()`。

- [ ] **Step 5: 改寫 $btnStart handler 的 plant 送出（line 898-929）**

把整段 `$btnStart.addEventListener('click', () => { ... });`（898-929）換成：
```javascript
$btnStart.addEventListener('click', () => {
  const FIGURE = { circle_r_m: 70, tri_r_m: 95 };
  // 原地單朵種花：無限畫整個圖（速度鎖步行）
  if (isSingleFlowerPlant()) {
    ws.send(JSON.stringify({
      type: 'start_loop_walk',
      route: [parsedFlowers[0]],
      loop: true,
      mode: 'plant',
      ...FIGURE,
    }));
    return;
  }
  if (!previewedRoute) return;
  const route = previewedRoute.ordered.slice(startFromIndex);
  if (route.length < 2) return;
  const mode = getCruiseMode();
  if (mode === 'plant') {
    // 多朵種花：每朵畫圖、停 dwell 秒、teleport 飛下一朵、無限循環。
    ws.send(JSON.stringify({
      type: 'start_loop_walk',
      route,
      loop: true,
      mode: 'plant',
      dwell_each_s: parseInt($dwellEach.value) || 60,
      ...FIGURE,
    }));
    return;
  }
  // 採花 / 快速採花：維持原行為
  ws.send(JSON.stringify({
    type: 'start_loop_walk',
    speed_kmh: parseInt($speed.value),
    route,
    loop: $loopChk.checked,
    mode,
    dwell_each_s: 0,
    dwell_last_s: 0,
    dwell_radius_m: 2.5,
    quick_dwell_s: 15,
  }));
});
```

- [ ] **Step 6: 手動驗證 UI（無裝置即可）**

Run: `python3 -c "import ast,re;print('ok')"`（佔位）；實際用瀏覽器：
1. `make start-ipad`（或 `uv run server.py --port 7766`）
2. 開 `http://localhost:7766/flower-cruise`
3. 切到「🌱 種花」→ 確認**速度滑桿消失、loop 開關消失**；貼 2 朵 → 「每朵停留秒數」欄出現、預設 60。
4. 切回「🌸 採花」→ 速度滑桿、loop 開關回來。
Expected: 上述切換正確、console 無錯誤（`browser_console_messages`）。

- [ ] **Step 7: Commit**

```bash
git add static/flower-cruise.html
git commit -m "feat(ui): flower-cruise plant mode sends figure params, locks speed, forces loop"
```

---

## Task 5: E2E test stories（`.claude/dev/e2e.md`）+ 執行

**Files:**
- Modify: `.claude/dev/e2e.md`

> 需要 iPad 經 **USB** 連線（DVT 正常）。種花是否真生效需真機觀察；E2E 主要驗證 WS 訊息流與地圖路徑、UI 行為。

- [ ] **Step 1: 在 `.claude/dev/e2e.md` 新增 stories**

```markdown
## 種花巡航：圓＋大三角形拼花

### Test Scenarios

1. **Story:** 多朵種花 → teleport-between + figure tick + 無限循環
   - **Steps:** USB 連 iPad → `make start-ipad` → 開 /flower-cruise → 貼 ≥2 朵 → 預覽 → 切「種花」→ 開始
   - **Expected Result:** 收到 `loop_walk_started`；tick 交替出現 `note:"teleport"`(step_m=0) 與 `note:"figure"`；`loop_lap` 持續遞增；地圖每朵畫出圓+三角形輪廓；按停止收到 `stopped`
   - **Status:** ⏳ In Progress
   - **Last Updated:** 2026-05-29

2. **Story:** 單朵種花 → 原地無限畫整個圖
   - **Steps:** 貼 1 朵 → 切「種花」→ 開始
   - **Expected Result:** 持續 `note:"figure"` tick、無 teleport；地圖畫出圓+三角形；停止收到 `stopped`
   - **Status:** ⏳ In Progress
   - **Last Updated:** 2026-05-29

3. **Story:** plant 模式 UI 鎖定
   - **Steps:** 切「種花」
   - **Expected Result:** 速度滑桿隱藏、loop 開關隱藏（強制循環）、多朵時出現「每朵停留秒數」=60；切回採花全部還原
   - **Status:** ⏳ In Progress
   - **Last Updated:** 2026-05-29
```

- [ ] **Step 2: 執行 E2E（Playwright MCP）**

先讀 `~/projects/E2E-PITFALLS.md`。用 Playwright MCP：`browser_navigate` 到 `http://localhost:7766/flower-cruise`、`browser_evaluate` 注入 WS 監聽收集 server 訊息、操作 UI、`browser_console_messages` 確認無錯。逐一驗證 3 個 stories。

- [ ] **Step 3: 回填結果並 Commit**

把通過的 story `Status` 改成 ✅ Passing（失敗則記 ❌ 與原因，回 systematic-debugging）。

```bash
git add .claude/dev/e2e.md
git commit -m "test(e2e): flower-cruise circle+triangle planting stories + results"
```

---

## Task 6: 更新專案 `CLAUDE.md` 設計決策

**Files:**
- Modify: `CLAUDE.md`（「### 花朵巡航：原地繞單朵花」段落）

> **注意：** `CLAUDE.md` 工作區已有未 commit 改動（非本功能）。只改「花朵巡航」這一段，勿動其他。

- [ ] **Step 1: 改寫該段**

把「### 花朵巡航：原地繞單朵花」整段，更新為描述新行為：

```markdown
### 花朵巡航：圓＋大三角形拼花

- 種花模式（`mode: plant`）每朵花原地畫一個「圓(70m) ＋ 大三角形(角95m)」的固定 polyline（`pikmin_walk.figure_points`）。
- **多朵**：teleport 到每朵 → 沿該朵圖走 `dwell_each_s`(預設 60s ≈ 75m) → 飛下一朵 → **無限循環**。每朵維護自己的弧長 offset（`point_at_offset`），跨圈一段一段把整個圖拼出來（約 13 圈畫滿一輪）。
- **單朵**：原地無限畫整個圖。
- 速度鎖在 `circle` profile 的 ~4.5 km/h（前端隱藏速度滑桿）；plant 強制 loop。
- **為什麼半徑要 70/95m**：iOS simulate-location 的 `horizontalAccuracy` 硬性 65m，半徑 < 65m 整個埋在雜訊裡 → PB 分不出移動 → 不種花。圓 70m 已驗證有效；大三角形角伸到 95m、邊切過內部（最近 ~47m）覆蓋中央。
- **為什麼分多圈拼**：>65m 的圖走完要 ~6 分，60s/朵畫不完；分散到多圈既保持每朵停留短、又每次都有效種花（種花量只看總步行時間）。
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md flower-cruise design (circle+triangle figure)"
```

---

## Self-Review 結果

- **Spec 覆蓋：** 圖形幾何→T1/T2；多朵 plant teleport+offset+loop→T3 S5/S6；單朵→T3 S4；速度鎖/loop 強制/dwell 單欄→T4；harvest/quick_harvest 不變→T3（保留分支）；單元測試→T1/T2；E2E→T5；CLAUDE.md→T6。無遺漏。
- **Placeholder：** 無 TBD；每個 code step 皆有完整程式碼與指令。
- **型別一致：** `figure_points(center, circle_r_m, tri_r_m, step_m)`、`point_at_offset(points, offset_m)`、`walk_figure(points, offset, seconds)` 全程一致；server import 與呼叫相符。
```
