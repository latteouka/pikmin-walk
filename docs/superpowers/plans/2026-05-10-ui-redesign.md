# Pikmin Walker UI 重做 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重做 Pikmin Walker 三個頁面的 UI — 首頁三欄佈局（地圖 | 工具列 | 書籤）、書籤獨立成欄並支援單一分類 + filter、頂部加 navbar 切換功能、首頁與花朵巡航的 input 持久化、整體視覺升級到 shadcn 美學（Tailwind CDN，不引入 React）。

**Architecture:** 保持單檔 vanilla HTML/JS、無 build pipeline。Tailwind 走 CDN dev mode，shadcn 風格用手寫 utility class。資料模型在 `shared.json` 加 `bookmark_categories` 陣列，每個 bookmark 加 `category` 欄位。`GET /api/bookmarks` response shape 從 array 改成 `{bookmarks, categories}` (breaking)；新增 `/api/bookmark-categories` CRUD。Persistent input 用 localStorage。

**Tech Stack:** Python + Starlette (server.py)、vanilla HTML/CSS/JS、Tailwind CDN、Leaflet（既有）、Playwright MCP（驗收用）。

**Spec:** `docs/superpowers/specs/2026-05-10-ui-redesign-design.md`

**Testing strategy:** 此 repo 沒有 pytest/Playwright 配置。Server task 用 `curl` + `jq` 做 acceptance 驗證；frontend 任務用瀏覽器手動 + Playwright MCP smoke test 驗證；最後 Task 10 跑 spec §10 驗收 checklist。

---

## File map

**Modify:**
- `server.py` — bookmark schema migration、API shape 變更、新增 bookmark-categories CRUD（Task 1–3）
- `static/index.html` — 三欄佈局 + 書籤欄 + 分類 UI + localStorage + shadcn 美學 + navbar（Task 4–9）
- `static/flower-cruise.html` — navbar + flowersText localStorage + shadcn 美學 + 適配新 API shape（Task 4, 7, 8, 9）
- `static/walk.html` — navbar + shadcn 美學 + 適配新 API shape（Task 4, 7, 9）

**No new files.** 設計刻意保留單檔架構。

---

## Pre-flight

- [ ] **Step 0.1: Confirm working directory and current state**

```bash
cd /Users/chunn/projects/pikmin-walk
git status
git log --oneline -5
```

Expected: clean tree on `main`, recent commit `757fb95 feat(flower-cruise): add 快速採花 mode`.

- [ ] **Step 0.2: Backup shared.json before touching schema**

```bash
cp shared.json shared.json.bak.2026-05-10
```

This is in case the migration goes wrong; restore via `cp shared.json.bak.2026-05-10 shared.json`.

- [ ] **Step 0.3: Inspect current shared.json shape**

```bash
jq 'keys' shared.json
jq '.bookmarks | length' shared.json
jq '.bookmarks[0]' shared.json
```

Note the current bookmark structure (should be `{name, lat, lon}` with no `category`).

---

## Task 1: server.py — bookmark schema migration

**Goal:** 啟動時自動為現有 bookmark 補 `category: "未分類"` 欄位、為 `shared.json` 補 `bookmark_categories` 陣列。Idempotent（重跑不變）。

**Files:**
- Modify: `server.py:100-119` — 在 `_migrate_to_shared()` 旁邊新增 `_migrate_bookmark_categories()`，並在啟動時呼叫

- [ ] **Step 1.1: Add migration helper function**

In `server.py`, immediately after the `_migrate_to_shared()` function definition (before the `_migrate_to_shared()` call on line ~119), insert:

```python
DEFAULT_CATEGORY = "未分類"


def _migrate_bookmark_categories() -> None:
    """Idempotent: ensure every bookmark has a `category` and shared.json has `bookmark_categories`.

    - Bookmarks without `category` → set to DEFAULT_CATEGORY ("未分類").
    - Build categories list as: [DEFAULT_CATEGORY, ...sorted(other categories used by bookmarks)].
    - Existing categories in shared.json are preserved (union with bookmark-derived set).
    - DEFAULT_CATEGORY is always first in the list.
    """
    shared = _read_shared()
    bookmarks = shared.get("bookmarks", [])
    if not bookmarks and "bookmark_categories" not in shared:
        # Nothing to migrate; first launch or no bookmarks yet.
        if SHARED_FILE.exists():
            shared.setdefault("bookmark_categories", [DEFAULT_CATEGORY])
            _write_shared(shared)
        return

    changed = False
    used_categories: set[str] = set()
    for bk in bookmarks:
        if "category" not in bk or not bk.get("category"):
            bk["category"] = DEFAULT_CATEGORY
            changed = True
        used_categories.add(bk["category"])

    existing = shared.get("bookmark_categories", [])
    # Union: existing categories ∪ used_categories ∪ {DEFAULT_CATEGORY}
    union = set(existing) | used_categories | {DEFAULT_CATEGORY}
    others = sorted(union - {DEFAULT_CATEGORY})
    new_list = [DEFAULT_CATEGORY, *others]
    if new_list != existing:
        shared["bookmark_categories"] = new_list
        changed = True

    if changed:
        _write_shared(shared)
        print(f"✓ migrated {len(bookmarks)} bookmarks; categories: {new_list}")
```

- [ ] **Step 1.2: Call the migration on startup**

Find the line `_migrate_to_shared()` (around line 119) and add `_migrate_bookmark_categories()` immediately after it:

```python
_migrate_to_shared()
_migrate_bookmark_categories()
```

- [ ] **Step 1.3: Run migration once by importing the module**

```bash
cd /Users/chunn/projects/pikmin-walk
python3 -c "import server"
```

Expected: prints `✓ migrated N bookmarks; categories: ['未分類', ...]` exactly once. No traceback.

- [ ] **Step 1.4: Verify the JSON has the new fields**

```bash
jq '.bookmark_categories' shared.json
jq '.bookmarks | map(.category) | unique' shared.json
jq '.bookmarks[0]' shared.json
```

Expected:
- `.bookmark_categories` is an array starting with `"未分類"`
- All bookmarks have a `category` field
- First bookmark has `name`, `lat`, `lon`, `category`

- [ ] **Step 1.5: Verify idempotency**

```bash
python3 -c "import server"
jq '.bookmark_categories' shared.json
```

Expected: re-running prints **nothing** (no `✓ migrated` line because `changed` stays False), and `.bookmark_categories` is unchanged.

- [ ] **Step 1.6: Commit**

```bash
git add server.py
git commit -m "feat(server): migrate bookmarks to add category + bookmark_categories"
```

---

## Task 2: server.py — bookmarks API new shape + accept category

**Goal:** `GET /api/bookmarks` 回傳 `{bookmarks, categories}`；`POST /api/bookmarks` 接受 optional `category`；`PATCH /api/bookmarks/{idx}` 接受 optional `category`；POST/PATCH/DELETE 回傳的 response shape 也要對齊新 shape。

**Files:**
- Modify: `server.py:687-731` — 改寫 `bookmarks_get`、`bookmarks_post`、`bookmarks_patch`、`bookmarks_delete`

- [ ] **Step 2.1: Replace the four bookmark handlers**

