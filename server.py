#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pymobiledevice3>=4.14",
#     "starlette>=1.0",
#     "uvicorn>=0.44",
#     "httpx>=0.28",
# ]
# ///
"""
Web UI for iOS location simulation.

Open http://localhost:7766 in a browser, click the map to drop waypoints,
pick a profile (walk/drive/transit/flight), hit Start, and watch the
phone (and the UI) move along the route in real time.

Prereq (terminal A, keeps running):
    sudo pymobiledevice3 remote tunneld

Run (terminal B):
    cd ~/projects/chores/pikmin-walk
    uv run server.py

Then open http://localhost:7766
"""
from __future__ import annotations

import asyncio
import json
import random
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import math
import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

import plistlib

from pymobiledevice3.bonjour import browse_mobdev2
from pymobiledevice3.common import get_home_folder
from pymobiledevice3.lockdown import create_using_tcp, create_using_usbmux
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.simulate_location import DtSimulateLocation
from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS, get_tunneld_devices

from pikmin_walk import PROFILES, circle_walk, haversine_m, random_walk, simulate

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"

# Runtime config — set by __main__ block before app starts.
# Per-device state (last_position, last_wifi_host): state-<udid>.json
# Shared state (bookmarks, google_maps_api_key): shared.json
TARGET_UDID: str | None = None
STATE_FILE: Path = HERE / "state.json"
SHARED_FILE: Path = HERE / "shared.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    data["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _read_state() -> dict:
    return _read_json(STATE_FILE)


def _write_state(state: dict) -> None:
    _write_json(STATE_FILE, state)


def _read_shared() -> dict:
    return _read_json(SHARED_FILE)


def _write_shared(data: dict) -> None:
    _write_json(SHARED_FILE, data)


def _migrate_to_shared() -> None:
    """One-time: pull bookmarks + api_key from any state*.json into shared.json."""
    if SHARED_FILE.exists():
        return
    shared = {}
    # Prefer original state.json if it has the data
    for candidate in [HERE / "state.json", *HERE.glob("state-*.json")]:
        d = _read_json(candidate)
        if not shared.get("bookmarks") and d.get("bookmarks"):
            shared["bookmarks"] = d["bookmarks"]
        if not shared.get("google_maps_api_key") and d.get("google_maps_api_key"):
            shared["google_maps_api_key"] = d["google_maps_api_key"]
        if shared.get("bookmarks") and shared.get("google_maps_api_key"):
            break
    if shared:
        _write_shared(shared)
        print(f"✓ migrated {len(shared.get('bookmarks', []))} bookmarks to shared.json")


_migrate_to_shared()


def _load_position() -> "tuple[float, float] | None":
    state = _read_state()
    pos = state.get("last_position")
    if not pos:
        return None
    try:
        return (float(pos["lat"]), float(pos["lon"]))
    except (KeyError, TypeError, ValueError):
        return None


def _save_position(pos: "tuple[float, float] | None") -> None:
    state = _read_state()
    if pos is None:
        state.pop("last_position", None)
    else:
        state["last_position"] = {"lat": pos[0], "lon": pos[1]}
    _write_state(state)


def _load_wifi_host() -> "str | None":
    return _read_state().get("last_wifi_host")


def _save_wifi_host(host: str) -> None:
    state = _read_state()
    state["last_wifi_host"] = host
    _write_state(state)


def _load_pair_records() -> "list[tuple[str, dict]]":
    """Load every *.plist in ~/.pymobiledevice3/ as (udid, record)."""
    home = get_home_folder()
    records = []
    for p in home.glob("*.plist"):
        if p.name.startswith("remote_"):
            continue
        udid = p.stem
        try:
            rec = plistlib.loads(p.read_bytes())
            records.append((udid, rec))
        except Exception:
            pass
    return records


def _addr_is_real_lan(addr: str) -> bool:
    """True for addresses that should survive USB unplug.

    Link-local IPv4 (169.254.x.x), link-local IPv6 (fe80::...), loopback
    and IPv6 loopback are all USB-adjacent or on-host — they die with
    the cable. Real LAN addresses (192.168.x, 10.x, 172.16-31.x) and
    global IPv6 survive.
    """
    a = addr.split("%", 1)[0]  # strip IPv6 zone id
    if a.startswith("fe80:") or a.startswith("169.254."):
        return False
    if a.startswith("127.") or a in {"::1", "0.0.0.0"}:
        return False
    return True


