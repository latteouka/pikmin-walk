# Pikmin Walker

iOS GPS location simulation 工具，透過 [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) 的開發者通道操控 CoreLocation。

支援 iOS 16（legacy DtSimulateLocation）和 iOS 17+（DVT LocationSimulation），含 Web UI 可在地圖上即時瞬移、規劃路線、或執行隨機漫步。

## 功能

- **瞬移** — 輸入地名（Nominatim / Google Places）或座標，一鍵傳送
- **路線移動** — 在地圖上點 waypoint，選擇步行 / 開車 / 捷運 / 飛機 profile 移動
- **隨機漫步** — Correlated random walk with home tether + trail repulsion，專為 Pikmin Bloom 種花設計
- **書籤** — 儲存 / 編輯 / 快速跳轉常用地點
- **Wi-Fi 無線** — 拔掉 USB 後仍可繼續控制（需同一 Wi-Fi 網路）
- **位置持久化** — server 重啟不失憶，不會 auto-clear 手機位置
- **Google Maps 底圖** — 可切換 Google / 衛星 / OSM 圖層
- **Google Places Autocomplete** — 設定 API key 後可搜尋景點（選配）

## 前置需求

- **macOS** (Apple Silicon / Intel)
- iPhone / iPad 已啟用 [Developer Mode](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)
- 手機已信任此 Mac（USB 插上後按「信任此電腦」）

> Mac 端的依賴（uv、Python 3.13、pymobiledevice3）會由 `make install` 自動處理。

## 快速開始（clone 後兩行起跑）

```bash
git clone https://github.com/latteouka/pikmin-walk.git
cd pikmin-walk

make install      # 一次性：裝 uv、pymobiledevice3、列出裝置、印手機端 checklist
make start        # 啟動 server（自動偵測裝置 + 拉 tunneld）
```

打開 `http://localhost:7766` 即可。要看其他指令跑 `make help`。

### iOS 16（不需要 tunneld，但要先掛 DDI）

```bash
pymobiledevice3 mounter auto-mount   # 一次性
make start
```

### 多裝置（選配）

如果想同時跑 iPad + iPhone，建立 `Makefile.local` 把 UDID 寫進去：

```bash
make list-devices                    # 拿到 UDIDs
cp Makefile.local.example Makefile.local
$EDITOR Makefile.local               # 填入你的 UDIDs
make start-all                       # 兩台同時起，iPad :7766、iPhone :7767
```

## 設定 Wi-Fi 無線模式

讓手機拔掉 USB 後仍可繼續 spoof。

### 前提

- Mac 和手機在同一個 Wi-Fi 網路
- Router 沒有 AP Client Isolation

### 步驟

```bash
# 1. 啟用 Wi-Fi 連線（USB 插著時跑）
pymobiledevice3 lockdown wifi-connections --state on

# 2. 啟用 Wi-Fi Debugging（Bonjour 才會在真 Wi-Fi 介面上廣播）
uv run --with pymobiledevice3 python -c "
import asyncio
from pymobiledevice3.lockdown import create_using_usbmux
async def main():
    ld = await create_using_usbmux()
    await ld.set_value(domain='com.apple.mobile.wireless_lockdown',
                       key='EnableWifiDebugging', value=True)
    print('done')
    await ld.close()
asyncio.run(main())
"

# 3. 匯出 pair record（Wi-Fi TCP lockdown 需要）
UDID=$(pymobiledevice3 usbmux list | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['Identifier'])")
pymobiledevice3 lockdown save-pair-record ~/.pymobiledevice3/${UDID}.plist

# 4. 重啟 server — 它會自動偵測 Wi-Fi 並優先使用
# 5. 拔掉 USB — 瞬移應該仍然 work
```

## CLI 模式（不開 Web UI）

```bash
# 瞬移到指定座標
uv run pikmin_walk.py walk      # 步行 profile，預設路線
uv run pikmin_walk.py drive     # 開車
uv run pikmin_walk.py transit   # 捷運
uv run pikmin_walk.py flight    # 飛機
uv run pikmin_walk.py rwalk     # 隨機漫步

# 清除模擬位置（回到真實 GPS）
uv run clear.py
```

## 移動 Profile

| Profile | 速度 | 用途 | 特性 |
|---|---|---|---|
| `walk` | 4.8 km/h | 一般步行 | ±15% 速度抖動、3m GPS 雜訊、隨機紅燈停 |
| `drive` | 55 km/h | 市區開車 | ±20% 速度抖動、偶發紅燈停車 |
| `transit` | 45 km/h | 捷運/公車 | 每站停 35 秒、低速度變異 |
| `flight` | 850 km/h | 飛機巡航 | 2 秒 tick、大圓航線 |
| `rwalk` | 19 km/h | 種花用漫步 | 400m 半徑 home tether、trail repulsion、correlated heading |

## Google Places Autocomplete（選配）

```bash
# 設定 Google Maps API key（需啟用 Maps JavaScript API + Places API）
curl -X POST http://localhost:7766/api/config \
  -H 'Content-Type: application/json' \
  -d '{"google_maps_api_key": "YOUR_KEY"}'
```

沒有 API key 也能用 — 搜尋走 Nominatim（OpenStreetMap 免費 geocoder）。

## 檔案結構

```
pikmin-walk/
├── server.py           # Starlette web server + WebSocket + device session
├── pikmin_walk.py      # Profile 定義、geodesy、simulate/random_walk generators
├── clear.py            # 一行清除模擬位置
├── static/
│   ├── index.html         # 主控台
│   ├── walk.html          # 道路漫步（OSRM-routed loop）
│   └── flower-cruise.html # 花朵巡航（種花路線）
├── Makefile               # `make help` 看全部指令
├── Makefile.local.example # 多裝置 UDIDs 模板（複製成 Makefile.local 後使用）
├── state-<udid>.json      # Runtime：last_position、Wi-Fi host（per-device，gitignored）
├── shared.json            # Runtime：bookmarks、API key（gitignored）
├── CLAUDE.md              # AI assistant context
└── README.md
```

## iOS 版本相容性

| | iOS ≤16 | iOS 17–18.1 | iOS 18.2+ |
|---|---|---|---|
| 通道 | legacy DtSimulateLocation | DVT LocationSimulation | DVT LocationSimulation |
| 需要 DDI | Yes | No | No |
| 需要 tunneld | No | Yes (sudo) | Yes (sudo) |
| Tunnel 協議 | — | QUIC | TCP only |
| Python 最低版本 | 3.8 | 3.8 | **3.13** |
| Wi-Fi 拔線 | TCP lockdown | QUIC tunnel | TCP tunnel |