Replace lines 687–731 (`bookmarks_get` through `bookmarks_delete`) with:

```python
def _bookmarks_response(shared: dict) -> dict:
    """Shape returned by every bookmark endpoint: {bookmarks, categories}."""
    return {
        "bookmarks": shared.get("bookmarks", []),
        "categories": shared.get("bookmark_categories", [DEFAULT_CATEGORY]),
    }


def _normalize_category(shared: dict, raw: str | None) -> str:
    """Return a category that exists in bookmark_categories. Unknown → DEFAULT_CATEGORY."""
    if not raw:
        return DEFAULT_CATEGORY
    name = str(raw).strip()
    if not name:
        return DEFAULT_CATEGORY
    cats = shared.get("bookmark_categories", [DEFAULT_CATEGORY])
    return name if name in cats else DEFAULT_CATEGORY


async def bookmarks_get(request):
    return JSONResponse(_bookmarks_response(_read_shared()))


async def bookmarks_post(request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "need name, lat, lon"}, status_code=400)
    if not name:
        name = f"{lat:.4f}, {lon:.4f}"
    shared = _read_shared()
    category = _normalize_category(shared, body.get("category"))
    bk = shared.setdefault("bookmarks", [])
    bk.append({"name": name, "lat": lat, "lon": lon, "category": category})
    _write_shared(shared)
    return JSONResponse(_bookmarks_response(shared))


async def bookmarks_patch(request):
    idx = int(request.path_params["idx"])
    body = await request.json()
    shared = _read_shared()
    bk = shared.get("bookmarks", [])
    if 0 <= idx < len(bk):
        if "name" in body:
            bk[idx]["name"] = str(body["name"]).strip()
        if "lat" in body:
            bk[idx]["lat"] = float(body["lat"])
        if "lon" in body:
            bk[idx]["lon"] = float(body["lon"])
        if "category" in body:
            bk[idx]["category"] = _normalize_category(shared, body["category"])
        _write_shared(shared)
    return JSONResponse(_bookmarks_response(shared))


async def bookmarks_delete(request):
    idx = int(request.path_params["idx"])
    shared = _read_shared()
    bk = shared.get("bookmarks", [])
    if 0 <= idx < len(bk):
        bk.pop(idx)
        _write_shared(shared)
    return JSONResponse(_bookmarks_response(shared))
```

- [ ] **Step 2.2: Start the server**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
curl -s http://localhost:5066/ -o /dev/null -w "%{http_code}\n"
```

Expected: `200`. (If `make dev` is not the right target, run `cat Makefile | grep -E '^[a-z-]+:' | head` to find it. Common alternatives: `make run`, `python server.py`.)

- [ ] **Step 2.3: GET returns new shape**

```bash
curl -s http://localhost:5066/api/bookmarks | jq 'keys'
curl -s http://localhost:5066/api/bookmarks | jq '.bookmarks[0]'
curl -s http://localhost:5066/api/bookmarks | jq '.categories'
```

Expected:
- `keys` → `["bookmarks", "categories"]`
- `.bookmarks[0]` includes `category`
- `.categories` starts with `"未分類"`

- [ ] **Step 2.4: POST a test bookmark with category**

```bash
curl -sX POST http://localhost:5066/api/bookmarks \
  -H 'Content-Type: application/json' \
  -d '{"name":"_TEST_PLAN_TASK2","lat":35.6,"lon":139.7,"category":"未分類"}' \
  | jq '.bookmarks[-1]'
