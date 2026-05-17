#!/usr/bin/env bash
#
# Pikmin Walker — sudoers rule for tunneld.
#
# Installs a single NOPASSWD rule so `make tunnel` (and `make restart-*`,
# which depends on it) can launch tunneld in the background without
# blocking on a password prompt. Without this, sudo would try to prompt
# but Makefile's `</dev/null` redirect makes that fail silently and
# tunneld never starts.
#
# Only one rule, scoped to the absolute path of pymobiledevice3 — we are
# NOT giving NOPASSWD to anything else.
#
# Undo: `make remove-sudo`.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
err()  { printf "${RED}✗${NC} %s\n" "$*" >&2; }
step() { printf "${BLUE}→${NC} %s\n" "$*"; }

ADMIN_USER="$(whoami)"
PMD_PATH="$HOME/.local/bin/pymobiledevice3"

if [ ! -x "$PMD_PATH" ]; then
    err "pymobiledevice3 not at $PMD_PATH"
    err "  run: uv tool install pymobiledevice3 --python 3.13"
    exit 1
fi
ok "pymobiledevice3 → $PMD_PATH"

TARGET=/etc/sudoers.d/pikmin-walk-tunneld
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

cat > "$TMP" <<EOF
# Pikmin Walker — auto-generated. DO NOT edit. Re-run scripts/setup_sudo.sh.
$ADMIN_USER ALL=(ALL) NOPASSWD: $PMD_PATH remote tunneld
EOF

# Validate before installing — visudo -cf rejects syntax errors, saving
# us from locking ourselves out of sudo.
if ! sudo visudo -cf "$TMP" >/dev/null; then
    err "sudoers syntax check failed"
    exit 1
fi
ok "sudoers syntax ok"

step "installing $TARGET (needs sudo once)..."
sudo install -m 0440 -o root -g wheel "$TMP" "$TARGET"
ok "installed"

echo
echo "規則內容："
echo "  # Pikmin Walker — auto-generated. DO NOT edit. Re-run scripts/setup_sudo.sh."
echo "  $ADMIN_USER ALL=(ALL) NOPASSWD: $PMD_PATH remote tunneld"
echo
echo "  允許的 user：$ADMIN_USER"
echo "  scope：只放 \`pymobiledevice3 remote tunneld\`，其他 sudo 操作仍需密碼"
echo
ok "完成。之後 \`make tunnel\` / \`make restart-*\` 不會再問密碼。"
echo "    要 undo: make remove-sudo"
