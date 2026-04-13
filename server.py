#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pymobiledevice3>=4.14",
#     "starlette>=1.0",
#     "uvicorn[standard]>=0.44",
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

from pikmin_walk import PROFILES, haversine_m, random_walk, simulate

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
STATE_FILE = HERE / "state.json"


def _read_state() -> dict:
    """Read the on-disk state blob, or {} if missing/corrupt."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    """Atomically write the state blob to disk."""
    state["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


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
    # Pick the first record with WiFiMACAddress — typically only one phone
    record = None
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

    # 1. Fast path: try cached host first
    cached = _load_wifi_host()
    if cached:
        ld = await _try_host(cached, "cached")
        if ld is not None:
            return ld

    # 2. Bonjour browse — extract real-LAN addresses
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
        # Last coordinate we successfully pushed to the phone. The iOS
        # simulate-location service is write-only, so this cache is the
        # only way the UI can answer "where is the device right now?".
        self.last_position: tuple[float, float] | None = None

    async def set_location(self, lat: float, lon: float) -> None:
        """Push a coordinate to the phone and remember it (disk + memory)."""
        assert self.loc_sim is not None
        await self.loc_sim.set(lat, lon)
        self.last_position = (lat, lon)
        _save_position(self.last_position)

    async def clear_location(self) -> None:
        assert self.loc_sim is not None
        await self.loc_sim.clear()
        self.last_position = None
        _save_position(None)

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
            rsd = rsds[0]
            for extra in rsds[1:]:
                await extra.close()
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
        # Wi-Fi requires a previously-saved pair record. On first USB
        # connection we auto-save one into ~/.pymobiledevice3/<udid>.plist
        # so subsequent restarts can go straight to Wi-Fi.
        lockdown = await _try_wifi(self._stack)
        if lockdown is not None:
            self.path = "legacy-wifi"
        else:
            lockdown = await _try_mobdev2_linklocal(self._stack)
            if lockdown is not None:
                self.path = "legacy-mobdev2"
            else:
                lockdown = await create_using_usbmux()
                self._stack.push_async_callback(lockdown.close)
                self.path = "legacy-usb"
                # Opportunistically persist a pair record so the next
                # server restart can pick Wi-Fi without needing USB.
                await _maybe_save_pair_record(lockdown)

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
    state = _read_state()
    return JSONResponse({
        "google_maps_api_key": state.get("google_maps_api_key", ""),
    })


async def config_post(request):
    body = await request.json()
    state = _read_state()
    if "google_maps_api_key" in body:
        state["google_maps_api_key"] = str(body["google_maps_api_key"]).strip()
    _write_state(state)
    return JSONResponse({"ok": True})


async def bookmarks_get(request):
    state = _read_state()
    return JSONResponse(state.get("bookmarks", []))


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
    state = _read_state()
    bk = state.setdefault("bookmarks", [])
    bk.append({"name": name, "lat": lat, "lon": lon})
    _write_state(state)
    return JSONResponse(bk)


async def bookmarks_patch(request):
    idx = int(request.path_params["idx"])
    body = await request.json()
    state = _read_state()
    bk = state.get("bookmarks", [])
    if 0 <= idx < len(bk):
        if "name" in body:
            bk[idx]["name"] = str(body["name"]).strip()
        if "lat" in body:
            bk[idx]["lat"] = float(body["lat"])
        if "lon" in body:
            bk[idx]["lon"] = float(body["lon"])
        _write_state(state)
    return JSONResponse(bk)


async def bookmarks_delete(request):
    idx = int(request.path_params["idx"])
    state = _read_state()
    bk = state.get("bookmarks", [])
    if 0 <= idx < len(bk):
        bk.pop(idx)
        _write_state(state)
    return JSONResponse(bk)


async def profiles_endpoint(request):
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
        }
    )


# --- WebSocket ------------------------------------------------------------


async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
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
        # If the browser goes away mid-run, stop the spoof so the phone
        # doesn't get stuck at a fake location.
        await session.stop_runner()


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

    if session.loc_sim is None:
        await ws.send_json(
            {
                "type": "error",
                "message": (
                    "手機還沒連上。iOS 17+ 需要 `sudo pymobiledevice3 remote tunneld`；"
                    "iOS ≤16 需要開發者模式開啟 + DDI 已掛載。"
                ),
            }
        )
        return
    if session.running_task and not session.running_task.done():
        await ws.send_json({"type": "error", "message": "已經在跑了，先按停止"})
        return

    if is_rwalk:
        total_m = 0.0  # random walk has no pre-known total distance
    else:
        total_m = sum(haversine_m(a, b) for a, b in zip(waypoints, waypoints[1:]))

    async def runner() -> None:
        rng = random.Random()
        try:
            await ws.send_json(
                {
                    "type": "started",
                    "profile": profile_name,
                    "label": profile.label,
                    "total_m": total_m,
                    "nominal_kmh": profile.nominal_kmh,
                    "is_random_walk": is_rwalk,
                    "center": {"lat": center[0], "lon": center[1]} if is_rwalk else None,
                    "radius_m": profile.max_radius_m if is_rwalk else 0.0,
                }
            )
            elapsed = 0.0
            ticks_iter = (
                random_walk(center, profile, rng) if is_rwalk
                else simulate(waypoints, profile, rng)
            )
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

OSRM_BASE = "https://router.project-osrm.org/route/v1/foot"


async def _osrm_route(
    start: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float]] | None:
    """Query OSRM for a walking route. Returns list of (lat, lon) or None."""
    url = (
        f"{OSRM_BASE}/{start[1]},{start[0]};{end[1]},{end[0]}"
        "?overview=full&geometries=geojson"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        coords = data["routes"][0]["geometry"]["coordinates"]
        # GeoJSON is [lon, lat] — flip to (lat, lon)
        return [(c[1], c[0]) for c in coords]
    except Exception:
        return None


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

    if session.loc_sim is None:
        await ws.send_json({"type": "error", "message": "手機未連線"})
        return
    if session.running_task and not session.running_task.done():
        await ws.send_json({"type": "error", "message": "已經在跑了，先按停止"})
        return

    center = (center_lat, center_lon)
    speed_mps = speed_kmh * 1000 / 3600
    tick_s = 1.0

    async def runner() -> None:
        rng = random.Random()
        current = center
        try:
            await ws.send_json({"type": "road_walk_started"})

            while True:
                # 1. Pick a random destination within radius
                dest = _random_point_in_radius(center, radius_m, rng)

                # 2. Get a road-following route from OSRM
                route = await _osrm_route(current, dest)
                if route is None or len(route) < 2:
                    # OSRM couldn't route (maybe in the ocean) — try again
                    continue

                # 3. Send planned route to UI
                await ws.send_json({
                    "type": "road_walk_leg",
                    "route": [[p[0], p[1]] for p in route],
                })

                # 4. Walk along the route waypoints
                from pikmin_walk import haversine_m, step_toward, jitter_position
                pos = route[0]
                await session.set_location(*pos)

                for target in route[1:]:
                    while haversine_m(pos, target) > 1.0:
                        step_m = speed_mps * tick_s * (1 + rng.gauss(0, 0.10))
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


async def _osrm_loop_route(
    waypoints: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    """Query OSRM for a walking route through all waypoints (loop)."""
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    url = f"{OSRM_BASE}/{coords_str}?overview=full&geometries=geojson&continue_straight=true"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        coords = data["routes"][0]["geometry"]["coordinates"]
        return [(c[1], c[0]) for c in coords]
    except Exception:
        return None


async def _handle_start_loop_walk(ws: WebSocket, msg: dict) -> None:
    try:
        center_lat = float(msg["lat"])
        center_lon = float(msg["lon"])
    except (KeyError, TypeError, ValueError):
        await ws.send_json({"type": "error", "message": "need lat, lon"})
        return
    radius_m = float(msg.get("radius_m", 5000))
    speed_kmh = float(msg.get("speed_kmh", 19))
    num_points = int(msg.get("num_points", 6))
    num_points = max(3, min(num_points, 16))

    if session.loc_sim is None:
        await ws.send_json({"type": "error", "message": "手機未連線"})
        return
    if session.running_task and not session.running_task.done():
        await ws.send_json({"type": "error", "message": "已經在跑了，先按停止"})
        return

    center = (center_lat, center_lon)
    speed_mps = speed_kmh * 1000 / 3600
    tick_s = 1.0

    async def runner() -> None:
        rng = random.Random()
        from pikmin_walk import haversine_m, step_toward, jitter_position

        try:
            # 1. Generate loop waypoints
            loop_wps = await _generate_loop_waypoints(center, radius_m, num_points, rng)

            # 2. Get OSRM route for the whole loop
            await ws.send_json({"type": "road_walk_leg", "route": [[p[0], p[1]] for p in loop_wps]})
            route = await _osrm_loop_route(loop_wps)
            if route is None or len(route) < 2:
                await ws.send_json({"type": "error", "message": "OSRM 無法規劃此循環路線，換個位置試試"})
                return

            # Calculate loop distance
            loop_dist = sum(
                haversine_m(route[i], route[i + 1]) for i in range(len(route) - 1)
            )

            await ws.send_json({
                "type": "loop_walk_started",
                "loop_route": [[p[0], p[1]] for p in route],
                "loop_distance_km": loop_dist / 1000,
                "num_points": num_points,
            })

            # 3. Walk the loop forever
            lap = 0
            while True:
                lap += 1
                await ws.send_json({"type": "loop_lap", "lap": lap})

                pos = route[0]
                await session.set_location(*pos)

                for target in route[1:]:
                    while haversine_m(pos, target) > 1.0:
                        step_m = speed_mps * tick_s * (1 + rng.gauss(0, 0.10))
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

        except asyncio.CancelledError:
            try:
                await ws.send_json({"type": "stopped"})
            except Exception:
                pass
            raise

    session.running_task = asyncio.create_task(runner())


async def _handle_teleport(ws: WebSocket, msg: dict) -> None:
    if session.loc_sim is None:
        await ws.send_json({"type": "error", "message": "手機還沒連上"})
        return
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


@asynccontextmanager
async def lifespan(app):
    # Restore the last-known position from disk BEFORE connecting, so the
    # hello message reflects cached state even if the device link hiccups.
    # We intentionally do NOT push this back to the phone at startup — the
    # phone should have kept the spoof itself (iOS persists it across
    # session drops as long as a paired tool reconnects in time), and we
    # avoid clobbering whatever the real current state is.
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
    yield
    await session.close()
    print("✓ closed device session")


app = Starlette(
    debug=False,
    routes=[
        Route("/", index),
        Route("/walk", walk_page),
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
    print("Pikmin Walker UI  →  http://localhost:7766")
    uvicorn.run(app, host="127.0.0.1", port=7766, log_level="warning")
