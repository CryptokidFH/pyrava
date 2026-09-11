"""Compiler for the Barava animation scripting bytecode.

A script is a flat byte string of ``[COMMAND][PARAM...]`` groups, built from
the command table in `animation.md`.  The compiled bytes are hex-encoded and
sent as ``VAR_ANIMATION_DATA`` to the ``CMPAM`` handler.

Structure mirrors the engine's own model:

* a **header** block per thread runs atomically on a single tick and sets up
  fixed colour state (masks, gradients, fills);
* a **thread** block runs one command per tick, unless wrapped in an
  **atomic** scope;
* **main** repeats forever, **loop(n)** repeats ``n`` times.

Header and thread indices must match or the two halves will not be in sync.

Example::

    script = AnimationScript()
    with script.header(0):
        script.select_zone(0)
        script.build_gradient()
        script.append_gradient(0, 255, 0, 0)
        script.append_gradient(255, 0, 0, 255)
        script.map_linear()
        script.deselect_all()
    with script.thread(0):
        with script.main():
            with script.atomic():
                script.select_zone(0)
                script.rotate_left(5)
                script.deselect_zone(0)
    payload = script.to_hex()
"""

from __future__ import annotations

from __future__ import annotations

import warnings
from contextlib import contextmanager
from enum import IntEnum
from typing import Iterator, NamedTuple

from .errors import CompileError

__all__ = ["Command", "AnimationScript", "disassemble", "ZONE_COUNT", "TICK_RATE_HZ"]

#: Physical zones on board Revision 1. Also the maximum thread count.
ZONE_COUNT = 5

#: Nominal engine tick rate. Useful for converting seconds to loop counts.
TICK_RATE_HZ = 41

#: Byte order for U16 parameters. `animation.md` does not state this; big
#: endian is assumed. If loop counts come out wrong on hardware, flip it.
U16_BYTEORDER = "big"


class Command(IntEnum):
    """Command IDs from the compiler reference table."""

    # interpreter instructions (0-14)
    SCRIPT_HEADER = 0
    OPEN_THREAD = 1
    OPEN_HEADER = 2
    START_SCOPE_MAIN = 3
    START_SCOPE_LOOP = 4
    START_SCOPE_ATOMIC = 5
    CLOSE_THREAD = 6
    CLOSE_HEADER = 7
    END_SCOPE_MAIN = 8
    END_SCOPE_LOOP = 9
    END_SCOPE_ATOMIC = 10

    # initialization instructions (15-31)
    SELECT_ZONE = 15
    DESELECT_ZONE = 16
    FILL_ZONE = 17
    BUILD_GRADIENT = 18
    APPEND_GRADIENT = 19
    MAP_LINEAR = 20
    MAP_SMOOTH = 21
    RELEASE_GRADIENT = 22
    RESET_L2 = 23
    SELECT_ALL = 24
    DESELECT_ALL = 25

    # L1 transform instructions (32-61)
    ROTATE_LEFT = 32
    ROTATE_RIGHT = 33
    ROTATE_UP = 34
    ROTATE_DOWN = 35
    SCALE_COLORS = 36
    DIFFUSE_COLORS = 37
    SET_RED = 38
    SET_GREEN = 39
    SET_BLUE = 40
    SET_RGB = 41

    # L2 transform instructions (62-93)
    ADD_VALUE = 62
    SUB_VALUE = 63
    MUL_VALUE = 64
    DIV_VALUE = 65
    ADD_CHANNELS = 66
    SUB_CHANNELS = 67
    MUL_CHANNELS = 68
    DIV_CHANNELS = 69
    ADD_RED = 70
    ADD_GREEN = 71
    ADD_BLUE = 72
    SUB_RED = 73
    SUB_GREEN = 74
    SUB_BLUE = 75
    MUL_RED = 76
    MUL_GREEN = 77
    MUL_BLUE = 78
    DIV_BLUE = 79
    DIV_RED = 80
    DIV_GREEN = 81


class _Spec(NamedTuple):
    params: int
    width: int  # bytes per parameter
    names: tuple[str, ...]


