#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=4.14"]
# ///
"""
Clear any simulated location on the connected iPhone/iPad (both iOS 16
legacy and iOS 17+ DVT paths). Run this to snap the device back to real GPS.

    uv run clear.py                          # auto-detect
    uv run clear.py --udid ABC               # target device
    uv run clear.py --home 25.033,121.565    # teleport home first (fixes timezone)
"""
import argparse
import asyncio

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.simulate_location import DtSimulateLocation


DVT_ATTEMPT_TIMEOUT_S = 20
DVT_TIMEOUT_S = 150
WIFI_TUNNEL_TRIES = 4


def _tunneld_get(path: str, params: dict, timeout: float) -> int:
    """GET a tunneld control endpoint; returns HTTP status (0 on error)."""
    import urllib.parse
    import urllib.request
    from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS

    host, port = TUNNELD_DEFAULT_ADDRESS
    url = f"http://{host}:{port}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except Exception:
        return 0


async def _request_wifi_tunnel(udid: str) -> bool:
    """Ask tunneld to bring up a Wi-Fi tunnel (Bonjour-resolved, no cached IP).

    Same contract as server.py's _request_tunneld_wifi_tunnel: pin
    connection_type=wifi, never pass ip.

    The device advertises on several addresses (Wi-Fi IPv6, hotspot
    172.20.10.1, ...); tunneld 9.9.x races one tunnel per address and
    returns 404 if the first to finish failed. Intermittent → retry.
    """
    for attempt in range(WIFI_TUNNEL_TRIES):
        status = await asyncio.to_thread(
            _tunneld_get, "/start-tunnel", {"udid": udid, "connection_type": "wifi"}, 30.0
        )
        if status == 200:
            return True
        print(f"  ↳ tunneld /start-tunnel → {status or 'error'} (try {attempt + 1}/{WIFI_TUNNEL_TRIES})")
        await asyncio.sleep(1)
    return False


async def _cancel_tunnel(udid: str) -> None:
    await asyncio.to_thread(_tunneld_get, "/cancel", {"udid": udid}, 5.0)


async def _pick_rsd(udid: str | None):
    """Return the tunneld rsd for udid (or the first one), closing the rest."""
    from pymobiledevice3.tunneld.api import get_tunneld_devices, TUNNELD_DEFAULT_ADDRESS

    try:
        rsds = await get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS)
    except Exception as e:
        print(f"tunneld query failed: {e}")
        return None
    chosen = None
    for r in rsds:
        if chosen is None and (udid is None or r.udid == udid):
            chosen = r
        else:
            await r.close()
    return chosen


async def _clear_on_rsd(rsd, home: tuple[float, float] | None) -> bool:
    from contextlib import AsyncExitStack
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    async with AsyncExitStack() as stack:
        dvt = await stack.enter_async_context(DvtProvider(rsd))
        loc_sim = await stack.enter_async_context(LocationSimulation(dvt))
        print(f"device: {rsd.product_type} / iOS {rsd.product_version} (DVT)")

        if home:
            print(f"↳ teleporting to home ({home[0]:.4f}, {home[1]:.4f}) to reset timezone...")
            await loc_sim.set(home[0], home[1])
            await asyncio.sleep(5)

        await loc_sim.clear()
        print("✓ cleared — device is back on real GPS")
        return True


async def _clear_via_dvt(udid: str | None, home: tuple[float, float] | None) -> bool:
    """Try iOS 17+ DVT tunnel path. Returns True on success.

    With a udid, two recoveries make Wi-Fi reliable:
      - no tunnel listed for the device (USB just unplugged, tunneld idle)
        → ask tunneld for a Wi-Fi tunnel and use it immediately (it
        idle-closes in ~1-2s if unconsumed, so no sleep in between);
      - a listed tunnel that hangs or fails (stale after unplug / network
        change) → cancel it, request a fresh Wi-Fi tunnel, retry once.
    """
    try:
        import pymobiledevice3.services.dvt.instruments.location_simulation  # noqa: F401
    except ImportError as e:
        print(f"DVT path unavailable: {e}")
        return False

    for attempt in range(2):
        rsd = await _pick_rsd(udid)
        if rsd is None and udid:
            print("↳ no tunnel for device — requesting Wi-Fi tunnel from tunneld...")
            if await _request_wifi_tunnel(udid):
                rsd = await _pick_rsd(udid)
        if rsd is None:
            return False

        if int(rsd.product_version.split(".")[0]) < 17:
            await rsd.close()
            return False

        try:
            return await asyncio.wait_for(_clear_on_rsd(rsd, home), DVT_ATTEMPT_TIMEOUT_S)
        except Exception as e:
            reason = "timed out" if isinstance(e, asyncio.TimeoutError) else f"failed: {e}"
            print(f"DVT clear {reason} (stale tunnel?)")
        finally:
            try:
                await rsd.close()
            except Exception:
                pass

        if not udid or attempt:
            return False
        print("↳ cancelling stale tunnel and retrying over Wi-Fi...")
        await _cancel_tunnel(udid)
        if not await _request_wifi_tunnel(udid):
            return False
    return False


async def _clear_via_legacy(udid: str | None, home: tuple[float, float] | None) -> bool:
    """Try legacy lockdown path (iOS ≤16)."""
    try:
        lockdown = await (
            create_using_usbmux(serial=udid) if udid
            else create_using_usbmux()
        )
    except Exception:
        return False

    try:
        ios_major = int(lockdown.product_version.split(".")[0])
        if ios_major >= 17:
            print(f"device: {lockdown.product_type} / iOS {lockdown.product_version} — needs DVT (not legacy)")
            return False
        print(f"device: {lockdown.product_type} / iOS {lockdown.product_version} (legacy)")
        sim = DtSimulateLocation(lockdown)

        if home:
            print(f"↳ teleporting to home ({home[0]:.4f}, {home[1]:.4f}) to reset timezone...")
            await sim.set(home[0], home[1])
            await asyncio.sleep(5)

        await sim.clear()
        print("✓ cleared — device is back on real GPS")
        return True
    except Exception as e:
        print(f"legacy clear failed: {e}")
        return False
    finally:
        await lockdown.close()


def _parse_home(value: str) -> tuple[float, float]:
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("format: LAT,LON (e.g. 25.033,121.565)")
    return float(parts[0].strip()), float(parts[1].strip())


async def main() -> None:
    parser = argparse.ArgumentParser(description="Clear simulated GPS location")
    parser.add_argument("--udid", help="Target device UDID")
    parser.add_argument("--home", type=_parse_home,
                        help="Teleport here before clearing (LAT,LON) — fixes timezone stuck issue")
    args = parser.parse_args()

    # A tunnel that died (e.g. USB unplugged) can stay listed in tunneld for
    # minutes; connecting to it hangs forever. Bound the DVT attempt so
    # `make restart-*` never stalls here.
    try:
        if await asyncio.wait_for(_clear_via_dvt(args.udid, args.home), DVT_TIMEOUT_S):
            return
    except asyncio.TimeoutError:
        print(f"DVT clear timed out after {DVT_TIMEOUT_S}s (stale tunnel?)")
    if await _clear_via_legacy(args.udid, args.home):
        return
    print("✗ no reachable device — is tunneld running? is the device connected?")
    raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
