"""High-level client for talking to a Barava device."""

from __future__ import annotations

import colorsys
import logging
import random
import threading
import warnings
import time
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence

from .animation import AnimationScript
from .const import (
    DEV_HANDLERS,
    VAR_BY_LABEL,
    ZONE_GROUPS,
    Handler,
    HeaterHealth,
    Header,
    Privilege,
    Var,
    Zone,
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

#: Minimum seconds between animation uploads. The firmware author warns that
#: loading a compiled animation forces heavy context switching in the drivers
#: and interpreter, and that flooding the lights with colour packets can wedge
#: the device. This is a conservative default, not a measured floor -- adjust
#: per device via ``BaravaDevice.min_animation_gap``.
DEFAULT_MIN_ANIMATION_GAP_S = 1.0

#: Longest gap between queue-pop pings while waiting on a reply. Bounded
#: below by the 500ms floor rather than the old 250ms, which was pinging the
#: device twice as fast as `barava_network_impl.md` allows.
MAX_POLL_DELAY = MIN_PING_INTERVAL_MS / 1000.0

#: Zone indices in the order the device's own app selects them in a theme
#: header. Confirmed by lighting each zone in turn: 4 is the top ring, 2/3
#: are the middle ring's inner/outer, 0/1 are the bottom ring's inner/outer.
#: See :class:`~pyrava.const.Zone` for named access.
ZONE_ORDER: tuple[int, ...] = (
    Zone.TOP, Zone.MIDDLE_INNER, Zone.MIDDLE_OUTER,
    Zone.BOTTOM_INNER, Zone.BOTTOM_OUTER,
)

#: Heater temperatures arrive as hundredths of a degree Fahrenheit
#: (6930 -> 69.30 F). Confirmed against hardware -- an earlier version
#: of this library assumed Celsius and only relabeled the same number.
HEATER_TEMP_SCALE = 100.0


def _f_to_c(fahrenheit: float | None) -> float | None:
    return None if fahrenheit is None else round((fahrenheit - 32) * 5 / 9, 2)


def describe_heater_health(raw: Any) -> str:
    """Human-readable ``HTHH`` label.

    ``0`` -> ``"OK"``, ``1`` -> ``"Fuse trip"`` (both confirmed against the
    app, which shows the latter as "FUSETRIP"), anything else numeric ->
    ``"Fault (code N)"`` so a code firmware adds later still reads sensibly
    instead of raising, and ``None`` -> ``"unknown"`` when the field is
    absent.
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


def punch_color(
    rgb: tuple[int, int, int],
    *,
    saturation: float = 1.5,
    min_value: float | None = None,
    value: float | None = None,
    value_gamma: float | None = None,
) -> tuple[int, int, int]:
    """Boost an RGB colour's saturation, leaving hue and brightness alone.

    Washed-out output on these LEDs tracks low *saturation*, not low
    brightness. A capture from the device's own app sets a "dimmed blue" as
    fully saturated (1.00) at 25% brightness, and it reads as a clean deep
    blue -- so dim colours are perfectly legible as long as they're
    saturated. By contrast a screen-derived colour like ``(68, 43, 43)``
    sits at 0.37 saturation and similar brightness, and that is what looks
    pale.

    Four independent levers, all off unless asked for except saturation:

    * ``saturation`` -- multiply saturation, capped at 1.0. Default 1.5.
    * ``value_gamma`` -- raise brightness by ``v ** gamma``. With gamma
      below 1 this lifts dark colours much more than bright ones, so the
      palette gets brighter while keeping its *relative* brightness
      ordering. Usually the best middle ground: 0.5 is a good starting
      point.
    * ``value`` -- pin brightness outright, e.g. ``value=1.0``. Maximum
      vibrancy, but it flattens every colour to the same brightness and so
      discards the relative-brightness information entirely. That loss is
      what reads as "less accurate" against the source image.
    * ``min_value`` -- floor brightness without touching anything above the
      floor.

    Precedence when several are given: ``value`` beats ``value_gamma``
    beats ``min_value``.
    """
    h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in rgb])
    s = min(1.0, s * saturation)
    if value is not None:
        v = value
    elif value_gamma is not None:
        v = v ** value_gamma
    elif min_value is not None:
        v = max(min_value, v)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def generate_gradient_stops(
    colors: Sequence[tuple[int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Turn a flat list of colours into evenly spaced ``(pos, r, g, b)`` stops.

    Spacing is ``i * (256 // N)`` for the i-th of N colours -- confirmed
    against two independently captured gradients that both used exactly this
    spacing for three stops (0, 85, 170), not the more obvious ``i * 255 /
    (N-1)`` that would run edge-to-edge. That matters because every zone is a
    physical *ring*: this spacing leaves an implicit final segment
    wrapping from the last stop back to the first, dividing the ring evenly
    over all N segments including the seam, rather than compressing the
    colours into 0-255 and leaving one oddly-sized wraparound gap.

    Only confirmed at N=3. The formula is applied uniformly for other N on
    the assumption it generalises, but hasn't been independently checked
    past three stops.
    """
    colors = list(colors)
    if len(colors) < 2:
        raise ValueError(
            "need at least two colours to build a gradient; use "
            "set_zone_colors() for a single solid colour"
        )
    step = 256 // len(colors)
    return [(i * step, r, g, b) for i, (r, g, b) in enumerate(colors)]


def _expand_zone_key(key: int | str) -> tuple[int, ...]:
    """Resolve a zone key to one or more zone indices.

    A plain zone (int or :class:`~pyrava.const.Zone`) resolves to itself. A
    string looks up :data:`~pyrava.const.ZONE_GROUPS` and expands to every
    zone in that group.
    """
    if isinstance(key, str):
        try:
            return tuple(int(z) for z in ZONE_GROUPS[key])
        except KeyError:
            raise ValueError(
                f"unknown zone group {key!r}; known groups: "
                f"{sorted(ZONE_GROUPS)}"
            ) from None
    return (int(key),)


def _lerp_rgb(
    a: tuple[int, int, int], b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _span_gradient_stops(
    colors: Sequence[tuple[int, int, int]], zone_count: int
) -> list[list[tuple[int, int, int, int]]]:
    """Split one continuous gradient across ``zone_count`` zones.

    Treats the zones as consecutive segments of a single ring: colour ``i``
    of N sits at fractional position ``i * zone_count / N`` along the whole
    span, with the sequence closing back to colour 0 at the far end (the
    same wraparound convention :func:`generate_gradient_stops` uses within
    one zone, generalised across several). Each zone's boundary colours are
    linearly interpolated so the last stop of one zone exactly matches the
    first stop of the next -- no seam between zones, provided the physical
    zones are actually adjacent in the order given.

    This is a client-side construction, not a device feature: there's no
    evidence the firmware understands a gradient spanning multiple zones,
    and it hasn't been checked against real hardware for anything other
    than exact even spacing within a single zone. Nothing here contradicts
    that model -- boundary interpolation is the same linear RGB blend
    ``MAP_LINEAR`` already does within a zone -- but treat it as unverified
    until you've looked at it on the lamp.
    """
    n = len(colors)
    if n < 2:
        raise ValueError("need at least two colours to build a gradient")
    if zone_count < 1:
        raise ValueError("zone_count must be at least 1")

    key_positions = [i * zone_count / n for i in range(n)]
    keyframes = list(zip(key_positions, colors)) + [(float(zone_count), colors[0])]

    def interpolate(pos: float) -> tuple[int, int, int]:
        for (p0, c0), (p1, c1) in zip(keyframes, keyframes[1:]):
            if p0 <= pos <= p1:
                t = 0.0 if p1 == p0 else (pos - p0) / (p1 - p0)
                return _lerp_rgb(c0, c1, t)
        return keyframes[-1][1]

    per_zone: list[list[tuple[int, int, int, int]]] = []
    for zone in range(zone_count):
        r, g, b = interpolate(float(zone))
        stops: list[tuple[int, int, int, int]] = [(0, r, g, b)]
        for pos, colour in keyframes[:-1]:
            if zone < pos < zone + 1:
                local = round((pos - zone) * 255)
                if 0 < local < 255 and local != stops[-1][0]:
                    stops.append((local, *colour))
        r, g, b = interpolate(float(zone + 1))
        if stops[-1][0] != 255:
            stops.append((255, r, g, b))
        per_zone.append(stops)
    return per_zone


#: Public alias for :func:`_span_gradient_stops`, useful for inspecting what
#: "span" style will produce without touching a device.
span_gradient_stops = _span_gradient_stops


def _spread_rotation(
    rotate: int | Mapping[int | str, int] | None,
    zones: Sequence[int],
) -> dict[int, int] | None:
    """Normalise a ``rotate`` argument to a per-zone mapping.

    Accepts a bare int, meaning "spin every target zone at this speed", or
    an explicit ``{zone: amount}`` mapping for per-zone control.
    """
    if rotate is None:
        return None
    if isinstance(rotate, Mapping):
        out: dict[int, int] = {}
        for key, amount in rotate.items():
            for zone in _expand_zone_key(key):
                out[zone] = int(amount)
        return out or None
    return {zone: int(rotate) for zone in zones}


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
        if self.ping_interval < MIN_PING_INTERVAL_MS:
            log.warning(
                "ping_interval=%d is below the %dms minimum for device "
                "stability; the device may drop the session or become "
                "unstable.",
                self.ping_interval, MIN_PING_INTERVAL_MS,
            )
        self.name = name
        self.transport = transport or HttpTransport(
            host, port, timeout=timeout, envelope=envelope
        )

        self.device_id: str | None = device_id
        #: Interval the device reports in its response key block.
        self.reported_ping_interval: int | None = None
        self.follow_device_interval = follow_device_interval
        self._warned_unregistered_batch = False
        #: Minimum seconds between animation uploads; see upload_animation().
        self.min_animation_gap: float = DEFAULT_MIN_ANIMATION_GAP_S
        self._last_animation_upload: float | None = None
        # Best-effort record of what this session last told the device each
        # zone should show. There's no device readback -- see
        # set_zone_state()'s docstring for what this can and can't do.
        self._zone_solids: dict[int, tuple[int, int, int]] = {}
        self._zone_gradients: dict[int, tuple[tuple[int, int, int, int], ...]] = {}
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
                # The device reports a value below the 500ms floor from
                # `barava_network_impl.md` (200 on firmware 1.0.1). The
                # firmware author has confirmed 500 is the real minimum, so
                # clamping is routine rather than notable -- no warning. The
                # raw value stays on reported_ping_interval if you want it.
                self.ping_interval = max(
                    MIN_PING_INTERVAL_MS, batch.ping_interval
                )
                if batch.ping_interval < MIN_PING_INTERVAL_MS:
                    log.debug(
                        "device reported %dms; clamped to the %dms floor",
                        batch.ping_interval, MIN_PING_INTERVAL_MS,
                    )

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
        """Compile and send an animation to the ``CMPAM`` handler.

        Loading a compiled animation is expensive on the device: the firmware
        author warns that each one forces heavy context switching across the
        drivers and interpreter, and that hammering the lights with colour
        packets can wedge it. So consecutive uploads are spaced at least
        :attr:`min_animation_gap` seconds apart -- a short blocking sleep is
        inserted if you call this faster than that. Set the attribute to 0 to
        disable, if you know a given sequence is safe.
        """
        if isinstance(script, AnimationScript):
            payload = script.to_hex()
        elif isinstance(script, (bytes, bytearray)):
            payload = bytes(script).hex().upper()
        else:
            payload = str(script).strip().upper()
        self._throttle_animation()
        batch = self.request(
            Handler.COMPILE_ANIMATION,
            {Var.ANIMATION_DATA.value: payload, Var.SPEC_ON.value: int(spectrum)},
            wait=False,
        )
        self._last_animation_upload = time.monotonic()
        return batch

    def _throttle_animation(self) -> None:
        gap = self.min_animation_gap
        if gap <= 0 or self._last_animation_upload is None:
            return
        elapsed = time.monotonic() - self._last_animation_upload
        if elapsed < gap:
            time.sleep(gap - elapsed)

    # -- zones -------------------------------------------------------------

    def build_theme(
        self,
        solids: Mapping[int | str, tuple[int, int, int]] | None = None,
        gradients: Mapping[int | str, Sequence[tuple[int, int, int, int]]] | None = None,
        *,
        rotate: Mapping[int | str, int] | None = None,
        smooth: bool = False,
        zones: Sequence[int] = ZONE_ORDER,
    ) -> AnimationScript:
        """Compose a static theme without sending it.

        Reproduces the structure the device's own app emits: a header that
        selects every zone, then one atomic scope holding a per-zone fill.
        ``solids`` maps a zone to one ``(r, g, b)``; ``gradients`` maps a zone
        to ``(pos, r, g, b)`` stops, where ``pos`` spans 0-255 across the
        zone (:func:`generate_gradient_stops` builds these from a flat list
        of colours). A zone in neither mapping is left dark, which is how
        the app writes an all-off theme.

        Any key -- in ``solids``, ``gradients``, or ``zones`` -- accepts a
        :class:`~pyrava.const.Zone` directly (``{Zone.TOP: (255, 0, 0)}``,
        confirmed by lighting each ring in turn) or a name from
        :data:`~pyrava.const.ZONE_GROUPS` (``{"lava_lamp": (255, 80, 0)}``),
        which expands to every zone in that group. Groups can overlap; when
        two entries touch the same physical zone, whichever is processed
        last wins, in the order: ``gradients`` then ``solids``, each in the
        order given.

        Returns the script so you can inspect or extend it; pass it to
        :meth:`upload_animation`, or use :meth:`set_zone_colors` to do both.
        """
        header_zones: list[int] = []
        for zone in zones:
            header_zones.extend(_expand_zone_key(zone))
        for key in (gradients or {}):
            header_zones.extend(_expand_zone_key(key))
        for key in (solids or {}):
            header_zones.extend(_expand_zone_key(key))
        seen: set[int] = set()
        header_zones = [z for z in header_zones if not (z in seen or seen.add(z))]

        # Resolve the per-zone fills once, so each thread can emit only its
        # own zones' instructions.
        zone_gradients: dict[int, Sequence[tuple[int, int, int, int]]] = {}
        for key, stops in (gradients or {}).items():
            for zone in _expand_zone_key(key):
                zone_gradients[zone] = stops
        zone_solids: dict[int, tuple[int, int, int]] = {}
        for key, color in (solids or {}).items():
            for zone in _expand_zone_key(key):
                zone_solids[zone] = color
                zone_gradients.pop(zone, None)

        # Zones that rotate are grouped by speed. A captured theme with one
        # rotating ring puts that ring in its own header/thread pair and
        # every other zone in a second pair, so that's the structure copied
        # here: one pair per distinct rotation, then one for the rest.
        rotations: dict[int, int] = {}
        for key, amount in (rotate or {}).items():
            for zone in _expand_zone_key(key):
                if int(amount) == 0:
                    raise ValueError(
                        "rotation amount 0 is refused: the firmware author "
                        "reports zero rotation deadlocks the animation "
                        "engine. Omit the zone instead."
                    )
                rotations[zone] = int(amount)

        groups: list[tuple[list[int], int | None]] = []
        by_speed: dict[int, list[int]] = {}
        for zone in header_zones:
            if zone in rotations:
                by_speed.setdefault(rotations[zone], []).append(zone)
        for amount, zone_list in by_speed.items():
            groups.append((zone_list, amount))
        static = [z for z in header_zones if z not in rotations]
        if static or not groups:
            groups.append((static, None))

        def emit_fills(script: AnimationScript, group: Sequence[int]) -> None:
            # Gradients first, then solids -- the order the device's own app
            # uses, confirmed by a captured theme mixing both. Emitting in
            # header order instead changes the bytes.
            members = set(group)
            for zone, stops in zone_gradients.items():
                if zone in members:
                    script.reset_l2()
                    script.select_zone(zone)
                    script.gradient(*stops, smooth=smooth)
            for zone, (r, g, b) in zone_solids.items():
                if zone in members:
                    script.reset_l2()
                    script.select_zone(zone)
                    script.set_rgb(r, g, b)

        script = AnimationScript()
        for index, (group, amount) in enumerate(groups):
            with script.header(index):
                script.reset_l2()
                for zone in group:
                    script.select_zone(zone)
            with script.thread(index):
                with script.atomic():
                    emit_fills(script, group)
                if amount is not None:
                    with script.main():
                        if amount > 0:
                            script.rotate_right(amount)
                        else:
                            script.rotate_left(-amount)
        return script

    def set_zone_colors(
        self,
        solids: Mapping[int | str, tuple[int, int, int]] | None = None,
        gradients: Mapping[int | str, Sequence[tuple[int, int, int, int]]] | None = None,
        *,
        rotate: Mapping[int | str, int] | None = None,
        smooth: bool = False,
        zones: Sequence[int] = ZONE_ORDER,
    ) -> Batch:
        """Set each LED zone's colour in one upload.

        ``light.set_zone_colors({Zone.TOP: (255, 0, 0), Zone.MIDDLE_INNER: (0, 255, 0)})``
        lights two rings and leaves the rest dark. Unlike ``set_fill_hue``,
        this takes real RGB -- the zone path carries all three channels, so
        saturation survives here.

        Keys accept a group name from :data:`~pyrava.const.ZONE_GROUPS`
        instead of a zone: ``light.set_zone_colors({"lava_lamp": (255, 80, 0)})``.
        See :meth:`build_theme` for how overlapping groups resolve.
        """
        batch = self.upload_animation(
            self.build_theme(
                solids, gradients, rotate=rotate, smooth=smooth, zones=zones
            )
        )
        self._remember_theme(solids, gradients, zones)
        return batch

    def _remember_theme(
        self,
        solids: Mapping[int | str, tuple[int, int, int]] | None,
        gradients: Mapping[int | str, Sequence[tuple[int, int, int, int]]] | None,
        zones: Sequence[int],
    ) -> None:
        """Update the local shadow of what each zone was last told to show."""
        touched: set[int] = set()
        for key in zones:
            touched.update(_expand_zone_key(key))
        for key, stops in (gradients or {}).items():
            for zone in _expand_zone_key(key):
                touched.add(zone)
                self._zone_gradients[zone] = tuple(stops)
                self._zone_solids.pop(zone, None)
        for key, color in (solids or {}).items():
            for zone in _expand_zone_key(key):
                touched.add(zone)
                self._zone_solids[zone] = color
                self._zone_gradients.pop(zone, None)
        # A selected zone that got neither a solid nor a gradient is dark in
        # this theme; drop any stale colour the shadow had for it.
        for zone in touched:
            if zone not in self._zone_solids and zone not in self._zone_gradients:
                self._zone_solids.pop(zone, None)
                self._zone_gradients.pop(zone, None)

    def set_zone_gradient(
        self,
        zone: int | str,
        *stops: tuple[int, int, int, int],
        smooth: bool = False,
    ) -> Batch:
        """Run a gradient across one zone (or every zone in a group), leaving
        the rest dark.

        ``light.set_zone_gradient(Zone.BOTTOM_INNER, (0, 7, 0, 63), (255, 0, 4, 54))``

        ``zone`` accepts a group name too -- every zone in the group gets
        the same gradient.
        """
        return self.set_zone_colors(gradients={zone: stops}, smooth=smooth)

    def set_zone_palette(
        self,
        colors: Sequence[tuple[int, int, int]],
        *,
        zones: int | str | Sequence[int | str] = ZONE_ORDER,
        rotate: int | Mapping[int | str, int] | None = None,
    ) -> Batch:
        """Give each zone one flat colour -- no gradient, no interpolation.

        ``light.set_zone_palette(colors)`` assigns ``colors[0]`` to the first
        zone, ``colors[1]`` to the second, and so on in :data:`ZONE_ORDER`,
        cycling if there are fewer colours than zones and ignoring any
        extras.

        Often clearer than a gradient for a sampled palette. Each zone holds
        many LEDs, so a three-stop gradient spreads its colours far apart and
        spends most of the ring on blended intermediates, which reads as
        washed out. One flat colour per zone keeps every sampled colour at
        full strength.

        ``zones`` accepts a single zone, a group name, or a sequence mixing
        both, so ``set_zone_palette(colors, zones="lava_lamp")`` spreads the
        palette across just those three rings.
        """
        colors = list(colors)
        if not colors:
            raise ValueError("need at least one colour")

        keys: Sequence[int | str] = (
            [zones] if isinstance(zones, (int, str)) else list(zones)
        )
        expanded: list[int] = []
        for key in keys:
            expanded.extend(_expand_zone_key(key))
        seen: set[int] = set()
        expanded = [z for z in expanded if not (z in seen or seen.add(z))]

        mapping = {
            zone: tuple(colors[i % len(colors)])
            for i, zone in enumerate(expanded)
        }
        return self.set_zone_colors(
            mapping, rotate=_spread_rotation(rotate, expanded)
        )

    def set_gradient(
        self,
        colors: Sequence[tuple[int, int, int]],
        *,
        zones: int | str | Sequence[int | str] = ZONE_ORDER,
        smooth: bool = False,
        style: str = "repeat",
        seed: int | None = None,
        rotate: int | Mapping[int | str, int] | None = None,
    ) -> Batch:
        """Build an evenly spaced gradient from N colours and apply it.

        ``light.set_gradient([(255, 0, 0), (0, 255, 0), (0, 0, 255)])`` puts
        a gradient on every zone (the default). Pass ``zones`` to target
        specific zones or a group instead: ``light.set_gradient(colors,
        zones="lava_lamp")``, ``light.set_gradient(colors, zones=[Zone.TOP,
        "downlamp"])``.

        ``style`` controls how the *same* colour set is applied across
        several zones:

        * ``"repeat"`` (default) -- every zone gets an identical gradient,
          same colours, same positions.
        * ``"offset"`` -- every zone gets the same colours, but the starting
          point shifts by one colour per zone, so adjacent zones don't show
          the exact same pattern. Cheap and deterministic. This is a static
          reordering, *not* animation -- for a spinning zone use the
          ``rotate=`` argument on :meth:`set_zone_colors`.
        * ``"vary"`` -- every zone gets its own independent shuffle of the
          same colour set, so the zones look related but not identical.
          ``seed`` makes a shuffle reproducible; omit it for a fresh one
          each call.
        * ``"span"`` -- treats the target zones as one continuous ring and
          splits a single gradient across them, so the last colour of one
          zone blends into the first colour of the next. See
          :func:`_span_gradient_stops` for what this assumes and its
          hardware-confirmation caveat.

        This is the natural hook for a generated palette -- e.g. cluster
        centres from a k-means pass over screen colours -- since it takes a
        plain list of RGB tuples with no positions to work out yourself. See
        :func:`generate_gradient_stops` for the spacing rule, and its
        confirmation caveat past three colours.
        """
        colors = list(colors)
        if len(colors) < 2:
            raise ValueError(
                "need at least two colours to build a gradient; use "
                "set_zone_colors() for a single solid colour"
            )

        keys: Sequence[int | str] = (
            [zones] if isinstance(zones, (int, str)) else list(zones)
        )
        target_zones: list[int] = []
        for key in keys:
            target_zones.extend(_expand_zone_key(key))
        seen: set[int] = set()
        target_zones = [z for z in target_zones if not (z in seen or seen.add(z))]

        gradients: dict[int, Sequence[tuple[int, int, int, int]]]
        if style == "repeat":
            stops = generate_gradient_stops(colors)
            gradients = {zone: stops for zone in target_zones}
        elif style == "offset":
            n = len(colors)
            gradients = {}
            for i, zone in enumerate(target_zones):
                shift = i % n
                rotated = colors[shift:] + colors[:shift]
                gradients[zone] = generate_gradient_stops(rotated)
        elif style == "vary":
            rng = random.Random(seed)
            gradients = {}
            for zone in target_zones:
                shuffled = list(colors)
                rng.shuffle(shuffled)
                gradients[zone] = generate_gradient_stops(shuffled)
        elif style == "span":
            per_zone = _span_gradient_stops(colors, len(target_zones))
            gradients = dict(zip(target_zones, per_zone))
        elif style == "rotate":
            raise ValueError(
                "style='rotate' was renamed to 'offset' because it collided "
                "with the rotate= animation argument. 'offset' shifts which "
                "colour each zone starts on (static); rotate= spins a zone's "
                "contents (animated). Use whichever you meant."
            )
        else:
            raise ValueError(
                f"unknown style {style!r}; expected 'repeat', 'offset', "
                "'vary', or 'span'"
            )

        return self.set_zone_colors(
            gradients=gradients,
            smooth=smooth,
            rotate=_spread_rotation(rotate, target_zones),
        )

    def clear_zones(self, *, zones: Sequence[int] = ZONE_ORDER) -> Batch:
        """Turn every addressable zone off, the way the app's blank theme does."""
        batch = self.upload_animation(self.build_theme(zones=zones))
        for zone in zones:
            for z in _expand_zone_key(zone):
                self._zone_solids.pop(z, None)
                self._zone_gradients.pop(z, None)
        return batch

    @property
    def known_zone_colors(self) -> dict[int, tuple[int, int, int]]:
        """Solid colours this session last set, by zone. See :meth:`set_zone_state`."""
        return dict(self._zone_solids)

    @property
    def known_zone_gradients(self) -> dict[int, tuple[tuple[int, int, int, int], ...]]:
        """Gradient stops this session last set, by zone. See :meth:`set_zone_state`."""
        return dict(self._zone_gradients)

    def set_zone_state(
        self, key: int | str, color: tuple[int, int, int] | None, *, smooth: bool = False
    ) -> Batch:
        """Change or clear one zone (or group) while leaving the rest as-is.

        There is no way to read the device's current per-zone colours back
        -- no ``GET`` for this exists in the protocol -- so "the rest"
        means whatever *this* ``BaravaDevice`` last sent via
        ``set_zone_colors()``, ``set_gradient()``, or this method, not the
        device's actual current state. If the app, another client, or an
        earlier script run changed the theme since, this doesn't know about
        it, and calling this will silently overwrite those changes back to
        whatever this session last knew.

        ``color=None`` turns the zone off -- this is the "clear zones 0/1
        without touching the rest of the theme" case: since there's no
        partial-theme update on the wire either, this works by replaying
        every other zone this session remembers alongside the new one.

        ``key`` accepts a group name; every zone in the group is set (or
        cleared) together.
        """
        for zone in _expand_zone_key(key):
            if color is None:
                self._zone_solids.pop(zone, None)
                self._zone_gradients.pop(zone, None)
            else:
                self._zone_solids[zone] = color
                self._zone_gradients.pop(zone, None)
        return self.set_zone_colors(
            dict(self._zone_solids), dict(self._zone_gradients), smooth=smooth
        )

    def clear_zone(self, key: int | str) -> Batch:
        """Turn off one zone or group, leaving others as this session last set them.

        Shorthand for ``set_zone_state(key, None)``; see that method's
        docstring for what "leaving others" actually means here.
        """
        return self.set_zone_state(key, None)

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
            # Only worth flagging when the caller asked for this. A derived
            # value can't be under the floor (ping_interval is already
            # clamped), so reaching here with interval=None would be a bug.
            if interval is not None:
                log.warning(
                    "requested poll interval %.3fs is below the %.3fs minimum "
                    "for device stability; using %.3fs.", chosen, floor, floor
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
