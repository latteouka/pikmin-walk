"""Enable Wi-Fi debugging on an iOS device.

This sets `EnableWifiDebugging=True` in the device's wireless_lockdown
domain so Bonjour broadcasts on the real Wi-Fi interface (not just the
USB-tethered one). Without this, the Wi-Fi tunnel after USB-unplug never
shows up to mobdev2 browse.

The other two Wi-Fi setup steps (wifi-connections --state on, and
save-pair-record) are done via the pymobiledevice3 CLI from install.sh.

Usage: python wifi_setup.py <udid>
"""
import asyncio
import sys

from pymobiledevice3.lockdown import create_using_usbmux


async def main(udid: str) -> None:
    ld = await create_using_usbmux(serial=udid)
    try:
        await ld.set_value(
            domain='com.apple.mobile.wireless_lockdown',
            key='EnableWifiDebugging',
            value=True,
        )
        print(f"EnableWifiDebugging=True")
    finally:
        await ld.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python wifi_setup.py <udid>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
