.PHONY: start stop restart status tunnel kill-tunnel clear logs help

# Default port
PORT := 7766

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

start: tunnel ## Start everything (tunneld + server)
	@if lsof -iTCP:$(PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "server already running on :$(PORT)"; \
	else \
		echo "starting server on :$(PORT)..."; \
		cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && \
		nohup uv run server.py > /tmp/pikmin-server.log 2>&1 & \
		sleep 5; \
		if lsof -iTCP:$(PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
			echo "✓ server up → http://localhost:$(PORT)"; \
		else \
			echo "✗ server failed to start — check: cat /tmp/pikmin-server.log"; \
		fi; \
	fi

tunnel: ## Start tunneld if not running (needs sudo)
	@if pgrep -f "pymobiledevice3.*tunneld" >/dev/null 2>&1; then \
		echo "✓ tunneld already running"; \
	else \
		echo "starting tunneld (needs sudo)..."; \
		sudo nohup pymobiledevice3 remote tunneld > /tmp/pikmin-tunneld.log 2>&1 & \
		sleep 3; \
		if pgrep -f "pymobiledevice3.*tunneld" >/dev/null 2>&1; then \
			echo "✓ tunneld up"; \
		else \
			echo "✗ tunneld failed — check: cat /tmp/pikmin-tunneld.log"; \
		fi; \
	fi

stop: ## Stop server (keep tunneld running)
	@pkill -f "uv run server.py" 2>/dev/null && echo "✓ server stopped" || echo "server not running"

kill-tunnel: ## Stop tunneld
	@sudo pkill -f "pymobiledevice3.*tunneld" 2>/dev/null && echo "✓ tunneld stopped" || echo "tunneld not running"

restart: stop ## Restart server
	@sleep 1
	@$(MAKE) start

status: ## Show running status
	@echo "== tunneld =="
	@if pgrep -f "pymobiledevice3.*tunneld" >/dev/null 2>&1; then \
		echo "  ✓ running (PID $$(pgrep -f 'pymobiledevice3.*tunneld' | head -1))"; \
	else \
		echo "  ✗ not running"; \
	fi
	@echo "== server =="
	@if lsof -iTCP:$(PORT) -sTCP:LISTEN -nP >/dev/null 2>&1; then \
		echo "  ✓ running on :$(PORT)"; \
	else \
		echo "  ✗ not running"; \
	fi
	@echo "== device =="
	@pymobiledevice3 usbmux list 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  USB: {x[\"DeviceName\"]} ({x[\"ProductType\"]}/{x[\"ProductVersion\"]})') for x in d]" 2>/dev/null || echo "  (no USB)"

clear: ## Clear simulated location (back to real GPS)
	@cd $(dir $(abspath $(lastword $(MAKEFILE_LIST)))) && uv run clear.py

logs: ## Tail server + tunneld logs
	@tail -f /tmp/pikmin-server.log /tmp/pikmin-tunneld.log
