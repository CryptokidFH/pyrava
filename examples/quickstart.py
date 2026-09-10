"""Walk through the main capabilities of the pyrava package.

Usage:
    python examples/quickstart.py               # discover devices via mDNS
    python examples/quickstart.py 192.168.1.42  # connect to a known address
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# Make `pyrava` importable regardless of the working directory this is run
# from: `python somefile.py` puts that file's own directory on sys.path, not
# the caller's cwd, so a script inside examples/ can't otherwise see the
# pyrava/ package one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrava import (
    AnimationScript,
    BaravaDevice,
    Handler,
    SubPacket,
    Var,
    disassemble,
    discover_devices,
    rgb_to_hue,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")


def connect() -> BaravaDevice:
    if len(sys.argv) > 1:
        device = BaravaDevice(sys.argv[1])
        device.register()  # DVIFO; learns the real device ID
        return device

    print("Browsing for _barava._tcp devices (this is the slow part)...")
    devices = discover_devices(timeout=5.0)
    if not devices:
        sys.exit("No devices found. Check you're on the same L2 network as the device.")
    for i, dev in enumerate(devices):
        print(f"  [{i}] {dev.name or '?'}  id={dev.device_id}  {dev.host}")
    return devices[0]


def show_info(device: BaravaDevice) -> None:
    info = device.get_device_info()
    print("\n--- device info ---")
    for var in (
        Var.DEVICE_NAME,
        Var.DEVICE_ID,
        Var.DEVICE_VERSION,
        Var.DEVICE_STATE,
        Var.DEVICE_GROUPS,
        Var.FILL_COLOR,
        Var.FILL_BRIGHTNESS,
        Var.DESK_STATE,
        Var.HEATER_STATE,
    ):
        if var.value in info:
            print(f"  {var.name.lower():18} {info[var.value]}")

    heater = device.get_heater_info()
    if heater:
        temps = device.read_temperatures_f()
        print("\n--- heater ---")
        print(f"  current temp       {temps['current']} \N{DEGREE SIGN}F")
        print(f"  target temp        {temps['target']} \N{DEGREE SIGN}F")
        print(f"  health             {heater.get(Var.HEATER_HEALTH.value)}")


def basic_control(device: BaravaDevice) -> None:
    print("\n--- lighting ---")
    device.set_state(True)
    device.set_fill_state(True)
    device.set_fill_brightness(180)

    for name, colour in (
        ("red", (255, 0, 0)),
        ("amber", (255, 120, 0)),
        ("blue", (0, 0, 255)),
    ):
        print(f"  fill -> {name}")
        device.set_fill_color(colour)  # RGB tuple -> converted to hue
        time.sleep(0.6)

    device.set_desk_state(True)
    device.set_desk_brightness(140)


def batched_control(device: BaravaDevice) -> None:
    """Several commands, one round trip.

    A batch is many packets aimed at one device, sent as a single request.
    """
    print("\n--- batched ---")
    response = device.send_batch(
        [
            SubPacket(Handler.SET_DESK_STATE, {Var.DESK_STATE.value: 1}),
            SubPacket(Handler.SET_DESK_BRIGHTNESS, {Var.DESK_BRIGHTNESS.value: 200}),
            SubPacket(Handler.SET_FILL_COLOR,
                      {Var.FILL_COLOR.value: int(rgb_to_hue(0, 255, 128))}),
        ]
    )
    print(f"  device returned {len(response)} queued packet(s)")


def animate(device: BaravaDevice) -> None:
    """Red-to-blue gradient on zones 0 and 3, rotating left every tick."""
    print("\n--- animation ---")
    script = AnimationScript()

    with script.header(0):
        script.select_zones(0, 3)
        script.gradient((0, 255, 0, 0), (255, 0, 0, 255), smooth=True)
        script.deselect_all()  # don't interfere with other threads

    with script.thread(0):
        with script.main():
            with script.atomic():
                script.select_zones(0, 3)
                script.rotate_left(5)
                script.deselect_zones(0, 3)

    print(f"  {len(script)} bytes of bytecode")
    for line in disassemble(script.compile()):
        print("   ", line)
    device.upload_animation(script)


def watch(device: BaravaDevice, seconds: float = 10.0) -> None:
    """Keep the session registered and print whatever the device queues up.

    The device never initiates; it parks responses in a per-sender queue and
    flushes them on your next ping. Stop pinging and it drops you.
    """
    print(f"\n--- polling for {seconds:.0f}s ---")

    def on_info(packet):
        temp = packet.get(Var.HEATER_CUR_TEMP)
        if temp is not None:
            print(f"  heater at {temp / 100:.2f} \N{DEGREE SIGN}F")

    # keepalive=None sends bare queue-pop pings, which request nothing new.
    # A single handler re-requests that one thing every tick; pass a list to
    # re-request several (see the CLI's `watch` command for that form).
    session = device.poll(interval=0.5, keepalive=Handler.GET_HEATER_INFO)
    session.on(Handler.GET_HEATER_INFO, on_info)
    try:
        time.sleep(seconds)
    finally:
        session.stop()
        print("  stopped")


def main() -> None:
    with connect() as device:
        print(f"\nConnected to {device!r} (session id {device.sender_id})")
        show_info(device)
        basic_control(device)
        batched_control(device)
        animate(device)
        watch(device, seconds=5)


if __name__ == "__main__":
    main()
