# 花朵巡航 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `/flower-cruise` 獨立頁面：使用者貼一組花朵 GPS 座標 → OSRM TSP 排序 → 在 ordered flowers 之間以直線（great-circle）無限繞圈走。

**Architecture:** 純 glue code 疊在既有 `_handle_start_loop_walk` 之上。新頁面 + 新 preview endpoint + 一個 OSRM helper。**走路階段 100% 複用** 現有 `start_loop_walk` WS action，server runner / WS protocol / pikmin_walk.py 一行不動。

**Tech Stack:** Starlette (Python), httpx, Leaflet 1.9, OSRM Trip API, vanilla JS

**Reference:** Spec at `docs/superpowers/specs/2026-04-30-flower-cruise-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `server.py` | Modify | 加 `_osrm_trip_order` helper、`preview_flower_cruise` endpoint、`flower_cruise_page` handler、2 行 Route 註冊 |
| `static/flower-cruise.html` | Create | 新頁面，基於 `walk.html` pattern：textarea 輸入花朵、地圖顯示粉色 markers + TSP polyline、speed slider、開始/暫停/停止 |
| `static/index.html` | Modify | 在既有「→ 道路漫步模式」連結旁邊加一個「→ 花朵巡航」連結 |

**No changes:** `pikmin_walk.py`、`_handle_start_loop_walk`、WS protocol、其他 endpoints

---

## Testing Approach

**No unit tests** — spec 已決定。理由：
1. 純 glue code（OSRM 是 trusted external service）
2. 既有 codebase 沒有 pytest 環境，為 ~130 行新功能架測試 infra 不成比例
3. 商業邏輯（OSRM call）已有 `_osrm_fetch` 封裝（見 server.py:1062）

**驗證手段：手動 smoke test**（Task 6 詳細列步驟）

---

## Task 1: 新增 `_osrm_trip_order` helper

**Files:** Modify `server.py`（在 `_osrm_trip_route` 函式後，~line 1105）

- [ ] **Step 1: 讀現有 `_osrm_trip_route` 確認 pattern**

Run: `sed -n '1094,1118p' server.py`
Expected: 看到 `_osrm_trip_route` 用 `_osrm_fetch` + `geometries=geojson&overview=full`。

- [ ] **Step 2: 在 `_osrm_trip_route` 函式正下方加入 `_osrm_trip_order`**

在 `server.py` line 1105（`_osrm_trip_route` 結束的下一行）加入：

```python
async def _osrm_trip_order(
    waypoints: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    """Return waypoints reordered by OSRM Trip TSP, no geometry.

    Unlike _osrm_trip_route which returns the routed polyline along roads,
    this function asks OSRM only for the optimal *visit order* of the
    given points. The caller is expected to connect them with great-circle
    lines themselves (花朵巡航 wants direct lines between flowers, not
    road geometry).

    Uses source=first to pin the starting point — without it OSRM is free
    to pick any waypoint as the start, which makes the order non-stable
    across previews of the same input.
    """
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    data = await _osrm_fetch(
        f"/trip/v1/foot/{coords_str}?roundtrip=true&source=first&geometries=geojson&overview=false"
    )
    if data is None or not data.get("waypoints"):
        return None
    # data["waypoints"][i] corresponds to input[i]; its waypoint_index is
    # the position in the optimized trip. Sort input indices by that.
    order = sorted(
        range(len(waypoints)),
        key=lambda i: data["waypoints"][i]["waypoint_index"],
    )
    return [waypoints[i] for i in order]
```

- [ ] **Step 3: 確認 Python syntax**

Run: `python3 -c "import ast; ast.parse(open('server.py').read())"`
Expected: 沒輸出（syntax OK）

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat(flower-cruise): add _osrm_trip_order helper for TSP visit order"
```

---

## Task 2: 新增 `preview_flower_cruise` endpoint

**Files:** Modify `server.py`（在 `preview_loop` 函式後，~line 711）

- [ ] **Step 1: 讀現有 `preview_loop` 確認 pattern**

Run: `sed -n '633,711p' server.py`
Expected: 看到 `preview_loop` 用 `await request.json()` + `JSONResponse({"error": ...})` pattern。

- [ ] **Step 2: 在 `preview_loop` 函式正下方加入 `preview_flower_cruise`**

```python
async def preview_flower_cruise(request):
    """Take a list of flower coords, return TSP-ordered list + lap stats.

    Body: { flowers: [[lat, lon], ...] }
    Returns: { ordered: [[lat, lon], ...], distance_km, lap_eta_min, count }

    The lap distance is great-circle (not OSRM-routed), because the walker
    will connect ordered flowers with straight lines, not road geometry.
    Lap ETA assumes 19 km/h (the page's default; the actual speed is set
    at start time and can be tuned live).
    """
    body = await request.json()
    flowers = body.get("flowers")
    if not isinstance(flowers, list):
        return JSONResponse({"error": "flowers must be a list"}, status_code=400)

    # Validate each flower
    parsed: list[tuple[float, float]] = []
    for item in flowers:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return JSONResponse({"error": "each flower must be [lat, lon]"}, status_code=400)
        try:
            lat, lon = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            return JSONResponse({"error": "flower coords must be numeric"}, status_code=400)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return JSONResponse({"error": f"out-of-range coord: {lat},{lon}"}, status_code=400)
        parsed.append((lat, lon))

    if len(parsed) < 2:
        return JSONResponse({"error": "至少需要 2 朵花"}, status_code=400)
    if len(parsed) > 100:
        # OSRM Trip API public server caps at ~100 waypoints
        return JSONResponse({"error": f"花太多（{len(parsed)} > 100），請分批"}, status_code=400)

    ordered = await _osrm_trip_order(parsed)
    if ordered is None:
        return JSONResponse({"error": "OSRM 無法規劃路線（網路或座標問題）"})

    # Great-circle lap distance: ordered[0] → ordered[1] → ... → ordered[-1] → ordered[0]
    from pikmin_walk import haversine_m
    closed = ordered + [ordered[0]]
    dist_m = sum(haversine_m(closed[i], closed[i + 1]) for i in range(len(closed) - 1))
    lap_eta_min = (dist_m / 1000) / 19.0 * 60  # at default speed

    return JSONResponse({
        "ordered": [[lat, lon] for lat, lon in ordered],
        "distance_km": dist_m / 1000,
        "lap_eta_min": lap_eta_min,
        "count": len(ordered),
    })
```

- [ ] **Step 3: 確認 Python syntax**

Run: `python3 -c "import ast; ast.parse(open('server.py').read())"`
Expected: 沒輸出

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat(flower-cruise): add /api/preview-flower-cruise endpoint"
```

---

## Task 3: 新增 page handler + 註冊 routes

**Files:** Modify `server.py` (handler 緊接 `walk_page` 後 ~line 568；routes 在 `app = Starlette(...)` 的 `routes=[...]` list 中)

- [ ] **Step 1: 加 `flower_cruise_page` handler**

在 `server.py` line 568（`walk_page` 函式後）加入：

```python
async def flower_cruise_page(request):
    return FileResponse(STATIC_DIR / "flower-cruise.html")
```

- [ ] **Step 2: 註冊 2 條 routes**

找到 `app = Starlette(...)` 區塊（~line 1272），在 `Route("/walk", walk_page),` 那行下面加：

```python
        Route("/flower-cruise", flower_cruise_page),
```

並在 `Route("/api/preview-loop", preview_loop, methods=["POST"]),` 那行下面加：

```python
        Route("/api/preview-flower-cruise", preview_flower_cruise, methods=["POST"]),
```

修改後該段應該長這樣：

```python
        Route("/", index),
        Route("/walk", walk_page),
        Route("/flower-cruise", flower_cruise_page),
        Route("/api/preview-loop", preview_loop, methods=["POST"]),
        Route("/api/preview-flower-cruise", preview_flower_cruise, methods=["POST"]),
        Route("/api/profiles", profiles_endpoint),
```

- [ ] **Step 3: 確認 Python syntax**

Run: `python3 -c "import ast; ast.parse(open('server.py').read())"`
Expected: 沒輸出

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat(flower-cruise): register /flower-cruise page and preview route"
```

---

## Task 4: 新增 `static/flower-cruise.html`

**Files:** Create `static/flower-cruise.html`

這是最大的一個 task，但結構清晰。整檔基於 `walk.html` pattern，差異：

| 區塊 | walk.html | flower-cruise.html |
|---|---|---|
| Title | "Pikmin Walker — Loop" | "Pikmin Walker — 花朵巡航" |
| Header | "● 循環路線" | "🌸 花朵巡航" |
| 形狀按鈕 | square / rect / circle | **整段移除**，換成 textarea |
| Lap distance slider | 有 | **整個移除** |
| Marker 顏色 | 藍色 (`#3b82f6`) | 粉色 (`#ec4899`) for flowers |
| Preview endpoint | `/api/preview-loop` | `/api/preview-flower-cruise` |
| Preview body | `{lat, lon, shape, lap_distance_km}` | `{flowers: [[lat,lon], ...]}` |
| Preview response field | `route` (OSRM polyline) | `ordered` (TSP-ordered flowers) |
| Polyline | OSRM 步道幾何 | 直線連 ordered + ordered[0] |
| 啟動 WS payload | `{type:'start_loop_walk', route: previewedRoute.route, ...}` | `{type:'start_loop_walk', route: ordered + [ordered[0]], ...}` |

WS protocol、speed slider、地圖切換、stats、pause/resume/stop 全部一字不改。

- [ ] **Step 1: Create `static/flower-cruise.html`** (完整檔案)

```html
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pikmin Walker — 花朵巡航</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {
      --bg: #0f172a;
      --panel: rgba(30,41,59,0.92);
      --panel-solid: #1e293b;
      --border: #475569;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --flower: #ec4899;
      --danger: #ef4444;
      --success: #10b981;
    }
    * { box-sizing: border-box; margin: 0; }
    html, body { height: 100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif; }
    body { color: var(--text); overflow: hidden; }
    #map { width: 100%; height: 100%; }
    .leaflet-container { font-family: inherit; }

    #panel {
      position: absolute;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 1000;
      background: var(--panel);
      backdrop-filter: blur(16px);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px 24px;
      min-width: 460px;
      max-width: 540px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    }
    .panel-header {
      display: flex; align-items: center; justify-content: space-between;
    }
    .panel-title { font-size: 16px; font-weight: 600; }
    .panel-title .dot { color: var(--flower); }
    #deviceStatus {
      font-size: 11px; padding: 4px 10px; border-radius: 20px;
      background: var(--panel-solid); border: 1px solid var(--border);
    }
    #deviceStatus.ok { border-color: var(--success); color: var(--success); }

    .input-block { display: flex; flex-direction: column; gap: 6px; }
    .input-block label {
      font-size: 11px; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.05em;
    }
    #flowersText {
      width: 100%; min-height: 120px; resize: vertical;
      font-family: ui-monospace, monospace; font-size: 12px;
      padding: 8px 10px; background: var(--panel-solid);
      border: 1px solid var(--border); border-radius: 8px;
      color: var(--text);
    }
    #flowersText:focus { outline: none; border-color: var(--flower); }
    .parse-row { display: flex; gap: 8px; align-items: center; }
    .parse-row .parse-info { flex: 1; font-size: 11px; color: var(--muted); }
    .parse-row button {
      padding: 6px 12px; font-size: 12px; font-family: inherit;
      background: var(--panel-solid); border: 1px solid var(--border);
      border-radius: 6px; color: var(--muted); cursor: pointer;
    }
    .parse-row button:hover { color: var(--text); }

    .control-row { display: flex; align-items: center; gap: 12px; }
    .control-row label {
      font-size: 12px; color: var(--muted); min-width: 60px; text-align: right;
    }
    .control-row input[type="range"] { flex: 1; accent-color: var(--accent); }
    .control-row .value {
      font-size: 13px; font-weight: 600; min-width: 72px;
      font-family: ui-monospace, monospace;
    }

    .preview-info {
      padding: 10px 14px; background: var(--panel-solid);
      border: 1px solid var(--border); border-radius: 8px;
      font-size: 12px; color: var(--muted); text-align: center;
      min-height: 40px; display: flex; align-items: center; justify-content: center; gap: 16px;
    }
    .preview-info .pi-num { font-size: 18px; font-weight: 700; color: var(--text); font-family: ui-monospace, monospace; }
    .preview-info .pi-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
    .preview-info.loading { color: var(--accent); }
    .preview-info.error { color: var(--danger); border-color: var(--danger); }

    .stats-row {
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
    }
    .stat {
      text-align: center; padding: 8px;
      background: var(--panel-solid); border-radius: 8px; border: 1px solid var(--border);
    }
    .stat .num { font-size: 18px; font-weight: 700; font-family: ui-monospace, monospace; }
    .stat .label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }

    .btn-row { display: flex; gap: 8px; }
    .btn-row button {
      flex: 1; padding: 12px; font-size: 14px; font-weight: 600;
      font-family: inherit; border: 1px solid var(--border); border-radius: 10px;
      cursor: pointer; color: var(--text); background: var(--panel-solid);
    }
    .btn-row button:hover:not(:disabled) { background: var(--border); }
    .btn-row button.primary { background: var(--accent); border-color: var(--accent); }
    .btn-row button.primary:hover:not(:disabled) { background: var(--accent-hover); }
    .btn-row button.danger { background: var(--danger); border-color: var(--danger); }
    .btn-row button:disabled { opacity: 0.4; cursor: not-allowed; }

    #statusLine { font-size: 11px; color: var(--muted); text-align: center; }
    #statusLine.running { color: var(--accent); }
    #statusLine.error { color: var(--danger); }

    .layer-control {
      position: absolute; top: 10px; right: 10px; z-index: 1000;
      display: flex; gap: 4px; background: var(--panel);
      padding: 4px; border-radius: 8px; backdrop-filter: blur(8px);
    }
    .layer-control button {
      padding: 6px 10px; font-size: 11px; font-weight: 500;
      background: transparent; border: 1px solid var(--border);
      border-radius: 5px; color: var(--muted); cursor: pointer;
    }
    .layer-control button.active { background: var(--accent); border-color: var(--accent); color: white; }
    .back-link {
      position: absolute; top: 10px; left: 10px; z-index: 1000;
      padding: 6px 14px; font-size: 12px; font-weight: 500;
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; color: var(--muted); text-decoration: none;
      backdrop-filter: blur(8px);
    }
    .back-link:hover { color: var(--text); }
  </style>
</head>
<body>
  <div id="map"></div>
  <a class="back-link" href="/">← 主控台</a>
  <div class="layer-control" id="layerControl">
    <button data-layer="google" class="active">Google</button>
    <button data-layer="satellite">衛星</button>
    <button data-layer="osm">OSM</button>
  </div>

  <div id="panel">
    <div class="panel-header">
      <span class="panel-title"><span class="dot">🌸</span> 花朵巡航</span>
      <span id="deviceStatus">連線中…</span>
    </div>

    <div class="input-block">
      <label>花朵座標（一行一個或任意分隔）</label>
      <textarea id="flowersText" placeholder="41.450779,31.795279&#10;41.449948,31.794372&#10;..."></textarea>
      <div class="parse-row">
        <span class="parse-info" id="parseInfo">尚未解析</span>
        <button id="btnClear">清空</button>
      </div>
    </div>

    <div class="control-row">
      <label>速度</label>
      <input type="range" id="speed" min="3" max="50" value="19" step="1">
      <span class="value" id="speedVal">19 km/h</span>
    </div>

    <div class="preview-info" id="previewInfo">貼上座標後按「預覽」</div>

    <div class="stats-row" id="statsRow" style="display:none;">
      <div class="stat"><div class="num" id="statDist">0.0</div><div class="label">km</div></div>
      <div class="stat"><div class="num" id="statTime">0:00</div><div class="label">時間</div></div>
      <div class="stat"><div class="num" id="statLaps">0</div><div class="label">圈</div></div>
    </div>

    <div class="btn-row">
      <button id="btnPreview">🗺 預覽路線</button>
      <button id="btnStart" class="primary" disabled>▶ 開始巡航</button>
      <button id="btnPause" disabled>⏸ 暫停</button>
      <button id="btnStop" class="danger" disabled>■ 停止</button>
    </div>

    <div id="statusLine">讀取裝置位置…</div>
  </div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map', { zoomControl: false }).setView([0, 0], 2);
L.control.zoom({ position: 'topright' }).addTo(map);

const tileLayers = {
  google: L.tileLayer('https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', { maxZoom: 21 }),
  satellite: L.tileLayer('https://mt1.google.com/vt/lyrs=s,h&x={x}&y={y}&z={z}', { maxZoom: 21 }),
  osm: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }),
};
let currentTile = tileLayers.google;
currentTile.addTo(map);
document.getElementById('layerControl').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-layer]');
  if (!btn) return;
  const key = btn.dataset.layer;
  if (!tileLayers[key] || tileLayers[key] === currentTile) return;
  map.removeLayer(currentTile);
  currentTile = tileLayers[key];
  currentTile.addTo(map);
  document.querySelectorAll('#layerControl button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
});

let deviceMarker = null;
const routeLine = L.polyline([], { color: '#ec4899', weight: 3, opacity: 0.7, dashArray: '6, 4' }).addTo(map);
const trailLine = L.polyline([], { color: '#3b82f6', weight: 4, opacity: 0.9 }).addTo(map);
const flowerMarkers = L.layerGroup().addTo(map);

function updateDevice(lat, lon, pan = false) {
  const ll = [lat, lon];
  if (!deviceMarker) {
    deviceMarker = L.circleMarker(ll, {
      radius: 9, color: '#fff', weight: 2, fillColor: '#3b82f6', fillOpacity: 1,
    }).addTo(map);
  } else deviceMarker.setLatLng(ll);
  if (pan) map.setView(ll, Math.max(map.getZoom(), 14));
}

const $flowersText = document.getElementById('flowersText');
const $parseInfo = document.getElementById('parseInfo');
const $btnClear = document.getElementById('btnClear');
const $speed = document.getElementById('speed');
const $speedVal = document.getElementById('speedVal');
const $preview = document.getElementById('previewInfo');
const $btnPreview = document.getElementById('btnPreview');
const $btnStart = document.getElementById('btnStart');
const $btnStop = document.getElementById('btnStop');
const $btnPause = document.getElementById('btnPause');
const $status = document.getElementById('statusLine');
const $deviceStatus = document.getElementById('deviceStatus');

let parsedFlowers = [];      // [[lat, lon], ...] from textarea
let previewedRoute = null;   // { ordered, distance_km, count } from server

function parseFlowers(text) {
  const nums = text.match(/-?\d+\.?\d*/g) || [];
  const pairs = [];
  for (let i = 0; i + 1 < nums.length; i += 2) {
    const lat = parseFloat(nums[i]);
    const lon = parseFloat(nums[i + 1]);
    if (!isNaN(lat) && !isNaN(lon) &&
        lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
      pairs.push([lat, lon]);
    }
  }
  return pairs;
}

function updateParseInfo() {
  parsedFlowers = parseFlowers($flowersText.value);
  if (parsedFlowers.length === 0) {
    $parseInfo.textContent = '尚未解析';
    $btnPreview.disabled = true;
    return;
  }
  $parseInfo.textContent = `已解析 ${parsedFlowers.length} 朵花`;
  $btnPreview.disabled = parsedFlowers.length < 2;
  // Render markers immediately so user sees what was parsed
  renderFlowerMarkers(parsedFlowers, false);
  if (parsedFlowers.length > 0) {
    map.fitBounds(L.latLngBounds(parsedFlowers).pad(0.2));
  }
  // Reset preview state since input changed
  previewedRoute = null;
  routeLine.setLatLngs([]);
  $btnStart.disabled = true;
  $preview.textContent = '輸入已變更，按「預覽路線」重新規劃';
  $preview.className = 'preview-info';
}

function renderFlowerMarkers(flowers, ordered) {
  flowerMarkers.clearLayers();
  flowers.forEach((f, i) => {
    const m = L.circleMarker([f[0], f[1]], {
      radius: 6, color: '#fff', weight: 2,
      fillColor: '#ec4899', fillOpacity: 1,
    }).addTo(flowerMarkers);
    if (ordered) {
      m.bindTooltip(String(i + 1), {
        permanent: true, direction: 'top', className: 'flower-num',
        offset: [0, -4],
      });
    } else {
      m.bindTooltip(`花 #${i + 1}`);
    }
  });
}

