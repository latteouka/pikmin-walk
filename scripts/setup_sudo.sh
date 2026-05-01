#!/usr/bin/env bash
# Install a sudoers.d rule so `pymobiledevice3 remote tunneld` does not
# prompt for a password. Idempotent — re-running just overwrites the file.
#
# To undo: make remove-sudo (or sudo rm /etc/sudoers.d/pikmin-walk-tunneld)
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

# 2. Build the sudoers line and validate it BEFORE installing — a broken
#    sudoers file can lock you out of sudo entirely.
SUDOERS_LINE="$USERNAME ALL=(ALL) NOPASSWD: $PMD remote tunneld"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
echo "$SUDOERS_LINE" > "$TMP"

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
echo "  $SUDOERS_LINE"
echo
echo "  允許的 user：$USERNAME"
echo "  允許的指令：$PMD remote tunneld"
echo "  其他 sudo 操作仍需密碼"
echo
echo "✨ 之後 make start 不會再問 sudo 密碼了。"
echo "  要 undo: make remove-sudo"