async def _try_wifi(stack: AsyncExitStack):
    """Try to connect to the phone via a real LAN address + saved pair record.

    Strategy:
      1. If state.json has a `last_wifi_host`, try it immediately — no
         Bonjour needed, sub-second startup.
      2. If that fails (phone DHCP changed, etc), fall back to a full
         Bonjour browse and try each real-LAN address found.
      3. Save the winning host to state.json for next time.
    """
    records = _load_pair_records()
    if not records:
        return None
    # Pick the pair record matching TARGET_UDID, or first one with WiFiMAC
    record = None
    if TARGET_UDID:
        for udid, rec in records:
            if udid == TARGET_UDID and "WiFiMACAddress" in rec:
                record = rec
                break
    else:
        for _, rec in records:
            if "WiFiMACAddress" in rec:
                record = rec
                break
    if record is None:
        return None

    async def _try_host(host: str, label: str):
        try:
            ld = await create_using_tcp(hostname=host, autopair=False, pair_record=record)
        except Exception:
            return None
        if not ld.paired:
            try:
                await ld.service.close()
            except Exception:
                pass
            return None
        print(f"  ↳ Wi-Fi lockdown via {host} ({label})")
        stack.push_async_callback(ld.service.close)
        _save_wifi_host(host)
        return ld

    cached = _load_wifi_host()
    if cached:
        ld = await _try_host(cached, "cached")
        if ld is not None:
            return ld

    try:
        answers = await browse_mobdev2(timeout=6)
    except Exception:
        return None

    wifi_mac = record.get("WiFiMACAddress", "")
    for answer in answers:
        if "@" not in answer.instance:
            continue
        if answer.instance.split("@", 1)[0] != wifi_mac:
            continue
        for addr in answer.addresses:
            if not _addr_is_real_lan(addr.ip):
                continue
            ld = await _try_host(addr.ip, addr.iface)
            if ld is not None:
                return ld

    return None


async def _try_mobdev2_linklocal(stack: AsyncExitStack):
    """Fallback: mobdev2 over USB-adjacent link-local interfaces."""
    records = _load_pair_records()
    if not records:
        return None
    if TARGET_UDID:
        records = [(u, r) for u, r in records if u == TARGET_UDID]
    by_mac = {rec["WiFiMACAddress"]: (udid, rec) for udid, rec in records
              if "WiFiMACAddress" in rec}
    if not by_mac:
        return None

    try:
        answers = await browse_mobdev2(timeout=5)
    except Exception:
        return None

    for answer in answers:
        if "@" not in answer.instance:
            continue
        wifi_mac = answer.instance.split("@", 1)[0]
        if wifi_mac not in by_mac:
            continue
        _, record = by_mac[wifi_mac]
        for addr in answer.addresses:
            try:
                ld = await create_using_tcp(
                    hostname=addr.ip,
                    autopair=False,
                    pair_record=record,
                )
            except Exception:
                continue
            if not ld.paired:
                try:
                    await ld.service.close()
                except Exception:
                    pass
                continue
            print(f"  ↳ mobdev2 link-local via {addr.ip}")
            stack.push_async_callback(ld.service.close)
            return ld

    return None


async def _maybe_save_pair_record(usb_lockdown) -> None:
    """Write a pair record to disk so future Wi-Fi attempts can use it."""
    try:
        home = get_home_folder()
        home.mkdir(parents=True, exist_ok=True)
        target = home / f"{usb_lockdown.udid}.plist"
        if target.exists():
            return  # already have one
        # pymobiledevice3 lockdown clients expose their pair record
        record = getattr(usb_lockdown, "pair_record", None)
        if record is None:
            return
        target.write_bytes(plistlib.dumps(record))
        print(f"  ↳ saved pair record to {target}")
    except Exception as e:
        print(f"  warn: could not save pair record: {e}")