```

Expected: returned object has `name=_TEST_PLAN_TASK2`, `category=未分類`.

- [ ] **Step 2.5: PATCH it (test invalid category falls back to default)**

```bash
LAST_IDX=$(curl -s http://localhost:5066/api/bookmarks | jq '.bookmarks | length - 1')
curl -sX PATCH http://localhost:5066/api/bookmarks/$LAST_IDX \
  -H 'Content-Type: application/json' \
  -d '{"category":"NotARealCategory"}' \
  | jq ".bookmarks[$LAST_IDX].category"
```

Expected: `"未分類"` (invalid category was normalized to default).

- [ ] **Step 2.6: DELETE the test bookmark**

```bash
LAST_IDX=$(curl -s http://localhost:5066/api/bookmarks | jq '.bookmarks | length - 1')
curl -sX DELETE http://localhost:5066/api/bookmarks/$LAST_IDX | jq '.bookmarks[-1].name'
```

Expected: any name **other than** `"_TEST_PLAN_TASK2"`.

- [ ] **Step 2.7: Stop the server**

```bash
pkill -f "python.*server.py" || true
```

- [ ] **Step 2.8: Commit**

```bash
git add server.py
git commit -m "feat(server): bookmark API returns {bookmarks, categories} + accepts category"
```

---

## Task 3: server.py — bookmark-categories CRUD API

**Goal:** 新增三個 endpoint：建立分類、重新命名分類（同步更新所有 bookmark 的 `category`）、刪除分類（原屬書籤 fallback 到「未分類」，「未分類」本身禁刪）。

**Files:**
- Modify: `server.py` — 新增三個 handler，註冊到 `Route` 清單（行 ~1540）

- [ ] **Step 3.1: Add three new handlers**

After the four bookmark handlers from Task 2 (after `bookmarks_delete`), insert:

```python
async def categories_post(request):
    """Create a new category."""
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    shared = _read_shared()
    cats = shared.setdefault("bookmark_categories", [DEFAULT_CATEGORY])
    if name not in cats:
        cats.append(name)
        _write_shared(shared)
    return JSONResponse(_bookmarks_response(shared))


async def categories_patch(request):
    """Rename a category. Updates every bookmark using the old name. DEFAULT_CATEGORY cannot be renamed."""
    old_name = request.path_params["name"]
    body = await request.json()
    new_name = str(body.get("name", "")).strip()
    if not new_name:
        return JSONResponse({"error": "name required"}, status_code=400)
    if old_name == DEFAULT_CATEGORY:
        return JSONResponse({"error": f"cannot rename {DEFAULT_CATEGORY}"}, status_code=400)
    shared = _read_shared()
    cats = shared.get("bookmark_categories", [])
    if old_name not in cats:
        return JSONResponse({"error": "category not found"}, status_code=404)
    if new_name in cats and new_name != old_name:
        return JSONResponse({"error": "name already exists"}, status_code=409)
    cats[cats.index(old_name)] = new_name
    for bk in shared.get("bookmarks", []):
        if bk.get("category") == old_name:
            bk["category"] = new_name
    _write_shared(shared)
    return JSONResponse(_bookmarks_response(shared))


async def categories_delete(request):
    """Delete a category. Bookmarks using it fall back to DEFAULT_CATEGORY. DEFAULT_CATEGORY cannot be deleted."""
    name = request.path_params["name"]
    if name == DEFAULT_CATEGORY:
        return JSONResponse({"error": f"cannot delete {DEFAULT_CATEGORY}"}, status_code=400)
    shared = _read_shared()
    cats = shared.get("bookmark_categories", [])
    if name not in cats:
        return JSONResponse({"error": "category not found"}, status_code=404)
    cats.remove(name)
    for bk in shared.get("bookmarks", []):
        if bk.get("category") == name:
            bk["category"] = DEFAULT_CATEGORY
    _write_shared(shared)
    return JSONResponse(_bookmarks_response(shared))
```

- [ ] **Step 3.2: Register the routes**

Find the `Route("/api/bookmarks/{idx:int}", bookmarks_delete, methods=["DELETE"]),` line (around line 1543). Immediately **after** it, add:

```python
        Route("/api/bookmark-categories", categories_post, methods=["POST"]),
        Route("/api/bookmark-categories/{name}", categories_patch, methods=["PATCH"]),
        Route("/api/bookmark-categories/{name}", categories_delete, methods=["DELETE"]),
```

- [ ] **Step 3.3: Restart the server**

```bash
pkill -f "python.*server.py" || true
sleep 1
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
curl -s http://localhost:5066/ -o /dev/null -w "%{http_code}\n"
```

Expected: `200`.

- [ ] **Step 3.4: POST creates a category**

```bash
curl -sX POST http://localhost:5066/api/bookmark-categories \
  -H 'Content-Type: application/json' \
  -d '{"name":"_TEST_PLAN_CAT"}' \
  | jq '.categories'
```

Expected: array contains `"_TEST_PLAN_CAT"`.

- [ ] **Step 3.5: PATCH renames the category**

```bash
curl -sX PATCH "http://localhost:5066/api/bookmark-categories/_TEST_PLAN_CAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"_TEST_PLAN_CAT2"}' \
  | jq '.categories'
```

Expected: `"_TEST_PLAN_CAT"` is gone, `"_TEST_PLAN_CAT2"` is present.

- [ ] **Step 3.6: PATCH on 「未分類」 returns 400**

```bash
curl -sX PATCH "http://localhost:5066/api/bookmark-categories/%E6%9C%AA%E5%88%86%E9%A1%9E" \
  -H 'Content-Type: application/json' \
  -d '{"name":"X"}' \
  -w "\n%{http_code}\n"
```

Expected: ends with `400`, JSON contains `cannot rename 未分類`.

- [ ] **Step 3.7: DELETE on 「未分類」 returns 400**

```bash
curl -sX DELETE "http://localhost:5066/api/bookmark-categories/%E6%9C%AA%E5%88%86%E9%A1%9E" \
  -w "\n%{http_code}\n"
```

Expected: ends with `400`, JSON contains `cannot delete 未分類`.

- [ ] **Step 3.8: DELETE removes the category and falls back bookmarks**

```bash
# Create a bookmark with category=_TEST_PLAN_CAT2
curl -sX POST http://localhost:5066/api/bookmarks \
  -H 'Content-Type: application/json' \
  -d '{"name":"_TEST_PLAN_BK","lat":1,"lon":2,"category":"_TEST_PLAN_CAT2"}' > /dev/null

# Delete the category
curl -sX DELETE "http://localhost:5066/api/bookmark-categories/_TEST_PLAN_CAT2" | jq '.categories'

# Verify the bookmark fell back to 未分類
curl -s http://localhost:5066/api/bookmarks \
  | jq '.bookmarks[] | select(.name == "_TEST_PLAN_BK") | .category'
```

Expected:
- `.categories` no longer contains `"_TEST_PLAN_CAT2"`
- The test bookmark's `.category` is now `"未分類"`

- [ ] **Step 3.9: Clean up the test bookmark**

```bash
TEST_IDX=$(curl -s http://localhost:5066/api/bookmarks \
  | jq '.bookmarks | map(.name == "_TEST_PLAN_BK") | index(true)')
curl -sX DELETE "http://localhost:5066/api/bookmarks/$TEST_IDX" > /dev/null
```

- [ ] **Step 3.10: Stop the server**

```bash
pkill -f "python.*server.py" || true
```

- [ ] **Step 3.11: Commit**

```bash
git add server.py
git commit -m "feat(server): add /api/bookmark-categories CRUD endpoints"
```

---

## Task 4: 三頁加 Tailwind CDN + sticky navbar

**Goal:** 三個 HTML 頁都引入 Tailwind CDN，注入 shadcn 色票 config，並在最上方加同樣的 navbar。`#app` grid 從 `1fr 420px` 變成 `grid-template-rows: 48px 1fr`，navbar 跨整個 row。**佈局重構（三欄）放到 Task 5。**

**Files:**
- Modify: `static/index.html:7-30` — `<head>` 加 Tailwind CDN + tailwind.config，`#app` 改 grid-template-rows
- Modify: `static/index.html:319-328` — `<body>` 開頭加 navbar
- Modify: `static/flower-cruise.html` — 同樣三件事
- Modify: `static/walk.html` — 同樣三件事

- [ ] **Step 4.1: Define the shared navbar HTML & shadcn theme**

These two snippets are reused in three files. The navbar uses `data-active` to mark the current page.

**Tailwind/shadcn theme snippet** (insert in `<head>` after the existing Leaflet stylesheet, before the existing `<style>`):

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
            'primary-foreground': 'hsl(222.2 47.4% 11.2%)',
            muted: 'hsl(217.2 32.6% 17.5%)',
            'muted-foreground': 'hsl(215 20.2% 65.1%)',
            accent: 'hsl(217.2 32.6% 17.5%)',
            'accent-foreground': 'hsl(210 40% 98%)',
            destructive: 'hsl(0 62.8% 30.6%)',
            'destructive-foreground': 'hsl(210 40% 98%)',
            card: 'hsl(222.2 84% 4.9%)',
            'card-foreground': 'hsl(210 40% 98%)',
          },
        },
      },
    };
  </script>
```

**Navbar HTML snippet** (insert as **first child** of `<body>`, before `<div id="app">`):

```html
<nav class="h-12 px-4 flex items-center justify-between border-b border-border bg-background/95 backdrop-blur sticky top-0 z-[1100]">
  <a href="/" class="flex items-center gap-2 text-foreground font-semibold tracking-tight">
    <span class="text-blue-500">●</span>
    <span>Pikmin Walker</span>
  </a>
  <div class="flex items-center gap-1" id="nav-links">
    <a href="/" data-page="home"
       class="px-3 py-1.5 rounded-md text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors">
      🌱 漫步種花
    </a>
    <a href="/flower-cruise" data-page="flower"
       class="px-3 py-1.5 rounded-md text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors">
      🌸 花朵巡航
    </a>
    <a href="/walk" data-page="walk"
       class="px-3 py-1.5 rounded-md text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors">
      🚶 道路漫步
    </a>
  </div>
</nav>
<script>
  // Highlight the current nav link based on current pathname
  (function() {
    const path = location.pathname;
    const map = { '/': 'home', '/flower-cruise': 'flower', '/walk': 'walk' };
    const active = map[path] || '';
    document.querySelectorAll('#nav-links a').forEach(a => {
      if (a.dataset.page === active) {
        a.classList.remove('text-muted-foreground');
        a.classList.add('bg-accent', 'text-foreground');
      }
    });
  })();
</script>
```

- [ ] **Step 4.2: Apply to `static/index.html`**

In `static/index.html`:

1. Find `<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">` (line 7). Immediately after that line, insert the **Tailwind/shadcn theme snippet** from Step 4.1.

