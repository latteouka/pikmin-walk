#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=4.14"]
# ///
"""
Clear any simulated location on the connected iPhone (both iOS 16 legacy
and iOS 17+ DVT paths). Run this to snap your phone back to real GPS.

    uv run clear.py
"""
import asyncio

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.simulate_location import DtSimulateLocation


async def main() -> None:
    lockdown = await create_using_usbmux()
    version = lockdown.product_version
    print(f"device: {lockdown.product_type} / iOS {version}")

    # iOS ≤16 path works via the legacy developer service, and iOS 17+
    # devices still expose it as a fallback, so this one call covers both.
    sim = DtSimulateLocation(lockdown)
    await sim.clear()
    print("✓ cleared — phone is back on real GPS")

    await lockdown.close()


if __name__ == "__main__":
    asyncio.run(main())
