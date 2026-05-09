---
title: Pikmin Walker UI 重做設計
created: 2026-05-10
status: approved
---

# Pikmin Walker UI 重做設計

## 1. 目的與動機

現有 `static/index.html` 兩欄佈局 (`1fr 420px`) 把書籤夾在「瞬移到」section 下方，
`max-height: 160px` 一次只看得到 4–5 個書籤。隨著書籤累積，找特定地點越來越痛苦。

同時三頁面（首頁、花朵巡航、道路漫步）的切換目前只是右側工具列底部兩個小連結，
缺乏導覽感；首頁的「地名或 lat, lon」輸入框重整就消失，每次回來都要重打。

本次重做要解決：

1. 書籤要能一次看到更多 → 獨立成欄、撐滿整個垂直高度
2. 書籤量大時要能分類 → 單一分類 + 下拉 filter
3. 三個功能頁要有正式的導覽 → 頂部 navbar
4. 常用 input 重整不消失 → localStorage 持久化
5. 整體視覺升級 → shadcn 美學 + Tailwind CDN（不引入 React/build pipeline）

## 2. Scope（要動哪些檔案）

| 檔案 | 改動量 | 內容 |
|---|---|---|
| `static/index.html` | 大改 | 三欄佈局、書籤獨立欄 + 分類、navbar、shadcn 美學、localStorage |
| `static/flower-cruise.html` | 中改 | navbar、shadcn 美學、jump input localStorage |
| `static/walk.html` | 小改 | navbar、shadcn 美學（保持原本佈局） |
| `server.py` | 小改 | bookmark schema 加 `category`、新增 `bookmark_categories` 欄位與 API |

**保持單檔 vanilla HTML/JS。** 不引入 React、不加 build pipeline。Tailwind 用官方 CDN
(`https://cdn.tailwindcss.com`)。視覺風格借 shadcn/ui 的色票與 component pattern，
但不依賴 Radix primitives — 用原生 `<select>` / `<details>` / 自製 dropdown 達成。

## 3. 整體佈局（首頁 `index.html`）

```
┌─ navbar (top, 48px tall, sticky) ───────────────────────────┐
│  ● Pikmin Walker      [🌱 漫步種花] [🌸 花朵巡航] [🚶 道路漫步]  │
├──────────────────┬──────────────┬──────────────────────────┤
│                  │              │  ★ 書籤                    │
│                  │  🎯 瞬移到    │  ┌────────────────┐        │
│                  │  漫步半徑     │  │ 全部 ▾ │+│✏️│         │ ← 分類 filter
│      MAP         │  速度        │  ├────────────────┤        │
│                  │  ▶ 開始      │  │ 札幌駅          │        │
│                  │  ■ 停止      │  │ 京都站          │        │ ← flex: 1
│                  │  狀態        │  │ 大阪城          │        │   撐滿
│                  │              │  │ ...            │        │
└──────────────────┴──────────────┴──────────────────────────┘
     1fr             ~360px         ~300px
```

CSS Grid:

```css
#app {
  display: grid;
  grid-template-rows: 48px 1fr;
  grid-template-columns: 1fr 360px 300px;
  height: 100vh;
}
nav { grid-column: 1 / -1; }
```

書籤欄內部結構:

```css
#bookmarks-pane {
  display: flex;
  flex-direction: column;
  height: 100%;        /* 撐滿 grid cell */
}
#bookmarks-header { flex: 0 0 auto; }   /* 分類 filter 列固定 */
#bookmarks {
  flex: 1 1 auto;
  overflow-y: auto;    /* 列表自己滾，不是整欄滾 */
  min-height: 0;       /* flex child 可縮 */
}
```

## 4. Navbar

三頁共用同一段 HTML 片段（手寫 inline，不抽 component — vanilla 沒模板系統）。
每頁的 active 用一個 `data-page` 屬性標記，CSS 對應變色。

```html
<nav class="navbar">
  <a href="/" class="brand">● Pikmin Walker</a>
  <div class="nav-links" data-active="home">  <!-- home / flower / walk -->
    <a href="/" data-page="home">🌱 漫步種花</a>
    <a href="/flower-cruise" data-page="flower">🌸 花朵巡航</a>
    <a href="/walk" data-page="walk">🚶 道路漫步</a>
  </div>
</nav>
```

