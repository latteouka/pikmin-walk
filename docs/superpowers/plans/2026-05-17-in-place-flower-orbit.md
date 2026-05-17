# 原地繞單朵花（種花模式）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `/flower-cruise` 的種花模式在只給 1 個座標時，自動以 10m 半徑、步行速度無限繞那個座標種花。

**Architecture:** 沿用既有 `orbit_at_flower()` → `circle_walk()` 繞圈引擎。伺服器放寬「≥2 朵」防呆、`runner()` 加單朵無限繞圈分支；前端自動偵測「種花模式 + 1 朵」並改用直接開始的流程。

**Tech Stack:** Python / Starlette / WebSocket（`server.py`）；vanilla HTML/JS + Leaflet（`static/flower-cruise.html`）。專案無 test runner，驗收用 Playwright-MCP 煙霧測試並記錄於 `.claude/dev/e2e.md`。

**Spec:** `docs/superpowers/specs/2026-05-17-in-place-flower-orbit-design.md`

---

## File Structure

| File | 改動 |
|---|---|
| `server.py` | `orbit_at_flower` 加 `report_steps` 參數；`_handle_start_loop_walk` 放寬防呆 + `runner()` 加單朵無限繞圈分支 |
| `static/flower-cruise.html` | 單朵原地繞花偵測、UI 狀態、地圖示意圈、開始流程、軌跡長度上限 |
| `.claude/dev/e2e.md` | 記錄 Playwright-MCP 煙霧測試結果 |
| `CLAUDE.md`（專案根） | 補一行原地繞花行為說明 |

**TDD 註記：** 本功能是 UI 接線 + 一個伺服器分支，繞圈數學（`circle_walk`）不在改動範圍且已驗證。依專案「Smart TDD」不需新單元測試；驗收為 `py_compile` 語法檢查 + Playwright-MCP 煙霧測試。

---

## Task 1: Server — 單朵無限繞圈支援

**Files:**
- Modify: `server.py` — `orbit_at_flower`（約 1852–1879）、`_handle_start_loop_walk` 防呆（約 1837–1839）、`runner()`（約 1881–1896）

- [ ] **Step 1: `orbit_at_flower` 加 `report_steps` 參數**

把整個 `orbit_at_flower` 巢狀函式（從 `async def orbit_at_flower` 到對應 `while session.paused:` 區塊結尾）替換為：

```python
        async def orbit_at_flower(
            center: tuple[float, float],
            duration_s: float,
            report_steps: bool = False,
        ) -> None:
            """繞 center 走小圓圈 duration_s 秒（Pikmin Bloom 需要持續位移才會種花）。

            report_steps=True 時每個 tick 帶真實位移距離（相鄰繞圈點 haversine），
            讓前端統計能累加；多朵路徑沿用 step_m=0.0。
            """
            if duration_s <= 0 or dwell_radius_m <= 0:
                return
            profile = PROFILES["circle"]
            iterator = circle_walk(
                center=center,
                profile=profile,
                rng=rng,
                get_radius=lambda: dwell_radius_m,
                get_speed_kmh=lambda: profile.nominal_kmh,
            )
            elapsed = 0.0
            prev: tuple[float, float] | None = None
            while elapsed < duration_s:
                tick = next(iterator)
                lat, lon = tick.position
                await session.set_location(lat, lon)
                step_m = 0.0
                if report_steps and prev is not None:
                    step_m = haversine_m(prev, (lat, lon))
                prev = (lat, lon)
                await ws.send_json({
                    "type": "tick",
                    "lat": lat,
                    "lon": lon,
                    "step_m": step_m,
                    "note": "orbit",
                })
                await asyncio.sleep(profile.tick_s)
                elapsed += profile.tick_s
                while session.paused:
                    await asyncio.sleep(0.5)
```

說明：`haversine_m` 已在 `runner()` 開頭 `from pikmin_walk import ...` 匯入，在閉包內可用。多朵路徑的三處呼叫都不傳 `report_steps`，預設 `False`，行為不變。

- [ ] **Step 2: 放寬 `_handle_start_loop_walk` 的「≥2 朵」防呆**

把：

```python
    if not raw_route or len(raw_route) < 2:
        await ws.send_json({"type": "error", "message": "先按「預覽路線」"})
        return
```

替換為：