#: Parameter layout per command. Commands absent from this table take none.
SPEC: dict[Command, _Spec] = {
    Command.OPEN_THREAD: _Spec(1, 1, ("thread_index",)),
    Command.OPEN_HEADER: _Spec(1, 1, ("thread_index",)),
    Command.START_SCOPE_LOOP: _Spec(1, 2, ("iterations",)),
    Command.SELECT_ZONE: _Spec(1, 1, ("zone",)),
    Command.DESELECT_ZONE: _Spec(1, 1, ("zone",)),
    # Captured themes show FILL_ZONE taking no operands: it opens a
    # gradient build, which BUILD_GRADIENT stops then fill. The spec
    # table implied (r, g, b), but that desyncs the stream.
    Command.FILL_ZONE: _Spec(0, 1, ()),
    # BUILD_GRADIENT carries each stop; confirmed against captured
    # themes (pos 0/85/170 for an evenly spaced three-stop gradient).
    Command.BUILD_GRADIENT: _Spec(4, 1, ("pos", "r", "g", "b")),
    Command.APPEND_GRADIENT: _Spec(4, 1, ("pos", "r", "g", "b")),
    Command.ROTATE_LEFT: _Spec(1, 1, ("amount",)),
    Command.ROTATE_RIGHT: _Spec(1, 1, ("amount",)),
    Command.ROTATE_UP: _Spec(1, 1, ("amount",)),
    Command.ROTATE_DOWN: _Spec(1, 1, ("amount",)),
    Command.SCALE_COLORS: _Spec(1, 1, ("scalar",)),
    Command.DIFFUSE_COLORS: _Spec(1, 1, ("scalar",)),
    Command.SET_RED: _Spec(1, 1, ("value",)),
    Command.SET_GREEN: _Spec(1, 1, ("value",)),
    Command.SET_BLUE: _Spec(1, 1, ("value",)),
    Command.SET_RGB: _Spec(3, 1, ("r", "g", "b")),
    Command.ADD_VALUE: _Spec(1, 1, ("scalar",)),
    Command.SUB_VALUE: _Spec(1, 1, ("scalar",)),
    Command.MUL_VALUE: _Spec(1, 1, ("scalar",)),
    Command.DIV_VALUE: _Spec(1, 1, ("scalar",)),
    Command.ADD_CHANNELS: _Spec(3, 1, ("r", "g", "b")),
    Command.SUB_CHANNELS: _Spec(3, 1, ("r", "g", "b")),
    Command.MUL_CHANNELS: _Spec(3, 1, ("r", "g", "b")),
    Command.DIV_CHANNELS: _Spec(3, 1, ("r", "g", "b")),
    Command.ADD_RED: _Spec(1, 1, ("scalar",)),
    Command.ADD_GREEN: _Spec(1, 1, ("scalar",)),
    Command.ADD_BLUE: _Spec(1, 1, ("scalar",)),
    Command.SUB_RED: _Spec(1, 1, ("scalar",)),
    Command.SUB_GREEN: _Spec(1, 1, ("scalar",)),
    Command.SUB_BLUE: _Spec(1, 1, ("scalar",)),
    Command.MUL_RED: _Spec(1, 1, ("scalar",)),
    Command.MUL_GREEN: _Spec(1, 1, ("scalar",)),
    Command.MUL_BLUE: _Spec(1, 1, ("scalar",)),
    Command.DIV_BLUE: _Spec(1, 1, ("scalar",)),
    Command.DIV_RED: _Spec(1, 1, ("scalar",)),
    Command.DIV_GREEN: _Spec(1, 1, ("scalar",)),
}

_NO_PARAMS = _Spec(0, 0, ())


def _u8(value: int, name: str) -> int:
    value = int(value)
    if not 0 <= value <= 255:
        raise CompileError(f"{name} must fit in a U8 (0-255), got {value}")
    return value


def _u16(value: int, name: str) -> int:
    value = int(value)
    if not 0 <= value <= 65535:
        raise CompileError(f"{name} must fit in a U16 (0-65535), got {value}")
    return value


