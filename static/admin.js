// Pikmin Walker — admin overlay (update flow + shutdown overlay).
//
// Loaded by all three pages (index, walk, flower-cruise). Owns:
//
//   * #navUpdate click       → check + apply git pull --ff-only
//   * Update badge           → red dot when origin/main has new commits
//   * Shutdown overlay       → fullscreen "restarting" + auto-reload
//                              after update_apply triggers `make restart-*`
//
// Each page's WS handleMessage() delegates to window.PikminAdmin.handle(m)
// FIRST and only handles its own events if .handle returns false.
// (Saves us pushing identical switch-cases into 3 files.)

(function() {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const safeModal = () => (typeof Modal !== 'undefined') ? Modal : null;

  // ─── Shutdown / reload overlay ─────────────────────────────────────

  let overlay = null;

  function showShutdownOverlay(opts = {}) {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.id = 'pikmin-shutdown-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:1300;background:rgba(15,23,42,0.95);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px;color:#f1f5f9;font-family:inherit;';

    const spinner = document.createElement('div');
    spinner.style.cssText = 'font-size:32px;animation:pikmin-spin 1.2s linear infinite;';
    spinner.textContent = opts.icon || '🔄';

    const title = document.createElement('div');
    title.style.cssText = 'font-size:16px;font-weight:600;';
    title.textContent = opts.title || '重啟伺服器中…';

    const sub = document.createElement('div');
    sub.style.cssText = 'font-size:13px;color:#94a3b8;';
    sub.textContent = opts.sub || '就緒後自動重新整理';

    const styleEl = document.createElement('style');
    styleEl.textContent = '@keyframes pikmin-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }';

    overlay.append(spinner, title, sub, styleEl);
    document.body.append(overlay);

    // Poll for the server to come back. We use /api/profiles because
    // it's a cheap GET that exists across all server versions. Bounded
    // by `deadline` so we surface a "stuck" state instead of spinning
    // forever silently.
    const startedAt = Date.now();
    const deadlineMs = 60_000;
    const tryReload = async () => {
      try {
        const res = await fetch('/api/profiles', { signal: AbortSignal.timeout(2000) });
        if (res.ok) { location.reload(); return; }
      } catch {}
      if (Date.now() - startedAt > deadlineMs) {
        title.textContent = '⚠ 60 秒沒回來';
        // Build the recovery hint with safe DOM methods (no innerHTML).
        sub.textContent = '';
        const line1 = document.createElement('div');
        line1.textContent = '看起來 make restart 卡住了。';
        const line2 = document.createElement('div');
        line2.style.cssText = 'margin-top:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#cbd5f5;';
        line2.textContent = 'tail /tmp/pikmin-restart.log';
        const line3 = document.createElement('div');
        line3.style.cssText = line2.style.cssText;
        line3.textContent = 'make stop && make start-ipad';
        sub.append(line1, line2, line3);
        spinner.style.animation = 'none';
        spinner.textContent = '⚠️';
        return;
      }
      setTimeout(tryReload, 800);
    };
    // 2.5s before first poll — server's `sleep 1` + restart cmd needs
    // time to actually take us down before we start probing.
    setTimeout(tryReload, 2500);
  }

  // ─── Update badge + button ─────────────────────────────────────────

  let pendingUpdate = null;  // last known summary from server

  // Update count: shown as a numeric badge (App-Store style). Distinct
  // shape from the tunnel-status dot so they never get confused.
  function setUpdateBadge(count) {
    const btn = document.getElementById('navUpdate');
    if (!btn) return;
    let badge = btn.querySelector('.update-badge');
    if (count > 0) {
      btn.classList.add('has-update');
      btn.setAttribute('title', `有 ${count} 個新版本`);
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'update-badge';
        badge.style.cssText = 'position:absolute;top:-2px;right:-2px;min-width:16px;height:16px;padding:0 4px;background:#ef4444;color:#fff;font-size:10px;font-weight:700;line-height:16px;text-align:center;border-radius:8px;border:1.5px solid var(--background,#0b1220);box-sizing:content-box;';
        btn.style.position = 'relative';
        btn.appendChild(badge);
      }
      badge.textContent = count > 99 ? '99+' : String(count);
    } else {
      btn.classList.remove('has-update');
      btn.setAttribute('title', '檢查更新');
      if (badge) badge.remove();
    }
  }

  // Show a busy state on the update button while /check is running.
  // Inline keyframe injection so we don't need a separate stylesheet.
  let _busyStyleInjected = false;
  function ensureBusyStyle() {
    if (_busyStyleInjected) return;
    const s = document.createElement('style');
    s.textContent = `
      @keyframes pikmin-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }
      @keyframes pikmin-pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }
      .pikmin-busy { pointer-events: none; opacity: 0.7; }
      .pikmin-busy .nav-update-icon { display: inline-block; animation: pikmin-spin 1s linear infinite; }
    `;
    document.head.appendChild(s);
    _busyStyleInjected = true;
  }
  function setUpdateBusy(busy) {
    const btn = document.getElementById('navUpdate');
    if (!btn) return;
    ensureBusyStyle();
    if (busy) btn.classList.add('pikmin-busy');
    else btn.classList.remove('pikmin-busy');
  }

  function commitListMarkup(commits) {
    if (!commits || !commits.length) return '';
    const items = commits.slice(0, 12).map(c =>
      `  • ${c.sha}  ${escapeText(c.subject)}`
    ).join('\n');
    const more = commits.length > 12 ? `\n  …還有 ${commits.length - 12} 個` : '';
    return items + more;
  }

  function escapeText(s) {
    return String(s).replace(/[<>&]/g, ch => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[ch]));
  }

  async function checkAndPrompt() {
    const M = safeModal();

    setUpdateBusy(true);
    let summary;
    try {
      const res = await fetch('/api/update/check', { signal: AbortSignal.timeout(20000) });
      summary = await res.json();
    } catch (e) {
      setUpdateBusy(false);
      if (M) await M.confirm({
        title: '檢查更新失敗',
        description: `無法連到 GitHub：${e}`,
        confirmText: '知道了', danger: false,
      });
      return;
    } finally {
      setUpdateBusy(false);
    }

    pendingUpdate = summary;

    if (summary.error) {
      if (M) await M.confirm({
        title: '檢查更新失敗',
        description: `${summary.error}\n${summary.detail || ''}`,
        confirmText: '知道了', danger: false,
      });
      return;
    }

    if (summary.up_to_date) {
      setUpdateBadge(0);
      if (M) await M.confirm({
        title: '✓ 已是最新版',
        description: `目前版本 ${summary.current_short}，沒有新的更新。`,
        confirmText: '知道了', danger: false,
      });
      return;
    }

    setUpdateBadge(summary.count);

    // Build a description with the commit list.
    const lines = [
      `目前 ${summary.current_short} → 新版 ${summary.latest_short}`,
      `共 ${summary.count} 個新 commit：`,
      '',
      commitListMarkup(summary.commits),
    ];
    if (summary.dirty) {
      lines.push('', '⚠ 偵測到本地修改，無法自動更新。請先 commit 或回報。');
    } else if (!summary.fast_forward) {
      lines.push('', '⚠ 分支已偏離，無法 fast-forward。請手動處理。');
    }

    if (!M) return;

    const canApply = !summary.dirty && summary.fast_forward;
    const ok = await M.confirm({
      title: canApply ? '套用更新？' : '無法自動更新',
      description: lines.join('\n'),
      confirmText: canApply ? '更新並重啟' : '知道了',
      danger: canApply,
    });
    if (!ok || !canApply) return;

    await applyUpdate();
  }

  async function applyUpdate() {
    const M = safeModal();
    let res;
    try {
      res = await fetch('/api/update/apply', {
        method: 'POST',
        signal: AbortSignal.timeout(60000),
      });
    } catch (e) {
      if (M) await M.confirm({
        title: '更新失敗',
        description: `${e}`, confirmText: '知道了', danger: false,
      });
      return;
    }

    let body = {};
    try { body = await res.json(); } catch {}

    if (!res.ok) {
      if (M) await M.confirm({
        title: '更新失敗',
        description: `${body.error || 'unknown'}\n${body.detail || ''}`,
        confirmText: '知道了', danger: false,
      });
      return;
    }

    setUpdateBadge(0);
    showShutdownOverlay({
      icon: '⬆️',
      title: '更新中…',
      sub: `${body.previous} → ${body.current}（${body.count} 個 commit）`,
    });
  }

  function wireUpdate() {
    const btn = document.getElementById('navUpdate');
    if (!btn) return;
    btn.addEventListener('click', () => { checkAndPrompt(); });
  }

  // ─── WS message hook ───────────────────────────────────────────────

  function handle(m) {
    switch (m.type) {
      case 'update_available':
        setUpdateBadge(m.count);
        pendingUpdate = m;
        return true;
      case 'server_shutdown':
        showShutdownOverlay();
        return true;
      default:
        return false;
    }
  }

  // ─── Bootstrap ────────────────────────────────────────────────────

  function init() {
    wireUpdate();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.PikminAdmin = { handle };
})();
