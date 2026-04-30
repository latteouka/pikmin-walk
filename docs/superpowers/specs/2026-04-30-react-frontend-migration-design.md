# React Frontend Migration 設計

**Created:** 2026-04-30

## Overview

把 pikmin-walk 的前端（3 個 vanilla HTML/CSS/JS 檔，共 1872 行）migrate 到 Next.js 16 + React 19 + TypeScript + Tailwind 4 + shadcn/ui，目的是讓未來新增頁面/功能時可以 compose 共用 component，而不是每次複製 400+ 行 boilerplate。

**Backend（Python + Starlette + pymobiledevice3）完全不動** — `pymobiledevice3` 沒有 JS 替代物，所以 backend 必鬚是 Python。WebSocket protocol、REST API endpoints、走路 runner 全部保留。

## Decisions（已釐清）

| 項目 | 決定 |
|---|---|
| 動機 | E：未來擴充（現有 vanilla 重複多、難以維護） |
| Stack | Next.js 16 + React 19 + TypeScript strict + Tailwind 4 + shadcn/ui + pnpm |
| 部署 | `output: 'export'`（純靜態），build output commit 進 git |
| 朋友裝機 | 只需 Python + uv，`make run` 一行（不需要 Node） |
| 遷移節奏 | C：骨架先 PR（含全部共用 component）→ 三頁逐個 port → 收尾 |
| Backend | 不動 |
| 地圖 | Leaflet 直接 + `useEffect`（不裝 react-leaflet） |
| WS | 自寫 `useWebSocket` hook |
| State | `useState` + Context（規模小，不需要 Zustand） |
| 圖示 | lucide-react |
| 捨棄的 t3 部分 | tRPC、Prisma、NextAuth、`@t3-oss/env-nextjs`、TanStack Query |

## Architecture

### 檔案佈局

```
~/projects/pikmin-walk/
├── server.py              # 不動 logic，只調整 static serve
├── pikmin_walk.py         # 完全不動
├── Makefile               # +pnpm install / pnpm build target
├── web/                   # ★ 新：Next.js source
│   ├── package.json
│   ├── next.config.ts     # output: 'export', trailingSlash: false
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── postcss.config.js
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx                    # /
│   │   │   ├── walk/page.tsx               # /walk
│   │   │   └── flower-cruise/page.tsx      # /flower-cruise
│   │   ├── components/
│   │   │   ├── map-panel.tsx
│   │   │   ├── control-panel.tsx
│   │   │   ├── device-status.tsx
│   │   │   ├── speed-slider.tsx
│   │   │   ├── stats-row.tsx
│   │   │   └── ui/                         # shadcn primitives
│   │   ├── hooks/
│   │   │   ├── use-websocket.ts
│   │   │   └── use-device-session.ts
│   │   ├── lib/
│   │   │   ├── api.ts
│   │   │   ├── flowers.ts
│   │   │   └── ws-types.ts
│   │   └── styles/globals.css
│   └── public/
├── static/                 # build output（commit 進 git）
│   ├── index.html
│   ├── walk/index.html
│   ├── flower-cruise/index.html
│   └── _next/static/...
└── docs/
```

### server.py 修改

```python
# 從這個：
Route("/", index),
Route("/walk", walk_page),
Route("/flower-cruise", flower_cruise_page),
# ... API routes ...
Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),

# 改成這個：
# /api/* and /ws routes（在 Mount 之前匹配）
Route("/api/preview-loop", preview_loop, methods=["POST"]),
Route("/api/preview-flower-cruise", preview_flower_cruise, methods=["POST"]),
# ... 其他 API ...
WebSocketRoute("/ws", ws_endpoint),
# 最後一個：static serve（fallback to /:page/index.html）
Mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static"),
```

`StaticFiles(html=True)` 會自動處理：
- `GET /` → `static/index.html`
- `GET /walk` → `static/walk/index.html`（trailing slash 自動處理）
- `GET /flower-cruise` → `static/flower-cruise/index.html`
- `GET /_next/static/*` → 對應 bundle