```python
    if not raw_route:
        await ws.send_json({"type": "error", "message": "先按「預覽路線」"})
        return
    # 1 個座標只在種花模式允許（= 原地繞單朵花）；採花/快速採花仍需要路線。
    if len(raw_route) == 1 and mode != "plant":
        await ws.send_json({"type": "error", "message": "採花模式至少需要 2 朵花"})
        return
```

說明：`mode` 在此函式上方（`mode = str(msg.get("mode", "harvest"))`）已定義。`not raw_route` 先擋掉空 list，之後 `len == 1` 即單朵。

- [ ] **Step 3: `runner()` 加單朵無限繞圈分支**

在 `runner()` 的 `try` 內，找到送出 `loop_walk_started` 的這段：

```python
            await ws.send_json({
                "type": "loop_walk_started",
                "loop_route": [[p[0], p[1]] for p in route],
                "loop_distance_km": loop_dist / 1000,
            })

            lap = 0
            while True:
```

在 `loop_walk_started` 送出之後、`lap = 0` 之前插入分支，變成：

```python
            await ws.send_json({
                "type": "loop_walk_started",
                "loop_route": [[p[0], p[1]] for p in route],
                "loop_distance_km": loop_dist / 1000,
            })

            # 原地繞單朵花：種花模式只給 1 個座標時，無限繞那個座標。
            # orbit_at_flower 只會在按停止（CancelledError）時離開。
            if len(route) == 1:
                await orbit_at_flower(route[0], float("inf"), report_steps=True)
                return

            lap = 0
            while True:
```

說明：`len(route) == 1` 時 `loop_dist` 為 `sum(...)` 空和 = 0，`set_location(*route[0])` 正常。`orbit_at_flower` 以 `inf` 時長無限繞圈，按停止時 `CancelledError` 由既有 `except asyncio.CancelledError` 接住送 `stopped`。`return` 為防禦性（實際不會自然到達）。

- [ ] **Step 4: 語法檢查**

Run: `python -m py_compile server.py`
Expected: 無輸出、exit code 0（語法正確）。

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "feat(server): infinite single-flower orbit for plant mode

orbit_at_flower gains report_steps so the in-place orbit accumulates
real distance; _handle_start_loop_walk accepts a 1-flower route only
in plant mode and runs an unbounded orbit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: UI — 單朵原地繞花偵測與顯示

**Files:**
- Modify: `static/flower-cruise.html`

- [ ] **Step 1: 重整模式 helper、加入單朵偵測**

把這段（約 550–558 行，`<script>` 內、無縮排）：

```javascript
function syncDwellVisibility() {
  $dwellRow.style.display = getCruiseMode() === 'plant' ? 'grid' : 'none';
}
$modeRadios.forEach(r => r.addEventListener('change', syncDwellVisibility));
syncDwellVisibility();

let parsedFlowers = [];      // [[lat, lon], ...] from textarea
let previewedRoute = null;   // { ordered, distance_km, count } from server
let startFromIndex = 0;      // 0-indexed; 0 = start from the first TSP flower
```

替換為：

```javascript
let parsedFlowers = [];      // [[lat, lon], ...] from textarea
let previewedRoute = null;   // { ordered, distance_km, count } from server
let startFromIndex = 0;      // 0-indexed; 0 = start from the first TSP flower

// 種花模式只解析出 1 朵花時 = 原地繞單朵花子模式（自動偵測，無新增開關）
function isSingleFlowerPlant() {
  return getCruiseMode() === 'plant' && parsedFlowers.length === 1;
}
// 多朵種花才顯示 dwell 欄位；單朵原地繞花不適用
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

說明：`let parsedFlowers` 宣告必須移到 `syncDwellVisibility()` 初次呼叫之前 —— 否則 `syncDwellVisibility` 讀 `parsedFlowers` 會踩到 `let` 的 temporal dead zone。`applySingleFlowerPlant` 是 function 宣告（Step 3 定義），會 hoist，在事件 listener 內引用沒問題。

- [ ] **Step 2: 加入繞圈示意圈圖層 + helper**

在 `redrawForStart` 函式的結尾 `}` 之後、`$flowersText.addEventListener('input', updateParseInfo);` 這行之前插入。做法：把

```javascript
$flowersText.addEventListener('input', updateParseInfo);
```

替換為：

```javascript
let orbitCircle = null;  // 10m 繞圈示意圈（單朵原地繞花）
function showOrbitCircle(flower) {
  clearOrbitCircle();
  orbitCircle = L.circle(flower, {
    radius: 10, color: '#ec4899', weight: 2,
    fillColor: '#ec4899', fillOpacity: 0.12, dashArray: '4, 4',
  }).addTo(map);
}
function clearOrbitCircle() {
  if (orbitCircle) { map.removeLayer(orbitCircle); orbitCircle = null; }
}