class AnimationScript:
    """Builds animation bytecode with scope tracking and validation."""

    def __init__(self, *, zone_count: int = ZONE_COUNT, strict: bool = True) -> None:
        self.zone_count = zone_count
        self.strict = strict
        self._buf = bytearray()
        self._scopes: list[str] = []
        self._headers: set[int] = set()
        self._threads: set[int] = set()

    # -- raw emission ------------------------------------------------------

    def emit(self, command: Command, *params: int) -> "AnimationScript":
        """Append one command and its parameters. Validates arity and width."""
        spec = SPEC.get(command, _NO_PARAMS)
        if len(params) != spec.params:
            raise CompileError(
                f"{command.name} takes {spec.params} parameter(s), got {len(params)}"
            )
        self._buf.append(int(command))
        for value, name in zip(params, spec.names):
            if spec.width == 1:
                self._buf.append(_u8(value, name))
            else:
                self._buf.extend(
                    _u16(value, name).to_bytes(spec.width, U16_BYTEORDER)
                )
        return self

    def _check_zone(self, zone: int) -> int:
        zone = int(zone)
        if self.strict and not 0 <= zone < self.zone_count:
            raise CompileError(
                f"zone {zone} out of range; board has {self.zone_count} zones (0-"
                f"{self.zone_count - 1})"
            )
        return zone

    def _require_scope(self, what: str) -> None:
        if self.strict and not self._scopes:
            raise CompileError(
                f"{what} must appear inside a header() or thread() block"
            )

    # -- structural scopes -------------------------------------------------

    @contextmanager
    def header(self, index: int) -> Iterator["AnimationScript"]:
        """Open the initialization block for thread ``index``.

        Everything inside executes on a single engine tick.
        """
        index = int(index)
        if self.strict and self._scopes:
            raise CompileError("header() must be at the root of the script")
        if index in self._headers:
            raise CompileError(f"header {index} is already defined")
        self._headers.add(index)
        self.emit(Command.OPEN_HEADER, _u8(index, "thread_index"))
        self._scopes.append("header")
        try:
            yield self
        finally:
            self._scopes.pop()
            self.emit(Command.CLOSE_HEADER)

    @contextmanager
    def thread(self, index: int) -> Iterator["AnimationScript"]:
        """Open the runtime block for thread ``index``."""
        index = int(index)
        if self.strict and self._scopes:
            raise CompileError("thread() must be at the root of the script")
        if self.strict and index >= self.zone_count:
            raise CompileError(
                f"thread {index} exceeds the {self.zone_count}-thread limit"
            )
        if index in self._threads:
            raise CompileError(f"thread {index} is already defined")
        self._threads.add(index)
        self.emit(Command.OPEN_THREAD, _u8(index, "thread_index"))
        self._scopes.append("thread")
        try:
            yield self
        finally:
            self._scopes.pop()
            self.emit(Command.CLOSE_THREAD)

    @contextmanager
    def main(self) -> Iterator["AnimationScript"]:
        """Open a forever-repeating scope. Nesting one blocks the thread."""
        self._require_scope("main()")
        if "main" in self._scopes:
            warnings.warn(
                "nesting main() inside main() blocks the thread like while(true)",
                stacklevel=3,
            )
        self.emit(Command.START_SCOPE_MAIN)
        self._scopes.append("main")
        try:
            yield self
        finally:
            self._scopes.pop()
            self.emit(Command.END_SCOPE_MAIN)

    @contextmanager
    def loop(self, iterations: int) -> Iterator["AnimationScript"]:
        """Open a scope repeated ``iterations`` times, one command per tick."""
        self._require_scope("loop()")
        self.emit(Command.START_SCOPE_LOOP, _u16(iterations, "iterations"))
        self._scopes.append("loop")
        try:
            yield self
        finally:
            self._scopes.pop()
            self.emit(Command.END_SCOPE_LOOP)

    @contextmanager
    def atomic(self) -> Iterator["AnimationScript"]:
        """Open a scope whose commands all execute on one tick."""
        self._require_scope("atomic()")
        self.emit(Command.START_SCOPE_ATOMIC)
        self._scopes.append("atomic")
        try:
            yield self
        finally:
            self._scopes.pop()
            self.emit(Command.END_SCOPE_ATOMIC)

    # -- zone selection ----------------------------------------------------

    def select_zone(self, zone: int) -> "AnimationScript":
        return self.emit(Command.SELECT_ZONE, self._check_zone(zone))

    def deselect_zone(self, zone: int) -> "AnimationScript":
        return self.emit(Command.DESELECT_ZONE, self._check_zone(zone))

    def select_zones(self, *zones: int) -> "AnimationScript":
        for zone in zones:
            self.select_zone(zone)
        return self

    def deselect_zones(self, *zones: int) -> "AnimationScript":
        for zone in zones:
            self.deselect_zone(zone)
        return self

    def select_all(self) -> "AnimationScript":
        return self.emit(Command.SELECT_ALL)

    def deselect_all(self) -> "AnimationScript":
        return self.emit(Command.DESELECT_ALL)

    # -- fills, gradients, masks ------------------------------------------

    def fill_zone(self) -> "AnimationScript":
        """Open a gradient build for the selected zone.

        Takes no operands. Captured themes show the sequence
        ``FILL_ZONE, BUILD_GRADIENT*, MAP_LINEAR``; the stops live on
        :meth:`build_gradient`.
        """
        return self.emit(Command.FILL_ZONE)

    def build_gradient(self, pos: int, r: int, g: int, b: int) -> "AnimationScript":
        """Add one gradient stop at ``pos`` (0-255, normalised across the zone)."""
        return self.emit(Command.BUILD_GRADIENT, pos, r, g, b)

    def append_gradient(self, pos: int, r: int, g: int, b: int) -> "AnimationScript":
        """Add a key frame via ``APPEND_GRADIENT`` (0x13).

        The captured themes all use :meth:`build_gradient` (0x12) instead, so
        this opcode's role is still unconfirmed.
        """
        return self.emit(Command.APPEND_GRADIENT, pos, r, g, b)

    def map_linear(self) -> "AnimationScript":
        return self.emit(Command.MAP_LINEAR)

    def map_smooth(self) -> "AnimationScript":
        """Map the gradient with sigmoid interpolation."""
        return self.emit(Command.MAP_SMOOTH)

    def release_gradient(self) -> "AnimationScript":
        return self.emit(Command.RELEASE_GRADIENT)

    def reset_l2(self) -> "AnimationScript":
        return self.emit(Command.RESET_L2)

    def gradient(self, *stops: tuple[int, int, int, int], smooth: bool = False):
        """Fill the selected zone with a gradient.

        ``stops`` are ``(pos, r, g, b)`` key frames. Emits the sequence the
        device's own app produces: ``FILL_ZONE``, one ``BUILD_GRADIENT`` per
        stop, then a mapping instruction.
        """
        if len(stops) < 2:
            raise CompileError("a gradient needs at least two key frames")
        self.fill_zone()
        for pos, r, g, b in stops:
            self.build_gradient(pos, r, g, b)
        return self.map_smooth() if smooth else self.map_linear()

    # -- L1 transforms -----------------------------------------------------

    def rotate_left(self, amount: int) -> "AnimationScript":
        return self.emit(Command.ROTATE_LEFT, amount)

    def rotate_right(self, amount: int) -> "AnimationScript":
        return self.emit(Command.ROTATE_RIGHT, amount)

    def rotate_up(self, amount: int) -> "AnimationScript":
        return self.emit(Command.ROTATE_UP, amount)

    def rotate_down(self, amount: int) -> "AnimationScript":
        return self.emit(Command.ROTATE_DOWN, amount)

    def scale_colors(self, scalar: int) -> "AnimationScript":
        return self.emit(Command.SCALE_COLORS, scalar)

    def diffuse_colors(self, scalar: int) -> "AnimationScript":
        return self.emit(Command.DIFFUSE_COLORS, scalar)

    def set_red(self, value: int) -> "AnimationScript":
        return self.emit(Command.SET_RED, value)

    def set_green(self, value: int) -> "AnimationScript":
        return self.emit(Command.SET_GREEN, value)

    def set_blue(self, value: int) -> "AnimationScript":
        return self.emit(Command.SET_BLUE, value)

    def set_rgb(self, r: int, g: int, b: int) -> "AnimationScript":
        return self.emit(Command.SET_RGB, r, g, b)

    # -- L2 transforms -----------------------------------------------------

    def add_value(self, scalar: int) -> "AnimationScript":
        return self.emit(Command.ADD_VALUE, scalar)

    def sub_value(self, scalar: int) -> "AnimationScript":
        return self.emit(Command.SUB_VALUE, scalar)

    def mul_value(self, scalar: int) -> "AnimationScript":
        return self.emit(Command.MUL_VALUE, scalar)

    def div_value(self, scalar: int) -> "AnimationScript":
        return self.emit(Command.DIV_VALUE, scalar)

    def add_channels(self, r: int, g: int, b: int) -> "AnimationScript":
        return self.emit(Command.ADD_CHANNELS, r, g, b)

    def sub_channels(self, r: int, g: int, b: int) -> "AnimationScript":
        return self.emit(Command.SUB_CHANNELS, r, g, b)

    def mul_channels(self, r: int, g: int, b: int) -> "AnimationScript":
        return self.emit(Command.MUL_CHANNELS, r, g, b)

    def div_channels(self, r: int, g: int, b: int) -> "AnimationScript":
        return self.emit(Command.DIV_CHANNELS, r, g, b)

    # -- output ------------------------------------------------------------

    def _validate(self) -> None:
        if self._scopes:
            raise CompileError(f"unclosed scope(s): {', '.join(self._scopes)}")
        orphan_headers = self._headers - self._threads
        orphan_threads = self._threads - self._headers
        if orphan_headers:
            warnings.warn(
                f"header(s) {sorted(orphan_headers)} have no matching thread; "
                "header and thread indices must pair up to stay in sync",
                stacklevel=3,
            )
        if orphan_threads:
            warnings.warn(
                f"thread(s) {sorted(orphan_threads)} have no matching header",
                stacklevel=3,
            )

    def compile(self, *, script_header: bool = False) -> bytes:
        """Return the compiled bytecode.

        The reference table lists a ``SCRIPT_HEADER`` (0x00) instruction, and
        earlier versions always prefixed it. Themes captured from the
        device's own app start directly at ``OPEN_HEADER`` (0x02) with no
        such prefix, so it is now off by default. Pass
        ``script_header=True`` to restore the old output.
        """
        self._validate()
        if not self._buf:
            raise CompileError("script is empty")
        if script_header:
            return bytes([Command.SCRIPT_HEADER]) + bytes(self._buf)
        return bytes(self._buf)

    def to_hex(self, *, uppercase: bool = True, script_header: bool = False) -> str:
        """Compile and hex-encode for the ``ANDT`` variable."""
        text = self.compile(script_header=script_header).hex()
        return text.upper() if uppercase else text

    def __len__(self) -> int:
        return len(self._buf) + 1

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<AnimationScript {len(self)} bytes, "
            f"headers={sorted(self._headers)}, threads={sorted(self._threads)}>"
        )