class DeviceSession:
    """Holds the location-simulation channel to the phone for the whole
    server lifetime.

    Supports two transports depending on iOS version:

    * **iOS 17+** (``self.path == "dvt"``) — RemoteXPC tunnel via tunneld,
      then DVT instrument ``com.apple.instruments.server.services.LocationSimulation``.
      Requires ``sudo pymobiledevice3 remote tunneld`` in a separate terminal.

    * **iOS ≤16** (``self.path == "legacy"``) — classic usbmux/lockdown,
      then the developer service ``com.apple.dt.simulatelocation``.
      Requires Developer Mode ON and a mounted DeveloperDiskImage.
      No sudo, no tunneld.

    Both paths expose the same ``async set(lat, lon)`` / ``async clear()``
    API (they share ``LocationSimulationBase``), so the runner below is
    transport-agnostic.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.loc_sim = None  # LocationSimulation (DVT) or DtSimulateLocation (legacy)
        self.udid: str | None = None
        self.product: str | None = None
        self.path: str | None = None  # "dvt" or "legacy"
        self.running_task: asyncio.Task | None = None
        # Live speed — can be changed mid-run via WS "set_speed" message.
        self.live_speed_kmh: float = 19.0
        # Live radius — rwalk reads this each tick instead of profile.max_radius_m
        self.live_radius_m: float = 1000.0
        # Pause flag — runners check each tick and sleep until unpaused.
        self.paused: bool = False
        # Reconnect rate-limiting to prevent WS reconnect storm from spawning
        # overlapping Bonjour browses (which caused CPU spin).
        self._reconnect_lock = asyncio.Lock()
        self._last_reconnect_at: float = 0.0
        # Idle tracking for auto-shutdown
        self.last_activity: float = 0.0
        self.active_ws: int = 0
        # Last coordinate we successfully pushed to the phone. The iOS
        # simulate-location service is write-only, so this cache is the
        # only way the UI can answer "where is the device right now?".
        self.last_position: tuple[float, float] | None = None

    def touch(self) -> None:
        """Mark activity (called on any user action)."""
        self.last_activity = asyncio.get_event_loop().time()

    async def reconnect(self) -> bool:
        """Tear down the current session and try to reconnect.

        Serialized by a lock (one reconnect at a time) and rate-limited
        (min 10s between attempts) so a WS reconnect storm can't spawn
        overlapping Bonjour browses.
        """
        now = asyncio.get_event_loop().time()
        if self.loc_sim is not None:
            return True  # already connected
        if now - self._last_reconnect_at < 10.0:
            return False  # cooling down
        async with self._reconnect_lock:
            if self.loc_sim is not None:
                return True
            self._last_reconnect_at = asyncio.get_event_loop().time()
            print("⟳ reconnecting to device...")

            self.loc_sim = None
            self.path = None
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = AsyncExitStack()

            if TARGET_UDID:
                try:
                    from pymobiledevice3.services.mobile_image_mounter import (
                        auto_mount_developer,
                        AlreadyMountedError,
                    )
                    ld = await create_using_usbmux(serial=TARGET_UDID)
                    try:
                        await auto_mount_developer(ld, xcode=str(Path.home() / ".pmd_xcode"))
                        print("  ↳ DDI mounted")
                    except AlreadyMountedError:
                        pass
                    except Exception:
                        pass
                    await ld.close()
                except Exception:
                    pass

            try:
                await self.connect()
                print(f"✓ reconnected: {self.udid} via {self.path}")
                return True
            except Exception as e:
                print(f"✗ reconnect failed: {e}")
                return False

    async def set_location(self, lat: float, lon: float) -> None:
        """Push a coordinate to the phone. Auto-reconnects on failure."""
        for attempt in range(2):
            try:
                if self.loc_sim is None:
                    raise ConnectionError("not connected")
                await self.loc_sim.set(lat, lon)
                self.last_position = (lat, lon)
                _save_position(self.last_position)
                return
            except Exception as e:
                if attempt == 0:
                    print(f"set_location failed ({e}), attempting reconnect...")
                    if not await self.reconnect():
                        raise
                else:
                    raise

    async def clear_location(self) -> None:
        for attempt in range(2):
            try:
                if self.loc_sim is None:
                    raise ConnectionError("not connected")
                await self.loc_sim.clear()
                self.last_position = None
                _save_position(None)
                return
            except Exception as e:
                if attempt == 0:
                    print(f"clear failed ({e}), attempting reconnect...")
                    if not await self.reconnect():
                        raise
                else:
                    raise

    async def connect(self) -> None:
        # Try iOS 17+ tunneld first — if tunneld is up, a device is tunnelled
        # and we take the DVT fast path.
        rsds = []
        try:
            rsds = await get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS)
        except Exception:
            # tunneld not running / unreachable — fine, fall through to legacy
            pass

        if rsds:
            # Filter by TARGET_UDID if set (multi-device mode)
            if TARGET_UDID:
                matching = [r for r in rsds if r.udid == TARGET_UDID]
                others = [r for r in rsds if r.udid != TARGET_UDID]
                for o in others:
                    await o.close()
                rsds = matching
            if rsds:
                rsd = rsds[0]
                for extra in rsds[1:]:
                    await extra.close()
                # iOS 16 and older don't support DVT LocationSimulation —
                # tunneld might return them but set() silently no-ops.
                # Force those to the legacy path.
                ios_major = int(rsd.product_version.split(".")[0])
                if ios_major < 17:
                    await rsd.close()
                else:
                    dvt = await self._stack.enter_async_context(DvtProvider(rsd))
                    self.loc_sim = await self._stack.enter_async_context(LocationSimulation(dvt))
                    self.udid = rsd.udid
                    self.product = f"{rsd.product_type} / iOS {rsd.product_version}"
                    self.path = "dvt"
                    return

        # Legacy path (iOS ≤16). Three transports to try, in priority
        # order — we want to end up on a Wi-Fi lockdown session so that
        # unplugging USB doesn't lose the developer session (and iOS
        # doesn't revert the spoof).
        #
        #   1. Wi-Fi TCP to a real LAN address (192.168.x.x / 10.x.x.x /
        #      172.16-31.x.x). Survives USB unplug as long as the phone
        #      stays on Wi-Fi.
        #   2. mobdev2 link-local (USB-adjacent virtual interfaces:
        #      en12/en13, 169.254.x.x, fe80::). Works but dies on unplug.
        #   3. Plain usbmux (USB direct). Always works when plugged in,
        #      always dies on unplug.
        #
        # For iOS ≤16, prefer USB if available — Wi-Fi TCP lockdown can
        # silently fail on developer-service calls (missing escrow bag).
        # Fall back to Wi-Fi only when USB isn't plugged.
        lockdown = None
        try:
            lockdown = await (
                create_using_usbmux(serial=TARGET_UDID) if TARGET_UDID
                else create_using_usbmux()
            )
            self._stack.push_async_callback(lockdown.close)
            self.path = "legacy-usb"
            await _maybe_save_pair_record(lockdown)
        except Exception:
            lockdown = None

        if lockdown is None:
            lockdown = await _try_wifi(self._stack)
            if lockdown is not None:
                self.path = "legacy-wifi"
            else:
                lockdown = await _try_mobdev2_linklocal(self._stack)
                if lockdown is not None:
                    self.path = "legacy-mobdev2"
        if lockdown is None:
            raise RuntimeError("no device reachable via USB or Wi-Fi")

        self.udid = lockdown.udid
        self.product = f"{lockdown.product_type} / iOS {lockdown.product_version}"
        # DtSimulateLocation opens and closes its developer-service channel on
        # every .set() call, so there's no long-lived context to enter here.
        self.loc_sim = DtSimulateLocation(lockdown)

    async def stop_runner(self) -> None:
        task = self.running_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.running_task = None

    async def close(self) -> None:
        # Stop any running simulation, but DO NOT clear the spoofed
        # location — the phone keeps whatever was last set. Restarting
        # the server (e.g. to tune code) should leave you wherever you
        # already teleported to. Use the "回到真實 GPS" button or
        # `uv run clear.py` when you actually want to snap back.
        await self.stop_runner()
        await self._stack.aclose()


session = DeviceSession()


# --- HTTP routes ----------------------------------------------------------


async def index(request):
    return FileResponse(STATIC_DIR / "index.html")


async def walk_page(request):
    return FileResponse(STATIC_DIR / "walk.html")


async def config_get(request):
    shared = _read_shared()
    return JSONResponse({
        "google_maps_api_key": shared.get("google_maps_api_key", ""),
    })


async def config_post(request):
    body = await request.json()
    shared = _read_shared()
    if "google_maps_api_key" in body:
        shared["google_maps_api_key"] = str(body["google_maps_api_key"]).strip()
    _write_shared(shared)
    return JSONResponse({"ok": True})


async def bookmarks_get(request):
    return JSONResponse(_read_shared().get("bookmarks", []))


async def bookmarks_post(request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "need name, lat, lon"}, status_code=400)
    if not name:
        name = f"{lat:.4f}, {lon:.4f}"
    shared = _read_shared()
    bk = shared.setdefault("bookmarks", [])
    bk.append({"name": name, "lat": lat, "lon": lon})
    _write_shared(shared)
    return JSONResponse(bk)


async def bookmarks_patch(request):
    idx = int(request.path_params["idx"])
    body = await request.json()
    shared = _read_shared()
    bk = shared.get("bookmarks", [])
    if 0 <= idx < len(bk):
        if "name" in body:
            bk[idx]["name"] = str(body["name"]).strip()
        if "lat" in body:
            bk[idx]["lat"] = float(body["lat"])
        if "lon" in body:
            bk[idx]["lon"] = float(body["lon"])
        _write_shared(shared)
    return JSONResponse(bk)


async def bookmarks_delete(request):
    idx = int(request.path_params["idx"])
    shared = _read_shared()
    bk = shared.get("bookmarks", [])
    if 0 <= idx < len(bk):
        bk.pop(idx)
        _write_shared(shared)
    return JSONResponse(bk)


async def preview_loop(request):
    """Generate loop waypoints + OSRM route for preview (no walking yet)."""
    body = await request.json()
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "need lat, lon"}, status_code=400)

    shape = body.get("shape", "square")
    target_km = float(body.get("lap_distance_km", 8))

    road_factor = 1.5
    center = (lat, lon)
    rng = random.Random()
    from pikmin_walk import destination_point, haversine_m as _hav

    # Generate corner points with small random jitter (±15° angle, 80-120%
    # radius). The jitter prevents corners from consistently landing in
    # water bodies or forests — each preview attempt gets a slightly
    # different shape, and OSRM snaps each corner to the nearest road.
    def _jittered_corners(angles_radii):
        """angles_radii: list of (base_angle, base_radius_m)"""
        pts = []
        for angle, radius in angles_radii:
            a = angle + rng.gauss(0, math.radians(12))
            r = radius * (0.85 + rng.random() * 0.3)
            pts.append(destination_point(center, a, r))
        return pts

    if shape == "square":
        side_m = (target_km * 1000) / 4 / road_factor
        diag = math.sqrt(2) * side_m / 2
        base_angles_radii = [
            (math.pi / 4, diag), (3 * math.pi / 4, diag),
            (5 * math.pi / 4, diag), (7 * math.pi / 4, diag),
        ]
    elif shape == "rect":
        short_m = (target_km * 1000) / 6 / road_factor
        long_m = short_m * 2
        d = math.sqrt((long_m / 2) ** 2 + (short_m / 2) ** 2)
        a = math.atan2(short_m / 2, long_m / 2)
        base_angles_radii = [(a, d), (math.pi - a, d), (math.pi + a, d), (2 * math.pi - a, d)]
    else:  # circle
        geo_perimeter = (target_km * 1000) / road_factor
        radius_m = geo_perimeter / (2 * math.pi)
        n = max(6, int(geo_perimeter / 600))
        base_angles_radii = [(2 * math.pi * i / n, radius_m) for i in range(n)]

    corners = _jittered_corners(base_angles_radii)

    # Let OSRM freely optimize the loop — don't force user's position
    # as a waypoint. This gives OSRM full freedom to find the shortest
    # non-overlapping circuit. The walker will be teleported to the
    # route's start point before walking begins.
    waypoints = corners
    route = None
    for attempt in range(2):
        route = await _osrm_trip_route(waypoints)
        if route is not None:
            break
        route = await _osrm_loop_route(waypoints + [waypoints[0]])
        if route is not None:
            break
        corners = _jittered_corners(base_angles_radii)
        waypoints = corners

    if route is None:
        return JSONResponse({"error": "OSRM 無法規劃路線，試試縮小距離或換位置"})

    from pikmin_walk import haversine_m
    dist = sum(haversine_m(route[i], route[i + 1]) for i in range(len(route) - 1))

    return JSONResponse({
        "route": [[p[0], p[1]] for p in route],
        "waypoints": [[p[0], p[1]] for p in waypoints],
        "distance_km": dist / 1000,
        "shape": shape,
    })


async def profiles_endpoint(request):
    # Only expose random-walk profiles — route-based profiles were removed
    # from the UI. `rwalk` is the only one in this category.
    return JSONResponse(
        {
            name: {
                "label": p.label,
                "nominal_kmh": p.nominal_kmh,
                "tick_s": p.tick_s,
                "waypoint_dwell_s": p.waypoint_dwell_s,
                "position_jitter_m": p.position_jitter_m,
                "speed_jitter": p.speed_jitter,
                "max_radius_m": p.max_radius_m,
                "is_random_walk": p.max_radius_m > 0,
            }
            for name, p in PROFILES.items()
            if p.max_radius_m > 0
        }
    )


# --- WebSocket ------------------------------------------------------------


async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session.active_ws += 1
    session.touch()
    # Auto-reconnect if device dropped (screen off, USB unplug, etc)
    if session.loc_sim is None:
        await session.reconnect()
    await websocket.send_json(
        {
            "type": "hello",
            "device": (
                {
                    "udid": session.udid,
                    "product": session.product,
                    "path": session.path,
                }
                if session.loc_sim is not None
                else None
            ),
            "last_position": (
                {"lat": session.last_position[0], "lon": session.last_position[1]}
                if session.last_position is not None
                else None
            ),
        }
    )

    try:
        while True:
            msg = await websocket.receive_json()
            session.touch()
            action = msg.get("type")
            if action == "start":
                await _handle_start(websocket, msg)
            elif action == "start_road_walk":
                await _handle_start_road_walk(websocket, msg)
            elif action == "start_loop_walk":
                await _handle_start_loop_walk(websocket, msg)
            elif action == "stop":
                await _handle_stop(websocket)
            elif action == "teleport":
                await _handle_teleport(websocket, msg)
            elif action == "pause":
                session.paused = True
                await websocket.send_json({"type": "paused"})
            elif action == "resume":
                session.paused = False
                await websocket.send_json({"type": "resumed"})
            elif action == "set_speed":
                try:
                    session.live_speed_kmh = max(1.0, min(50.0, float(msg.get("speed_kmh", 19))))
                except (TypeError, ValueError):
                    pass
            elif action == "set_radius":
                try:
                    session.live_radius_m = max(1.0, min(10000.0, float(msg.get("radius_m", 1000))))
                except (TypeError, ValueError):
                    pass
            elif action == "clear":
                if session.loc_sim is not None:
                    try:
                        await session.clear_location()
                        await websocket.send_json({"type": "cleared"})
                    except Exception as e:
                        await websocket.send_json({"type": "error", "message": f"clear failed: {e}"})
            elif action == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        await session.stop_runner()
    finally:
        session.active_ws = max(0, session.active_ws - 1)
        session.touch()


async def _handle_start(ws: WebSocket, msg: dict) -> None:
    profile_name = msg.get("profile", "walk")
    raw_waypoints = msg.get("waypoints", [])
    try:
        waypoints = [(float(lat), float(lon)) for lat, lon in raw_waypoints]
    except (TypeError, ValueError):
        await ws.send_json({"type": "error", "message": "invalid waypoints"})
        return

    if profile_name not in PROFILES:
        await ws.send_json({"type": "error", "message": f"unknown profile {profile_name}"})
        return
    profile = PROFILES[profile_name]
    is_rwalk = profile.max_radius_m > 0

    # Route profiles need ≥2 waypoints; random walks only need a center.
    # For routes: if the user only tapped ONE destination, prepend the
    # phone's current position as the start point — "walk from here to
    # there" with a single click.
    if is_rwalk:
        if waypoints:
            center = waypoints[0]
        elif session.last_position is not None:
            center = session.last_position
        else:
            await ws.send_json(
                {"type": "error", "message": "隨機漫步需要先點地圖選起點，或先瞬移到某處"}
            )
            return
    else:
        if len(waypoints) == 1 and session.last_position is not None:
            # Single destination → start from current device position
            waypoints = [session.last_position, *waypoints]
        elif len(waypoints) == 0 and session.last_position is not None:
            await ws.send_json({"type": "error", "message": "點地圖選一個目的地"})
            return
        elif len(waypoints) < 2:
            await ws.send_json({"type": "error", "message": "先瞬移到某處，再點一個目的地"})
            return

    if session.running_task and not session.running_task.done():
        await ws.send_json({"type": "error", "message": "已經在跑了，先按停止"})
        return

    if is_rwalk:
        total_m = 0.0  # random walk has no pre-known total distance
    else:
        total_m = sum(haversine_m(a, b) for a, b in zip(waypoints, waypoints[1:]))

    # Resolve initial speed/radius from the UI's current slider values,
    # falling back to profile defaults if the client didn't include them.
    # Same clamp bounds as the live set_radius / set_speed WS handlers, so
    # all three entry points agree on what's a valid value.
    if is_rwalk:
        try:
            initial_radius_m = max(1.0, min(10000.0, float(msg.get("radius_m", profile.max_radius_m))))
        except (TypeError, ValueError):
            initial_radius_m = profile.max_radius_m
        try:
            initial_speed_kmh = max(1.0, min(50.0, float(msg.get("speed_kmh", profile.nominal_kmh))))
        except (TypeError, ValueError):
            initial_speed_kmh = profile.nominal_kmh
    else:
        initial_radius_m = 0.0
        initial_speed_kmh = profile.nominal_kmh

    async def runner() -> None:
        rng = random.Random()
        try:
            await ws.send_json(
                {
                    "type": "started",
                    "profile": profile_name,
                    "label": profile.label,
                    "total_m": total_m,
                    "nominal_kmh": initial_speed_kmh,
                    "is_random_walk": is_rwalk,
                    "center": {"lat": center[0], "lon": center[1]} if is_rwalk else None,
                    "radius_m": initial_radius_m if is_rwalk else 0.0,
                }
            )
            elapsed = 0.0
            if is_rwalk:
                session.live_radius_m = initial_radius_m
                session.live_speed_kmh = initial_speed_kmh
                # Tight loops use circle_walk; everything else diffuses
                # via random_walk. Both honor the live radius/speed
                # sliders.
                walker = circle_walk if profile_name == "circle" else random_walk
                ticks_iter = walker(
                    center, profile, rng,
                    get_radius=lambda: session.live_radius_m,
                    get_speed_kmh=lambda: session.live_speed_kmh,
                )
            else:
                ticks_iter = simulate(waypoints, profile, rng)
            for tick in ticks_iter:
                await session.set_location(*tick.position)
                await ws.send_json(
                    {
                        "type": "tick",
                        "lat": tick.position[0],
                        "lon": tick.position[1],
                        "elapsed": elapsed,
                        "dwell": tick.dwell_s,
                        "note": tick.note,
                    }
                )
                sleep_s = tick.dwell_s if tick.dwell_s > 0 else profile.tick_s
                await asyncio.sleep(sleep_s)
                elapsed += sleep_s
            await ws.send_json({"type": "done", "elapsed": elapsed})
        except asyncio.CancelledError:
            try:
                await ws.send_json({"type": "stopped"})
            except Exception:
                pass
            raise
        # No auto-clear in finally: stopping a simulation (or the runner
        # erroring out) leaves the phone at the last position we pushed.
        # If you want to snap back to real GPS, use the UI "回到真實 GPS"
        # button or `uv run clear.py` — both are explicit.

    session.running_task = asyncio.create_task(runner())


# --- OSRM Road Walk -----------------------------------------------------------

# Local OSRM instances (Docker, instant response)
# Each covers one region; _osrm_fetch tries all of them.
OSRM_LOCAL_INSTANCES = [
    "http://127.0.0.1:5050",   # Ontario
    "http://127.0.0.1:5051",   # Taiwan
]
# Public OSRM (last resort fallback)
OSRM_PUBLIC = "https://router.project-osrm.org"


async def _osrm_route(
    start: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float]] | None:
    """Query OSRM for a walking route. Returns list of (lat, lon) or None."""
    data = await _osrm_fetch(
        f"/route/v1/foot/{start[1]},{start[0]};{end[1]},{end[0]}"
        "?overview=full&geometries=geojson"
    )
    if data is None or not data.get("routes"):
        return None
    return [(c[1], c[0]) for c in data["routes"][0]["geometry"]["coordinates"]]


def _random_point_in_radius(
    center: tuple[float, float], radius_m: float, rng: random.Random
) -> tuple[float, float]:
    """Pick a uniformly random point within `radius_m` of `center`."""
    # Square-root for uniform area distribution
    r = radius_m * math.sqrt(rng.random())
    theta = rng.uniform(0, 2 * math.pi)
    from pikmin_walk import destination_point
    return destination_point(center, theta, r)


async def _handle_start_road_walk(ws: WebSocket, msg: dict) -> None:
    try:
        center_lat = float(msg["lat"])
        center_lon = float(msg["lon"])
    except (KeyError, TypeError, ValueError):
        await ws.send_json({"type": "error", "message": "need lat, lon"})
        return
    radius_m = float(msg.get("radius_m", 400))
    speed_kmh = float(msg.get("speed_kmh", 19))

    if session.running_task and not session.running_task.done():
        await ws.send_json({"type": "error", "message": "已經在跑了，先按停止"})
        return

    center = (center_lat, center_lon)
    session.live_speed_kmh = speed_kmh
    tick_s = 1.0

    async def runner() -> None:
        rng = random.Random()
        current = center
        try:
            await ws.send_json({"type": "road_walk_started"})

            while True:
                dest = _random_point_in_radius(center, radius_m, rng)
                route = await _osrm_route(current, dest)
                if route is None or len(route) < 2:
                    continue

                await ws.send_json({
                    "type": "road_walk_leg",
                    "route": [[p[0], p[1]] for p in route],
                })

                from pikmin_walk import haversine_m, step_toward, jitter_position
                pos = route[0]
                await session.set_location(*pos)

                for target in route[1:]:
                    while haversine_m(pos, target) > 1.0:
                        cur_speed_mps = session.live_speed_kmh * 1000 / 3600
                        step_m = cur_speed_mps * tick_s * (1 + rng.gauss(0, 0.10))
                        step_m = max(0.5, step_m)
                        pos = step_toward(pos, target, step_m)
                        noisy = jitter_position(pos, 1.0, rng)
                        await session.set_location(*noisy)
                        actual_step = haversine_m(pos, noisy)
                        await ws.send_json({
                            "type": "tick",
                            "lat": noisy[0],
                            "lon": noisy[1],
                            "step_m": step_m,
                        })
                        await asyncio.sleep(tick_s)
                        while session.paused:
                            await asyncio.sleep(0.5)

                # 5. Arrived at destination — it becomes next start
                current = pos

        except asyncio.CancelledError:
            try:
                await ws.send_json({"type": "stopped"})
            except Exception:
                pass
            raise

    session.running_task = asyncio.create_task(runner())


async def _generate_loop_waypoints(
    center: tuple[float, float], radius_m: float, num_points: int, rng: random.Random
) -> list[tuple[float, float]]:
    """Generate N waypoints in a rough circle around center, then close the loop."""
    from pikmin_walk import destination_point
    points = []
    base_angle = rng.uniform(0, 2 * math.pi)
    for i in range(num_points):
        angle = base_angle + (2 * math.pi * i / num_points)
        # ±15% radius jitter + ±15° angle jitter for natural shape
        r = radius_m * (0.7 + rng.random() * 0.6)
        a = angle + rng.gauss(0, math.radians(15))
        points.append(destination_point(center, a, r))
    points.append(points[0])  # close the loop
    return points


async def _osrm_fetch(path: str) -> dict | None:
    """Try all local OSRM instances first, then public. Returns parsed JSON or None.

    Each local instance covers a different region. An instance that doesn't
    cover the requested coordinates may still return code:"Ok" but with
    distance=0 (all points snapped to the same phantom node). We check
    for meaningful distance before accepting a result.
    """
    all_servers = OSRM_LOCAL_INSTANCES + [OSRM_PUBLIC]
    for base in all_servers:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base}{path}")
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != "Ok":
                    continue
                # Check for meaningful distance — an out-of-coverage instance
                # returns Ok but distance=0 (all coords snap to one point).
                dist = 0.0
                if data.get("routes"):
                    dist = data["routes"][0].get("distance", 0)
                elif data.get("trips"):
                    dist = data["trips"][0].get("distance", 0)
                if dist < 10:  # less than 10m = not a real route
                    continue
                return data
        except Exception:
            continue
    return None


async def _osrm_trip_route(
    waypoints: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    """OSRM Trip API (TSP solver) for optimal round-trip loops."""
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    data = await _osrm_fetch(
        f"/trip/v1/foot/{coords_str}?roundtrip=true&geometries=geojson&overview=full"
    )
    if data is None or not data.get("trips"):
        return None
    return [(c[1], c[0]) for c in data["trips"][0]["geometry"]["coordinates"]]


async def _osrm_trip_order(
    waypoints: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    """Return waypoints reordered by OSRM Trip TSP, no geometry.

    Unlike _osrm_trip_route which returns the routed polyline along roads,
    this function asks OSRM only for the optimal *visit order* of the
    given points. The caller is expected to connect them with great-circle
    lines themselves (花朵巡航 wants direct lines between flowers, not
    road geometry).

    Uses source=first to pin the starting point — without it OSRM is free
    to pick any waypoint as the start, which makes the order non-stable
    across previews of the same input.
    """
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    data = await _osrm_fetch(
        f"/trip/v1/foot/{coords_str}?roundtrip=true&source=first&overview=false"
    )
    if data is None or not data.get("waypoints"):
        return None
    # data["waypoints"][i] corresponds to input[i]; its waypoint_index is
    # the position in the optimized trip. Sort input indices by that.
    order = sorted(
        range(len(waypoints)),
        key=lambda i: data["waypoints"][i]["waypoint_index"],
    )
    return [waypoints[i] for i in order]


async def _osrm_loop_route(
    waypoints: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    """OSRM Route API for ordered waypoint loop."""
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    data = await _osrm_fetch(
        f"/route/v1/foot/{coords_str}?overview=full&geometries=geojson&continue_straight=true"
    )
    if data is None or not data.get("routes"):
        return None
    return [(c[1], c[0]) for c in data["routes"][0]["geometry"]["coordinates"]]


async def _handle_start_loop_walk(ws: WebSocket, msg: dict) -> None:
    """Walk a pre-computed route (from /api/preview-loop) in an infinite loop."""
    raw_route = msg.get("route")
    speed_kmh = float(msg.get("speed_kmh", 19))

    if not raw_route or len(raw_route) < 2:
        await ws.send_json({"type": "error", "message": "先按「預覽路線」"})
        return
    if session.running_task and not session.running_task.done():
        await ws.send_json({"type": "error", "message": "已經在跑了，先按停止"})
        return

    route = [(float(p[0]), float(p[1])) for p in raw_route]
    session.live_speed_kmh = speed_kmh  # initialize from the start request
    tick_s = 1.0

    async def runner() -> None:
        rng = random.Random()
        from pikmin_walk import haversine_m, step_toward, jitter_position

        try:
            loop_dist = sum(haversine_m(route[i], route[i + 1]) for i in range(len(route) - 1))

            # Teleport to route start before walking
            await session.set_location(*route[0])

            await ws.send_json({
                "type": "loop_walk_started",
                "loop_route": [[p[0], p[1]] for p in route],
                "loop_distance_km": loop_dist / 1000,
            })

            lap = 0
            while True:
                lap += 1
                await ws.send_json({"type": "loop_lap", "lap": lap})

                pos = route[0]
                await session.set_location(*pos)

                for target in route[1:]:
                    while haversine_m(pos, target) > 1.0:
                        # Read live speed each tick so slider changes apply instantly
                        cur_speed_mps = session.live_speed_kmh * 1000 / 3600
                        step_m = cur_speed_mps * tick_s * (1 + rng.gauss(0, 0.10))
                        step_m = max(0.5, step_m)
                        pos = step_toward(pos, target, step_m)
                        noisy = jitter_position(pos, 1.0, rng)
                        await session.set_location(*noisy)
                        await ws.send_json({
                            "type": "tick",
                            "lat": noisy[0],
                            "lon": noisy[1],
                            "step_m": step_m,
                        })
                        await asyncio.sleep(tick_s)
                        while session.paused:
                            await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            try:
                await ws.send_json({"type": "stopped"})
            except Exception:
                pass
            raise

    session.running_task = asyncio.create_task(runner())


async def _handle_teleport(ws: WebSocket, msg: dict) -> None:
    if session.running_task and not session.running_task.done():
        await ws.send_json({"type": "error", "message": "模擬執行中，先按停止再瞬移"})
        return
    try:
        lat = float(msg["lat"])
        lon = float(msg["lon"])
    except (KeyError, TypeError, ValueError):
        await ws.send_json({"type": "error", "message": "invalid teleport payload"})
        return
    try:
        await session.set_location(lat, lon)
    except Exception as e:
        await ws.send_json({"type": "error", "message": f"teleport failed: {e}"})
        return
    await ws.send_json({"type": "teleported", "lat": lat, "lon": lon})


async def _handle_stop(ws: WebSocket) -> None:
    await session.stop_runner()
    try:
        await ws.send_json({"type": "stopped"})
    except Exception:
        pass


# --- Lifespan + app -------------------------------------------------------


# Idle timeout (minutes). Server self-exits after this much time with
# no connected WS clients and no running simulation task. Set via
# --idle-minutes CLI arg (default 30). 0 disables auto-shutdown.
IDLE_MINUTES: float = 30.0


async def _idle_watchdog() -> None:
    """Background task: exit process when idle for too long."""
    if IDLE_MINUTES <= 0:
        return
    while True:
        await asyncio.sleep(60)
        now = asyncio.get_event_loop().time()
        idle = now - session.last_activity
        has_work = session.active_ws > 0 or (
            session.running_task and not session.running_task.done()
        )
        if idle > IDLE_MINUTES * 60 and not has_work:
            print(f"💤 idle {idle/60:.0f}m, no clients, no task — shutting down")
            import os, signal
            os.kill(os.getpid(), signal.SIGTERM)
            return


@asynccontextmanager
async def lifespan(app):
    cached = _load_position()
    if cached is not None:
        session.last_position = cached
        print(f"✓ restored last_position {cached[0]:.5f}, {cached[1]:.5f} from state.json")

    try:
        await session.connect()
        print(f"✓ device connected: {session.udid} ({session.product}) via {session.path}")
    except Exception as e:
        print(f"⚠ could not connect to device: {e}")
        print("  iOS 17+ : sudo pymobiledevice3 remote tunneld")
        print("  iOS ≤16 : enable Developer Mode on the phone + mount DDI")

    # Start watchdog
    session.last_activity = asyncio.get_event_loop().time()
    watchdog = asyncio.create_task(_idle_watchdog())

    yield

    watchdog.cancel()
    try:
        await watchdog
    except (asyncio.CancelledError, Exception):
        pass
    await session.close()
    print("✓ closed device session")


app = Starlette(
    debug=False,
    routes=[
        Route("/", index),
        Route("/walk", walk_page),
        Route("/api/preview-loop", preview_loop, methods=["POST"]),
        Route("/api/profiles", profiles_endpoint),
        Route("/api/config", config_get, methods=["GET"]),
        Route("/api/config", config_post, methods=["POST"]),
        Route("/api/bookmarks", bookmarks_get, methods=["GET"]),
        Route("/api/bookmarks", bookmarks_post, methods=["POST"]),
        Route("/api/bookmarks/{idx:int}", bookmarks_patch, methods=["PATCH"]),
        Route("/api/bookmarks/{idx:int}", bookmarks_delete, methods=["DELETE"]),
        WebSocketRoute("/ws", ws_endpoint),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7766, help="HTTP port")
    parser.add_argument("--udid", type=str, default=None, help="Device UDID (pins session to one device)")
    parser.add_argument("--idle-minutes", type=float, default=30.0,
                        help="Auto-exit after N minutes idle (0 = disabled)")
    args = parser.parse_args()

    TARGET_UDID = args.udid
    IDLE_MINUTES = args.idle_minutes
    if TARGET_UDID:
        # Separate state file per device so bookmarks/last_position don't collide
        STATE_FILE = HERE / f"state-{TARGET_UDID[:12]}.json"
        print(f"Target device: {TARGET_UDID}")
        print(f"State file: {STATE_FILE.name}")

    print(f"Pikmin Walker UI  →  http://localhost:{args.port}")
    # loop=asyncio (stdlib) — avoid uvloop's UDPTransport bugs when
    # Bonjour browse connections fail. watchfiles never loads without reload.
    uvicorn.run(app, host="127.0.0.1", port=args.port,
                log_level="warning", loop="asyncio")
