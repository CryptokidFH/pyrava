"""High-level client for talking to a Barava device."""

from __future__ import annotations

import colorsys
import logging
import threading
import warnings
import time
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence

from .animation import AnimationScript
from .const import (
    DEV_HANDLERS,
    VAR_BY_LABEL,
    Handler,
    HeaterHealth,
    Header,
    Privilege,
    Var,
)
from .discovery import CANDIDATE_TYPES, DiscoveredService, browse, scan
from .errors import BaravaError, TransportError
from .packet import (
    Batch,
    HexInt,
    Packet,
    SubPacket,
    encode_batch,
    encode_body,
    parse_groups,
    parse_response,
)
from .transport import DEFAULT_PATH, DEFAULT_PORT, Envelope, HttpTransport, Transport

__all__ = [
    "BaravaDevice",
    "PollingSession",
    "discover_devices",
    "rgb",
    "new_sender_id",
]

log = logging.getLogger("pyrava")

#: Interval sent in our ping-interval header, and the default poll cadence.
#: `barava_network_impl.md` recommends 800-1000ms for general use.
DEFAULT_PING_INTERVAL_MS = 1000

#: Hard floor from `barava_network_impl.md`: "The keepalive interval should be
#: no less than 500MS for maximum network and device stability." The device
#: itself advertises 200 in its response key block, which is below this; we
#: don't adopt a value under the floor.
MIN_PING_INTERVAL_MS = 500

#: The floor is explicitly allowed for data polling (e.g. heater temperature);
#: anything else should sit at DEFAULT_PING_INTERVAL_MS.
DATA_POLL_INTERVAL_MS = 500

#: Longest gap between queue-pop pings while waiting on a reply. Bounded
#: below by the 500ms floor rather than the old 250ms, which was pinging the
#: device twice as fast as `barava_network_impl.md` allows.
MAX_POLL_DELAY = MIN_PING_INTERVAL_MS / 1000.0

#: Zone indices in the order the device's own app selects them in a theme
#: header (``SELECT_ZONE 4, 2, 3, 0, 1``). Five zones, matching the hardware:
#: a top ring, a middle inner and outer ring, and a bottom inner and outer.
#:
#: Which index is which ring is only partly pinned down. A captured theme
#: described as setting "the first three zones" to red/green/blue set 4, 2
#: and 3, so those are the first three in this order. A theme described as
#: colouring "the bottom two zones" drove a gradient on zone 0. That leaves
#: 0 and 1 as the bottom pair and 4/2/3 as the top and middle group, without
#: fixing inner vs outer either way. ``examples/zone_probe.py`` lights one
#: zone at a time so you can label them for your unit.
ZONE_ORDER: tuple[int, ...] = (4, 2, 3, 0, 1)

#: Heater temperatures arrive as hundredths of a degree Fahrenheit
#: (6930 -> 69.30 F). Confirmed against hardware -- an earlier version
#: of this library assumed Celsius and only relabeled the same number.
HEATER_TEMP_SCALE = 100.0


def _f_to_c(fahrenheit: float | None) -> float | None:
    return None if fahrenheit is None else round((fahrenheit - 32) * 5 / 9, 2)


def describe_heater_health(raw: Any) -> str:
    """Human-readable ``HTHH`` label.

    ``0`` -> ``"OK"``, ``1`` -> ``"Fault"``, anything else numeric ->
    ``"Fault (code N)"`` so an unseen fault code still reads sensibly instead
    of raising, and ``None`` -> ``"unknown"`` when the field is absent.
    """
    if raw is None:
        return "unknown"
    try:
        return str(HeaterHealth(int(raw)))
    except (TypeError, ValueError):
        return f"Fault (code {raw})"


def rgb(r: int, g: int, b: int) -> int:
    """Pack three 8-bit channels into a 24-bit ``0xRRGGBB`` integer.

    Not used for the fill light: empirical testing showed ``FCLR`` isn't a
    packed RGB value (see :func:`rgb_to_hue` and
    :meth:`BaravaDevice.set_fill_hue`). Kept as a general-purpose utility in
    case another confirmed field turns out to actually use this layout.
    """
    for name, value in (("r", r), ("g", g), ("b", b)):
        if not 0 <= int(value) <= 255:
            raise ValueError(f"{name} must be 0-255, got {value}")
    return (int(r) << 16) | (int(g) << 8) | int(b)


