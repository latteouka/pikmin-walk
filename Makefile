.PHONY: start stop restart status tunnel kill-tunnel clear logs help \
        start-ipad start-iphone stop-ipad stop-iphone status-all list-devices

IPAD_UDID   := 00008122-001E78461E61801C
IPHONE_UDID := 4cbd580a720741942cf86a34933abd87f6d384fd
IPAD_PORT   := 7766
IPHONE_PORT := 7767

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ─── Legacy single-device targets (use auto-detect) ──────────────────────

start: tunnel ## Start server (auto-detect device, port 7766)
	@if lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "server already running on :$(IPAD_PORT)"; \
	else \
		cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && \
		nohup uv run server.py > /tmp/pikmin-server.log 2>&1 & \
		sleep 5; \
		lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1 && \
			echo "✓ server up → http://localhost:$(IPAD_PORT)" || \
			echo "✗ server failed — check: cat /tmp/pikmin-server.log"; \
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
		nohup uv run server.py --port $(IPAD_PORT) --udid $(IPAD_UDID) \
			> /tmp/pikmin-ipad.log 2>&1 & \
		sleep 5; \
		lsof -iTCP:$(IPAD_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1 && \
			echo "✓ iPad → http://localhost:$(IPAD_PORT)" || \
			echo "✗ failed — cat /tmp/pikmin-ipad.log"; \
	fi

mount-iphone-ddi: ## Mount DeveloperDiskImage on iPhone (needed after reboot)
	@uv run --quiet --with pymobiledevice3 python scripts/mount_ddi.py $(IPHONE_UDID) 2>&1 | tail -1

start-iphone: mount-iphone-ddi ## Start iPhone server on port 7767 (auto-mounts DDI)
	@if lsof -iTCP:$(IPHONE_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "iPhone server already running on :$(IPHONE_PORT)"; \
	else \
		cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && \
		nohup uv run server.py --port $(IPHONE_PORT) --udid $(IPHONE_UDID) \
			> /tmp/pikmin-iphone.log 2>&1 & \
		sleep 5; \
		lsof -iTCP:$(IPHONE_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1 && \
			echo "✓ iPhone → http://localhost:$(IPHONE_PORT)" || \
			echo "✗ failed — cat /tmp/pikmin-iphone.log"; \
	fi

stop-ipad: ## Stop iPad server only
	@pkill -f "server.py --port $(IPAD_PORT)" 2>/dev/null && echo "✓ iPad stopped" || echo "not running"

stop-iphone: ## Stop iPhone server only
	@pkill -f "server.py --port $(IPHONE_PORT)" 2>/dev/null && echo "✓ iPhone stopped" || echo "not running"

restart-ipad: stop-ipad ## Restart iPad server
	@sleep 1
	@$(MAKE) start-ipad

restart-iphone: stop-iphone ## Restart iPhone server
	@sleep 1
	@$(MAKE) start-iphone

restart-all: stop ## Restart BOTH servers
	@sleep 1
	@$(MAKE) start-all

start-all: tunnel start-ipad start-iphone ## Start BOTH iPad + iPhone servers

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
		echo "  ✓ iPad   → :$(IPAD_PORT)" || echo "  ✗ iPad   (:$(IPAD_PORT))"
	@lsof -iTCP:$(IPHONE_PORT) -sTCP:LISTEN -nP >/dev/null 2>&1 && \
		echo "  ✓ iPhone → :$(IPHONE_PORT)" || echo "  ✗ iPhone (:$(IPHONE_PORT))"
	@echo "== USB devices =="
	@pymobiledevice3 usbmux list 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  {x[\"DeviceName\"]} ({x[\"ProductType\"]}/{x[\"ProductVersion\"]}) {x[\"UniqueDeviceID\"][:12]}') for x in d]" 2>/dev/null || echo "  (none)"

list-devices: ## List connected USB devices with UDIDs
	@pymobiledevice3 usbmux list

clear: ## Clear simulated location on first connected device
	@cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && uv run clear.py

logs: ## Tail all logs
	@tail -f /tmp/pikmin-server.log /tmp/pikmin-ipad.log /tmp/pikmin-iphone.log /tmp/pikmin-tunneld.log 2>/dev/null
