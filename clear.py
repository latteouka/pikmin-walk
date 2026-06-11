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


async def _clear_via_dvt(udid: str | None, home: tuple[float, float] | None) -> bool:
    """Try iOS 17+ DVT tunnel path. Returns True on success."""
    try:
        from pymobiledevice3.tunneld import get_tunneld_devices, TUNNELD_DEFAULT_ADDRESS
        from pymobiledevice3.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.dvt.dvt_provider import DvtProvider
        from contextlib import AsyncExitStack
    except ImportError:
        return False

    try:
        rsds = await get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS)
    except Exception:
        return False

    if not rsds:
        return False

    if udid:
        matching = [r for r in rsds if r.udid == udid]
        for r in rsds:
            if r.udid != udid:
                await r.close()
        rsds = matching

    if not rsds:
        return False

    rsd = rsds[0]
    for extra in rsds[1:]:
        await extra.close()

    ios_major = int(rsd.product_version.split(".")[0])
    if ios_major < 17:
        await rsd.close()
        return False

    async with AsyncExitStack() as stack:
        try:
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
        except Exception as e:
            print(f"DVT clear failed: {e}")
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

    if await _clear_via_dvt(args.udid, args.home):
        return
    if await _clear_via_legacy(args.udid, args.home):
        return
    print("✗ no reachable device — is tunneld running? is the device connected?")
    raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