2. Replace the existing `body` rule and `#app` rule in `<style>`:

   **Old:**
   ```css
   body {
     font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif;
     color: var(--text);
     background: var(--bg);
     overflow: hidden;
   }
   #app { display: grid; grid-template-columns: 1fr 420px; height: 100vh; }
   ```

   **New:**
   ```css
   body {
     font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif;
     color: var(--text);
     background: var(--bg);
     overflow: hidden;
     display: flex;
     flex-direction: column;
     height: 100vh;
   }
   #app {
     flex: 1 1 auto;
     min-height: 0;
     display: grid;
     grid-template-columns: 1fr 420px;
   }
   ```

   (Note: `#app` keeps `1fr 420px` for now — Task 5 changes it to three columns. Using flex on body lets navbar take its natural 48px and `#app` fill the rest.)

3. Find `<body>` (line ~318). Insert the **Navbar HTML snippet** from Step 4.1 immediately after the opening `<body>` tag, before `<div id="app">`.

4. Add the dark class to body so Tailwind's `dark:` variants work (also gives default text/bg colors). Change `<body>` to `<body class="dark">`.

- [ ] **Step 4.3: Apply to `static/flower-cruise.html`**

Same three modifications:

1. Insert the **Tailwind/shadcn theme snippet** in `<head>` after the Leaflet stylesheet link.
2. Add `class="dark"` to `<body>` and update `body { ... }` CSS to flex column with `height: 100vh` (the existing layout will need its top container adjusted — find the outermost grid/flex on `#app` or main wrapper).
3. Insert the **Navbar HTML snippet** as the first child of `<body>`.

```bash
# Confirm the file structure first:
grep -nE '<body|<div id="app"|grid-template' static/flower-cruise.html | head
```

Use the grep output to identify exactly where to make the changes. If `#app` already uses `display: grid`, change body to flex column (`display: flex; flex-direction: column; height: 100vh`) and let `#app` flex-grow with `flex: 1 1 auto; min-height: 0`.

- [ ] **Step 4.4: Apply to `static/walk.html`**

Same as 4.3 but for `static/walk.html`.

```bash
grep -nE '<body|<div id="app"|grid-template' static/walk.html | head
```

- [ ] **Step 4.5: Smoke test all three pages**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
for path in / /flower-cruise /walk; do
  curl -s "http://localhost:5066$path" | grep -q 'cdn.tailwindcss.com' && echo "$path: tailwind OK" || echo "$path: MISSING tailwind"
  curl -s "http://localhost:5066$path" | grep -q 'data-page="home"' && echo "$path: navbar OK" || echo "$path: MISSING navbar"
done
```

Expected: all six lines say `OK`.

- [ ] **Step 4.6: Visual smoke via Playwright MCP**

Use the Playwright MCP (`mcp__plugin_playwright_playwright__browser_navigate`) to visit each of:
- `http://localhost:5066/` → take screenshot, verify navbar shows `🌱 漫步種花` highlighted
- `http://localhost:5066/flower-cruise` → screenshot, verify `🌸 花朵巡航` highlighted
- `http://localhost:5066/walk` → screenshot, verify `🚶 道路漫步` highlighted

Visually confirm:
- Navbar is sticky at top with dark background + bottom border
- Map / existing layout shifts down by 48px and is fully visible
- No console errors except expected Tailwind production warning

- [ ] **Step 4.7: Stop server, commit**

```bash
pkill -f "python.*server.py" || true
git add static/index.html static/flower-cruise.html static/walk.html
git commit -m "feat(ui): add sticky navbar + Tailwind CDN with shadcn theme to all 3 pages"
```

---

## Task 5: index.html — 三欄佈局重構（書籤拉到右邊獨立欄）

**Goal:** 把 `#app` 改成 `1fr 360px 300px` 三欄。中間欄是現有工具列（不含書籤）；右欄是新的書籤面板（撐滿垂直高度）。書籤的 list (`<ul id="bookmarks">`) 從「瞬移到」section 移出來放到右欄。**分類 UI 在 Task 6，這個 task 只動結構。**

**Files:**
- Modify: `static/index.html` — `#app` grid、`#side` 結構、新增 `<aside id="bookmarks-pane">`

- [ ] **Step 5.1: Update `#app` grid columns**

Find the `#app` rule (was modified in Task 4.2). Change `grid-template-columns: 1fr 420px;` to:

```css
#app {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr 360px 300px;
}
```

- [ ] **Step 5.2: Add styles for the new bookmarks pane**

Add these rules to `<style>` (anywhere — easiest is right after the existing `#bookmarks` rule, line ~257):

```css
#bookmarks-pane {
  background: var(--panel);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}
#bookmarks-pane > .pane-header {
  flex: 0 0 auto;
  padding: 16px 16px 12px 16px;
  border-bottom: 1px solid var(--border);
}
#bookmarks-pane > .pane-header h2 {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 10px 0;
  letter-spacing: 0.08em;
}
#bookmarks-pane > #bookmarks {
  flex: 1 1 auto;
  min-height: 0;          /* allow flex shrinking */
  max-height: none;       /* override the 160px cap */
  overflow-y: auto;
  padding: 8px;
  margin: 0;
  background: transparent;
}
```

Then **delete** the existing `max-height: 160px;` line inside the `#bookmarks` rule (line ~265) — it's overridden but cleaner to remove. Keep the rest of the `#bookmarks` rule.

- [ ] **Step 5.3: Move `<ul id="bookmarks">` out of the side toolbar**

In the HTML body:

1. **Remove** the line `<ul id="bookmarks"></ul>` from inside the "🎯 瞬移到" section (line ~346).

2. **Remove** the hint paragraph just above it: `<div class="hint">先在上方輸入地名/座標按 GO 瞬移，再取個名字按 ★ 存書籤</div>` will move to the new pane.

3. After the closing `</aside>` of `#side` (around line ~398), but **inside** the `<div id="app">`, add a new aside:

```html
  <aside id="bookmarks-pane">
    <div class="pane-header">
      <h2>★ 書籤</h2>
      <div class="hint" style="margin-bottom: 10px;">先在中間欄輸入地名/座標按 GO，再取個名字按 ★ 存</div>
      <!-- Category filter UI placeholder; populated in Task 6 -->
      <div id="bookmarks-controls"></div>
    </div>
    <ul id="bookmarks"></ul>
  </aside>
```

- [ ] **Step 5.4: Smoke test the layout**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
```

Use Playwright MCP to navigate to `http://localhost:5066/` and take a screenshot. Visually verify:
- Three columns: map (left, widest), middle controls (~360px), bookmarks (~300px right)
- Bookmarks list is tall — fills the right column except for the header area at top
- No horizontal scroll
- Existing functionality (jump input, sliders, start button) works in the middle column
- Existing bookmarks render in the right column

- [ ] **Step 5.5: Stop server, commit**

```bash
pkill -f "python.*server.py" || true
git add static/index.html
git commit -m "feat(ui): split bookmarks into its own right column with full height"
```

---

## Task 6: index.html — 書籤分類 UI + 邏輯

