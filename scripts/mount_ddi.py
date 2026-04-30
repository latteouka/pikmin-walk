"""Mount iOS DeveloperDiskImage. Usage: python mount_ddi.py <udid>"""
import asyncio
import sys
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobile_image_mounter import auto_mount_developer
from pymobiledevice3.exceptions import AlreadyMountedError


async def main(udid: str) -> None:
    lockdown = await create_using_usbmux(serial=udid)
    try:
        await auto_mount_developer(lockdown, xcode=str(Path.home() / ".pmd_xcode"))
        print("✓ DDI mounted")
    except AlreadyMountedError:
        print("✓ DDI already mounted")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python mount_ddi.py <udid>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