// 依目前模式 / 已解析花朵數，套用「原地繞單朵花」的 UI 狀態。
// 0 或 ≥2 朵時只負責清掉繞圈示意圈，不碰其他按鈕狀態。
function applySingleFlowerPlant() {
  clearOrbitCircle();
  if (parsedFlowers.length !== 1) return;
  if (isSingleFlowerPlant()) {
    showOrbitCircle(parsedFlowers[0]);
    $btnStart.disabled = false;
    $preview.textContent = '🔁 原地繞花 · 10m 圈 · ~4.5 km/h 步行 — 按「開始」無限繞圈';
    $preview.className = 'preview-info';
  } else {
    // 1 朵但模式是採花 / 快速採花
    $btnStart.disabled = true;
    $preview.textContent = '採花模式至少需要 2 朵花';
    $preview.className = 'preview-info';
  }
}

$flowersText.addEventListener('input', updateParseInfo);
```

- [ ] **Step 3: `updateParseInfo` —— 0 朵時收尾、結尾套用單朵狀態**

把 `updateParseInfo` 的 0 朵早退分支：

```javascript
  if (parsedFlowers.length === 0) {
    $parseInfo.textContent = '尚未解析';
    $btnPreview.disabled = true;
    $btnInOrder.disabled = true;
    return;
  }
```

替換為：

```javascript
  if (parsedFlowers.length === 0) {
    $parseInfo.textContent = '尚未解析';
    $btnPreview.disabled = true;
    $btnInOrder.disabled = true;
    $btnStart.disabled = true;
    clearOrbitCircle();
    return;
  }
```

再把 `updateParseInfo` 的結尾：

```javascript
  $btnStart.disabled = true;
  $preview.textContent = '輸入已變更，按「預覽路線」重新規劃';
  $preview.className = 'preview-info';
}
```

替換為：

```javascript
  $btnStart.disabled = true;
  $preview.textContent = '輸入已變更，按「預覽路線」重新規劃';
  $preview.className = 'preview-info';
  // 種花模式 + 剛好 1 朵 → 自動進入原地繞花；其餘情況維持上面的重置
  syncDwellVisibility();
  applySingleFlowerPlant();
}
```

說明：`applySingleFlowerPlant` 在單朵種花時會覆寫 `$btnStart`／`$preview`；≥2 朵時只清示意圈（不存在則 no-op）並早退，不動 `$btnStart`（多朵仍須走預覽流程）。

- [ ] **Step 4: Commit**

```bash
git add static/flower-cruise.html
git commit -m "feat(ui): detect single-flower plant mode + 10m orbit hint circle

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: UI — 開始流程、軌跡上限、按鈕狀態修正

**Files:**
- Modify: `static/flower-cruise.html`

- [ ] **Step 1: `$btnStart` click 加單朵原地繞花分支**

把 `$btnStart` 的 click handler：

```javascript
$btnStart.addEventListener('click', () => {
  if (!previewedRoute) return;
  const route = previewedRoute.ordered.slice(startFromIndex);
  if (route.length < 2) return;
```

替換為：

```javascript
$btnStart.addEventListener('click', () => {
  // 原地繞單朵花：種花模式只解析出 1 朵時，繞那朵無限圈（速度滑桿不適用，
  // 伺服器固定用 circle profile 的 ~4.5 km/h 步行速度）。
  if (isSingleFlowerPlant()) {
    ws.send(JSON.stringify({
      type: 'start_loop_walk',
      speed_kmh: parseInt($speed.value),
      route: [parsedFlowers[0]],
      loop: true,
      mode: 'plant',
      dwell_each_s: 0,
      dwell_last_s: 0,
      dwell_radius_m: 10,
      quick_dwell_s: 0,
    }));
    return;
  }
  if (!previewedRoute) return;
  const route = previewedRoute.ordered.slice(startFromIndex);
  if (route.length < 2) return;
```

說明：其餘 handler 內容（`const mode = getCruiseMode();` 以下到 `}));`）不變。`parsedFlowers[0]` 是 `[lat, lon]`，伺服器 `route = [(float(p[0]), float(p[1])) for p in raw_route]` 可接受。

- [ ] **Step 2: `tick` handler 加軌跡長度上限**