def rgb_to_hue(r: int, g: int, b: int) -> float:
    """Approximate an RGB colour as a 0-360 hue for :meth:`~BaravaDevice.set_fill_color`.

    ``FCLR`` appears to expose only a single hue axis -- brightness is
    separate (``FBRT``) and there's no evidence of a saturation control -- so
    this keeps only the hue component of the given colour. Greys (including
    black and white) have no defined hue and convert to ``0``.
    """
    for name, value in (("r", r), ("g", g), ("b", b)):
        if not 0 <= int(value) <= 255:
            raise ValueError(f"{name} must be 0-255, got {value}")
    hue_fraction, _saturation, _value = colorsys.rgb_to_hsv(
        r / 255, g / 255, b / 255
    )
    return hue_fraction * 360


def new_sender_id(prefix: str = "~") -> str:
    """Generate a runtime session ID.

    Shaped like the ones the vendor app produces: a ``~`` followed by 12
    uppercase hex characters. The app makes a fresh one on every launch, which
    is why observed IDs change between sessions. Any value works as long as it
    is stable for the lifetime of your session and contains no protocol
    delimiters.
    """
    return f"{prefix}{uuid.uuid4().hex[:12].upper()}"


class BaravaDevice:
    """A single device, addressed by IP or hostname.

    ``sender_id`` identifies *this script*, not the device. It travels in the
    ``device-id`` header so the device can register you in its response queue,
    and it is the key of the sender block in a batch.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        sender_id: str | None = None,
        ping_interval: int = DEFAULT_PING_INTERVAL_MS,
        timeout: float = 5.0,
        envelope: Envelope = "raw",
        transport: Transport | None = None,
        name: str | None = None,
        device_id: str | None = None,
        follow_device_interval: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.sender_id = sender_id or new_sender_id()
        self._validate_sender_id(self.sender_id)
        self.ping_interval = int(ping_interval)
        self.name = name
        self.transport = transport or HttpTransport(
            host, port, timeout=timeout, envelope=envelope
        )

        self.device_id: str | None = device_id
        #: Interval the device reports in its response key block.
        self.reported_ping_interval: int | None = None
        self.follow_device_interval = follow_device_interval
        self._warned_unregistered_batch = False
        self._warned_low_interval = False
        self.privilege: Privilege | int | None = None
        self.last_info: dict[str, Any] = {}
        #: Raw text of the most recent response, kept for debugging.
        self.last_raw: str = ""
        #: ``(headers, body)`` of the most recent request.
        self.last_request: tuple[dict[str, str], str] | None = None
        #: Serialises transport access. A PollingSession runs its cycle on a
        #: background thread, so a direct setter called from your own thread
        #: would otherwise interleave two exchanges on one session.
        self._io_lock = threading.RLock()

    # -- construction helpers ---------------------------------------------

    @classmethod
    def from_service(cls, service: DiscoveredService, **kwargs: Any) -> "BaravaDevice":
        kwargs.setdefault("name", service.name)
        return cls(service.host, service.port, **kwargs)

    @staticmethod
    def _validate_sender_id(sender_id: str) -> None:
        if sender_id.strip() == "*":
            raise BaravaError(
                "'*' is not a valid sender ID. A wildcard in the sender block "
                "sets a keep-alive interval for every listening device and "
                "fires a network interrupt to extend the keep-alive signal. "
                "Use a concrete per-session ID (see new_sender_id())."
            )
        if not sender_id or any(c in sender_id for c in "<>={},!&$"):
            raise BaravaError(f"sender ID {sender_id!r} contains reserved characters")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        label = self.name or self.device_id or self.host
        return f"<BaravaDevice {label} at {self.host}:{self.port}>"

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "BaravaDevice":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    # -- core request path -------------------------------------------------

    def _headers(self, **extra: Any) -> dict[str, str]:
        headers: dict[str, Any] = {
            Header.DEVICE_ID.value: self.sender_id,
            Header.PING_INTERVAL.value: self.ping_interval,
        }
        headers.update(extra)
        return {str(k): str(v) for k, v in headers.items() if v is not None}

    def _exchange(
        self,
        handler: Handler | str | None,
        body: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, Any] | None = None,
    ) -> Batch:
        """One HTTP round trip. ``handler=None`` sends a bare queue-pop ping."""
        headers = self._headers()
        if handler is not None:
            headers[Header.BODY_HANDLER.value] = str(
                getattr(handler, "value", handler)
            )
            payload = encode_body(body) if body else encode_body()
        else:
            # A plain ping carries no handler and no body; it exists only to
            # pop whatever the device has queued for this session.
            payload = ""
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        with self._io_lock:
            log.debug("-> %s %s", headers, payload or "(empty)")
            raw = self.transport.send(headers, payload)
            log.debug("<- %s", raw)
            self.last_request = (headers, payload)
            self.last_raw = raw
            batch = parse_response(raw)
            self._absorb(batch)
        return batch

    def _absorb(self, batch: Batch) -> None:
        """Pick up identity the device volunteers in every response.

        The key block of a response carries the device's own ID and the
        interval it is running at. ``DVIFO`` bodies do not include ``DVID``,
        so this is the only reliable source.
        """
        if batch.device_id and batch.device_id != self.sender_id:
            self.device_id = batch.device_id
        if batch.ping_interval:
            self.reported_ping_interval = batch.ping_interval
            if self.follow_device_interval:
                # Firmware 1.0.1 advertises 200ms, below the 500ms floor in
                # `barava_network_impl.md`. Following it verbatim would ping
                # 2.5x faster than the vendor says is safe for device
                # stability, so the floor wins.
                if batch.ping_interval < MIN_PING_INTERVAL_MS:
                    if not self._warned_low_interval:
                        log.warning(
                            "device advertises a %dms ping interval, below the "
                            "%dms minimum in the vendor's design notes; using "
                            "%dms instead. Pass follow_device_interval=False "
                            "to pin your own value.",
                            batch.ping_interval,
                            MIN_PING_INTERVAL_MS,
                            MIN_PING_INTERVAL_MS,
                        )
                        self._warned_low_interval = True
                    self.ping_interval = MIN_PING_INTERVAL_MS
                else:
                    self.ping_interval = batch.ping_interval

    def ping(self) -> Batch:
        """Pop the response queue without issuing a command."""
        return self._exchange(None)

    def request(
        self,
        handler: Handler | str,
        body: Mapping[str, Any] | None = None,
        *,
        extra_headers: Mapping[str, Any] | None = None,
        wait: bool = True,
        max_polls: int = 3,
        poll_delay: float | None = None,
    ) -> Batch:
        """Send one packet and return the device's response.

        This is not a matched request/response protocol. The device parks
        replies in a per-session queue and flushes them on a later ping, so
        the HTTP response to a command often carries the answer to an
        *earlier* one. With ``wait=True`` this sends the command and then
        pings until a packet tagged with ``handler`` comes back.

        Setters have no useful reply; pass ``wait=False`` to skip the polling
        and save the round trips.
        """
        label = str(getattr(handler, "value", handler))
        if label in {h.value for h in DEV_HANDLERS} and self.privilege in (
            None,
            Privilege.USER,
        ):
            log.warning(
                "%s requires elevated privilege; call set_device_type() with a "
                "valid key first",
                label,
            )

        batch = self._exchange(handler, body, extra_headers)
        if not wait or self._satisfies(batch, label):
            return batch

        delay = (
            poll_delay
            if poll_delay is not None
            else min(self.ping_interval / 1000.0, MAX_POLL_DELAY)
        )
        for _ in range(max_polls):
            time.sleep(delay)
            batch = self._exchange(None)
            if self._satisfies(batch, label):
                return batch

        log.debug("no %s packet after %d poll(s); returning last queue", label, max_polls)
        return batch

    @staticmethod
    def _satisfies(batch: Batch, label: str) -> bool:
        """Has the queue produced the reply we're waiting on?"""
        if batch.by_handler(label):
            return True
        # Firmware that answers inline, without a body-handler tag, still
        # counts: one untagged packet carrying data is the reply.
        return (
            len(batch) == 1
            and batch.packets[0].handler is None
            and bool(batch.packets[0].body)
        )

    def send_batch(self, subpackets: Sequence[SubPacket]) -> Batch:
        """Send several packets to this device inside one request.

        A batch is many packets aimed at one device, delivered as a single
        request; it is not a way to address several devices at once.
        """
        owner = self.device_id
        if owner is None:
            if not self._warned_unregistered_batch:
                log.warning(
                    "batching before the device ID is known; call register() "
                    "first. Falling back to the session ID, which the device "
                    "may reject. (This warning won't repeat for this device.)"
                )
                self._warned_unregistered_batch = True
            owner = self.sender_id
        headers = self._headers(**{Header.BATCHED_PACKET.value: ""})
        payload = encode_batch(owner, self.ping_interval, list(subpackets))
        log.debug("-> [batch] %s %s", headers, payload)
        raw = self.transport.send(headers, payload)
        log.debug("<- %s", raw)
        return parse_response(raw)

    # -- convenience -------------------------------------------------------

    @staticmethod
    def _first(batch: Batch, *keys: Any) -> dict[str, Any]:
        merged = batch.flatten()
        if not keys:
            return merged
        wanted = {str(getattr(k, "value", k)) for k in keys}
        return {k: v for k, v in merged.items() if k in wanted}

    # -- information -------------------------------------------------------

    def get_device_info(self) -> dict[str, Any]:
        """Request ``DVIFO`` and cache the result.

        This doubles as the registration ping: the first request from a new
        sender ID registers you in the device's response queue.
        """
        batch = self.request(Handler.DEVICE_INFO)
        info = batch.flatten(Handler.DEVICE_INFO) or batch.flatten()
        if info:
            self.last_info = info
            # DVID is documented but firmware 1.0.1 omits it; the device ID
            # comes from the response key block via _absorb().
            device_id = info.get(Var.DEVICE_ID.value)
            if device_id:
                self.device_id = str(device_id)
            if self.name is None and info.get(Var.DEVICE_NAME.value):
                self.name = str(info[Var.DEVICE_NAME.value])
            if Var.DEVICE_TYPE.value in info:
                try:
                    self.privilege = Privilege(int(info[Var.DEVICE_TYPE.value]))
                except (TypeError, ValueError):
                    self.privilege = info[Var.DEVICE_TYPE.value]
        return info

    def get_heater_info(self) -> dict[str, Any]:
        return self.request(Handler.GET_HEATER_INFO).flatten(Handler.GET_HEATER_INFO)

    def read_temperatures_f(self) -> dict[str, float | None]:
        """Current and target heater temperature, in Fahrenheit, one exchange.

        Confirmed against hardware: ``HTCT``/``HTST`` are Fahrenheit, not
        Celsius as first assumed — 6930 reads as 69.30 °F, not 69.30 °C.
        """
        info = self.get_heater_info()

        def scaled(key: str) -> float | None:
            raw = info.get(key)
            return None if raw is None else raw / HEATER_TEMP_SCALE

        return {
            "current": scaled(Var.HEATER_CUR_TEMP.value),
            "target": scaled(Var.HEATER_SET_TEMP.value),
        }

    def read_temperature_f(self) -> float | None:
        """Current heater temperature in degrees Fahrenheit.

        ``HTCT`` is transmitted in hundredths of a degree (6930 -> 69.30).
        """
        raw = self.get_heater_info().get(Var.HEATER_CUR_TEMP.value)
        return None if raw is None else raw / HEATER_TEMP_SCALE

    def read_target_temperature_f(self) -> float | None:
        """User-set target temperature in degrees Fahrenheit."""
        raw = self.get_heater_info().get(Var.HEATER_SET_TEMP.value)
        return None if raw is None else raw / HEATER_TEMP_SCALE

    def heater_status(self) -> str:
        """Human-readable heater health: ``"OK"``, ``"Fault"``, or
        ``"Fault (code N)"`` for a code that isn't 0 or 1.

        Confirmed against the app: its "Heater Status" readout shows "Ok"
        when ``HTHH`` is 0. The fault side is inferred, not yet observed.
        """
        raw = self.get_heater_info().get(Var.HEATER_HEALTH.value)
        return describe_heater_health(raw)

    def is_heater_ok(self) -> bool | None:
        """Whether the heater currently reports no fault (``HTHH == 0``).

        Returns ``None`` if ``HTHH`` wasn't in the response at all.
        """
        raw = self.get_heater_info().get(Var.HEATER_HEALTH.value)
        return None if raw is None else int(raw) == HeaterHealth.OK

    # -- deprecated Celsius aliases -----------------------------------------
    # Earlier versions assumed HTCT/HTST were already Celsius and just
    # relabeled the raw value. That was wrong: the device reports Fahrenheit.
    # These aliases now convert properly rather than silently mislabeling.

    def read_temperatures_c(self) -> dict[str, float | None]:
        """Deprecated: use :meth:`read_temperatures_f`.

        Earlier versions returned the raw Fahrenheit value mislabeled as
        Celsius. This now converts correctly, but the name is being retired
        to avoid another silent unit mismatch.
        """
        warnings.warn(
            "read_temperatures_c() returned mislabeled Fahrenheit values "
            "before this fix; it now converts properly, but the method is "
            "deprecated in favour of read_temperatures_f(). Update callers.",
            DeprecationWarning,
            stacklevel=2,
        )
        return {k: _f_to_c(v) for k, v in self.read_temperatures_f().items()}

    def read_temperature_c(self) -> float | None:
        """Deprecated: use :meth:`read_temperature_f`. See that method's note."""
        warnings.warn(
            "read_temperature_c() returned mislabeled Fahrenheit values "
            "before this fix; it now converts properly, but the method is "
            "deprecated in favour of read_temperature_f(). Update callers.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _f_to_c(self.read_temperature_f())

    def read_target_temperature_c(self) -> float | None:
        """Deprecated: use :meth:`read_target_temperature_f`."""
        warnings.warn(
            "read_target_temperature_c() returned mislabeled Fahrenheit "
            "values before this fix; it now converts properly, but the "
            "method is deprecated in favour of read_target_temperature_f().",
            DeprecationWarning,
            stacklevel=2,
        )
        return _f_to_c(self.read_target_temperature_f())

    def register(self) -> str | None:
        """Register this session and return the device's own ID."""
        self.get_device_info()
        return self.device_id

    # -- naming and groups -------------------------------------------------

    def set_device_name(self, name: str) -> Batch:
        return self.request(Handler.SET_DEVICE_NAME, {Var.DEVICE_NAME.value: name}, wait=False)

    def add_group(self, group_name: str, group_id: str) -> Batch:
        return self.request(
            Handler.ADD_DEVICE_GROUP,
            {Var.GROUP_NAME.value: group_name, Var.GROUP_ID.value: group_id},
        )

    def remove_group(self, group_id: str) -> Batch:
        return self.request(Handler.REMOVE_DEVICE_GROUP, {Var.GROUP_ID.value: group_id}, wait=False)

    def wipe_groups(self) -> Batch:
        return self.request(Handler.WIPE_DEVICE_GROUPS, wait=False)

    # -- lighting ----------------------------------------------------------

    def set_state(self, on: bool | int) -> Batch:
        """Turn the light on or off."""
        return self.request(Handler.SET_DEVICE_STATE, {Var.DEVICE_STATE.value: int(on)}, wait=False)

    def set_fill_hue(self, hue_degrees: float) -> Batch:
        """Set the fill light's hue directly, 0-360.

        Empirical finding, not confirmed against firmware source: ``FCLR``
        is not a packed ``0xRRGGBB`` int as first assumed. Testing several
        values against the actual light showed a hue-like value inside
        0-360 producing distinct, plausible colours (128 -> green, 255 ->
        violet/purple, 360 -> magenta near the wraparound seam), while any
        value *outside* that range came back red regardless of magnitude --
        consistent with an out-of-range fallback rather than a colour.

        The exact rotation and zero-point haven't been independently
        verified beyond those spot checks. ``examples/fill_color_probe.py``
        sweeps the range so you can confirm or correct the mapping for your
        unit.
        """
        if not 0 <= hue_degrees <= 360:
            raise ValueError(
                f"hue must be 0-360, got {hue_degrees}. Values outside this "
                "range were observed to fall back to red rather than "
                "erroring, so this is enforced here instead of on-device."
            )
        return self.request(
            Handler.SET_FILL_COLOR,
            {Var.FILL_COLOR.value: int(round(hue_degrees))},
            wait=False,
        )

    def set_fill_color(self, color: tuple[int, int, int] | float) -> Batch:
        """Set the fill light's colour.

        Accepts an ``(r, g, b)`` tuple (0-255 each), converted to a hue via
        :func:`rgb_to_hue` -- saturation and lightness don't survive, since
        the device appears to expose only a single hue axis. A bare number
        is sent directly as a hue in degrees; see :meth:`set_fill_hue` for
        what's confirmed versus inferred about this field.
        """
        if isinstance(color, (tuple, list)):
            hue = rgb_to_hue(*color)
        else:
            hue = float(color)
        return self.set_fill_hue(hue)

    def set_fill_state(self, on: bool | int) -> Batch:
        return self.request(Handler.SET_FILL_STATE, {Var.FILL_STATE.value: int(on)}, wait=False)

    def set_fill_brightness(self, brightness: int) -> Batch:
        return self.request(Handler.SET_FILL_BRIGHTNESS, {Var.FILL_BRIGHTNESS.value: int(brightness)}, wait=False)

    @property
    def groups(self) -> dict[str, str]:
        """Group memberships as ``{group_id: group_name}`` from the last info.

        ``DVGR`` arrives as an array of blocks — ``&{<545d577b=&My-Room>}`` —
        so the raw value is a list of block strings until decoded.
        """
        return parse_groups(self.last_info.get(Var.DEVICE_GROUPS.value, []))

    def set_desk_color(self, temperature: int) -> Batch:
        """Set the desk light's colour temperature.

        `network.md` calls ``DSKCL`` "sets desk light temp" and firmware
        reports ``DCLR`` as a small integer (292 observed), so this is a
        colour temperature rather than a packed RGB value. Passing an RGB
        tuple here would send a nonsense number, so it isn't accepted.
        """
        return self.request(
            Handler.SET_DESK_COLOR,
            {Var.DESK_COLOR.value: int(temperature)},
            wait=False,
        )

    def set_desk_brightness(self, brightness: int) -> Batch:
        return self.request(Handler.SET_DESK_BRIGHTNESS, {Var.DESK_BRIGHTNESS.value: int(brightness)}, wait=False)

    def set_desk_state(self, on: bool | int) -> Batch:
        return self.request(Handler.SET_DESK_STATE, {Var.DESK_STATE.value: int(on)}, wait=False)

    # -- heater ------------------------------------------------------------

    def set_heater_state(self, on: bool | int) -> Batch:
        return self.request(Handler.SET_HEATER_STATE, {Var.HEATER_STATE.value: int(on)}, wait=False)

    # -- spectrum and animation -------------------------------------------

    def set_spectrum(
        self, on: bool | int, palette: Sequence[int] | None = None
    ) -> Batch:
        """Toggle the spectrum visualiser, optionally setting its palette."""
        body: dict[str, Any] = {}
        if palette:
            body[Var.SPEC_PALLETE.value] = [int(c) for c in palette]
        body[Var.SPEC_ON.value] = int(on)
        return self.request(Handler.TOGGLE_SPECTRUM, body, wait=False)

    def upload_animation(
        self, script: AnimationScript | bytes | str, *, spectrum: bool | int = 0
    ) -> Batch:
        """Compile and send an animation to the ``CMPAM`` handler."""
        if isinstance(script, AnimationScript):
            payload = script.to_hex()
        elif isinstance(script, (bytes, bytearray)):
            payload = bytes(script).hex().upper()
        else:
            payload = str(script).strip().upper()
        return self.request(
            Handler.COMPILE_ANIMATION,
            {Var.ANIMATION_DATA.value: payload, Var.SPEC_ON.value: int(spectrum)},
            wait=False,
        )

    # -- zones -------------------------------------------------------------

    def build_theme(
        self,
        solids: Mapping[int, tuple[int, int, int]] | None = None,
        gradients: Mapping[int, Sequence[tuple[int, int, int, int]]] | None = None,
        *,
        smooth: bool = False,
        zones: Sequence[int] = ZONE_ORDER,
    ) -> AnimationScript:
        """Compose a static theme without sending it.

        Reproduces the structure the device's own app emits: a header that
        selects every zone, then one atomic scope holding a per-zone fill.
        ``solids`` maps a zone to one ``(r, g, b)``; ``gradients`` maps a zone
        to ``(pos, r, g, b)`` stops, where ``pos`` spans 0-255 across the
        zone. A zone in neither mapping is left dark, which is how the app
        writes an all-off theme.

        Returns the script so you can inspect or extend it; pass it to
        :meth:`upload_animation`, or use :meth:`set_zone_colors` to do both.
        """
        script = AnimationScript()
        with script.header(0):
            script.reset_l2()
            for zone in zones:
                script.select_zone(zone)
        with script.thread(0):
            with script.atomic():
                for zone, stops in (gradients or {}).items():
                    script.reset_l2()
                    script.select_zone(zone)
                    script.gradient(*stops, smooth=smooth)
                for zone, (r, g, b) in (solids or {}).items():
                    script.reset_l2()
                    script.select_zone(zone)
                    script.set_rgb(r, g, b)
        return script

    def set_zone_colors(
        self,
        solids: Mapping[int, tuple[int, int, int]] | None = None,
        gradients: Mapping[int, Sequence[tuple[int, int, int, int]]] | None = None,
        *,
        smooth: bool = False,
        zones: Sequence[int] = ZONE_ORDER,
    ) -> Batch:
        """Set each LED zone's colour in one upload.

        ``light.set_zone_colors({4: (255, 0, 0), 2: (0, 255, 0)})`` lights
        two zones and leaves the rest dark. Unlike ``set_fill_hue``, this
        takes real RGB -- the zone path carries all three channels, so
        saturation survives here.
        """
        return self.upload_animation(
            self.build_theme(solids, gradients, smooth=smooth, zones=zones)
        )

    def set_zone_gradient(
        self,
        zone: int,
        *stops: tuple[int, int, int, int],
        smooth: bool = False,
    ) -> Batch:
        """Run a gradient across one zone, leaving the others dark.

        ``light.set_zone_gradient(0, (0, 7, 0, 63), (255, 0, 4, 54))``
        """
        return self.set_zone_colors(gradients={zone: stops}, smooth=smooth)

    def clear_zones(self, *, zones: Sequence[int] = ZONE_ORDER) -> Batch:
        """Turn every addressable zone off, the way the app's blank theme does."""
        return self.upload_animation(self.build_theme(zones=zones))

    # -- maintenance -------------------------------------------------------

    def check_for_updates(self) -> Batch:
        return self.request(Handler.RUN_UPDATE_CHECK)

    def run_update(self) -> Batch:
        return self.request(Handler.RUN_UPDATE)

    def wipe_device(self) -> Batch:
        """Factory-reset the device. This clears all stored configuration."""
        return self.request(Handler.WIPE_DEVICE_INFO)

    def disconnect_wifi(self) -> Batch:
        return self.request(Handler.DISCONNECT_WIFI)

    def set_device_type(self, key: str) -> Batch:
        """Raise privilege using a ``BARAVA-...`` key.

        An invalid key immediately revokes elevated privileges and forces the
        device back to user mode, so do not call this speculatively.
        """
        batch = self.request(Handler.SET_DEVICE_TYPE, {Var.DEVICE_TYPE.value: key})
        level = batch.flatten(Handler.SET_DEVICE_TYPE).get(
            Var.DEVICE_TYPE.value
        ) or batch.flatten().get(Var.DEVICE_TYPE.value)
        if level is not None:
            try:
                self.privilege = Privilege(int(level))
            except (TypeError, ValueError):
                self.privilege = level
        return batch

    # -- polling -----------------------------------------------------------

    def poll(
        self,
        on_packet: Callable[[Packet], None] | None = None,
        *,
        interval: float | None = None,
        keepalive: Handler | str | Sequence[Handler | str] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        enforce_interval_floor: bool = True,
    ) -> "PollingSession":
        """Start a background ping loop.

        This is the intended primary mode of communication. The vendor's
        design notes describe a cyclical interface where the keepalive loop
        carries both device state updates and your commands, rather than
        one-off requests: use :meth:`PollingSession.enqueue` to send commands
        along the cycle instead of firing separate exchanges.

        The device holds your responses in a queue and flushes them, batched,
        on your next ping. Missing your own ping interval gets you
        unregistered, so keep this running for as long as you want events.

        ``keepalive`` controls what goes out each tick when nothing else is
        queued:

        * ``None`` (default) -- a bare ping. Pops whatever is already queued
          but requests nothing new, so ``session.on(...)`` callbacks only
          fire for replies to commands sent some other way (``enqueue()``,
          or a direct call on ``device`` from another thread).
        * a single handler -- re-sent, unwaited, every tick.
        * a sequence of handlers -- all re-sent together as a batch every
          tick, so several ``session.on(...)`` subscriptions can all stay
          live. This is what you want for a general "watch" loop.

        Because replies are deferred by one ping, a handler passed here
        starts appearing in callbacks on the *second* tick, not the first.

        ``interval`` is clamped to :data:`MIN_PING_INTERVAL_MS` (500ms), the
        floor the vendor gives for device stability; 500ms is explicitly
        allowed for data polling like heater temperature, and
        :data:`DEFAULT_PING_INTERVAL_MS` (1000ms) suits everything else. Pass
        ``enforce_interval_floor=False`` only for tests against a mock.
        """
        chosen = interval if interval is not None else self.ping_interval / 1000.0
        floor = MIN_PING_INTERVAL_MS / 1000.0
        if enforce_interval_floor and chosen < floor:
            log.warning(
                "poll interval %.3fs is below the %.3fs minimum in the "
                "vendor's design notes; using %.3fs.", chosen, floor, floor
            )
            chosen = floor
        session = PollingSession(
            self,
            interval=chosen,
            keepalive=keepalive,
            on_packet=on_packet,
            on_error=on_error,
        )
        session.start()
        return session


class PollingSession:
    """Background thread that pings a device and dispatches its response queue."""

    def __init__(
        self,
        device: BaravaDevice,
        *,
        interval: float,
        keepalive: Handler | str | Sequence[Handler | str] | None = None,
        on_packet: Callable[[Packet], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.device = device
        self.interval = max(0.02, float(interval))
        self.keepalive = keepalive
        self.on_packet = on_packet
        self.on_error = on_error
        self._handlers: dict[str, list[Callable[[Packet], None]]] = {}
        self._queue: list[SubPacket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: Set to cut the current inter-tick wait short, so an urgent command
        #: goes out immediately instead of waiting for the next interval.
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_batch: Batch | None = None

    # -- subscription ------------------------------------------------------

    def on(
        self, handler: Handler | str, callback: Callable[[Packet], None]
    ) -> "PollingSession":
        """Register a callback for packets carrying a given ``body-handler``."""
        label = str(getattr(handler, "value", handler))
        self._handlers.setdefault(label, []).append(callback)
        return self

    def enqueue(
        self,
        handler: Handler | str,
        body: Mapping[str, Any] | None = None,
        *,
        urgent: bool = False,
    ) -> None:
        """Queue a command to ride out on the keepalive cycle.

        The vendor's design notes split responses by importance. A passive
        one is appended to the keepalive packet and goes out on the next
        tick, never interrupting the device. An important one -- their
        examples are device colour and privilege -- is appended to the
        queued keepalive packet *and* triggers an interrupt that forces it
        out immediately.

        ``urgent=True`` is that second case: the command joins whatever is
        already queued and the tick fires now rather than at the next
        interval, so it still goes out as one merged packet rather than a
        separate exchange racing the loop.
        """
        with self._lock:
            self._queue.append(
                SubPacket(str(getattr(handler, "value", handler)), dict(body or {}))
            )
        if urgent:
            self._wake.set()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="pyrava-poll", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()  # cut any in-progress interval wait short
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def __enter__(self) -> "PollingSession":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- loop --------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                with self._lock:
                    pending, self._queue = self._queue, []
                if pending:
                    batch = self.device.send_batch(pending)
                elif self.keepalive is None:
                    batch = self.device.ping()
                elif isinstance(self.keepalive, (list, tuple)):
                    if len(self.keepalive) == 1:
                        batch = self.device.request(self.keepalive[0], wait=False)
                    else:
                        batch = self.device.send_batch(
                            [SubPacket(str(getattr(h, "value", h)), {})
                             for h in self.keepalive]
                        )
                else:
                    batch = self.device.request(self.keepalive, wait=False)
                self.last_batch = batch
                self._dispatch(batch)
            except (TransportError, BaravaError) as exc:
                log.debug("poll cycle failed: %s", exc)
                if self.on_error:
                    self.on_error(exc)
            elapsed = time.monotonic() - started
            if self._stop.is_set():
                break
            # Wait on _wake rather than _stop so an urgent enqueue can cut the
            # interval short; stop() sets both, so shutdown is still prompt.
            self._wake.wait(max(0.0, self.interval - elapsed))
            self._wake.clear()

    def _dispatch(self, batch: Batch) -> None:
        """Route each packet to its callbacks, queueing any response they give.

        The vendor's design describes mirrored routing on both ends, where a
        handler callback "should always produce a client response". A
        callback here may return ``None`` (no reply, the common case), a
        single :class:`SubPacket`, or an iterable of them; anything returned
        is queued to ride out on the next cycle.
        """
        for packet in batch:
            if self.on_packet:
                self._queue_reply(self.on_packet(packet))
            for callback in self._handlers.get(packet.handler or "", []):
                self._queue_reply(callback(packet))

    def _queue_reply(self, reply: Any) -> None:
        if reply is None:
            return
        replies = [reply] if isinstance(reply, SubPacket) else list(reply)
        if not replies:
            return
        with self._lock:
            for item in replies:
                if not isinstance(item, SubPacket):
                    log.warning(
                        "handler callback returned %r, expected SubPacket or "
                        "None; ignoring", type(item).__name__
                    )
                    continue
                self._queue.append(item)


# --------------------------------------------------------------------------
# Discovery front door
# --------------------------------------------------------------------------


def discover_devices(
    timeout: float = 5.0,
    *,
    sender_id: str | None = None,
    ping_interval: int = DEFAULT_PING_INTERVAL_MS,
    register: bool = True,
    service_type: str | Sequence[str] | None = None,
    fallback_scan: bool = True,
    interfaces: Iterable[str] | None = None,
    **kwargs: Any,
) -> list[BaravaDevice]:
    """Find devices via mDNS and, by default, register with each one.

    Tries the documented service type first, then the other types a device
    like this plausibly advertises under. If nothing turns up and
    ``fallback_scan`` is set, enumerates every service type on the network and
    keeps whatever looks like a Barava instance.

    Registration sends ``DVIFO``, which populates each device's real
    ``device_id`` from the response key block. Devices that fail to answer are
    still returned, with ``device_id`` left as ``None``.

    Discovery depends on multicast reaching your machine. If this comes back
    empty while a direct ``BaravaDevice(ip)`` works, the cause is almost
    always local: a firewall blocking inbound UDP 5353, a VPN or hypervisor
    adapter capturing multicast, or client isolation on the access point. Pass
    ``interfaces=["192.168.1.x"]`` to pin Zeroconf to the right adapter.
    """
    sender_id = sender_id or new_sender_id()

    types = CANDIDATE_TYPES if service_type is None else service_type
    services = browse(
        timeout,
        service_type=types,
        interfaces=interfaces,
        require_hint=service_type is None,
    )
    if not services and fallback_scan and service_type is None:
        log.info("no match on the usual service types; scanning all of them")
        services = [s for s in scan(timeout, interfaces=interfaces)
                    if s.looks_like_barava]

    devices: list[BaravaDevice] = []
    for service in services:
        device = BaravaDevice.from_service(
            service, sender_id=sender_id, ping_interval=ping_interval, **kwargs
        )
        if service.device_id:
            device.device_id = service.device_id
        if register:
            try:
                device.register()
            except (TransportError, BaravaError) as exc:
                log.warning("registration failed for %s: %s", service, exc)
        devices.append(device)
    return devices
