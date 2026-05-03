.PHONY: install setup-sudo remove-sudo start stop restart status tunnel \
        kill-tunnel clear logs help \
        start-ipad start-iphone start-iphone2 stop-ipad stop-iphone stop-iphone2 \
        restart-ipad restart-iphone restart-iphone2 mount-iphone-ddi mount-iphone2-ddi \
        status-all list-devices

# Per-machine overrides (device UDIDs etc). The file is gitignored — copy
# Makefile.local.example or run `make list-devices` to get your UDIDs.
-include Makefile.local

IPAD_UDID    ?=
IPHONE_UDID  ?=
IPHONE2_UDID ?=
IPAD_PORT    ?= 7766
IPHONE_PORT  ?= 7767
IPHONE2_PORT ?= 7770

# Build "--udid <id>" only when the variable is non-empty, so the multi-device
# targets fall back to auto-detect when no UDID is configured.
IPAD_UDID_FLAG    := $(if $(IPAD_UDID),--udid $(IPAD_UDID))
IPHONE_UDID_FLAG  := $(if $(IPHONE_UDID),--udid $(IPHONE_UDID))
IPHONE2_UDID_FLAG := $(if $(IPHONE2_UDID),--udid $(IPHONE2_UDID))

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## One-time setup on macOS (uv + pymobiledevice3 + 裝置 checklist)
	@bash scripts/install.sh

setup-sudo: ## Install sudoers rule so tunneld no longer prompts for password
	@bash scripts/setup_sudo.sh

remove-sudo: ## Remove the sudoers rule installed by setup-sudo
	@if [ -f /etc/sudoers.d/pikmin-walk-tunneld ]; then \
		sudo rm /etc/sudoers.d/pikmin-walk-tunneld && \
		echo "✓ removed /etc/sudoers.d/pikmin-walk-tunneld"; \
	else \
		echo "  (沒有 setup-sudo 過，沒事可做)"; \
	fi

# ─── Legacy single-device targets (use auto-detect) ──────────────────────

start: tunnel ## Start server (auto-detect device, port 7766)
	@if lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "server already running on :$(IPAD_PORT)"; \
	else \
		cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && \
		nohup uv run server.py > /tmp/pikmin-server.log 2>&1 & \
		for i in $$(seq 1 15); do \
			sleep 1; \
			if lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
				echo "✓ server up → http://localhost:$(IPAD_PORT) ($${i}s)"; \
				exit 0; \
			fi; \
		done; \
		echo "✗ server failed after 15s — check: cat /tmp/pikmin-server.log"; \
	fi

stop: ## Stop all servers
	@pkill -f "uv run server.py" 2>/dev/null && echo "✓ servers stopped" || echo "no server running"

restart: stop ## Restart default server
	@sleep 1
	@$(MAKE) start

# ─── Multi-device targets ────────────────────────────────────────────────

start-ipad: tunnel ## Start iPad server on port 7766
	@if lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "iPad server already running on :$(IPAD_PORT)"; \
	else \
		cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && \
		nohup uv run server.py --port $(IPAD_PORT) $(IPAD_UDID_FLAG) \
			> /tmp/pikmin-ipad.log 2>&1 & \
		for i in $$(seq 1 15); do \
			sleep 1; \
			if lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
				echo "✓ iPad → http://localhost:$(IPAD_PORT) ($${i}s)"; \
				exit 0; \
			fi; \
		done; \
		echo "✗ failed after 15s — cat /tmp/pikmin-ipad.log"; \
	fi

mount-iphone-ddi: ## Mount DeveloperDiskImage on iPhone (needed after reboot)
	@if [ -z "$(IPHONE_UDID)" ]; then \
		echo "✗ IPHONE_UDID not set — add it to Makefile.local"; exit 1; \
	fi
	@uv run --quiet --with pymobiledevice3 python scripts/mount_ddi.py $(IPHONE_UDID) 2>&1 | tail -1

