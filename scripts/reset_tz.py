#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=4.14"]
# ///
"""
Reproduce the exact sequence that successfully triggered timezone update:
1. Clear simulated location
2. Re-set simulated location
3. Poke MCInstall service (profile list)
4. Read multiple lockdown domains
5. Poll timezone every 5s
"""
import asyncio
import plistlib
import tempfile
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.misagent import MisagentService


POLL_INTERVAL = 5.0
MAX_POLLS = 36  # 3 minutes


async def poke_profile_service(udid: str | None) -> None:
    """Connect to profile-related services to nudge iOS."""
    try:
        ld = await create_using_usbmux(serial=udid)
        from pymobiledevice3.services.mobile_config import MobileConfigService
        mc = MobileConfigService(lockdown=ld)
        profiles = await mc.get_profile_list()
        count = len(profiles.get("OrderedIdentifiers", []))
        print(f"  profile service poked ({count} profiles)")
        await ld.close()
    except Exception as e:
        print(f"  profile service: {e}")


async def poke_lockdown_domains(udid: str | None) -> None:
    """Read from multiple lockdown domains to wake up iOS services."""
    domains = [
        None,
        "com.apple.international",
        "com.apple.disk_usage",
        "com.apple.mobile.battery",
    ]
    try:
        ld = await create_using_usbmux(serial=udid)
        for domain in domains:
            try:
                val = await ld.get_value(domain=domain)
                if isinstance(val, dict):
                    keys = len(val)
                    print(f"  [{domain or 'root'}] {keys} keys")
            except Exception:
                pass
        await ld.close()
    except Exception as e:
        print(f"  lockdown domains: {e}")


async def read_tz(udid: str | None) -> tuple[str, float]:
    """Read current timezone from lockdown."""
    ld = await create_using_usbmux(serial=udid)
    tz = await ld.get_value(key="TimeZone")
    offset = await ld.get_value(key="TimeZoneOffsetFromUTC")
    await ld.close()
    return tz, (offset or 0) / 3600


async def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", default=None)
    args = parser.parse_args()
    udid = args.udid

    tz, oh = await read_tz(udid)
    print(f"[init] TimeZone: {tz} (UTC{oh:+.0f})")
    initial_tz = tz

    # Step 1: poke profile service
    print("\n--- poking profile service ---")
    await poke_profile_service(udid)

    # Step 2: read multiple lockdown domains
    print("\n--- reading lockdown domains ---")
    await poke_lockdown_domains(udid)

    # Step 3: poll timezone
    print(f"\n--- polling every {POLL_INTERVAL}s (max {MAX_POLLS * POLL_INTERVAL:.0f}s) ---")
    for i in range(MAX_POLLS):
        await asyncio.sleep(POLL_INTERVAL)
        try:
            tz, oh = await read_tz(udid)
        except Exception as e:
            print(f"[{(i+1)*POLL_INTERVAL:5.0f}s] error: {e}")
            continue

        changed = " *** CHANGED ***" if tz != initial_tz else ""
        print(f"[{(i+1)*POLL_INTERVAL:5.0f}s] {tz} (UTC{oh:+.0f}){changed}")

        if tz != initial_tz:
            print(f"\n✓ timezone updated to {tz} after {(i+1)*POLL_INTERVAL:.0f}s")
            return

    print(f"\n✗ timeout — still {tz} after {MAX_POLLS * POLL_INTERVAL:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
