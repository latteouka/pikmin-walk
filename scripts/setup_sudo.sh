#!/usr/bin/env bash
# Install a sudoers.d rule that grants the current user NOPASSWD access
# to exactly the commands Pikmin Walker needs as root:
#
#   * `pymobiledevice3 remote tunneld`        — manual dev-mode launch
#   * `launchctl kickstart -k system/com.pikmin.tunneld`
#                                             — smart restart bouncing
#                                               the tunneld LaunchDaemon
#   * `pkill -f pymobiledevice3.*tunneld`     — dev-mode tunnel reset
#
# Idempotent — re-running overwrites the file. To undo: make remove-sudo
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
err()  { printf "${RED}✗${NC} %s\n" "$*" >&2; }
step() { printf "${BLUE}→${NC} %s\n" "$*"; }

SUDOERS_FILE="/etc/sudoers.d/pikmin-walk-tunneld"

# 1. Locate pymobiledevice3 (must use the absolute path in sudoers — sudoers
#    does not expand ~ or look up PATH).
PMD=$(command -v pymobiledevice3 || true)
if [ -z "$PMD" ]; then
    err "找不到 pymobiledevice3 — 先跑 make install"
    exit 1
fi
# Resolve symlinks so the rule binds to the real binary (uv tool path stays
# stable across reinstalls).
PMD_REAL=$(readlink -f "$PMD" 2>/dev/null || readlink "$PMD" 2>/dev/null || echo "$PMD")
ok "pymobiledevice3 → $PMD"

USERNAME=$(whoami)

# 2. Build the sudoers entries and validate before installing — a broken
#    sudoers file can lock you out of sudo entirely.
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
{
    echo "# Pikmin Walker — auto-generated. DO NOT edit. Re-run scripts/setup_sudo.sh."
    echo "$USERNAME ALL=(ALL) NOPASSWD: $PMD remote tunneld"
    echo "$USERNAME ALL=(ALL) NOPASSWD: /bin/launchctl kickstart -k system/com.pikmin.tunneld"
    echo "$USERNAME ALL=(ALL) NOPASSWD: /bin/launchctl bootstrap system /Library/LaunchDaemons/com.pikmin.tunneld.plist"
    echo "$USERNAME ALL=(ALL) NOPASSWD: /bin/launchctl bootout system/com.pikmin.tunneld"
    echo "$USERNAME ALL=(ALL) NOPASSWD: /usr/bin/pkill -f pymobiledevice3.*tunneld"
} > "$TMP"

if ! sudo visudo -cf "$TMP" >/dev/null 2>&1; then
    err "產生的 sudoers entry 語法錯誤："
    cat "$TMP"
    exit 1
fi
ok "sudoers 語法檢查通過"

# 3. Move into place with the strict permissions sudoers.d requires (440,
#    owned by root:wheel). `install` does the copy + chown + chmod atomically.
step "安裝 sudoers rule（會問一次 sudo 密碼，是最後一次）..."
sudo install -m 440 -o root -g wheel "$TMP" "$SUDOERS_FILE"
ok "已寫入 $SUDOERS_FILE"

echo
echo "規則內容："
sed 's/^/  /' "$SUDOERS_FILE" 2>/dev/null || sudo sed 's/^/  /' "$SUDOERS_FILE"
echo
echo "  允許的 user：$USERNAME"
echo "  其他 sudo 操作仍需密碼"
echo
echo "✨ 之後 make start / 智慧重啟按鈕都不會再問 sudo 密碼。"
echo "  要 undo: make remove-sudo"
