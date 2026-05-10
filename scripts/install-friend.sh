#!/usr/bin/env bash
#
# Pikmin Walker — 朋友機器一鍵安裝（admin 用）。
#
# 執行流程：
#   1. install.sh        — uv + pymobiledevice3 + 裝置 Wi-Fi 設定
#   2. setup_sudo.sh     — NOPASSWD 給 tunneld + launchctl 操作
#   3. launchd/install-launchd.sh
#                        — 渲染 + bootstrap 兩個 plist（Agent + Daemon）
#   4. 驗證              — 等 :7766 起來，列印開啟連結
#
# 跑兩次以上是安全的（每一步都 idempotent）。
# 解除：scripts/install-friend-uninstall.sh

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()    { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
err()   { printf "${RED}✗${NC} %s\n" "$*" >&2; }
phase() { printf "\n${CYAN}━━━ %s ━━━${NC}\n\n" "$*"; }

PORT="${PORT:-7766}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$HERE/.." && pwd)"
cd "$PROJECT_DIR"

phase "0/4  停掉手動跑的 server / tunneld（避免跟 launchd 搶 port）"
make stop 2>&1 | sed 's/^/  /' || true
make kill-tunnel 2>&1 | sed 's/^/  /' || true
sleep 1

phase "1/4  依賴與裝置設定（install.sh）"
bash scripts/install.sh

phase "2/4  sudoers（setup_sudo.sh — 會問一次 sudo 密碼）"
bash scripts/setup_sudo.sh

phase "3/4  LaunchAgent + LaunchDaemon（launchd/install-launchd.sh）"
bash scripts/launchd/install-launchd.sh "$PORT"

phase "4/4  驗證 server 起來"
echo "等待 :$PORT 回應…（最多 30s）"
for i in $(seq 1 30); do
    if curl -sf "http://localhost:$PORT/api/profiles" >/dev/null 2>&1; then
        ok "server 上線 (${i}s)"
        OK=1
        break
    fi
    sleep 1
done
if [ "${OK:-0}" != "1" ]; then
    warn "server 沒在 30s 內回應 — 看 /tmp/pikmin-walker.log"
    tail -n 30 /tmp/pikmin-walker.log 2>/dev/null || true
    exit 1
fi

# Bonus: 確認 LaunchDaemon (tunneld) 也是活的
if sudo launchctl print system/com.pikmin.tunneld 2>/dev/null | grep -q "state = running"; then
    ok "tunneld LaunchDaemon 也活著"
else
    warn "tunneld 沒在 running 狀態 — 看 /var/log/pikmin-tunneld.log"
fi

cat <<EOF

🌸 全部就緒。

  📍 朋友打開瀏覽器  → http://localhost:$PORT
  🔄 萬一連線異常  → 點右上 🔄 一律修好（會自動偵測 tunneld 是否要 bounce）
  ⬆️ 想拿 bug fix  → 點右上 ⬆️，會 git pull + 自動重啟

  log:    tail -f /tmp/pikmin-walker.log /var/log/pikmin-tunneld.log
  解除：  bash scripts/launchd/uninstall-launchd.sh && make remove-sudo

EOF