$flowersText.addEventListener('input', updateParseInfo);
$btnClear.addEventListener('click', () => {
  $flowersText.value = '';
  updateParseInfo();
});

$speed.addEventListener('input', () => {
  $speedVal.textContent = `${$speed.value} km/h`;
  updateLapTime();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'set_speed', speed_kmh: parseInt($speed.value) }));
  }
});

function updateLapTime() {
  if (!previewedRoute) return;
  const km = previewedRoute.distance_km;
  const mins = km / parseInt($speed.value) * 60;
  $preview.replaceChildren();
  addPreviewStat(previewedRoute.count + ' 朵', '花朵');
  addPreviewStat(km.toFixed(2) + ' km', '每圈距離');
  addPreviewStat(mins.toFixed(1) + ' min', '每圈時間');
}

function addPreviewStat(num, label) {
  const d = document.createElement('div');
  const n = document.createElement('div'); n.className = 'pi-num'; n.textContent = num;
  const l = document.createElement('div'); l.className = 'pi-label'; l.textContent = label;
  d.append(n, l);
  $preview.append(d);
}

function setStatus(text, cls = '') { $status.textContent = text; $status.className = cls; }

let totalDist = 0, startTime = null, lapCount = 0, statsTimer = null;
function resetStats() {
  totalDist = 0; startTime = null; lapCount = 0;
  document.getElementById('statDist').textContent = '0.0';
  document.getElementById('statTime').textContent = '0:00';
  document.getElementById('statLaps').textContent = '0';
  if (statsTimer) clearInterval(statsTimer);
}
function startStatsTimer() {
  startTime = Date.now();
  statsTimer = setInterval(() => {
    const s = Math.floor((Date.now() - startTime) / 1000);
    document.getElementById('statTime').textContent =
      `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
  }, 1000);
}

$btnPreview.addEventListener('click', async () => {
  if (parsedFlowers.length < 2) {
    setStatus('至少需要 2 朵花', 'error');
    return;
  }
  $preview.textContent = 'OSRM TSP 規劃中…';
  $preview.className = 'preview-info loading';
  $btnStart.disabled = true;

  try {
    const resp = await fetch('/api/preview-flower-cruise', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flowers: parsedFlowers }),
    });
    const data = await resp.json();
    if (data.error) {
      $preview.textContent = data.error;
      $preview.className = 'preview-info error';
      return;
    }
    previewedRoute = {
      ordered: data.ordered,
      distance_km: data.distance_km,
      count: data.count,
    };
    // Polyline = ordered + back to ordered[0]
    const closed = data.ordered.concat([data.ordered[0]]);
    routeLine.setLatLngs(closed);
    renderFlowerMarkers(data.ordered, true);
    map.fitBounds(routeLine.getBounds().pad(0.15));
    $preview.className = 'preview-info';
    updateLapTime();
    $btnStart.disabled = false;
  } catch (err) {
    $preview.textContent = `Error: ${err.message}`;
    $preview.className = 'preview-info error';
  }
});

let ws;
function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onclose = () => { setStatus('斷線，3s 重連', 'error'); setTimeout(connectWS, 3000); };
  ws.onerror = () => setStatus('WS error', 'error');
  ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
}

function handleMessage(m) {
  switch (m.type) {
    case 'hello':
      if (m.device) {
        $deviceStatus.textContent = m.device.product;
        $deviceStatus.className = 'ok';
      }
      if (m.last_position) {
        updateDevice(m.last_position.lat, m.last_position.lon, true);
        setStatus(`位置: ${m.last_position.lat.toFixed(4)}, ${m.last_position.lon.toFixed(4)}`);
      } else {
        setStatus('先在主控台瞬移到某處');
      }
      break;
    case 'teleported':
      updateDevice(m.lat, m.lon);
      break;
    case 'loop_walk_started':
      $btnStart.disabled = true; $btnStop.disabled = false; $btnPause.disabled = false; $btnPreview.disabled = true;
      isPaused = false; $btnPause.textContent = '⏸ 暫停';
      document.getElementById('statsRow').style.display = 'grid';
      resetStats(); startStatsTimer();
      trailLine.setLatLngs([]);
      setStatus('巡航中…', 'running');
      break;
    case 'loop_lap':
      lapCount++;
      document.getElementById('statLaps').textContent = String(lapCount);
      break;
    case 'tick':
      updateDevice(m.lat, m.lon);
      trailLine.addLatLng([m.lat, m.lon]);
      totalDist += (m.step_m || 0);
      document.getElementById('statDist').textContent = (totalDist / 1000).toFixed(1);
      break;
    case 'paused':
      isPaused = true;
      $btnPause.textContent = '▶ 繼續';
      setStatus('已暫停', '');
      if (statsTimer) clearInterval(statsTimer);
      break;
    case 'resumed':
      isPaused = false;
      $btnPause.textContent = '⏸ 暫停';
      setStatus('巡航中…', 'running');
      startStatsTimer();
      break;
    case 'stopped':
    case 'done':
      setStatus(m.type === 'done' ? '完成' : '已停止');
      $btnStart.disabled = false; $btnStop.disabled = true; $btnPause.disabled = true; $btnPreview.disabled = false;
      isPaused = false; $btnPause.textContent = '⏸ 暫停';
      if (statsTimer) clearInterval(statsTimer);
      break;
    case 'error':
      setStatus(`Error: ${m.message}`, 'error');
      $btnStart.disabled = false; $btnStop.disabled = true; $btnPreview.disabled = false;
      if (statsTimer) clearInterval(statsTimer);
      break;
  }
}

$btnStart.addEventListener('click', () => {
  if (!previewedRoute) return;
  // Close the loop: append ordered[0] as the final waypoint so the runner
  // returns to start before the next iteration.
  const closed = previewedRoute.ordered.concat([previewedRoute.ordered[0]]);
  ws.send(JSON.stringify({
    type: 'start_loop_walk',
    speed_kmh: parseInt($speed.value),
    route: closed,
  }));
});

let isPaused = false;
$btnPause.addEventListener('click', () => {
  if (isPaused) ws.send(JSON.stringify({ type: 'resume' }));
  else ws.send(JSON.stringify({ type: 'pause' }));
});

$btnStop.addEventListener('click', () => ws.send(JSON.stringify({ type: 'stop' })));

connectWS();
</script>
</body>
</html>
```

- [ ] **Step 2: 確認 HTML 結構（簡單檢查 script 部分有閉合）**

Run: `tail -5 static/flower-cruise.html`
Expected: 看到 `</script>\n</body>\n</html>`

- [ ] **Step 3: Commit**

```bash
git add static/flower-cruise.html
git commit -m "feat(flower-cruise): add /flower-cruise page UI"
```

---

## Task 5: 在主頁加入 nav link

**Files:** Modify `static/index.html` (~line 384)

- [ ] **Step 1: 找到既有的 `/walk` link**

Run: `sed -n '383,392p' static/index.html`
Expected: 看到 `<a href="/walk" style="...">🚶 道路漫步模式 →</a>`

- [ ] **Step 2: 在 `/walk` link 上方加入 `/flower-cruise` link**

把這段：

```html
    <a href="/walk" style="
      display: block; text-align: center; padding: 12px;
      margin-top: auto;
      background: var(--panel-2); border: 1px solid var(--border);
      border-radius: 8px; color: var(--accent); text-decoration: none;
      font-size: 14px; font-weight: 500;
    ">🚶 道路漫步模式 →</a>
```

替換成：

```html
    <a href="/flower-cruise" style="
      display: block; text-align: center; padding: 12px;
      margin-top: auto;
      background: var(--panel-2); border: 1px solid var(--border);
      border-radius: 8px; color: var(--accent); text-decoration: none;
      font-size: 14px; font-weight: 500;
    ">🌸 花朵巡航 →</a>
    <a href="/walk" style="
      display: block; text-align: center; padding: 12px;
      margin-top: 8px;
      background: var(--panel-2); border: 1px solid var(--border);
      border-radius: 8px; color: var(--accent); text-decoration: none;
      font-size: 14px; font-weight: 500;
    ">🚶 道路漫步模式 →</a>
```

注意：
- 第一個 link `margin-top: auto` 把整組推到底
- 第二個 link 改成 `margin-top: 8px`（不能再 auto，否則兩個都飛到底）

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat(flower-cruise): add nav link from main page"
```

---

## Task 6: 手動 smoke test（驗收）

**Files:** None — runtime verification only

- [ ] **Step 1: 啟動 server**

Run:
```bash
cd /Users/chunn/projects/pikmin-walk
make run
```
（或者直接 `uv run server.py`，看 Makefile）

預期：看到 `Pikmin Walker UI  →  http://localhost:7766` 訊息，沒有 traceback。

- [ ] **Step 2: 確認 `/flower-cruise` 頁面開得起來**

Run（另開 terminal）:
```bash
curl -sI http://localhost:7766/flower-cruise | head -1
```
Expected: `HTTP/1.1 200 OK`

開瀏覽器到 `http://localhost:7766/flower-cruise`，應該看到深色面板、空 textarea、「貼上座標後按預覽」字樣。

- [ ] **Step 3: 測試 textarea 解析**

把 spec 裡使用者提供的 31 個座標貼進去：

```
41.450779,31.795279
41.449948,31.794372
41.449624,31.795348
41.452536,31.794378
41.451554,31.796099
41.450812,31.792871
41.451453,31.793620
41.451981,31.793474
41.452682,31.793780
41.452254,31.790507
41.451949,31.790507
41.450888,31.790813
41.449341,31.788606
41.448095,31.789797
41.447800,31.788019
41.448848,31.788204
41.447064,31.788365
41.447483,31.790626
41.446360,31.790043
41.446248,31.792581
41.445850,31.792229
41.446973,31.792025
41.447022,31.794224
41.449039,31.795133
41.447998,31.795149
41.448224,31.796136
41.448092,31.793067
41.448548,31.791547
41.448522,31.789965
41.450020,31.789667
41.449075,31.792559
```

Expected:
- 「已解析 31 朵花」
- 地圖上看到 31 個粉色 markers（hover 顯示「花 #N」）
- 地圖自動 zoom 到花群範圍
- 「預覽路線」按鈕變可點

- [ ] **Step 4: 測試 preview**

按「預覽路線」。

Expected:
- preview-info 區塊先顯示「OSRM TSP 規劃中…」（藍色）
- 1-3 秒後顯示「31 朵 / X.XX km / Y.Y min」三個 stat
- 地圖上出現粉色虛線 polyline（dashArray），連接 ordered flowers + 回到第 1 朵
- 每個 marker 旁邊有數字 tooltip（1, 2, 3, ..., 31）
- 「開始巡航」按鈕變可點

如果失敗：
- 「OSRM 無法規劃路線」→ 檢查網路、檢查 `_osrm_fetch` 的 server log
- Markers 沒出現 → 開 DevTools console 看有沒有 JS error

- [ ] **Step 5: 測試走起來**

確認 iOS 裝置已透過 tunneld 連上（`make run` 啟動時會印 device 訊息）。先在 `/` 主頁瞬移到附近某處（例如第一朵花），再回到 `/flower-cruise`，按「開始巡航」。

Expected:
- WS message `loop_walk_started` 收到
- stats-row 顯示出來
- 藍色裝置 marker 開始動，沿著粉色虛線跑
- 紅色 trail 跟著畫
- 走完一圈後 lap 數 +1
- 第二圈自動接著走（無限 loop）

- [ ] **Step 6: 測試控制按鈕**

- 拖 speed slider → 速度即時變（裝置 marker 移動速度肉眼可見變化）
- 按「暫停」→ 裝置停下、按鈕變「▶ 繼續」、status 顯示「已暫停」
- 按「繼續」→ 接著走
- 按「停止」→ 完全停止、按鈕回到初始狀態

- [ ] **Step 7: 測試錯誤處理**

開 DevTools console，分別貼以下測試：

| 輸入 | 預期 |
|---|---|
| 空白 | 「尚未解析」、「預覽路線」按鈕 disabled |
| `25.0` | 「尚未解析」（只有 1 個數字、不到一對） |
| `25.0 121.5` | 「已解析 1 朵花」、「預覽路線」disabled（< 2） |
| `200,400` | 「尚未解析」（lat/lon 範圍外被丟掉） |
| `25.0,121.5,gibberish,more,25.1,121.6` | 「已解析 2 朵花」（regex 抓數字、配對前 4 個有效數字） |

- [ ] **Step 8: 測試從主頁進入**

回到 `http://localhost:7766/`。

Expected:
- 側邊欄底部有「🌸 花朵巡航 →」連結（在「🚶 道路漫步模式」上方）
- 點擊跳到 `/flower-cruise`

- [ ] **Step 9: 標記完成**

如果上述全部通過，這個功能就完成了。如果有任何不對，回頭修改對應 task 的程式碼。

---

## Self-Review

**Spec coverage check:**

| Spec section | Implemented in |
|---|---|
| 不偏移、用原座標 | Task 2（`preview_flower_cruise` 直接用 input flowers，無偏移計算） |
| OSRM TSP 排序、不取幾何 | Task 1（`_osrm_trip_order` 用 `overview=false`） |
| 直線連點 | Task 4（前端 polyline 用 `data.ordered.concat([ordered[0]])`），server 端 `_handle_start_loop_walk` 既有邏輯就是直線 |
| 無限 loop | 複用 `_handle_start_loop_walk`，已是 `while True` 結構 |
| 預設速度 19 km/h、slider 1–50 | Task 4（`<input min="3" max="50" value="19">`，server 既有 `set_speed` clamp 1–50） |
| Textarea + 寬鬆解析 | Task 4（`parseFlowers()` 用 regex `/-?\d+\.?\d*/g`） |
| 獨立頁面 `/flower-cruise` | Tasks 3, 4 |
| 100% 複用 `_handle_start_loop_walk` | Task 4（前端送 `start_loop_walk` WS message） |
| 主頁加 nav link | Task 5 |
| TSP 起點固定 first | Task 1（`?source=first` query param） |

**Placeholder scan:** 已掃過全文、沒有 TBD/TODO/「fill in details」等。

**Type consistency:**
- `preview_flower_cruise` 回傳欄位 `ordered`、`distance_km`、`count` — Task 4 前端 `previewedRoute.ordered/distance_km/count` 完全對齊 ✔
- `_osrm_trip_order` 回傳 `list[tuple[float, float]] | None`，`preview_flower_cruise` 檢查 `if ordered is None` ✔
- WS payload `{ type: 'start_loop_walk', route, speed_kmh }` 跟既有 `_handle_start_loop_walk` 簽名一致（讀 `msg.get("route")` 跟 `msg.get("speed_kmh", 19)`）✔
