"""Command-line interface: ``pyrava <command>`` or ``python -m pyrava``."""

from __future__ import annotations

import argparse
import colorsys
import json
import logging
import sys
import time
from typing import Any

from . import __version__
from .client import (
    BaravaDevice,
    describe_heater_health,
    discover_devices,
    punch_color,
)
from .const import Handler, Var
from .palette import swatch
from .errors import BaravaError, DiscoveryError, TransportError


def _enable_ansi() -> bool:
    """Best-effort enable ANSI escapes on Windows; no-op elsewhere.

    Windows 10 1607+ supports VT100 sequences once
    ENABLE_VIRTUAL_TERMINAL_PROCESSING is set on the console handle; older
    consoles, or output that isn't a real terminal, don't. Returns whether
    escapes can actually be used, so callers can fall back to plain output.
    """
    if not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        mode.value |= 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode))
    except Exception:
        return False


class _Redraw:
    """Print a fixed block of lines, redrawing it in place on each update.

    Falls back to plain sequential printing when the terminal can't do
    cursor movement (piped output, an unsupported console) — output stays
    readable either way, it just scrolls instead of redrawing.
    """

    def __init__(self, ansi: bool | None = None) -> None:
        self.ansi = _enable_ansi() if ansi is None else ansi
        self._printed = 0

    def render(self, lines: list[str]) -> None:
        if self.ansi:
            if self._printed:
                sys.stdout.write(f"\x1b[{self._printed}A")
            for line in lines:
                sys.stdout.write(f"\r\x1b[2K{line}\n")
            sys.stdout.flush()
            self._printed = len(lines)
        else:
            print("\n".join(lines))

    def finish(self, message: str) -> None:
        if self.ansi:
            print(message)
        else:
            print(message)


def _connect(args: argparse.Namespace) -> BaravaDevice:
    """Resolve the target device from --host, or fall back to discovery."""
    if args.host:
        device = BaravaDevice(args.host, port=args.port)
        device.register()
        return device
    devices = discover_devices(timeout=args.timeout)
    if not devices:
        raise DiscoveryError(
            "no devices found. Pass --host <ip> to skip mDNS, or run "
            "'pyrava doctor' to diagnose discovery."
        )
    return devices[0]


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif isinstance(payload, dict):
        width = max((len(str(k)) for k in payload), default=0)
        for key, value in payload.items():
            print(f"{str(key):<{width}}  {value}")
    else:
        print(payload)