把 `handleMessage` 內的 `case 'tick':`：

```javascript
    case 'tick':
      updateDevice(m.lat, m.lon);
      trailLine.addLatLng([m.lat, m.lon]);
      totalDist += (m.step_m || 0);
      document.getElementById('statDist').textContent = (totalDist / 1000).toFixed(1);
      break;
```

替換為：

```javascript
    case 'tick': {
      updateDevice(m.lat, m.lon);
      trailLine.addLatLng([m.lat, m.lon]);
      // 原地繞花會跑數小時 — 限制軌跡點數避免 polyline 無限增長拖慢瀏覽器
      const pts = trailLine.getLatLngs();
      if (pts.length > 600) trailLine.setLatLngs(pts.slice(-600));
      totalDist += (m.step_m || 0);
      document.getElementById('statDist').textContent = (totalDist / 1000).toFixed(1);
      break;
    }
```

說明：`case` 加 `{ }` 區塊，讓 `const pts` 有獨立 block scope（避免 switch 內 lexical declaration 警告）。

- [ ] **Step 3: 修正 stopped/done 時的「預覽」按鈕狀態**

本功能讓「只有 1 朵花時也能開始→停止」變成可達狀態；停止後不該把只能 ≥2 朵用的「預覽路線」按鈕重新啟用。把 `case 'stopped':` / `case 'done':` 區塊內這行：

```javascript
      $btnStart.disabled = false; $btnStop.disabled = true; $btnPause.disabled = true; $btnPreview.disabled = false; $btnInOrder.disabled = parsedFlowers.length < 2;
```

替換為：

```javascript
      $btnStart.disabled = false; $btnStop.disabled = true; $btnPause.disabled = true; $btnPreview.disabled = parsedFlowers.length < 2; $btnInOrder.disabled = parsedFlowers.length < 2;
```

說明：只改 `$btnPreview.disabled` 一處，與相鄰 `$btnInOrder` 的判斷對齊。停止後若還是單朵種花，`$btnStart` 仍 enabled（可再次開始），符合預期。

- [ ] **Step 4: Commit**

```bash
git add static/flower-cruise.html
git commit -m "feat(ui): start in-place orbit for single flower + cap trail length

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 煙霧測試與文件

**Files:**
- Modify: `.claude/dev/e2e.md`、`CLAUDE.md`（專案根）

- [ ] **Step 1: 重啟伺服器載入 server.py 變更**

`server.py` 改動需重啟才生效（前端可直接重新整理）。用專案既有重啟機制（navbar 的 ✨ 更新鈕或對應的 `make` target）重啟，確認 `http://localhost:7766/flower-cruise` 可開。

- [ ] **Step 2: Playwright-MCP 煙霧測試 —— UI 狀態（不需裝置）**

用 Playwright-MCP 開 `http://localhost:7766/flower-cruise`，逐項驗證：

1. **單朵自動偵測**：座標框貼一個座標（例 `25.0686,121.5350`）→「開始」啟用、「預覽路線」「按照順序」停用、`previewInfo` 顯示「🔁 原地繞花 …」、地圖出現粉紅虛線圈、dwell 欄位（每朵繞圈秒數/最後一朵久留秒數）隱藏。
2. **模式切換**：切到「採花」→「開始」停用、`previewInfo` 顯示「採花模式至少需要 2 朵花」、示意圈消失；切回「種花」→ 重新啟用、示意圈重現。
3. **清空**：清空座標框 →「開始」停用、示意圈消失。
4. **多朵迴歸**：貼 ≥2 個座標 + 種花模式 → dwell 欄位顯示、需按「預覽/按順序」才能開始（既有流程不受影響）。
5. 全程 `browser_console_messages` 無 error。

- [ ] **Step 3: Playwright-MCP 煙霧測試 —— 即時繞圈（需連線裝置）**

若有 iOS 裝置連線（`deviceStatus` 顯示綠色機型）：種花模式單朵 → 按「開始」→ 應收到 `loop_walk_started`、地圖標記開始繞 10m 圈、`tick` 持續進來、統計列「km」緩慢累加、「時間」遞增 → 按「停止」→ 收到 `stopped`、按鈕復原。
若無裝置連線：在 `e2e.md` 標註此情境為 `⏳ 未驗證（無裝置）`，不阻擋其餘項目。

- [ ] **Step 4: 記錄結果到 `.claude/dev/e2e.md`**

在 `.claude/dev/e2e.md` 末端新增一節（沿用既有格式）：

