#!/usr/bin/env bash
# Pikmin Walker — macOS 一次性安裝腳本
# 由 `make install` 呼叫。重複執行是安全的。
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
err()  { printf "${RED}✗${NC} %s\n" "$*" >&2; }
step() { printf "${BLUE}→${NC} %s\n" "$*"; }

echo "🌸 Pikmin Walker — macOS 一鍵安裝"
echo

# ─── 1. macOS 確認 ──────────────────────────────────────────────
if [ "$(uname -s)" != "Darwin" ]; then
    err "本腳本只支援 macOS（你在 $(uname -s) 上）"
    exit 1
fi
ok "macOS"

# ─── 2. uv（Python 套件管理）─────────────────────────────────────
if command -v uv >/dev/null 2>&1; then
    ok "uv 已安裝（$(uv --version 2>&1 | head -1)）"
else
    step "uv 沒裝，從 https://astral.sh/uv/install.sh 下載安裝器..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv 安裝後改 ~/.zshrc / ~/.bash_profile，當前 shell 還沒 reload
    UV_BIN="$HOME/.local/bin/uv"
    if [ -x "$UV_BIN" ]; then
        export PATH="$HOME/.local/bin:$PATH"
        ok "uv 安裝完成（$($UV_BIN --version | head -1)）"
        warn "為了讓 uv 在新 terminal 也能用，請執行 source ~/.zshrc 或重開 terminal"
    else
        err "uv 安裝後找不到執行檔（$UV_BIN），請手動裝：https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
fi

# ─── 3. pymobiledevice3（透過 uv tool）──────────────────────────
if uv tool list 2>/dev/null | grep -q "^pymobiledevice3"; then
    ok "pymobiledevice3 已安裝"
else
    step "安裝 pymobiledevice3（綁定 Python 3.13；首次需下載 Python 3.13，約 30s）..."
    uv tool install pymobiledevice3 --python 3.13
    ok "pymobiledevice3 安裝完成"
fi

# ─── 4. 偵測連著的 iOS 裝置 ──────────────────────────────────────
echo
step "偵測 USB 上的 iOS 裝置..."
DEVICE_JSON=$(pymobiledevice3 usbmux list 2>/dev/null || echo "[]")
# Dedupe UDIDs — usbmux list sometimes returns the same device twice
UDIDS=$(printf "%s" "$DEVICE_JSON" | python3 -c "
import sys, json
seen = set()
for d in json.load(sys.stdin):
    udid = d.get('UniqueDeviceID', '')
    if udid and udid not in seen:
        seen.add(udid)
        print(udid)
" 2>/dev/null || true)
COUNT=$(printf "%s" "$UDIDS" | grep -c . || true)

if [ "$COUNT" = "0" ]; then
    warn "沒偵測到裝置"
    echo "  原因可能是："
    echo "    - 還沒插 USB"
    echo "    - 手機沒解鎖 / 沒按「信任此電腦」"
    echo "    - 手機沒開「開發者模式」"
    echo "  請對照下方 checklist 處理後重跑 make install"
else
    ok "偵測到 $COUNT 台獨立裝置："
    printf "%s" "$DEVICE_JSON" | python3 -c "
import sys, json
seen = set()
for d in json.load(sys.stdin):
    udid = d.get('UniqueDeviceID', '')
    if udid in seen: continue
    seen.add(udid)
    name = d.get('DeviceName', '?')
    pt = d.get('ProductType', '?')
    pv = d.get('ProductVersion', '?')
    print(f'    {name} — {pt} / iOS {pv}  UDID={udid[:12]}…')
"
fi

# ─── 4b. 為每台裝置自動設定 Wi-Fi tunnel（best-effort）─────────────
if [ "$COUNT" != "0" ]; then
    echo
    step "為每台裝置設定 Wi-Fi tunnel（拔 USB 後仍可繼續操控）..."
    mkdir -p "$HOME/.pymobiledevice3"
    while IFS= read -r UDID; do
        [ -z "$UDID" ] && continue
        SHORT="${UDID:0:12}…"

        # Step 1: enable wifi-connections (lockdown over Wi-Fi)
        if pymobiledevice3 lockdown wifi-connections --udid "$UDID" --state on >/dev/null 2>&1; then
            ok "[$SHORT] wifi-connections=on"
        else
            warn "[$SHORT] wifi-connections enable 失敗（裝置可能未信任，跳過）"
            continue
        fi

        # Step 2: enable wifi-debugging (Bonjour broadcast on real Wi-Fi)
        if uv run --quiet --with pymobiledevice3 --python 3.13 \
                scripts/wifi_setup.py "$UDID" >/dev/null 2>&1; then
            ok "[$SHORT] wifi-debugging=on"
        else
            warn "[$SHORT] wifi-debugging enable 失敗"
        fi

        # Step 3: save pair record so Wi-Fi TCP lockdown can read it
        PAIR_FILE="$HOME/.pymobiledevice3/${UDID}.plist"
        if pymobiledevice3 lockdown save-pair-record --udid "$UDID" "$PAIR_FILE" >/dev/null 2>&1; then
            ok "[$SHORT] pair record → ~/.pymobiledevice3/${UDID:0:8}….plist"
        else
            warn "[$SHORT] pair record 存檔失敗"
        fi

        # Step 4: developer mode — only matters if it's currently off; if so,
        # reveal the toggle in the device's Settings UI (user still has to
        # tap it manually — Apple does not let us flip it programmatically).
        DEV_STATUS=$(pymobiledevice3 amfi developer-mode-status --udid "$UDID" 2>/dev/null || echo "unknown")
        if [ "$DEV_STATUS" = "true" ]; then
            ok "[$SHORT] developer mode 已開啟"
        elif [ "$DEV_STATUS" = "false" ]; then
            pymobiledevice3 amfi reveal-developer-mode --udid "$UDID" >/dev/null 2>&1 || true
            warn "[$SHORT] developer mode 未開 — 設定選項已 reveal，請手動到「設定 → 隱私權與安全性 → 開發者模式」打開"
        else
            warn "[$SHORT] developer mode 狀態未知"
        fi
    done <<< "$UDIDS"
fi

# ─── 5. 裝置端 checklist（手動）─────────────────────────────────
echo
echo "📱 iOS 裝置端設定 checklist（如果上面有偵測到裝置就大多完成了）："
echo
echo "    ☐ 設定 → 隱私權與安全性 → 開發者模式 → 開啟（會重啟手機）"
echo "    ☐ USB 接上 Mac，手機跳「信任此電腦」按「信任」+ 輸入密碼"
echo "    ☐ (iOS 17+) make start 會自動拉 tunneld，會問你 sudo 密碼"
echo
echo "  Wi-Fi 模式（拔 USB 後繼續操控）的前提："
echo "    - Mac 跟手機在同一個 Wi-Fi（router 不能有 AP Client Isolation）"
echo "    - 上面的「wifi-connections=on / wifi-debugging=on / pair record」全 ✓"
echo

# ─── 6. 下一步 ──────────────────────────────────────────────────
echo "✨ 安裝完成。下一步："
echo
echo "    make start             # 啟動 server"
echo "    open http://localhost:7766"
echo
