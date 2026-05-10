# E2E Test Stories and Results

> Pikmin Walker is a vanilla HTML/JS tool with no Playwright test runner configured.
> Acceptance is Playwright-MCP smoke verification during development; results are recorded here.

## UI Redesign (2026-05-10)

Spec: `docs/superpowers/specs/2026-05-10-ui-redesign-design.md`
Plan: `docs/superpowers/plans/2026-05-10-ui-redesign.md`
Branch: `ui-redesign-2026-05-10`

### Test Scenarios

1. **Story:** Navbar present + active highlighted across all 3 pages
   - **Steps:** Visit `/`, `/flower-cruise`, `/walk` in turn
   - **Expected:** Each page's nav link has `bg-accent text-foreground` while others are `text-muted-foreground`. Navbar at the top, 48px tall, sticky.
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

2. **Story:** Home page three-column layout
   - **Steps:** Visit `/`, observe layout
   - **Expected:** Map (1fr) | controls (~360px) | bookmarks pane (~300px). Bookmarks list scrolls within its column without expanding the pane. No horizontal overflow.
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

3. **Story:** Create / rename / delete bookmark category via shadcn-style Modal
   - **Steps:**
     1. Click `+` next to filter dropdown — Modal opens with title "新增書籤分類", placeholder "例：日本、本土、收藏"
     2. Type `TestCat` → 確定 → category appears in filter and add-bookmark dropdown
     3. Click `✏️` next to TestCat in manage panel — Modal opens with title "重新命名分類", default value `TestCat`
     4. Change to `TestCat2` → 確定 → category renamed, bookmarks using old name updated
     5. Click `🗑️` next to TestCat2 — confirm Modal opens with red 刪除 button
     6. Confirm → category removed; bookmarks using TestCat2 fall back to "未分類"
   - **Expected:** Each step works without browser-native prompt/confirm. Modal supports Esc to cancel, Enter to confirm.
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

4. **Story:** Recategorize bookmark via per-row dropdown
   - **Steps:** On any bookmark row, change its category `<select>`; reload the page.
   - **Expected:** PATCH `/api/bookmarks/{idx}` is sent with `{category}`; new category persists across reload.
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

5. **Story:** Category filter narrows visible bookmarks
   - **Steps:** Change filter dropdown to a specific category; switch back to `全部`.
   - **Expected:** Only matching bookmarks visible when filtered; all visible when 全部.
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

6. **Story:** "未分類" cannot be renamed or deleted (UI + server)
   - **Steps:** Open manage panel; observe ✏️ and 🗑️ buttons on the 未分類 row. Also try `curl -X PATCH /api/bookmark-categories/未分類` and `curl -X DELETE /api/bookmark-categories/未分類`.
   - **Expected:** Both buttons disabled with title `系統分類，無法修改`. Server returns 400 with explicit error message.
   - **Status:** ✅ Passing (verified during Task 3 curl tests + Task 6 visual)
   - **Last Updated:** 2026-05-10

7. **Story:** Persistent jumpInput across reloads
   - **Steps:** Type `Tokyo Tower` in `#jumpInput`; reload the home page.
   - **Expected:** Input still contains `Tokyo Tower`. localStorage key `pikmin.jumpInput`.
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

8. **Story:** Persistent flowersText on flower-cruise across reloads
   - **Steps:** Paste lines into `#flowersText` on `/flower-cruise`; reload.
   - **Expected:** Text persists. localStorage key `pikmin.flowerCruise.flowersText`.
   - **Status:** ✅ Passing
   - **Last Updated:** 2026-05-10

9. **Story:** Persistent filter / lastSelectedCategory selection
   - **Steps:** Change category filter dropdown; change add-bookmark category dropdown; reload.
   - **Expected:** Both selections restored after reload. localStorage keys `pikmin.bookmarkCategoryFilter`, `pikmin.lastSelectedCategory`.
   - **Status:** ✅ Passing (Task 8 implementer also fixed a stub-driven order-of-precedence bug here)
   - **Last Updated:** 2026-05-10

10. **Story:** Schema migration is idempotent
    - **Steps:** `python3 -c "import server"` (or via `make start-ipad`) twice; check `shared.json`.
    - **Expected:** First run prints `✓ migrated N bookmarks; categories: ['未分類']`; second run silent; `bookmark_categories` unchanged; every bookmark has `category` field.
    - **Status:** ✅ Passing (verified during Task 1)
    - **Last Updated:** 2026-05-10

11. **Story:** Bookmark `<li>` is a click target for teleport (regression repaired)
    - **Steps:** Click anywhere on a bookmark row except the ✏️ / select / ✕ icons.
    - **Expected:** Phone teleports to that bookmark's coordinates. Earlier the row's `<input>` swallowed clicks; now name is a `<span>` and click region covers the row.
    - **Status:** ✅ Passing
    - **Last Updated:** 2026-05-10

12. **Story:** Navbar maintains 48px height when sidebar content overflows (regression repaired)
    - **Steps:** Open the home page; observe `<nav>` height.
    - **Expected:** 48px. Earlier the nav was compressed to ~33px because it lacked `flex-shrink: 0` and the body's flex layout was squeezing it. Fixed via `shrink-0` Tailwind class.
    - **Status:** ✅ Passing
    - **Last Updated:** 2026-05-10

13. **Story:** No browser-native prompt/confirm/alert anywhere
    - **Steps:** `grep -nE '\b(prompt|confirm|alert)\(' static/*.html | grep -v Modal\.`
    - **Expected:** Only `Modal.confirm` matches; no native dialog calls.
    - **Status:** ✅ Passing
    - **Last Updated:** 2026-05-10

### Notes

- Visual aesthetic: shadcn-flavored dark UI achieved via Tailwind CDN + a hand-tuned color config. No build pipeline introduced.
- Out of scope (deferred): bookmark drag-reorder, multi-tag bookmarks, full React rewrite, category colors, import/export.
