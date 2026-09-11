"""Light each named LED zone in turn, to sanity-check the confirmed mapping.

Zone identity was confirmed by running this against real hardware: 4 is the
top ring, 2/3 are the middle ring's inner/outer, 0/1 are the bottom ring's
inner/outer -- see pyrava.const.Zone. This script now exists mainly to
re-verify that mapping still holds (e.g. after a firmware update) rather
than to discover it.

    python examples/zone_probe.py 192.168.1.249
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrava import BaravaDevice
from pyrava.const import Zone

ZONE_ORDER = (Zone.TOP, Zone.MIDDLE_INNER, Zone.MIDDLE_OUTER,
              Zone.BOTTOM_INNER, Zone.BOTTOM_OUTER)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--colour", default="255,255,255",
                        help="r,g,b used to light each zone (default white)")
    parser.add_argument("--hold", type=float, default=1.0,
                        help="seconds to hold each zone (default 1)")
    args = parser.parse_args()

    rgb = tuple(int(p) for p in args.colour.split(","))
    if len(rgb) != 3:
        parser.error("--colour needs three channels, e.g. 255,255,255")

    device = BaravaDevice(args.host)
    device.register()
    print(f"connected to {device.device_id or device.host}")
    print(f"expected mapping: {[(z.value, z.name) for z in ZONE_ORDER]}\n")

    try:
        for zone in ZONE_ORDER:
            print(f"  {zone.name} (index {zone.value}) lit as rgb{rgb}")
            device.set_zone_colors({zone: rgb})
            time.sleep(args.hold)
    except KeyboardInterrupt:
        print("\nstopped early")
    finally:
        device.clear_zones()
        print("\nall zones cleared")

    print("\nIf a name didn't match what lit up, pyrava.const.Zone needs updating.")


if __name__ == "__main__":
    main()
