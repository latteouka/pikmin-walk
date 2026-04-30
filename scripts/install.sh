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
COUNT=$(printf "%s" "$DEVICE_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

if [ "$COUNT" = "0" ]; then
    warn "沒偵測到裝置"
    echo "  原因可能是："
    echo "    - 還沒插 USB"
    echo "    - 手機沒解鎖 / 沒按「信任此電腦」"
    echo "    - 手機沒開「開發者模式」"
    echo "  請對照下方 checklist 處理後重跑 make install"
else
    ok "偵測到 $COUNT 台裝置："
    printf "%s" "$DEVICE_JSON" | python3 -c "
import sys, json
for d in json.load(sys.stdin):
    name = d.get('DeviceName', '?')
    pt = d.get('ProductType', '?')
    pv = d.get('ProductVersion', '?')
    udid = d.get('UniqueDeviceID', '')
    print(f'    {name} — {pt} / iOS {pv}  UDID={udid[:12]}…')
"
fi

# ─── 5. 裝置端 checklist（手動）─────────────────────────────────
echo
echo "📱 iOS 裝置端設定（手動，在手機上操作）："
echo
echo "    ☐ 設定 → 隱私權與安全性 → 開發者模式 → 開啟（會重啟手機）"
echo "    ☐ USB 接上 Mac，手機跳「信任此電腦」按「信任」+ 輸入密碼"
echo "    ☐ (iOS 17+) make start 會自動拉 tunneld，會問你 sudo 密碼"
echo

# ─── 6. 下一步 ──────────────────────────────────────────────────
echo "✨ 安裝完成。下一步："
echo
echo "    make start             # 啟動 server"
echo "    open http://localhost:7766"
echo
