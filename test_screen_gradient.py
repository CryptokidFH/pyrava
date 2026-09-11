"""Throwaway test: screen colours -> pyrava gradient. Delete after verifying.

utils.get_dominant_colors() returns HSV tuples (hue 0-360, sat/val 0-1).
pyrava's set_gradient() wants RGB tuples (0-255 each). This bridges the two.

Muted source colours render faithfully -- and therefore palely -- on the
lamp's diffused LEDs. Pass --punch to boost saturation before sending.

    python test_screen_gradient.py 192.168.1.249
    python test_screen_gradient.py 192.168.1.249 --punch --zones lava_lamp
    python test_screen_gradient.py 192.168.1.249 --dry-run
"""

import argparse
import colorsys

from utils import get_dominant_colors

from pyrava import BaravaDevice, generate_gradient_stops, punch_color


def hsv_to_rgb255(h_deg, s, v):
    """HSV (hue 0-360, sat/val 0-1) -> RGB tuple 0-255."""
    r, g, b = colorsys.hsv_to_rgb(h_deg / 360.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--zones", default=None,
                        help="zone index or group name; default = all zones")
    parser.add_argument("--smooth", action="store_true")
    parser.add_argument("--punch", action="store_true",
                        help="boost saturation so muted colours read vividly")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    hsv_colors = get_dominant_colors(n_colors=args.n)
    rgb_colors = [hsv_to_rgb255(h, s, v) for (h, s, v) in hsv_colors]
    if args.punch:
        rgb_colors = [punch_color(c) for c in rgb_colors]
    print("RGB:", rgb_colors)
    print("stops:", generate_gradient_stops(rgb_colors))

    if args.dry_run:
        print("dry run -- nothing sent")
        return

    device = BaravaDevice(args.host)
    device.register()
    print(f"connected to {device.device_id or device.host}")

    zones = args.zones
    if zones is not None and zones.isdigit():
        zones = int(zones)

    if zones is not None:
        device.set_gradient(rgb_colors, zones=zones, smooth=args.smooth)
    else:
        device.set_gradient(rgb_colors, smooth=args.smooth)
    print("gradient sent")


if __name__ == "__main__":
    main()
