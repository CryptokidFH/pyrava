"""Command-line interface: ``pyrava <command>`` or ``python -m pyrava``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

from . import __version__
from .client import BaravaDevice, describe_heater_health, discover_devices
from .const import Handler, Var
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


def _fmt_bool(value: Any, labels: tuple[str, str] = ("on", "off")) -> str:
    if value is None:
        return "?"
    return labels[0] if int(value) else labels[1]


def _fmt_color(value: Any) -> str:
    if value is None:
        return "?"
    try:
        return f"#{int(value):06X}"
    except (TypeError, ValueError):
        return str(value)


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

    def render() -> None:
        header = f"watching {device.device_id or device.host}  (Ctrl-C to stop)"
        stamp = last_update["at"] or "--:--:--"
        redraw.render([
            header,
            "-" * len(header),
            f"device : {state['device']}",
            f"lights : {state['lights']}",
            f"heater : {state['heater']}",
            f"updated: {stamp}",
        ])

    def on_info(packet) -> None:
        name = packet.get(Var.DEVICE_NAME, "?")
        version = packet.get(Var.DEVICE_VERSION, "?")
        state["device"] = f"{name}  (firmware {version})"
        state["lights"] = (
            f"power {_fmt_bool(packet.get(Var.DEVICE_STATE))}   "
            f"fill {_fmt_bool(packet.get(Var.FILL_STATE))} "
            f"{_fmt_color(packet.get(Var.FILL_COLOR))} "
            f"bri {packet.get(Var.FILL_BRIGHTNESS, '?')}   "
            f"desk {_fmt_bool(packet.get(Var.DESK_STATE))} "
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
        state["heater"] = (
            f"current {current_f}\N{DEGREE SIGN}F   "
            f"target {target_f}\N{DEGREE SIGN}F   status {status}"
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
