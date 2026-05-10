#!/bin/bash
#
# Pikmin Walker — tunneld supervisor wrapper.
#
# Invoked by the com.pikmin.tunneld LaunchDaemon (runs as root). Why a
# wrapper instead of pointing the plist directly at pymobiledevice3:
#
#   1. Reap orphan tunneld procs from a previous SIGKILL. utun network
#      interfaces are process-scoped, so once the orphan dies kernel
#      reaps the utun automatically — no manual `ifconfig destroy`.
#
#   2. Pin the binary to the admin user's uv-installed pymobiledevice3
#      so the version stays in lockstep with the LaunchAgent.
#
#   3. exec into pymobiledevice3 so SIGTERM from launchd reaches the
#      Python process directly (otherwise this shell would catch it
#      first and tunneld becomes an orphan child briefly).

set -euo pipefail

# Read admin user info from env (set by the LaunchDaemon plist).
ADMIN_HOME="${PIKMIN_ADMIN_HOME:-/var/root}"
PMD="$ADMIN_HOME/.local/bin/pymobiledevice3"

# 1. Pre-start cleanup. `|| true` because: pkill exits 1 when nothing
#    matches (perfectly fine), but `set -e` would treat that as fatal.
pkill -f "pymobiledevice3.*remote tunneld" 2>/dev/null || true
sleep 1

# 2. Sanity-check the binary exists. Without this the launchd error
#    just shows "exit 127" with no clue why.
if [ ! -x "$PMD" ]; then
    echo "✗ pymobiledevice3 not at $PMD" >&2
    echo "  run scripts/install.sh as the admin user first" >&2
    exit 1
fi

# 3. Hand off. exec replaces this shell so the Python process is PID
#    of the LaunchDaemon — no shell layer between launchd and tunneld
#    eating signals.
exec "$PMD" remote tunneld