**Goal:** 把 spec §5 的所有分類 UI 加進來，包含 filter dropdown、新增/管理分類面板、加書籤時選分類、書籤列上的分類切換。`renderBookmarks` 改寫成接受 `{bookmarks, categories}`，並加 `data-original-idx` 處理 filter 後的 idx 映射。

**Files:**
- Modify: `static/index.html` — `<div id="bookmarks-controls">` HTML + JS

- [ ] **Step 6.1: HTML for filter + manage panel**

Replace the placeholder `<div id="bookmarks-controls"></div>` from Task 5 with:

```html
      <div id="bookmarks-controls" style="display:flex;flex-direction:column;gap:8px;">
        <div style="display:flex;gap:6px;align-items:center;">
          <select id="categoryFilter" style="flex:1;">
            <option value="__all__">全部</option>
          </select>
          <button id="categoryAdd" title="新增分類" style="width:auto;padding:8px 10px;flex:0 0 auto;">+</button>
          <button id="categoryManageToggle" title="管理分類" style="width:auto;padding:8px 10px;flex:0 0 auto;">✏️</button>
        </div>
        <div id="categoryManagePanel" style="display:none;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:8px;font-size:12px;max-height:200px;overflow-y:auto;"></div>
      </div>
```

- [ ] **Step 6.2: HTML for "add bookmark" category dropdown**

In the existing "🎯 瞬移到" section, find the `<div class="jump-row" style="margin-top: 4px;">` block that contains `bookmarkName` input + `bookmarkAdd` button. Add a category dropdown to the right of `bookmarkAdd`:

**Old:**
```html
      <div class="jump-row" style="margin-top: 4px;">
        <input type="text" id="bookmarkName" placeholder="書籤名稱（自訂）" />
        <button id="bookmarkAdd" title="加入書籤">★ 存</button>
      </div>
```

**New:**
```html
      <div class="jump-row" style="margin-top: 4px;">
        <input type="text" id="bookmarkName" placeholder="書籤名稱（自訂）" />
        <select id="bookmarkAddCategory" style="flex:0 0 auto;width:auto;min-width:80px;"></select>
        <button id="bookmarkAdd" title="加入書籤" style="flex:0 0 auto;width:auto;padding:10px 14px;">★ 存</button>
      </div>
```

- [ ] **Step 6.3: Replace the JavaScript bookmark logic**

The existing `renderBookmarks`, `loadBookmarks`, `addBookmark`, `renameBookmark`, `deleteBookmark` block (lines ~696–818) needs replacement. The new version maintains a single `bookmarkState = {bookmarks, categories}` and re-renders both the list and the dropdowns whenever it changes.

**Find** this block:

```javascript
function renderBookmarks(list) {
```

…through the line `loadBookmarks();` (right before the `Profile list from backend` section).

**Replace the entire block with:**

```javascript
//
// ---- Bookmarks state + UI ----
//
const DEFAULT_CATEGORY = '未分類';
const ALL_FILTER = '__all__';
let bookmarkState = { bookmarks: [], categories: [DEFAULT_CATEGORY] };

function applyBookmarkState(state) {
  bookmarkState = state;
  refreshCategoryFilter();
  refreshAddCategoryDropdown();
  refreshManagePanel();
  renderBookmarks();
}

function refreshCategoryFilter() {
  const sel = document.getElementById('categoryFilter');
  const prev = sel.value || storage.get('pikmin.bookmarkCategoryFilter', ALL_FILTER);
  sel.replaceChildren();
  const all = document.createElement('option');
  all.value = ALL_FILTER; all.textContent = `全部 (${bookmarkState.bookmarks.length})`;
  sel.append(all);
  bookmarkState.categories.forEach(cat => {
    const count = bookmarkState.bookmarks.filter(b => b.category === cat).length;
    const opt = document.createElement('option');
    opt.value = cat; opt.textContent = `${cat} (${count})`;
    sel.append(opt);
  });
  // Restore selection if still valid
  if (prev === ALL_FILTER || bookmarkState.categories.includes(prev)) {
    sel.value = prev;
  } else {
    sel.value = ALL_FILTER;
  }
}

function refreshAddCategoryDropdown() {
  const sel = document.getElementById('bookmarkAddCategory');
  const prev = sel.value || storage.get('pikmin.lastSelectedCategory', DEFAULT_CATEGORY);
  sel.replaceChildren();
  bookmarkState.categories.forEach(cat => {
    const opt = document.createElement('option');
    opt.value = cat; opt.textContent = cat;
    sel.append(opt);
  });
  sel.value = bookmarkState.categories.includes(prev) ? prev : DEFAULT_CATEGORY;
}

function refreshManagePanel() {
  const panel = document.getElementById('categoryManagePanel');
  panel.replaceChildren();
  bookmarkState.categories.forEach(cat => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px;align-items:center;padding:4px 6px;';
    const label = document.createElement('span');
    label.textContent = cat;
    label.style.cssText = 'flex:1;color:var(--text);';
    const renameBtn = document.createElement('button');
    renameBtn.textContent = '✏️';
    renameBtn.style.cssText = 'width:auto;padding:4px 8px;font-size:12px;';
    const deleteBtn = document.createElement('button');
    deleteBtn.textContent = '🗑️';
    deleteBtn.style.cssText = 'width:auto;padding:4px 8px;font-size:12px;';
    if (cat === DEFAULT_CATEGORY) {
      renameBtn.disabled = true;
      deleteBtn.disabled = true;
      renameBtn.title = '系統分類，無法修改';
      deleteBtn.title = '系統分類，無法修改';
    } else {
      renameBtn.addEventListener('click', () => renameCategory(cat));
      deleteBtn.addEventListener('click', () => deleteCategory(cat));
    }
    row.append(label, renameBtn, deleteBtn);
    panel.append(row);
  });
}

function renderBookmarks() {
  const ul = document.getElementById('bookmarks');
  ul.replaceChildren();
  const filter = document.getElementById('categoryFilter').value || ALL_FILTER;

  bookmarkState.bookmarks.forEach((bk, i) => {
    if (filter !== ALL_FILTER && bk.category !== filter) return;

    const li = document.createElement('li');
    li.dataset.originalIdx = String(i);
    li.title = `${bk.lat.toFixed(5)}, ${bk.lon.toFixed(5)}`;

    const nameEl = document.createElement('input');
    nameEl.type = 'text';
    nameEl.className = 'bk-name';
    nameEl.value = bk.name;
    nameEl.addEventListener('focus', e => e.stopPropagation());
    nameEl.addEventListener('click', e => e.stopPropagation());
    nameEl.addEventListener('blur', () => {
      const newName = nameEl.value.trim();
      if (newName && newName !== bk.name) renameBookmark(i, newName);
    });
    nameEl.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); nameEl.blur(); }
    });

    const catSel = document.createElement('select');
    catSel.style.cssText = 'flex:0 0 auto;width:auto;padding:2px 4px;font-size:11px;background:var(--panel);border:1px solid var(--border);border-radius:4px;color:var(--muted);';
    bookmarkState.categories.forEach(cat => {
      const opt = document.createElement('option');
      opt.value = cat; opt.textContent = cat;
      catSel.append(opt);
    });
    catSel.value = bk.category || DEFAULT_CATEGORY;
    catSel.addEventListener('click', e => e.stopPropagation());
    catSel.addEventListener('change', () => recategorizeBookmark(i, catSel.value));

    const rm = document.createElement('button');
    rm.textContent = '✕';
    rm.title = '刪除書籤';
    rm.addEventListener('click', e => { e.stopPropagation(); deleteBookmark(i); });

    li.append(nameEl, catSel, rm);
    li.addEventListener('click', () => {
      document.getElementById('jumpInput').value = bk.name;
      teleportTo(bk.lat, bk.lon);
      map.setView([bk.lat, bk.lon], 15);
      setStatus(`瞬移到 ${bk.name}`, 'done');
    });
    ul.append(li);
  });

  if (!ul.children.length) {
    const empty = document.createElement('li');
    empty.style.cssText = 'color:var(--muted);text-align:center;padding:16px;background:transparent;border:1px dashed var(--border);font-family:inherit;';
    empty.textContent = filter === ALL_FILTER ? '尚無書籤' : `「${filter}」分類目前沒有書籤`;
    ul.append(empty);
  }
}

function loadBookmarks() {
  fetch('/api/bookmarks')
    .then(r => r.json())
    .then(applyBookmarkState)
    .catch(() => {});
}

async function addBookmark() {
  const jumpInput = document.getElementById('jumpInput').value.trim();
  const nameInput = document.getElementById('bookmarkName').value.trim();
  const category = document.getElementById('bookmarkAddCategory').value || DEFAULT_CATEGORY;
  if (!jumpInput) { setStatus('先在「地名或 lat, lon」輸入位置再按 ★', 'error'); return; }

  let lat, lon;
  const m = jumpInput.match(/^(-?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)$/);
  if (m) {
    lat = parseFloat(m[1]); lon = parseFloat(m[2]);
  } else {
    try {
      const url = 'https://nominatim.openstreetmap.org/search'
        + `?format=json&limit=1&q=${encodeURIComponent(jumpInput)}`;
      const res = await fetch(url);
      const hits = await res.json();
      if (!hits.length) { setStatus(`找不到「${jumpInput}」`, 'error'); return; }
      lat = parseFloat(hits[0].lat); lon = parseFloat(hits[0].lon);
    } catch (err) {
      setStatus(`geocode 失敗: ${err.message}`, 'error');
      return;
    }
  }

  const name = nameInput || jumpInput;
  try {
    const res = await fetch('/api/bookmarks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, lat, lon, category }),
    });
    const state = await res.json();
    applyBookmarkState(state);
    document.getElementById('bookmarkName').value = '';
    setStatus(`★ 已加入書籤「${name}」(${category})`, 'done');
  } catch (err) {
    setStatus(`書籤儲存失敗: ${err.message}`, 'error');
  }
}

async function renameBookmark(idx, newName) {
  try {
    const res = await fetch(`/api/bookmarks/${idx}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    });
    applyBookmarkState(await res.json());
    setStatus(`書籤已改名為「${newName}」`, 'done');
  } catch (err) {
    setStatus(`改名失敗: ${err.message}`, 'error');
  }
}

