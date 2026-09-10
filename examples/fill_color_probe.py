"""Sweep the fill light's FCLR value so you can map it to real colours.

`set_fill_color()`'s hue conversion is inferred from a handful of spot
checks (see the client's docstring and the README), not from firmware
source. This script sends a sequence of raw values directly -- no RGB
conversion -- and asks what you saw at each one, so the mapping can be
confirmed or corrected.

    python examples/fill_color_probe.py 192.168.1.249
    python examples/fill_color_probe.py 192.168.1.249 --step 15
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrava import BaravaDevice
from pyrava.const import Handler, Var


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--step", type=float, default=30.0,
                         help="degrees between test values (default 30)")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--stop", type=float, default=360.0)
    parser.add_argument("--also-out-of-range", action="store_true",
                         help="also test a couple of values past 360, to "
                              "confirm the fallback-to-red behaviour")
    args = parser.parse_args()

    device = BaravaDevice(args.host)
    device.register()
    print(f"connected to {device.device_id or device.host}\n")
    device.set_fill_state(True)

    values = []
    v = args.start
    while v <= args.stop + 1e-9:
        values.append(round(v, 1))
        v += args.step
    if args.also_out_of_range:
        values += [400, 500]

    results: dict[float, str] = {}
    print("Press Enter after each colour change to record what you saw.")
    print("Type a description, or just Enter to skip. Ctrl-C to stop early.\n")

    try:
        for value in values:
            device.request(
                Handler.SET_FILL_COLOR, {Var.FILL_COLOR.value: int(round(value))},
                wait=False,
            )
            seen = input(f"  sent {value:>6} -> what colour? ").strip()
            if seen:
                results[value] = seen
    except KeyboardInterrupt:
        print("\nstopped early")

    if not results:
        print("\nNo observations recorded.")
        return

    print("\n" + "=" * 40)
    print("value    colour")
    print("=" * 40)
    for value, seen in sorted(results.items()):
        print(f"{value:>6}   {seen}")
    print("=" * 40)
    print(
        "\nIf this doesn't look like a smooth 0-360 hue progression, "
        "set_fill_hue()'s docstring and the README's confirmed-facts table "
        "need correcting -- paste this table back for that."
    )


if __name__ == "__main__":
    main()