樣式: 背景 `--panel`、底部 1px border、active link 用 accent 底色。
Sticky `top: 0`，z-index 高於 Leaflet 控制項。

`flower-cruise.html` 與 `walk.html` 整體佈局不變，只在最上方加 navbar
並把現有的 grid 改成 `grid-template-rows: 48px 1fr`。

## 5. 書籤欄與分類

### 資料結構（`state.json`）

```json
{
  "bookmarks": [
    {"name": "札幌駅", "lat": 43.0686, "lon": 141.3507, "category": "日本"}
  ],
  "bookmark_categories": ["未分類", "日本", "本土"]
}
```

### Migration

`server.py` 啟動時 (`load_state`)：若 bookmark 沒有 `category` 欄位 → 補填 `"未分類"`；
若沒有 `bookmark_categories` 欄位 → 預設 `["未分類"]`，並 union 進所有 bookmark 的
existing category（防 state.json 被手動編輯）。

### API

| Method | Path | 行為 |
|---|---|---|
| `GET` | `/api/bookmarks` | 回 `{bookmarks: [...], categories: [...]}` （**改成物件，原本是 array**）|
| `POST` | `/api/bookmarks` | body `{name, lat, lon, category?}`，回新狀態 |
| `PATCH` | `/api/bookmarks/{idx}` | body `{name?, category?}`，回新狀態 |
| `DELETE` | `/api/bookmarks/{idx}` | 回新狀態 |
| `POST` | `/api/bookmark-categories` | body `{name}`，新增分類 |
| `PATCH` | `/api/bookmark-categories/{old_name}` | body `{name}`，重新命名（同步更新所有書籤的 category）|
| `DELETE` | `/api/bookmark-categories/{name}` | 刪除分類，原屬書籤 fallback 到 `"未分類"`，`"未分類"` 本身禁刪 |

> **Breaking change 注意**：`GET /api/bookmarks` 從 array 變物件，所有頁面的 `renderBookmarks(list)` 要改成 `renderBookmarks(state.bookmarks, state.categories)`。

### UI

書籤欄頂部：

```
┌─────────────────────────────────────┐
│ [全部 ▾]   [+ 新分類]   [✏️ 管理]    │
└─────────────────────────────────────┘
```

- `select` 列出 `["全部", ...categories]`，預設選「全部」
- `+ 新分類` → 用 `prompt()` 取名稱 → POST `/api/bookmark-categories`
- `✏️ 管理` → toggle 一個 inline `<div>` 面板顯示在書籤欄頂部，列出所有分類，每行有「重新命名」/「刪除」按鈕；按重新命名觸發 `prompt()` 取新名字 → PATCH；按刪除觸發 `confirm()` → DELETE。「未分類」這列的兩個按鈕都是 `disabled` 並加 hover tooltip「系統分類，無法修改」

書籤 list item：

```
┌─────────────────────────────────────┐
│ 札幌駅          [日本▾]      ✕      │
│ 43.0686, 141.3507                   │
└─────────────────────────────────────┘
```

- 名稱 click-to-edit（沿用現有 `bk-name` input 邏輯）
- 分類用小型 dropdown（`<select>`）就地切換
- `✕` 刪除

加書籤輸入：「★ 存」按鈕旁多一個分類 dropdown，預設值為 `pikmin.lastSelectedCategory` localStorage。

Filter 行為：選「全部」顯示所有；選某分類則只顯示該分類的書籤。Filter 值寫進 `pikmin.bookmarkCategoryFilter` localStorage。

## 6. localStorage 持久化

| Key | 寫入時機 | 讀取時機 |
|---|---|---|
| `pikmin.jumpInput` | 首頁 `#jumpInput` 的 `input` 事件（debounce 200ms） | DOMContentLoaded |
| `pikmin.flowerCruise.flowersText` | 花朵巡航 `#flowersText` textarea 的 `input` 事件（debounce 300ms） | DOMContentLoaded |
| `pikmin.bookmarkCategoryFilter` | 分類 filter `change` 事件 | DOMContentLoaded |
| `pikmin.lastSelectedCategory` | 加書籤時的 dropdown `change` 事件 | DOMContentLoaded |