`/api/*` 跟 `/ws` 因為是顯式 Route 在 Mount 之前定義，會先匹配。

### 共用 Hooks

```typescript
// hooks/use-websocket.ts — 純 WS connection management
useWebSocket(): {
  ready: boolean;
  send: (msg: WsClientMessage) => void;
  onMessage: (handler: (msg: WsServerMessage) => void) => () => void; // unsubscribe fn
}

// hooks/use-device-session.ts — 包 useWebSocket，封裝裝置狀態
useDeviceSession(): {
  device: { product: string; udid: string } | null;
  position: { lat: number; lon: number } | null;
  isWalking: boolean;
  isPaused: boolean;
  status: { text: string; tone: 'idle' | 'running' | 'error' };
  teleport: (lat: number, lon: number) => void;
  pause: () => void;
  resume: () => void;
  stop: () => void;
}
```

### 共用 Components

```tsx
<MapPanel center={[lat, lon]} zoom={13} onMapReady={(map) => {...}}>
  {/* page-specific overlays */}
</MapPanel>

<ControlPanel title="..." deviceStatus={...}>
  {children}  // page-specific controls
</ControlPanel>

<DeviceStatus device={device} />
<SpeedSlider value={speedKmh} onChange={setSpeedKmh} min={3} max={25} />
<StatsRow>
  <Stat label="km" value="..." />
  <Stat label="時間" value="..." />
</StatsRow>
```

### 為什麼 `<MapPanel>` 用 imperative bridge

Leaflet 是 imperative API（add/remove markers, polylines, layer groups），不容易純 declarative 包。用 `onMapReady(map)` 把 Leaflet instance 暴露給頁面層，由頁面 `useEffect` 處理 page-specific overlays（花朵 markers、TSP polyline、trail）。比 wrap 每個 layer type 成 React component 簡單。

### Dev 流程

兩個 terminal：
- Terminal A：`make run`（Python on :7766）
- Terminal B：`cd web && pnpm dev`（Next.js dev on :3000）

React code 用 absolute URLs 連 backend：
```typescript
// hooks/use-websocket.ts
const wsUrl = process.env.NODE_ENV === 'development'
  ? 'ws://localhost:7766/ws'
  : `ws://${location.host}/ws`;
```

API fetches 同樣絕對 URL（dev 時跨 :3000 → :7766）。

### 朋友裝機流程

```bash
# Friends 只需要：
git clone git@github.com:latteouka/pikmin-walk.git
cd pikmin-walk
make run     # Python 跑起來，serve static/ 內已 commit 的 build output
```

不需要 Node.js / pnpm。

### Maintainer dev/build 流程

```bash
# Setup once
cd web && pnpm install

# Dev
make run            # terminal A
cd web && pnpm dev  # terminal B