def disassemble(data: bytes | str) -> list[str]:
    """Render compiled bytecode as readable lines. Useful for debugging.

    Accepts raw bytes or a hex string.
    """
    if isinstance(data, str):
        data = bytes.fromhex(data.strip())

    lines: list[str] = []
    indent = 0
    i = 0
    closers = {
        Command.CLOSE_HEADER,
        Command.CLOSE_THREAD,
        Command.END_SCOPE_MAIN,
        Command.END_SCOPE_LOOP,
        Command.END_SCOPE_ATOMIC,
    }
    openers = {
        Command.OPEN_HEADER,
        Command.OPEN_THREAD,
        Command.START_SCOPE_MAIN,
        Command.START_SCOPE_LOOP,
        Command.START_SCOPE_ATOMIC,
    }

    while i < len(data):
        try:
            command = Command(data[i])
        except ValueError:
            lines.append(f"{'  ' * indent}??? 0x{data[i]:02X}")
            i += 1
            continue
        spec = SPEC.get(command, _NO_PARAMS)
        i += 1
        args: list[int] = []
        for _ in range(spec.params):
            chunk = data[i : i + spec.width]
            if len(chunk) < spec.width:
                lines.append(f"{'  ' * indent}{command.name} <truncated>")
                return lines
            args.append(int.from_bytes(chunk, U16_BYTEORDER))
            i += spec.width
        if command in closers:
            indent = max(0, indent - 1)
        rendered = ", ".join(
            f"{n}={v}" for n, v in zip(spec.names, args)
        )
        lines.append(f"{'  ' * indent}{command.name}" + (f"({rendered})" if args else ""))
        if command in openers:
            indent += 1
    return lines
