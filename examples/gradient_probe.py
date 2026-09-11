"""Figure out how the device actually interprets gradient stop bytes.

Our gradient decode is structurally right (pos + 3 bytes per stop), but it's
unclear whether those 3 bytes are literal RGB, or hue/sat-ish values where
magnitude is ignored. Two hardware observations suggest the latter:

  * set_fill_color((68,43,43)) shows full red -- hue picks the max channel
    and discards how much bigger it is.
  * A gradient of [(61,169,240),(68,43,43)] showed white->blue, not
    light-blue -> dark-red. The dark, low-saturation red washed out to white.

This sends a few deliberately-chosen single-zone gradients so you can report
what actually appears. Each is held until you press Enter. Uploads are
throttled by the library; this stays well within that.

    python gradient_probe.py 192.168.1.249

For each pair below, note what you SEE (both stops), then we can pin the
encoding down from the results.
"""

import argparse
from pyrava import BaravaDevice, Zone

# (label, stopA_rgb, stopB_rgb) -- chosen to separate the hypotheses.
CASES = [
    # Same hue (red), very different brightness. If RGB: dark red -> bright
    # red. If hue-only: both look identical (full red).
    ("red dark vs bright", (40, 0, 0), (255, 0, 0)),
    # Same hue+sat, different value only. If value is honoured they differ.
    ("blue dark vs bright", (0, 0, 40), (0, 0, 255)),
    # High-sat vs low-sat, same hue. Tests whether low sat washes to white.
    ("blue saturated vs washed", (0, 0, 255), (100, 100, 255)),
    # A true mid grey. If value/RGB honoured: grey. If hue-only: could be
    # anything or off.
    ("grey", (60, 60, 60), (180, 180, 180)),
    # The exact pair from your report, to reproduce it deliberately.
    ("your reported pair", (61, 169, 240), (68, 43, 43)),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--zone", type=int, default=int(Zone.TOP))
    args = parser.parse_args()

    device = BaravaDevice(args.host)
    device.register()
    print(f"connected to {device.device_id or device.host}")
    print(f"probing on zone {args.zone}\n")

    from pyrava.errors import TransportError
    try:
        for label, a, b in CASES:
            print(f"  {label}: stop A = rgb{a}, stop B = rgb{b}")
            try:
                device.set_zone_gradient(args.zone, (0, *a), (128, *b))
            except TransportError as exc:
                print(f"    upload failed: {exc}; skipping")
                continue
            input("    press Enter when you've noted what appeared... ")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            device.clear_zones()
        except TransportError:
            print("couldn't clear; power-cycle if unresponsive")

    print("\nReport, for each case: what colour was stop A end vs stop B end,")
    print("and whether brightness/intensity actually varied. That tells us")
    print("whether stops are literal RGB or hue/sat with magnitude ignored.")


if __name__ == "__main__":
    main()