start-iphone: mount-iphone-ddi ## Start iPhone server on port 7767 (auto-mounts DDI)
	@if lsof -iTCP:$(IPHONE_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "iPhone server already running on :$(IPHONE_PORT)"; \
	else \
		cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && \
		nohup uv run server.py --port $(IPHONE_PORT) $(IPHONE_UDID_FLAG) \
			> /tmp/pikmin-iphone.log 2>&1 & \
		for i in $$(seq 1 15); do \
			sleep 1; \
			if lsof -iTCP:$(IPHONE_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
				echo "✓ iPhone → http://localhost:$(IPHONE_PORT) ($${i}s)"; \
				exit 0; \
			fi; \
		done; \
		echo "✗ failed after 15s — cat /tmp/pikmin-iphone.log"; \
	fi

mount-iphone2-ddi: ## Mount DDI on iPhone 2 (needed after reboot)
	@if [ -z "$(IPHONE2_UDID)" ]; then \
		echo "✗ IPHONE2_UDID not set — add it to Makefile.local"; exit 1; \
	fi
	@uv run --quiet --with pymobiledevice3 python scripts/mount_ddi.py $(IPHONE2_UDID) 2>&1 | tail -1

start-iphone2: mount-iphone2-ddi ## Start iPhone-2 server on port 7770 (auto-mounts DDI)
	@if lsof -iTCP:$(IPHONE2_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "iPhone-2 server already running on :$(IPHONE2_PORT)"; \
	else \
		cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && \
		nohup uv run server.py --port $(IPHONE2_PORT) $(IPHONE2_UDID_FLAG) \
			> /tmp/pikmin-iphone2.log 2>&1 & \
		for i in $$(seq 1 15); do \
			sleep 1; \
			if lsof -iTCP:$(IPHONE2_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
				echo "✓ iPhone-2 → http://localhost:$(IPHONE2_PORT) ($${i}s)"; \
				exit 0; \
			fi; \
		done; \
		echo "✗ failed after 15s — cat /tmp/pikmin-iphone2.log"; \
	fi

stop-ipad: ## Stop iPad server only
	@pkill -f "server.py --port $(IPAD_PORT)" 2>/dev/null && echo "✓ iPad stopped" || echo "not running"

stop-iphone: ## Stop iPhone server only
	@pkill -f "server.py --port $(IPHONE_PORT)" 2>/dev/null && echo "✓ iPhone stopped" || echo "not running"

stop-iphone2: ## Stop iPhone-2 server only
	@pkill -f "server.py --port $(IPHONE2_PORT)" 2>/dev/null && echo "✓ iPhone-2 stopped" || echo "not running"

restart-ipad: stop-ipad ## Restart iPad server
	@sleep 1
	@$(MAKE) start-ipad

restart-iphone: stop-iphone ## Restart iPhone server
	@sleep 1
	@$(MAKE) start-iphone

restart-iphone2: stop-iphone2 ## Restart iPhone-2 server
	@sleep 1
	@$(MAKE) start-iphone2

restart-all: stop ## Restart all servers
	@sleep 1
	@$(MAKE) start-all

start-all: tunnel start-ipad start-iphone start-iphone2 ## Start ALL servers (iPad + iPhone + iPhone-2)

up: osrm-up tunnel start-all ## Start EVERYTHING (OSRM + tunneld + both servers)

# ─── OSRM ────────────────────────────────────────────────────────────────

osrm-up: ## Start OSRM Docker containers (Ontario + Taiwan)
	@docker ps --format '{{.Names}}' | grep -q osrm-ontario || \
		docker start osrm-ontario >/dev/null 2>&1 && echo "✓ osrm-ontario (:5050)" || \
		echo "  ⚠ osrm-ontario missing — docker run needed"
	@docker ps --format '{{.Names}}' | grep -q osrm-taiwan || \
		docker start osrm-taiwan >/dev/null 2>&1 && echo "✓ osrm-taiwan (:5051)" || \
		echo "  ⚠ osrm-taiwan missing — docker run needed"

osrm-down: ## Stop OSRM containers
	@docker stop osrm-ontario osrm-taiwan 2>/dev/null | sed 's/^/✓ /' || true

# ─── Shared ──────────────────────────────────────────────────────────────

tunnel: ## Start tunneld if not running (sudo, iOS 17+ only)
	@if pgrep -f "pymobiledevice3.*tunneld" >/dev/null 2>&1; then \
		echo "✓ tunneld already running"; \
	else \
		echo "starting tunneld (needs sudo)..."; \
		sudo nohup pymobiledevice3 remote tunneld > /tmp/pikmin-tunneld.log 2>&1 & \
		sleep 3; \
		pgrep -f "pymobiledevice3.*tunneld" >/dev/null 2>&1 && \
			echo "✓ tunneld up" || echo "✗ tunneld failed"; \
	fi

kill-tunnel: ## Stop tunneld
	@sudo pkill -f "pymobiledevice3.*tunneld" 2>/dev/null && echo "✓ tunneld stopped" || echo "not running"

cleanup: ## Stop EVERYTHING (servers + tunneld + OSRM containers)
	@pkill -f "uv run server.py" 2>/dev/null && echo "✓ servers" || echo "  (no servers)"
	@sudo pkill -f "pymobiledevice3.*tunneld" 2>/dev/null && echo "✓ tunneld" || echo "  (no tunneld)"
	@docker stop osrm-ontario osrm-taiwan 2>/dev/null | sed 's/^/✓ /' || echo "  (no OSRM containers)"
	@echo "done"

status: ## Show running status (all servers + tunneld + devices)
	@echo "== tunneld =="
	@pgrep -f "pymobiledevice3.*tunneld" >/dev/null 2>&1 && \
		echo "  ✓ running" || echo "  ✗ not running"
	@echo "== servers =="
	@lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1 && \
		echo "  ✓ iPad     → :$(IPAD_PORT)" || echo "  ✗ iPad     (:$(IPAD_PORT))"
	@lsof -iTCP:$(IPHONE_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1 && \
		echo "  ✓ iPhone   → :$(IPHONE_PORT)" || echo "  ✗ iPhone   (:$(IPHONE_PORT))"
	@lsof -iTCP:$(IPHONE2_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1 && \
		echo "  ✓ iPhone-2 → :$(IPHONE2_PORT)" || echo "  ✗ iPhone-2 (:$(IPHONE2_PORT))"
	@echo "== USB devices =="
	@pymobiledevice3 usbmux list 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  {x[\"DeviceName\"]} ({x[\"ProductType\"]}/{x[\"ProductVersion\"]}) {x[\"UniqueDeviceID\"][:12]}') for x in d]" 2>/dev/null || echo "  (none)"

list-devices: ## List connected USB devices with UDIDs
	@pymobiledevice3 usbmux list

clear: ## Clear simulated location on first connected device
	@cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && uv run clear.py

logs: ## Tail all logs
	@tail -f /tmp/pikmin-server.log /tmp/pikmin-ipad.log /tmp/pikmin-iphone.log /tmp/pikmin-iphone2.log /tmp/pikmin-tunneld.log 2>/dev/null
