"""Work out whether fades are possible, and how the timing behaves.

The bytecode has the pieces a fade needs -- ``SCALE_COLORS`` (0x24), a
counted ``LOOP`` scope, and a forever ``MAIN`` scope -- but none of the
captured themes from the device's own app use any of them, so their exact
semantics are unconfirmed:

  * Is SCALE_COLORS' operand a multiplier (255 = unchanged) or something
    else? Does it compound when repeated?
  * Does one tick of a LOOP/MAIN scope equal one command, or one pass
    through the whole scope?
  * How long is a tick?

Each case below isolates one of those. Uploads are throttled by the library;
watch the lamp and note what happens.

    python examples/fade_probe.py 192.168.1.249
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrava import AnimationScript, BaravaDevice, Zone
from pyrava.errors import TransportError

ZONE = int(Zone.TOP)
BASE = (0, 80, 255)


def fill_only() -> AnimationScript:
    s = AnimationScript()
    with s.header(0):
        s.reset_l2(); s.select_zone(ZONE)
    with s.thread(0):
        with s.atomic():
            s.reset_l2(); s.select_zone(ZONE); s.set_rgb(*BASE)
    return s


def scaled_once(scalar: int) -> AnimationScript:
    """One scale, applied once. Tells us what the operand means at all."""
    s = AnimationScript()
    with s.header(0):
        s.reset_l2(); s.select_zone(ZONE)
    with s.thread(0):
        with s.atomic():
            s.reset_l2(); s.select_zone(ZONE); s.set_rgb(*BASE)
            s.scale_colors(scalar)
    return s


def looped_scale(scalar: int, iterations: int) -> AnimationScript:
    """Repeated scaling. If it compounds, this is a fade-out."""
    s = AnimationScript()
    with s.header(0):
        s.reset_l2(); s.select_zone(ZONE)
    with s.thread(0):
        with s.atomic():
            s.reset_l2(); s.select_zone(ZONE); s.set_rgb(*BASE)
        with s.loop(iterations):
            s.scale_colors(scalar)
    return s


def forever_scale(scalar: int) -> AnimationScript:
    """MAIN scope: does it re-run and decay to black, or settle?"""
    s = AnimationScript()
    with s.header(0):
        s.reset_l2(); s.select_zone(ZONE)
    with s.thread(0):
        with s.atomic():
            s.reset_l2(); s.select_zone(ZONE); s.set_rgb(*BASE)
        with s.main():
            s.scale_colors(scalar)
    return s


def diffuse(scalar: int) -> AnimationScript:
    """DIFFUSE_COLORS is undocumented too -- probably a blur along the ring."""
    s = AnimationScript()
    with s.header(0):
        s.reset_l2(); s.select_zone(ZONE)
    with s.thread(0):
        with s.atomic():
            s.reset_l2(); s.select_zone(ZONE)
            s.gradient((0, 255, 0, 0), (128, 0, 0, 255))
        with s.main():
            s.diffuse_colors(scalar)
    return s


CASES = [
    ("baseline fill, no transform", fill_only(),
     "the reference colour"),
    ("scale_colors(255) once", scaled_once(255),
     "same as baseline? then 255 means 'unchanged'"),
    ("scale_colors(128) once", scaled_once(128),
     "half brightness? then the operand is a multiplier"),
    ("scale_colors(0) once", scaled_once(0),
     "black? confirms multiplier. unchanged? means something else"),
    ("loop(20) x scale(230)", looped_scale(230, 20),
     "fades out over 20 steps? then scaling compounds -- fades are possible"),
    ("main() x scale(230)", forever_scale(230),
     "decays to black and stays? or resets each pass?"),
    ("main() x diffuse(128)", diffuse(128),
     "gradient blurs/smears along the ring?"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    args = parser.parse_args()

    device = BaravaDevice(args.host)
    device.register()
    print(f"connected to {device.device_id or device.host}\n")
    print("For each case, note what the TOP ring does.\n")

    try:
        for label, script, question in CASES:
            print(f"  {label}")
            print(f"      watch for: {question}")
            try:
                device.upload_animation(script)
            except TransportError as exc:
                print(f"      upload failed: {exc}")
                time.sleep(2)
                continue
            input("      press Enter when you've noted it... ")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            device.clear_zones()
            print("\ncleared")
        except TransportError:
            print("\ncouldn't clear; power-cycle if unresponsive")

    print("""
What the answers unlock:

  * If scale_colors is a multiplier AND compounds in a loop, fade-out is
    just loop(n) x scale(k), and fade-in can be approximated by starting
    dark and scaling up (if k > 255 is even accepted -- it isn't, the
    operand is one byte, so fade-in may need a different approach).
  * If MAIN re-runs the scope every tick, a crossfade could be built as
    fade-out, swap, fade-in -- a fade through black rather than a true
    blend, since there's no opcode for interpolating toward a target.
  * A true crossfade between two colour sets has no obvious primitive and
    probably does need firmware support.
""")


if __name__ == "__main__":
    main()
