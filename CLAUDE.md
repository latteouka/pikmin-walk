# Pikmin Walker

## Overview

iOS GPS location simulation 工具，透過 pymobiledevice3 操控 CoreLocation。Web UI (Starlette + Leaflet) 支援瞬移、路線移動、隨機漫步。

## Architecture

```
Browser (Leaflet)  ←WebSocket→  server.py (Starlette)  ←DVT/Lockdown→  iPhone/iPad
                                     ↓
                               pikmin_walk.py
                            (profiles, geodesy,
                             simulate, random_walk)
```

### 兩條 iOS 通道

- **iOS ≤16 (legacy)**: `DtSimulateLocation` via TCP lockdown (port 62078)。每次 `set()` 開新 service connection。Wi-Fi 用 `create_using_tcp()` + pair record。
- **iOS 17+ (DVT)**: `LocationSimulation` via RemoteXPC tunnel。需要 `sudo pymobiledevice3 remote tunneld` 在背景跑。Wi-Fi 靠 tunneld 自動 discover。

### Server 連線優先順序

1. **iOS 17+ tunneld** — `get_tunneld_devices()` 找到就用 DVT
2. **Wi-Fi TCP lockdown** — 讀 `state.json` 的 `last_wifi_host` 嘗試直連（秒級啟動），失敗才走 Bonjour browse
3. **mobdev2 link-local** — USB-adjacent 的 fe80::/169.254.x.x 介面
4. **USB usbmux** — 最後的 fallback，首次使用時自動存 pair record

### state.json（runtime，gitignore）

```json
{
  "last_position": {"lat": 43.0686, "lon": 141.3507},
  "last_wifi_host": "192.168.0.113",
  "bookmarks": [{"name": "札幌駅", "lat": 43.0686, "lon": 141.3507}],
  "google_maps_api_key": "",
  "saved_at": "2026-04-12T..."
}
```

- `last_position`: server 重啟時載入但**不** push 到手機（避免蓋掉實際狀態）
- `last_wifi_host`: cached Wi-Fi IP，跳過 Bonjour browse 加速啟動
- `bookmarks`: 使用者自訂地點，REST CRUD via `/api/bookmarks`
- Server 關閉時**不** call `clear()` — 手機保持在最後位置

## File Responsibilities

| File | 職責 |
|---|---|
| `server.py` | HTTP routes, WebSocket handler, DeviceSession (device lifecycle + transport selection), state persistence |
| `pikmin_walk.py` | Profile dataclass, geodesy helpers (haversine, bearing, destination_point), `simulate()` (route-based), `random_walk()` (correlated walk + trail repulsion), CLI entry point |
| `clear.py` | One-shot clear: 連 lockdown → `DtSimulateLocation.clear()` → 手機回真實 GPS |
| `static/index.html` | Single-file SPA: Leaflet map, tile layer switcher, waypoint management, WebSocket client, bookmark CRUD, Google Places autocomplete |

## Key Design Decisions

### random_walk 的物理模型

- **Correlated heading**: 每 tick 加 gaussian noise (σ=22°)，不是 white noise — 產生自然的 S 形軌跡
- **Home tether**: 超過 `max_radius_m` 時 heading 被拉往 center，pull strength ∝ 超出距離
- **Trail repulsion**: 最近 300 ticks 的位置作為 inverse-square repulsion field，避免短期重複踩同一條路
- **Position jitter 不回寫**: yield 的是 noisy 座標，但 `current` 保持 clean — 防止 random walk drift

### Wi-Fi pair record 的路徑

- USB pairing record: `/var/db/lockdown/<UDID>.plist` (SIP 保護，usbmux 以 root 讀)
- Wi-Fi TCP lockdown: 讀不到 `/var/db/lockdown/`，需要匯出到 `~/.pymobiledevice3/<UDID>.plist`
- `get_mobdev2_lockdowns()` 用 `WiFiMACAddress` 欄位 match Bonjour 廣播
- 首次 USB 連線時 server 會 auto-save pair record (`_maybe_save_pair_record()`)

### 為什麼 nominal_kmh 是 19 而不是 15

rwalk profile 的 position jitter (σ=1.0m) 在 step ≈ 5.3 m 的情況下膨脹比 ≈ 1.04x。Effective speed ≈ 19.8 km/h。如果使用者報告 Pikmin Bloom 不算步數，降 nominal 到 13-14 km/h。

## API Endpoints

| Method | Path | 用途 |
|---|---|---|
| GET | `/` | Serve index.html |
| GET | `/api/profiles` | 所有 movement profile 的參數 |
| GET | `/api/config` | Google Maps API key |
| POST | `/api/config` | 更新 config (e.g., API key) |
| GET | `/api/bookmarks` | 書籤列表 |
| POST | `/api/bookmarks` | 新增書籤 `{name, lat, lon}` |
| PATCH | `/api/bookmarks/{idx}` | 編輯書籤 `{name}` |
| DELETE | `/api/bookmarks/{idx}` | 刪除書籤 |
| WS | `/ws` | 雙向：hello, teleport, start, stop, clear, tick |

## WebSocket Messages

### Client → Server

| type | payload | 說明 |
|---|---|---|
| `start` | `{profile, waypoints}` | 開始移動/漫步 |
| `stop` | — | 停止移動 |
| `teleport` | `{lat, lon}` | 瞬移 |
| `clear` | — | 清除模擬位置 |

### Server → Client

| type | payload | 說明 |
|---|---|---|
| `hello` | `{device, last_position}` | 連線初始化 |
| `started` | `{profile, total_m, is_random_walk, ...}` | 移動開始 |
| `tick` | `{lat, lon, elapsed, dwell, note}` | 每秒位置更新 |
| `teleported` | `{lat, lon}` | 瞬移完成 |
| `done` | `{elapsed}` | 路線走完 |
| `stopped` | — | 使用者停止 |
| `cleared` | — | 位置已清除 |
| `error` | `{message}` | 錯誤 |

## Caveats

- `state.json` 是 runtime 資料，不要 commit（已加 `.gitignore`）
- tunneld 需要 sudo（建立 utun 介面），server 本身不需要
- iOS 18.2+ 必須用 Python 3.13（PSK cipher），`uv tool install pymobiledevice3 --python 3.13`
- `horizontalAccuracy` 永遠回報 65m、`altitude` 永遠 0 — 這是 iOS simulate-location 的硬限制，無法改
- AP Client Isolation 會阻擋 Wi-Fi 連線 — 換一個沒有 isolation 的網路