# -- commands --------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    devices = discover_devices(timeout=args.timeout)
    if not devices:
        print("No devices found. Try 'pyrava doctor'.", file=sys.stderr)
        return 1
    for device in devices:
        print(f"{device.device_id or '?':14} {device.name or '?':20} {device.host}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    device = _connect(args)
    info = device.get_device_info()
    if args.json:
        _emit(
            {
                "device_id": device.device_id,
                "name": device.name,
                "host": device.host,
                "groups": device.groups,
                "variables": info,
            },
            True,
        )
    else:
        print(f"device id  {device.device_id}")
        print(f"name       {device.name}")
        print(f"endpoint   {device.transport.url}")
        print(f"groups     {device.groups or '(none)'}")
        print()
        _emit(info, False)
    return 0


def cmd_heater(args: argparse.Namespace) -> int:
    device = _connect(args)
    temps = device.read_temperatures_f()
    info = device.get_heater_info()
    status = describe_heater_health(info.get(Var.HEATER_HEALTH.value))
    if args.json:
        _emit({"temperatures_f": temps, "status": status, "variables": info}, True)
    else:
        print(f"current  {temps['current']} \N{DEGREE SIGN}F")
        print(f"target   {temps['target']} \N{DEGREE SIGN}F")
        print(f"status   {status}")
        print()
        _emit(info, False)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    device = _connect(args)
    actions = 0
    if args.power is not None:
        device.set_state(args.power)
        actions += 1
    if args.fill_color:
        device.set_fill_color(_parse_rgb(args.fill_color))
        actions += 1
    if args.fill_hue is not None:
        device.set_fill_hue(args.fill_hue)
        actions += 1
    if args.fill_brightness is not None:
        device.set_fill_brightness(args.fill_brightness)
        actions += 1
    if args.desk is not None:
        device.set_desk_state(args.desk)
        actions += 1
    if args.desk_brightness is not None:
        device.set_desk_brightness(args.desk_brightness)
        actions += 1
    if args.desk_temp is not None:
        device.set_desk_color(args.desk_temp)
        actions += 1
    if args.heater is not None:
        device.set_heater_state(args.heater)
        actions += 1
    if not actions:
        print("Nothing to do; pass at least one setting.", file=sys.stderr)
        return 2
    print(f"sent {actions} command(s) to {device.device_id or device.host}")
    return 0


def _parse_rgb(text: str) -> tuple[int, int, int]:
    """Accept ``#RRGGBB``, ``RRGGBB``, or ``r,g,b`` and return an RGB triple.

    Only the hue survives once this reaches the device -- see
    :meth:`~pyrava.client.BaravaDevice.set_fill_color`. Pass ``--fill-hue``
    instead for a raw 0-360 value with no conversion.
    """
    text = text.strip()
    if "," in text:
        parts = [int(p) for p in text.split(",")]
        if len(parts) != 3:
            raise ValueError("colour needs exactly three channels")
        return tuple(parts)  # type: ignore[return-value]
    hex_text = text[1:] if text.startswith("#") else text
    value = int(hex_text, 16)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def cmd_raw(args: argparse.Namespace) -> int:
    """Send an arbitrary handler, for poking at undocumented behaviour."""
    device = _connect(args)
    body = {}
    for pair in args.var or []:
        key, _, value = pair.partition("=")
        try:
            body[key] = int(value)
        except ValueError:
            body[key] = value
    batch = device.request(args.handler, body or None, wait=not args.no_wait)
    if args.json:
        _emit({"handlers": batch.handlers, "body": batch.flatten(),
               "raw": device.last_raw}, True)
    else:
        print(f"sent     {device.last_request[1]}")
        print(f"received {device.last_raw}")
        print()
        _emit(batch.flatten(), False)
    return 0


#: SGR codes for the handful of colors watch uses. Kept to widely-supported
#: basic codes (not 256-color/truecolor) since these are for text, not the
#: swatch chip -- that already uses truecolor via pyrava.palette.swatch.
_SGR = {"red": 31, "green": 32, "yellow": 33, "cyan": 36, "dim": 2, "bold": 1}


def _c(text: str, *colors: str, enabled: bool) -> str:
    """Wrap text in ANSI SGR codes, or return it plain if not enabled."""
    if not enabled or not colors:
        return text
    codes = ";".join(str(_SGR[c]) for c in colors)
    return f"\033[{codes}m{text}\033[0m"


def _fmt_bool(value: Any, labels: tuple[str, str] = ("on", "off"), *, color: bool = False) -> str:
    if value is None:
        return "?"
    on = bool(int(value))
    text = labels[0] if on else labels[1]
    return _c(text, "green" if on else "dim", enabled=color)


def _fmt_color(value: Any, *, swatch_chip: bool = False) -> str:
    """Format FCLR, a hue in degrees -- not a packed colour, despite the name.

    An earlier version of this formatter rendered the raw int as ``#XXXXXX``
    hex, which looked like a colour but was actually just the hue number
    misread as one. This shows the actual hue, plus an approximate preview
    swatch at full saturation/value (the real saturation and brightness
    aren't part of this field, so the swatch is illustrative, not exact).
    """
    if value is None:
        return "?"
    try:
        hue = int(value)
    except (TypeError, ValueError):
        return str(value)
    label = f"{hue}\N{DEGREE SIGN}"
    if swatch_chip:
        r, g, b = colorsys.hsv_to_rgb(hue / 360, 1.0, 1.0)
        chip = swatch((round(r * 255), round(g * 255), round(b * 255)), width=2)
        return f"{label} {chip}"
    return label


def cmd_watch(args: argparse.Namespace) -> int:
    device = _connect(args)
    session = device.poll(
        interval=args.interval,
        keepalive=[Handler.DEVICE_INFO, Handler.GET_HEATER_INFO],
    )

    # --json wants a machine-readable stream, one event per line, not a
    # redrawn block -- so it keeps the old scrolling behaviour.
    if args.json:
        def emit_json(handler_name: str, packet):
            print(json.dumps(
                {"time": time.strftime("%H:%M:%S"), "handler": handler_name,
                 "body": packet.body},
                default=str,
            ))
        session.on(Handler.DEVICE_INFO, lambda p: emit_json("DVIFO", p))
        session.on(Handler.GET_HEATER_INFO, lambda p: emit_json("HTIFO", p))
        print(f"# watching {device.device_id or device.host}, "
              f"one JSON object per event, Ctrl-C to stop", file=sys.stderr)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            session.stop()
        return 0

    state: dict[str, str] = {
        "device": "waiting for data...",
        "lights": "waiting for data...",
        "heater": "waiting for data...",
    }
    last_update = {"at": None}
    redraw = _Redraw()
    use_color = redraw.ansi  # colour rides on the same terminal capability

    def render() -> None:
        header = f"watching {device.device_id or device.host}  (Ctrl-C to stop)"
        stamp = last_update["at"] or "--:--:--"
        redraw.render([
            _c(header, "bold", "cyan", enabled=use_color),
            _c("-" * len(header), "dim", enabled=use_color),
            f"device : {state['device']}",
            f"lights : {state['lights']}",
            f"heater : {state['heater']}",
            _c(f"updated: {stamp}", "dim", enabled=use_color),
        ])

    def on_info(packet) -> None:
        name = packet.get(Var.DEVICE_NAME, "?")
        version = packet.get(Var.DEVICE_VERSION, "?")
        state["device"] = f"{name}  (firmware {version})"
        state["lights"] = (
            f"power {_fmt_bool(packet.get(Var.DEVICE_STATE), color=use_color)}   "
            f"fill {_fmt_bool(packet.get(Var.FILL_STATE), color=use_color)} "
            f"{_fmt_color(packet.get(Var.FILL_COLOR), swatch_chip=use_color)} "
            f"bri {packet.get(Var.FILL_BRIGHTNESS, '?')}   "
            f"desk {_fmt_bool(packet.get(Var.DESK_STATE), color=use_color)} "
            f"bri {packet.get(Var.DESK_BRIGHTNESS, '?')}"
        )
        last_update["at"] = time.strftime("%H:%M:%S")
        render()

    def on_heater(packet) -> None:
        current = packet.get(Var.HEATER_CUR_TEMP)
        target = packet.get(Var.HEATER_SET_TEMP)
        status = describe_heater_health(packet.get(Var.HEATER_HEALTH))
        current_f = "?" if current is None else f"{current / 100:.2f}"
        target_f = "?" if target is None else f"{target / 100:.2f}"
        status_color = (
            "green" if status == "OK"
            else "yellow" if status == "unknown"
            else "red"  # "Fault" or "Fault (code N)"
        )
        state["heater"] = (
            f"current {current_f}\N{DEGREE SIGN}F   "
            f"target {target_f}\N{DEGREE SIGN}F   "
            f"status {_c(status, status_color, enabled=use_color)}"
        )
        last_update["at"] = time.strftime("%H:%M:%S")
        render()

    session.on(Handler.DEVICE_INFO, on_info)
    session.on(Handler.GET_HEATER_INFO, on_heater)
    render()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        redraw.finish("stopped")
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    """Sample screen colours and push them to the lamp."""
    try:
        colors = dominant_colors(args.n)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.punch:
        colors = [punch_color(c) for c in colors]
    if args.sort:
        colors = sort_by_hue(colors)

    print(format_palette(colors))
    if args.preview:
        print("\npreview only -- nothing sent")
        return 0

    device = _connect(args)
    if args.gradient:
        device.set_gradient(colors, zones=args.zones) if args.zones \
            else device.set_gradient(colors)
        print("gradient sent")
    else:
        device.set_zone_palette(colors, zones=args.zones) if args.zones \
            else device.set_zone_palette(colors)
        print("palette sent")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose mDNS, the usual reason discovery comes up empty."""
    from .discovery import CANDIDATE_TYPES, browse, list_service_types, scan

    print("Service types answering on this network:")
    try:
        types = list_service_types(timeout=args.timeout)
    except Exception as exc:
        print(f"  failed: {exc}")
        types = []
    if not types:
        print("  none -- mDNS is not reaching this machine.")
        print("  On Windows, allow inbound UDP 5353 for python.exe.")
        print("  Also check VPN/hypervisor adapters and AP client isolation.")
        return 1
    for t in types:
        print(f"  {t}")

    print("\nCandidate types:")
    for candidate in CANDIDATE_TYPES:
        hits = browse(args.timeout / len(CANDIDATE_TYPES), service_type=candidate)
        print(f"  [{'FOUND' if hits else '  -  '}] {candidate}")
        for service in hits:
            print(f"           {service}  id={service.device_id}")

    print("\nEverything resolvable:")
    for service in scan(args.timeout):
        flag = "  <-- looks like Barava" if service.looks_like_barava else ""
        print(f"  {service}{flag}")
    return 0


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyrava", description="Control Barava smart devices from the shell."
    )
    parser.add_argument("--version", action="version", version=f"pyrava {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every frame")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", help="device IP; skips mDNS discovery")
    common.add_argument("--port", type=int, default=8080)
    common.add_argument("--timeout", type=float, default=5.0)
    common.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="list devices on the network")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("info", parents=[common], help="show device info")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("heater", parents=[common], help="show heater state")
    p.set_defaults(func=cmd_heater)

    p = sub.add_parser("set", parents=[common], help="change device settings")
    onoff = {"type": _boolish, "choices": [True, False], "metavar": "on|off"}
    p.add_argument("--power", **onoff)
    p.add_argument("--desk", **onoff)
    p.add_argument("--heater", **onoff)
    p.add_argument(
        "--fill-color",
        help="#RRGGBB, RRGGBB, or r,g,b -- only the hue is used, see README",
    )
    p.add_argument(
        "--fill-hue", type=float, metavar="0-360",
        help="raw hue, bypassing RGB-to-hue conversion",
    )
    p.add_argument("--fill-brightness", type=int)
    p.add_argument("--desk-brightness", type=int)
    p.add_argument("--desk-temp", type=int, help="colour temperature, not RGB")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("raw", parents=[common], help="send an arbitrary handler")
    p.add_argument("handler", help="e.g. DVIFO")
    p.add_argument("--var", action="append", metavar="KEY=VALUE")
    p.add_argument("--no-wait", action="store_true")
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("watch", parents=[common], help="stream queued events")
    p.add_argument(
        "--interval", type=float, default=1.0, metavar="SECONDS",
        help="poll cadence; clamped to a 0.5s floor per the vendor's design "
             "notes (0.5 is allowed for data polling like temperature)",
    )
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("screen", parents=[common],
                       help="sample screen colours onto the lamp")
    p.add_argument("--n", type=int, default=5, help="colours to sample")
    p.add_argument("--gradient", action="store_true",
                   help="blend the colours instead of one flat colour per zone")
    p.add_argument("--zones", help="zone index or group name")
    p.add_argument("--punch", action="store_true", help="boost saturation")
    p.add_argument("--sort", action="store_true",
                   help="order by hue; helps gradients avoid muddy blends")
    p.add_argument("--preview", action="store_true",
                   help="show the swatches without sending anything")
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("doctor", help="diagnose mDNS discovery")
    p.add_argument("--timeout", type=float, default=6.0)
    p.set_defaults(func=cmd_doctor)

    return parser


def _boolish(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in {"on", "true", "1", "yes"}:
        return True
    if lowered in {"off", "false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError(f"expected on/off, got {text!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(message)s",
    )
    try:
        return args.func(args)
    except (BaravaError, TransportError, DiscoveryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