async function recategorizeBookmark(idx, category) {
  try {
    const res = await fetch(`/api/bookmarks/${idx}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category }),
    });
    applyBookmarkState(await res.json());
  } catch (err) {
    setStatus(`分類更新失敗: ${err.message}`, 'error');
  }
}

async function deleteBookmark(idx) {
  try {
    const res = await fetch(`/api/bookmarks/${idx}`, { method: 'DELETE' });
    applyBookmarkState(await res.json());
  } catch (err) {
    setStatus(`刪除失敗: ${err.message}`, 'error');
  }
}

async function addCategory() {
  const name = (prompt('新分類名稱：') || '').trim();
  if (!name) return;
  if (bookmarkState.categories.includes(name)) {
    setStatus(`分類「${name}」已存在`, 'error'); return;
  }
  try {
    const res = await fetch('/api/bookmark-categories', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    applyBookmarkState(await res.json());
    setStatus(`已新增分類「${name}」`, 'done');
  } catch (err) {
    setStatus(`新增失敗: ${err.message}`, 'error');
  }
}

async function renameCategory(oldName) {
  const newName = (prompt(`重新命名「${oldName}」為：`, oldName) || '').trim();
  if (!newName || newName === oldName) return;
  try {
    const res = await fetch(`/api/bookmark-categories/${encodeURIComponent(oldName)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    });
    if (!res.ok) throw new Error((await res.json()).error || `HTTP ${res.status}`);
    applyBookmarkState(await res.json());
    setStatus(`分類「${oldName}」已改名為「${newName}」`, 'done');
  } catch (err) {
    setStatus(`改名失敗: ${err.message}`, 'error');
  }
}