# Build before commit
cd web && pnpm build  # 產出 web/out/
cp -r web/out/* ../static/  # （或 build script 自動做）
git add static/ web/src/...
git commit
```

考慮加 git pre-commit hook 自動 rebuild + add `static/`，避免忘記 commit build output。但這是後期 nice-to-have，PR #5 再決定。

## PR Sequence

| PR | 範圍 |
|---|---|
| **#1 骨架** | `web/` 全新建立、Next.js + shadcn init、3 個 stub page、所有共用 component + hooks 實作、`server.py` 改 Mount、舊 HTML 暫保留 |
| **#2 花朵巡航** | 把 flower-cruise.html port 到 React、刪舊 HTML |
| **#3 道路漫步** | 把 walk.html port、刪舊 HTML |
| **#4 主控台** | 把 index.html port（最大的，933 行）、刪舊 HTML |
| **#5 收尾** | 移除 server.py 中過期 page handler、Makefile 加 build target、README 更新、`.gitignore` 整理 |

每個 PR 都走 subagent-driven development（implementer → spec review → code review → 實機測 → merge）。

### Fail-safe

PR #1 不刪舊 HTML — `static/index.html` 等仍存在。如果 React build 有問題，`Mount` fallback 會 serve 舊版。直到 PR #2-#4 各自刪掉對應的舊檔才完全切換。

## File-by-File Migration Notes

### `static/flower-cruise.html` (484 行) → React

最簡單的，剛寫的、結構清楚。會用：
- `<ControlPanel title="🌸 花朵巡航">`
- `<MapPanel onMapReady>` + page useEffect 畫粉色 markers + TSP polyline
- shadcn `<Textarea>` for 花朵輸入
- `<SpeedSlider>` + `<StatsRow>`
- `useDeviceSession()` hook
- `lib/flowers.ts:parseFlowers(text)` regex

### `static/walk.html` (455 行) → React

形狀按鈕（square/rect/circle）改成 shadcn `<ToggleGroup>` 或 `<Tabs>`。Lap distance slider + speed slider 用 shadcn `<Slider>`。

### `static/index.html` (933 行) → React 主控台

最複雜，包含：
- 地圖點選 waypoints（多 mode：teleport / route / circle 中心）
- bookmark CRUD（list + add + delete）— shadcn `<Card>` + `<Input>` + 動態 list
- Google Places autocomplete — 直接用 google-maps-services 或留現有 script tag
- Profile 切換（rwalk / circle）+ 對應 sliders（半徑、速度、軌跡記憶）
- Teleport / Clear / Stop / 隨機漫步控制
- 狀態顯示區

每個區塊抽成獨立 component（`<BookmarkList>`, `<PlaceSearch>`, `<ProfileTabs>`, etc.）。

## Caveats

- **Build output 噪音**：每次 UI 改動 PR 會帶大量 `static/` diff。Review 時看 `web/src/` 為主。
- **`output: 'export'` 限制**：不能用 Next.js Image optimization、API routes、middleware；都不需要，可接受。
- **Tailwind 4 vs shadcn**：Tailwind 4 的 CSS-first config 跟 shadcn CLI 的 ts config 有 friction，可能需要混用 — PR #1 第一個踩到的就要解。
- **既有 Google Maps API key**：state 在 `shared.json`，`/api/config` 已有 GET/POST，React 端從 `/api/config` 讀 key 後 inject `<script>`。
- **WS reconnection**：既有 vanilla code 有 3s 自動重連，新 hook 也要保留。
- **本地 OSRM instances**：server 會 try 本地 OSRM_LOCAL_INSTANCES → public OSRM。前端不用知道，呼叫一樣。

## Out of Scope

- ❌ tRPC / Prisma / NextAuth（backend 是 Python）
- ❌ TanStack Query（fetch 太簡單，3 個 endpoint）
- ❌ Vercel deploy（這是 local tool，自己跑 Python serve）
- ❌ react-leaflet wrapper（直接用 Leaflet）
- ❌ Storybook / 視覺 regression test（個人工具）
- ❌ Unit test（vanilla 版也沒 test，glue code 為主）
- ❌ Auto-rebuild git hook（PR #5 才考慮）

## Resolved Questions

無 — 所有 open questions 都在腦力激盪階段釐清完畢。

## Implementation Plan Hint（給下一步 writing-plans）

預估工作量：
- PR #1（骨架 + 共用 component）：~1500 行新增（Next.js boilerplate + 5 components + 2 hooks + types）
- PR #2（flower-cruise）：~300 行（page + cleanup 484 行舊 HTML）
- PR #3（walk）：~300 行（同上 455 行舊 HTML）
- PR #4（index 主控台）：~700 行（最複雜，933 行舊 HTML）
- PR #5（收尾）：~50 行 + 文件

總計：~2850 行新 TypeScript/TSX，刪除 ~1872 行舊 HTML。

每個 PR 走 subagent-driven development（implementer → spec compliance review → code quality review → 實機 smoke test → merge）。每個 PR 應該獨立可 merge：寫完一個就 merge，不堆積。