```markdown
## 原地繞單朵花（2026-05-17）

Spec: `docs/superpowers/specs/2026-05-17-in-place-flower-orbit-design.md`
Plan: `docs/superpowers/plans/2026-05-17-in-place-flower-orbit.md`
Branch: `feat/in-place-flower-orbit`

### Test Scenarios

1. **Story:** 種花模式貼 1 個座標自動進入原地繞花
   - **Steps:** `/flower-cruise` 種花模式，座標框貼 1 個座標
   - **Expected Result:** 「開始」啟用、「預覽/按順序」停用、出現 10m 示意圈、dwell 欄位隱藏、previewInfo 顯示原地繞花說明
   - **Status:** （填入實測結果）
   - **Last Updated:** 2026-05-17

2. **Story:** 模式切換正確開關單朵原地繞花
   - **Steps:** 單朵狀態下切採花↔種花
   - **Expected Result:** 採花時「開始」停用 + 提示「至少需要 2 朵花」+ 示意圈消失；切回種花還原
   - **Status:** （填入實測結果）
   - **Last Updated:** 2026-05-17

3. **Story:** 多朵種花流程不受影響（迴歸）
   - **Steps:** 貼 ≥2 個座標、種花模式、預覽→開始
   - **Expected Result:** dwell 欄位顯示、需預覽才能開始，行為與改動前一致
   - **Status:** （填入實測結果）
   - **Last Updated:** 2026-05-17

4. **Story:** 單朵原地繞花實際繞圈（需裝置）
   - **Steps:** 種花模式單朵 → 開始 → 觀察 → 停止
   - **Expected Result:** loop_walk_started → 繞 10m 圈 + tick 串流 + km 累加 → 停止後 stopped、按鈕復原
   - **Status:** （填入實測結果，無裝置則標 ⏳ 未驗證）
   - **Last Updated:** 2026-05-17
```

把每項 `Status` 換成實測的 ✅ / ❌ / ⏳。

- [ ] **Step 5: 更新專案 `CLAUDE.md`**

在 `CLAUDE.md`（專案根）的 `static/index.html` 那列 File Responsibilities 下方、或 Caveats 區，補一行說明（擇一最貼近處）：

```markdown
- 花朵巡航種花模式：座標框只給 1 個座標時自動進入「原地繞花」—— 以 10m 半徑、circle profile 步行速度（~4.5 km/h，速度滑桿不適用）無限繞那個座標，按停止才結束。
```

- [ ] **Step 6: Commit**

```bash
git add .claude/dev/e2e.md CLAUDE.md
git commit -m "docs: record in-place flower orbit e2e results + CLAUDE.md note

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- 觸發（種花 + 1 朵自動偵測）→ Task 2 Step 1/3。
- 10m 半徑 → Task 3 Step 1（client 送 `dwell_radius_m: 10`）。
- ~4.5 km/h 速度、滑桿不生效 → Task 1 Step 1 沿用 `profile.nominal_kmh`；無改動即達成。
- 無限繞圈 → Task 1 Step 3（`float("inf")`）。
- `預覽/按順序` 維持停用 → Task 2 Step 3（`updateParseInfo` 不啟用、單朵 helper 不碰它們）。
- previewInfo 說明文字 + 10m 示意圈 → Task 2 Step 2/3。
- 隱藏 dwell 欄位 → Task 2 Step 1（`syncDwellVisibility` 改 `>= 2`）。
- 軌跡上限 → Task 3 Step 2。
- report_steps 距離累加 → Task 1 Step 1 + Task 3 Step 2。
- 錯誤處理（1 朵 + 非種花模式）→ Task 1 Step 2（伺服器）+ Task 2 Step 2（前端 `applySingleFlowerPlant` else 分支）。
- 測試 → Task 4。

**Placeholder scan:** 無 TBD/TODO；所有程式碼步驟皆附完整 old→new。`e2e.md` 的 `Status`／`CLAUDE.md` 位置為實測時填入，已明確指示。

**Type/名稱一致性:** `isSingleFlowerPlant`、`applySingleFlowerPlant`、`showOrbitCircle`、`clearOrbitCircle`、`orbitCircle`、`report_steps` 在各 Task 間名稱一致。`orbit_at_flower` 簽名 `(center, duration_s, report_steps=False)` 與唯一新呼叫點（Task 1 Step 3）相符；三處既有呼叫不傳 `report_steps`、相容。