async function deleteCategory(name) {
  if (!confirm(`確定要刪除分類「${name}」？\n屬於這個分類的書籤會自動歸到「${DEFAULT_CATEGORY}」。`)) return;
  try {
    const res = await fetch(`/api/bookmark-categories/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    });
    if (!res.ok) throw new Error((await res.json()).error || `HTTP ${res.status}`);
    applyBookmarkState(await res.json());
    setStatus(`已刪除分類「${name}」`, 'done');
  } catch (err) {
    setStatus(`刪除失敗: ${err.message}`, 'error');
  }
}

document.getElementById('bookmarkAdd').addEventListener('click', addBookmark);
document.getElementById('categoryAdd').addEventListener('click', addCategory);
document.getElementById('categoryManageToggle').addEventListener('click', () => {
  const panel = document.getElementById('categoryManagePanel');
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
});
document.getElementById('categoryFilter').addEventListener('change', () => {
  storage.set('pikmin.bookmarkCategoryFilter', document.getElementById('categoryFilter').value);
  renderBookmarks();
});
document.getElementById('bookmarkAddCategory').addEventListener('change', () => {
  storage.set('pikmin.lastSelectedCategory', document.getElementById('bookmarkAddCategory').value);
});

loadBookmarks();
```

> Note: `storage.get` / `storage.set` are introduced in Task 8. For now, **insert a placeholder** so this Task 6 code doesn't crash — paste this **above** the bookmark code:
>
> ```javascript
> // Temporary stub; replaced by real localStorage helper in Task 8
> const storage = { get: (_k, fb = '') => fb, set: () => {} };
> ```

- [ ] **Step 6.4: Smoke test category UI**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
```

Use Playwright MCP at `http://localhost:5066/`:
1. Verify category filter dropdown shows existing categories with counts
2. Click `+`, enter `_TEST_CAT_TASK6`, confirm — dropdown updates
3. Click `✏️` — manage panel toggles open, `_TEST_CAT_TASK6` has working buttons, `未分類` has disabled buttons
4. Add a new bookmark with category `_TEST_CAT_TASK6`, confirm it appears in the list
5. Change filter dropdown to `_TEST_CAT_TASK6`, only that bookmark shows
6. Switch back to `全部`, all show
7. On any bookmark row, change its category dropdown to `未分類` — verify it sticks after refresh (check via `curl /api/bookmarks`)
8. Delete the test category via 🗑️, confirm — bookmark falls back to `未分類`
9. Delete the test bookmark via ✕

- [ ] **Step 6.5: Stop server, commit**

```bash
pkill -f "python.*server.py" || true
git add static/index.html
git commit -m "feat(ui): add bookmark category filter, add/manage panel, per-row recategorize"
```

---

## Task 7: 適配新 API shape — flower-cruise & walk

**Goal:** `GET /api/bookmarks` 變了 shape；如果其他兩頁有用到 `/api/bookmarks`，要同步改。

**Files:**
- Modify (if needed): `static/flower-cruise.html`
- Modify (if needed): `static/walk.html`

- [ ] **Step 7.1: Check if other pages fetch bookmarks**

```bash
grep -n "api/bookmarks" static/flower-cruise.html static/walk.html
```

If grep shows **no matches** in either file → skip to Step 7.4 (no changes needed) and write a no-op commit message saying so.

If grep shows usage, continue to 7.2.

- [ ] **Step 7.2: Update flower-cruise.html (if applicable)**

For each `fetch('/api/bookmarks')` call in `flower-cruise.html`, change the consumer from treating the response as an array to extracting `.bookmarks`:

**Old pattern:**
```javascript
fetch('/api/bookmarks').then(r => r.json()).then(list => {
  list.forEach(bk => { /* ... */ });
});
```

**New pattern:**
```javascript
fetch('/api/bookmarks').then(r => r.json()).then(state => {
  state.bookmarks.forEach(bk => { /* ... */ });
});
```

Same applies to any `POST` / `PATCH` / `DELETE` `/api/bookmarks*` handlers — the response is now `{bookmarks, categories}`, not an array.

- [ ] **Step 7.3: Update walk.html (if applicable)**

Same as 7.2 for `static/walk.html`.

- [ ] **Step 7.4: Smoke test all three pages still load and bookmarks work**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
```

Visit each of the three pages via Playwright MCP. Open DevTools console; verify no `TypeError` or `undefined` errors related to bookmarks. If a page uses bookmarks visually, confirm they render.

- [ ] **Step 7.5: Stop server, commit**

```bash
pkill -f "python.*server.py" || true
git add -u static/
git commit -m "refactor(ui): adapt flower-cruise/walk to new {bookmarks, categories} API shape" --allow-empty
```

(Use `--allow-empty` if there were no actual changes; the commit then documents that we checked.)

---

## Task 8: localStorage 持久化

**Goal:** 替換 Task 6 中的 storage stub 為實作；首頁的 `#jumpInput` 與花朵巡航的 `#flowersText` 在 input 後 debounce 寫入 localStorage，page load 時讀回。filter 與加書籤時的分類 dropdown 也記住上次選擇。

**Files:**
- Modify: `static/index.html` — 真實 storage helper、jumpInput 持久化
- Modify: `static/flower-cruise.html` — flowersText 持久化

- [ ] **Step 8.1: Replace the storage stub in index.html**

Find the stub:
```javascript
// Temporary stub; replaced by real localStorage helper in Task 8
const storage = { get: (_k, fb = '') => fb, set: () => {} };
```

Replace with:
```javascript
const storage = {
  get(key, fallback = '') {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch {}
  },
  debounce(fn, ms = 200) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  },
};
```

- [ ] **Step 8.2: Persist `#jumpInput` in index.html**

Find the `handleJumpInput` definition (early in `<script>`). Right after **the line that wires the keydown handler**:

```javascript
document.getElementById('jumpInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); handleJumpInput(); }
});
```

…add immediately after:

```javascript
// Persist jumpInput across reloads
const $jumpInput = document.getElementById('jumpInput');
const saveJumpInput = storage.debounce(() => storage.set('pikmin.jumpInput', $jumpInput.value), 200);
$jumpInput.addEventListener('input', saveJumpInput);
$jumpInput.value = storage.get('pikmin.jumpInput', '');
```

- [ ] **Step 8.3: Persist `#flowersText` in flower-cruise.html**

In `static/flower-cruise.html`, find the `<textarea id="flowersText"...>` element (line 221). After the existing `<script>` block initializes (typically at the bottom or in a DOMContentLoaded handler), add:

```javascript
// Persist flowersText across reloads
(function() {
  const ta = document.getElementById('flowersText');
  if (!ta) return;
  const KEY = 'pikmin.flowerCruise.flowersText';
  let t;
  const save = () => { clearTimeout(t); t = setTimeout(() => {
    try { localStorage.setItem(KEY, ta.value); } catch {}
  }, 300); };
  ta.addEventListener('input', save);
  try {
    const v = localStorage.getItem(KEY);
    if (v && !ta.value) ta.value = v;
  } catch {}
})();
```

> Use a self-contained IIFE so we don't depend on the storage helper not existing in this file.

Place it just before the closing `</script>` of the **main inline script** (the one that owns the page logic). If unsure, add it as a brand-new `<script>` block right before `</body>`.

- [ ] **Step 8.4: Smoke test**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
```

Use Playwright MCP:
1. Visit `http://localhost:5066/`. In `#jumpInput` type `Tokyo Tower`. Reload. Verify `Tokyo Tower` is still there.
2. Visit `http://localhost:5066/flower-cruise`. In `#flowersText` paste a couple of lines like `35.0,139.0\n35.1,139.1`. Reload. Verify text is still there.
3. On the home page, change category filter to a non-default. Reload. Verify the filter dropdown selection persists.
4. On the home page, change the "add bookmark" category dropdown. Reload. Verify it persists.

- [ ] **Step 8.5: Stop server, commit**

```bash
pkill -f "python.*server.py" || true
git add static/index.html static/flower-cruise.html
git commit -m "feat(ui): persist jumpInput / flowersText / filters in localStorage"
```

---

## Task 9: shadcn 視覺 sweep（用 frontend-design skill）

**Goal:** 把書籤欄、jump 區、控制鈕重做出有設計感、不像 generic AI dark UI 的版本。範圍只在 `static/index.html` 的 `#side` 與 `#bookmarks-pane` 內部視覺；功能都已經在前面 task 完成。

**Files:**
- Modify: `static/index.html`

- [ ] **Step 9.1: Invoke frontend-design skill**

Use the `Skill` tool with `frontend-design`. Brief the skill: redesign the right sidebar (`#side` middle column controls + `#bookmarks-pane` right column) of `static/index.html` to match shadcn aesthetics — sleek dark theme, subtle borders, well-tuned spacing, focus ring, hover states. Tailwind utility classes are available via CDN. Do **not** restructure HTML semantics (id attributes must remain intact for the existing JS to work).

Constraints to pass:
- Keep all element ids: `#jumpInput`, `#jumpButton`, `#bookmarkName`, `#bookmarkAdd`, `#bookmarkAddCategory`, `#categoryFilter`, `#categoryAdd`, `#categoryManageToggle`, `#categoryManagePanel`, `#bookmarks`, `#radiusSlider`, `#radiusVal`, `#speedSlider`, `#speedVal`, `#start`, `#stop`, `#clearLoc`, `#status`, `#device-info`
- Preserve grid layout `1fr 360px 300px` and 48px navbar
- Keep all behavior wiring (event listeners reference the same elements)

Deliverable: visual upgrade only.

- [ ] **Step 9.2: Manual visual review**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
```

Use Playwright MCP screenshot the home page. Walk through each interaction (focus jump input, hover buttons, open manage panel, click bookmark) and visually verify no styling regressions. Compare before/after if needed.

- [ ] **Step 9.3: Apply same visual treatment to flower-cruise.html and walk.html**

Manually apply the same color/border/radius/typography tokens to the existing controls in flower-cruise.html and walk.html. Do **not** restructure HTML — just sweep visual classes/styles. Goal is consistency, not full redesign.

- [ ] **Step 9.4: Stop server, commit**

```bash
pkill -f "python.*server.py" || true
git add static/
git commit -m "feat(ui): shadcn visual sweep across index/flower-cruise/walk"
```

---

## Task 10: 驗收 — Spec §10 acceptance + .claude/dev/e2e.md

**Goal:** 跑 spec §10 的驗收 checklist，紀錄在 `.claude/dev/e2e.md`。

**Files:**
- Create: `.claude/dev/e2e.md` (new — repo doesn't have one yet)

- [ ] **Step 10.1: Start server**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
```

- [ ] **Step 10.2: Run all 12 acceptance items from spec §10 via Playwright MCP**

For each item, navigate / interact / screenshot. Items:

1. 三頁都有 navbar，active 頁面正確高亮
2. 首頁三欄佈局正確顯示，書籤欄獨立並撐滿可用高度
3. 書籤可建立分類（按 `+`，input prompt，新增到清單）
4. 書籤可重新命名分類（管理面板按 ✏️）
5. 書籤可刪除分類（管理面板按 🗑️，書籤 fallback 到「未分類」）
6. 書籤可在分類間移動（每行的 dropdown）
7. 分類 filter 正確過濾
8. 「未分類」無法被刪除或重新命名（管理面板上的兩個按鈕都 disabled）
9. 重整首頁，jumpInput 仍保留上次值
10. 重整花朵巡航，flowersText 仍保留上次值
11. 重整後 filter 維持上次選擇
12. 既有 `shared.json`（沒有 `category` 欄位）能正常 load 且自動補「未分類」(已在 Task 1 驗過 — 重述紀錄即可)
13. 視覺風格貼近 shadcn（小圓角、subtle border、focus ring）— 主觀，留 screenshot

- [ ] **Step 10.3: Write `.claude/dev/e2e.md`**

```bash
mkdir -p .claude/dev
```

Create `.claude/dev/e2e.md` with content following the format in `~/projects/CLAUDE.md`:

```markdown
# E2E Test Stories and Results

## UI Redesign (2026-05-10)

### Test Scenarios

1. **Story:** Navbar active state highlights correct page across all 3 pages
   - **Steps:** Visit `/`, `/flower-cruise`, `/walk` in turn
   - **Expected Result:** Each page's nav link has accent background; others muted
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

2. **Story:** Home page three-column layout with full-height bookmarks pane
   - **Steps:** Visit `/`, observe layout, scroll bookmarks list
   - **Expected Result:** map(left) | controls(center, ~360px) | bookmarks(right, ~300px); bookmarks list scrolls within its column without expanding the pane
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

3. **Story:** Create / rename / delete bookmark category
   - **Steps:** Click `+` → enter "TestCat" → click ✏️ → rename to "TestCat2" → click 🗑️ on it → confirm
   - **Expected Result:** Each action updates the category list; bookmarks using the deleted category fall back to 未分類
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

4. **Story:** Recategorize bookmark via per-row dropdown
   - **Steps:** Change a bookmark's category dropdown to a different category, refresh
   - **Expected Result:** Category persists across refresh
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

5. **Story:** Category filter narrows visible bookmarks
   - **Steps:** Change filter dropdown to a specific category
   - **Expected Result:** Only matching bookmarks visible; "全部" shows all
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

6. **Story:** 未分類 cannot be renamed or deleted
   - **Steps:** Open manage panel; observe ✏️ and 🗑️ buttons on 未分類 row
   - **Expected Result:** Both buttons disabled with tooltip "系統分類，無法修改"
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

7. **Story:** Persistent jumpInput across page reloads
   - **Steps:** Type in #jumpInput on /, reload, observe input value
   - **Expected Result:** Value persists
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

8. **Story:** Persistent flowersText on flower-cruise across reloads
   - **Steps:** Paste lines into #flowersText on /flower-cruise, reload
   - **Expected Result:** Text persists
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

9. **Story:** Persistent filter selection
   - **Steps:** Change filter dropdown, reload
   - **Expected Result:** Same filter is selected after reload
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

10. **Story:** Schema migration is idempotent
    - **Steps:** `python3 -c "import server"` twice; check shared.json
    - **Expected Result:** First run prints migration message; second run silent; bookmark_categories unchanged
    - **Status:** ✅ Passing
    - **Last Updated:** 2026-05-10
```

(Update Status to ❌/⏳ for any item that doesn't pass; fix in Task 11 if so.)

- [ ] **Step 10.4: Stop server, commit**

```bash
pkill -f "python.*server.py" || true
git add .claude/dev/e2e.md
git commit -m "docs(ui): record UI redesign E2E acceptance results"
```

---

## Task 11: Fix-up (only if Task 10 found regressions)

If any item in Task 10 was ❌ or ⏳, list the failures here, fix each one with a focused commit, and re-run the failing scenario. If everything passed, skip this task.

- [ ] **Step 11.1:** For each failure, identify root cause (use `superpowers:systematic-debugging` if non-trivial).
- [ ] **Step 11.2:** Fix, re-test, commit per fix.
- [ ] **Step 11.3:** Update `.claude/dev/e2e.md` with the corrected status.

---

## Wrap-up

- [ ] **Step W.1: Final smoke test all three pages**

```bash
make dev > /tmp/pikmin-server.log 2>&1 &
sleep 3
```

Browse all three pages once; sanity check no console errors.

```bash
pkill -f "python.*server.py" || true
```

- [ ] **Step W.2: Review the diff before pushing**

```bash
git log --oneline main..HEAD
git diff main..HEAD --stat
```

- [ ] **Step W.3: Decide merge strategy**

This was developed on `main` per the user's git conventions: simple changes commit + push direct, feature changes via PR. Given this is a multi-file feature with breaking API change, **prefer PR** even though development was on main. Use the `commit-commands:commit-push-pr` skill or follow the user's standard PR flow.

- [ ] **Step W.4: Update spec status**

Edit `docs/superpowers/specs/2026-05-10-ui-redesign-design.md` frontmatter — change `status: approved` to `status: implemented`. Commit:

```bash
git add docs/superpowers/specs/2026-05-10-ui-redesign-design.md
git commit -m "docs(ui-redesign): mark spec as implemented"
```

- [ ] **Step W.5: Clean up backup file**

```bash
ls shared.json.bak.2026-05-10 && rm shared.json.bak.2026-05-10
```

(Only after confirming everything works in production.)
