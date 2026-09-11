"""Light one LED zone at a time so you can label which ring is which.

The captured themes tell us there are five zones and the order the app
selects them (4, 2, 3, 0, 1), but not which index is the top ring, the
middle inner/outer pair, or the bottom inner/outer pair. This walks them
one at a time and records what you see.

    python examples/zone_probe.py 192.168.1.249
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrava import ZONE_ORDER, BaravaDevice


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--colour", default="255,255,255",
                        help="r,g,b used to light each zone (default white)")
    parser.add_argument("--hold", type=float, default=0.0,
                        help="seconds to hold each zone; 0 waits for Enter")
    args = parser.parse_args()

    rgb = tuple(int(p) for p in args.colour.split(","))
    if len(rgb) != 3:
        parser.error("--colour needs three channels, e.g. 255,255,255")

    device = BaravaDevice(args.host)
    device.register()
    print(f"connected to {device.device_id or device.host}")
    print(f"lighting each zone in turn as rgb{rgb}\n")

    labels: dict[int, str] = {}
    try:
        for zone in ZONE_ORDER:
            device.set_zone_colors({zone: rgb})
            if args.hold:
                print(f"  zone {zone} lit")
                time.sleep(args.hold)
            else:
                seen = input(f"  zone {zone} lit -- which ring? ").strip()
                if seen:
                    labels[zone] = seen
    except KeyboardInterrupt:
        print("\nstopped early")
    finally:
        device.clear_zones()
        print("\nall zones cleared")

    if labels:
        print("\n" + "=" * 34)
        print("zone   ring")
        print("=" * 34)
        for zone in ZONE_ORDER:
            if zone in labels:
                print(f"{zone:>4}   {labels[zone]}")
        print("=" * 34)
        print("\nPaste this back and the zone constants can be named properly.")


if __name__ == "__main__":
    main()
