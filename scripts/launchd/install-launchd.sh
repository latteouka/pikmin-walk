#!/usr/bin/env bash
#
# Pikmin Walker — launchd install.
#
# Renders both plist templates (server LaunchAgent + tunneld LaunchDaemon)
# with the current user's paths and bootstraps them. Idempotent —
# re-running re-renders templates and reloads. Single sudo prompt.
#
# Usage:
#     scripts/launchd/install-launchd.sh [PORT]
#
# PORT defaults to 7766. Set non-default if running multiple devices,
# but the LaunchAgent label stays "com.pikmin.walker" — only one server
# instance per launchd domain.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
err()  { printf "${RED}✗${NC} %s\n" "$*" >&2; }
step() { printf "${BLUE}→${NC} %s\n" "$*"; }

PORT="${1:-7766}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$HERE/../.." && pwd)"
ADMIN_USER="$(whoami)"
ADMIN_HOME="$HOME"
UV_PATH="$(command -v uv || true)"

if [ -z "$UV_PATH" ]; then
    err "uv not found in PATH — install via https://docs.astral.sh/uv/"
    exit 1
fi
ok "uv → $UV_PATH"

PMD_PATH="$ADMIN_HOME/.local/bin/pymobiledevice3"
if [ ! -x "$PMD_PATH" ]; then
    err "pymobiledevice3 not at $PMD_PATH"
    err "  run: uv tool install pymobiledevice3 --python 3.13"
    exit 1
fi
ok "pymobiledevice3 → $PMD_PATH"

AGENT_LABEL="com.pikmin.walker"
DAEMON_LABEL="com.pikmin.tunneld"
AGENT_DIR="$ADMIN_HOME/Library/LaunchAgents"
DAEMON_DIR="/Library/LaunchDaemons"
AGENT_PATH="$AGENT_DIR/$AGENT_LABEL.plist"
DAEMON_PATH="$DAEMON_DIR/$DAEMON_LABEL.plist"
mkdir -p "$AGENT_DIR"

# ─── Render templates ────────────────────────────────────────────────────

render() {
    local tmpl="$1" out="$2"
    sed \
        -e "s|__UV_PATH__|$UV_PATH|g" \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__USER_HOME__|$ADMIN_HOME|g" \
        -e "s|__ADMIN_USER__|$ADMIN_USER|g" \
        -e "s|__PORT__|$PORT|g" \
        "$tmpl" > "$out"
}

step "rendering LaunchAgent → $AGENT_PATH"
render "$HERE/com.pikmin.walker.plist.template" "$AGENT_PATH"
plutil -lint "$AGENT_PATH" >/dev/null
ok "  agent plist valid"

TMP_DAEMON=$(mktemp)
trap 'rm -f "$TMP_DAEMON"' EXIT
step "rendering LaunchDaemon → $DAEMON_PATH (needs sudo)"
render "$HERE/com.pikmin.tunneld.plist.template" "$TMP_DAEMON"
plutil -lint "$TMP_DAEMON" >/dev/null
ok "  daemon plist valid"

sudo install -m 644 -o root -g wheel "$TMP_DAEMON" "$DAEMON_PATH"

# Wrapper must be executable AND world-readable so root can read it.
chmod 755 "$HERE/tunneld-wrapper.sh"

# ─── (Re)load services ───────────────────────────────────────────────────

UID_NUM="$(id -u)"
AGENT_TARGET="gui/$UID_NUM/$AGENT_LABEL"
DAEMON_TARGET="system/$DAEMON_LABEL"

step "reloading LaunchDaemon ($DAEMON_TARGET)"
sudo launchctl bootout "$DAEMON_TARGET" 2>/dev/null || true
sudo launchctl bootstrap system "$DAEMON_PATH"
sudo launchctl enable "$DAEMON_TARGET"
ok "  $DAEMON_LABEL bootstrapped"

step "reloading LaunchAgent ($AGENT_TARGET)"
launchctl bootout "$AGENT_TARGET" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$AGENT_PATH"
launchctl enable "$AGENT_TARGET"
ok "  $AGENT_LABEL bootstrapped"

sleep 2

# ─── Verify ──────────────────────────────────────────────────────────────

step "verifying"
if launchctl print "$AGENT_TARGET" 2>/dev/null | grep -q "state = running"; then
    ok "  agent running"
else
    warn "  agent not running yet — check /tmp/pikmin-walker.log"
fi

if sudo launchctl print "$DAEMON_TARGET" 2>/dev/null | grep -q "state = running"; then
    ok "  daemon running"
else
    warn "  daemon not running yet — check /var/log/pikmin-tunneld.log"
fi

echo
ok "done. open → http://localhost:$PORT"
echo "    logs:   tail -f /tmp/pikmin-walker.log /var/log/pikmin-tunneld.log"
echo "    stop:   scripts/launchd/uninstall-launchd.sh"
