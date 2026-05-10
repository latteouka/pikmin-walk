#!/usr/bin/env bash
#
# Pikmin Walker — launchd uninstall.
#
# Boots out both services and removes the plists. Idempotent.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }

UID_NUM="$(id -u)"
AGENT_LABEL="com.pikmin.walker"
DAEMON_LABEL="com.pikmin.tunneld"
AGENT_PATH="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
DAEMON_PATH="/Library/LaunchDaemons/$DAEMON_LABEL.plist"

# Boot out — ignore errors (already gone is fine).
launchctl bootout "gui/$UID_NUM/$AGENT_LABEL" 2>/dev/null && ok "agent booted out" || warn "agent not loaded"
sudo launchctl bootout "system/$DAEMON_LABEL" 2>/dev/null && ok "daemon booted out" || warn "daemon not loaded"

# Remove plists.
[ -f "$AGENT_PATH" ] && rm "$AGENT_PATH" && ok "removed $AGENT_PATH"
[ -f "$DAEMON_PATH" ] && sudo rm "$DAEMON_PATH" && ok "removed $DAEMON_PATH"

ok "done. logs: /tmp/pikmin-walker.log, /var/log/pikmin-tunneld.log"