封裝小 helper:

```js
const storage = {
  get(key, fallback = '') {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch {}
  },
};
```

## 7. shadcn 美學策略

引入 Tailwind CDN（dev mode，沒 build step；首屏會稍慢但可接受 — 本工具是 local 自用）：

```html
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    darkMode: 'class',
    theme: {
      extend: {
        colors: {
          border: 'hsl(217.2 32.6% 17.5%)',
          input: 'hsl(217.2 32.6% 17.5%)',
          ring: 'hsl(212.7 26.8% 83.9%)',
          background: 'hsl(222.2 84% 4.9%)',
          foreground: 'hsl(210 40% 98%)',
          primary: 'hsl(210 40% 98%)',
          muted: 'hsl(217.2 32.6% 17.5%)',
          'muted-foreground': 'hsl(215 20.2% 65.1%)',
          accent: 'hsl(217.2 32.6% 17.5%)',
          destructive: 'hsl(0 62.8% 30.6%)',
        },
      },
    },
  };
</script>
<body class="dark bg-background text-foreground">
```

Component utility class（手寫，不抽 .css 檔；inline 在 HTML 裡）:

- **Button (default)**: `inline-flex items-center justify-center rounded-md text-sm font-medium h-9 px-4 bg-primary text-background hover:bg-primary/90 transition-colors`
- **Button (destructive)**: 同上但 `bg-destructive text-destructive-foreground hover:bg-destructive/90`
- **Button (outline)**: `border border-input bg-background hover:bg-accent hover:text-accent-foreground`
- **Button (ghost)**: `hover:bg-accent hover:text-accent-foreground`
- **Input**: `flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring`
- **Card**: `rounded-lg border bg-card text-card-foreground shadow-sm`
- **List item (interactive)**: `flex items-center gap-3 px-3 py-2 rounded-md hover:bg-accent transition-colors cursor-pointer`

實作時用 `frontend-design` skill 把書籤欄做出**有設計感**的版本，避免落入 generic dark UI。

## 8. 不做（YAGNI）

- 拖曳排序書籤、拖曳換分類
- 一個書籤多個 tag（已決定單一分類）
- React 化、build pipeline、Vite/Next
- 分類顏色標籤
- 書籤 import/export
- 跨裝置同步分類偏好（localStorage 限本機）
- 走路頁 (`walk.html`) 的內容區塊重排（只加 navbar + 視覺升級）

## 9. 風險與決策紀錄

- **Tailwind CDN dev mode 警告**：CDN 版本 console 會出現 production 警告。本工具是 local 自用，可接受。
- **`/api/bookmarks` response shape breaking**：所有 client（三頁都會 fetch 嗎？）需同步更新。實作時要 grep 所有 `fetch('/api/bookmarks')` 確認。
- **`"未分類"` 的特殊地位**：禁刪、是預設、無法重命名。UI 要在管理面板隱藏這三個動作。
- **state.json 同時被多個 page 寫**：現狀沒 lock，並發寫會有 race。本次不處理（現狀已有此問題，非本次造成）。
- **書籤 indexed by position**：API 用 `idx`，當分類 filter 套用時前端要把 filter 後的 visible index 映射回原始 index。實作時用 `data-original-idx` 屬性記住。

## 10. 驗收

- [ ] 三頁都有 navbar，active 頁面正確高亮
- [ ] 首頁三欄佈局正確顯示，書籤欄獨立並撐滿可用高度
- [ ] 書籤可建立/重新命名/刪除分類
- [ ] 書籤可在分類間移動
- [ ] 分類 filter 正確過濾
- [ ] 「未分類」無法被刪除或重新命名
- [ ] 重整首頁，jumpInput 仍保留上次值
- [ ] 重整花朵巡航，對應 input 仍保留上次值
- [ ] 重整後 filter 維持上次選擇
- [ ] 既有 `state.json`（沒有 `category` 欄位）能正常 load 且自動補 `"未分類"`
- [ ] 視覺風格貼近 shadcn（小圓角、subtle border、focus ring）

## 11. 後續實作步驟

呼叫 `writing-plans` skill 產出細部 plan。
