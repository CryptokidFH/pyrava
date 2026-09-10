"""Encoding and decoding of the Barava packet wire format.

Grammar (from `network.md`)::

    block   := "<" key "=" value ">"
    value   := marker scalar | marker "{" scalar ("," scalar)* "}"
    marker  := "!"          # base-10 integer
             | "&"          # string
             | "$"          # base-16 integer, decoded to decimal
    body    := block*                       e.g. "<SPLT=!{255,0,0}><SPON=!1>"
    batch   := "<" block "=" "{" subpacket ("," subpacket)* "}" ">"
    subpkt  := "{" "{" block ("," block)* "}" "," body "}"

The batch key is itself a block whose key is the sender ID and whose value is
the ping interval: ``<<xxyyzz112233=&200>={...}>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .const import (
    ARRAY_END,
    ARRAY_SPLIT,
    ARRAY_START,
    BLOCK_END,
    BLOCK_SPLIT,
    BLOCK_START,
    MARKER_HEX,
    MARKER_NUMBER,
    MARKER_STRING,
    MARKERS,
    RESERVED_CHARS,
    VAR_BY_LABEL,
    Header,
)
from .errors import EncodeError, ParseError

__all__ = [
    "HexInt",
    "Packet",
    "SubPacket",
    "Batch",
    "encode_value",
    "encode_block",
    "encode_body",
    "encode_batch",
    "parse_body",
    "parse_batch",
    "looks_like_batch",
]


# ==========================================================================
# Value wrappers
# ==========================================================================


class HexInt(int):
    """An integer that should be transmitted with the ``$`` (base-16) marker.

    The device decodes ``$FF`` and ``!255`` to the same value, so this is
    purely a formatting choice -- useful for colour values and register dumps.

    >>> encode_block("FCLR", HexInt(0xFF00FF))
    '<FCLR=$FF00FF>'
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"HexInt(0x{int(self):X})"


# ==========================================================================
# Encoding
# ==========================================================================


def _check_string(text: str) -> str:
    """Reject strings that would corrupt the delimiter-based framing.

    The protocol has no escape sequences, so a string containing a delimiter
    is unrepresentable rather than merely awkward.
    """
    bad = sorted(RESERVED_CHARS.intersection(text))
    if bad:
        raise EncodeError(
            f"string value {text!r} contains reserved character(s) {''.join(bad)!r}; "
            "the wire format has no escaping mechanism"
        )
    return text


def _scalar_marker(value: Any) -> str:
    if isinstance(value, HexInt):
        return MARKER_HEX
    if isinstance(value, bool):
        # bool is an int subclass; the device models booleans as 0/1 ints.
        return MARKER_NUMBER
    if isinstance(value, int):
        return MARKER_NUMBER
    if isinstance(value, str):
        return MARKER_STRING
    raise EncodeError(f"cannot encode value of type {type(value).__name__}: {value!r}")


def _scalar_text(value: Any) -> str:
    if isinstance(value, HexInt):
        n = int(value)
        if n < 0:
            raise EncodeError("hex values must be non-negative")
        text = format(n, "X")
        return text if len(text) % 2 == 0 else "0" + text
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _check_string(value)
    raise EncodeError(f"cannot encode value of type {type(value).__name__}: {value!r}")


def encode_value(value: Any) -> str:
    """Encode a Python value as a marked wire value (no surrounding block).

    >>> encode_value(12345)
    '!12345'
    >>> encode_value([1, 2, 3])
    '!{1,2,3}'
    >>> encode_value("my string")
    '&my string'
    """
    if isinstance(value, (list, tuple)):
        items = list(value)
        if not items:
            raise EncodeError("cannot encode an empty array: the marker is ambiguous")
        markers = {_scalar_marker(v) for v in items}
        if len(markers) > 1:
            raise EncodeError(
                f"array is heterogeneous ({sorted(markers)}); every element must "
                "share one type marker"
            )
        marker = markers.pop()
        joined = ARRAY_SPLIT.join(_scalar_text(v) for v in items)
        return f"{marker}{ARRAY_START}{joined}{ARRAY_END}"
    return _scalar_marker(value) + _scalar_text(value)


def encode_block(key: str, value: Any) -> str:
    """Encode one ``<KEY=VALUE>`` block.

    Passing ``value=None`` produces the empty block ``<>`` used as the body of
    parameterless handlers such as ``DVIFO``.
    """
    key = str(key)
    if value is None and not key:
        return f"{BLOCK_START}{BLOCK_END}"
    _check_string(key)
    return f"{BLOCK_START}{key}{BLOCK_SPLIT}{encode_value(value)}{BLOCK_END}"


EMPTY_BODY = f"{BLOCK_START}{BLOCK_END}"


def encode_body(values: Mapping[str, Any] | None = None, **kwargs: Any) -> str:
    """Encode a mapping of variables into a packet body.

    >>> encode_body({"SPLT": [255, 0, 0, 0, 255, 0], "SPON": 1})
    '<SPLT=!{255,0,0,0,255,0}><SPON=!1>'
    """
    merged: dict[str, Any] = {}
    if values:
        merged.update({str(k): v for k, v in values.items()})
    merged.update({str(k): v for k, v in kwargs.items()})
    if not merged:
        return EMPTY_BODY
    return "".join(encode_block(k, v) for k, v in merged.items())


# ==========================================================================
# Structures
# ==========================================================================


@dataclass
class Packet:
    """One logical request or response: a header mapping plus a body."""

    headers: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    raw_body: str = ""

    @property
    def handler(self) -> str | None:
        """The ``body-handler`` value, if the packet carries one."""
        value = self.headers.get(Header.BODY_HANDLER.value)
        return str(value) if value is not None else None

    def get(self, var: Any, default: Any = None) -> Any:
        """Look up a body variable by label or :class:`~pyrava.const.Var`."""
        return self.body.get(str(getattr(var, "value", var)), default)

    def __contains__(self, var: Any) -> bool:
        return str(getattr(var, "value", var)) in self.body


@dataclass
class SubPacket:
    """One entry of an outbound batch."""

    handler: str
    body: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> str:
        headers: dict[str, Any] = {Header.BODY_HANDLER.value: str(self.handler)}
        headers.update({str(k): v for k, v in self.extra_headers.items()})
        header_blocks = ARRAY_SPLIT.join(
            encode_block(k, v if not isinstance(v, str) else str(v))
            for k, v in headers.items()
        )
        body = encode_body(self.body)
        return (
            f"{ARRAY_START}"
            f"{ARRAY_START}{header_blocks}{ARRAY_END}"
            f"{ARRAY_SPLIT}{body}"
            f"{ARRAY_END}"
        )


@dataclass
class Batch:
    """A decoded batch: the shared sender block plus its member packets.

    On a response the key block holds the **device's** own ID and the
    device's ping interval, e.g. ``<<68b6b33d3b38=&200>=&{...}>``. This is the
    only place the device ID appears — ``DVIFO`` bodies do not carry ``DVID``
    — so :attr:`device_id` is how you learn it.
    """

    sender_id: str
    ping_interval: int | None
    packets: list[Packet] = field(default_factory=list)
    raw: str = ""

    @property
    def device_id(self) -> str:
        """The key block's ID. On responses this is the device's own ID."""
        return self.sender_id

    def __iter__(self) -> Iterator[Packet]:
        return iter(self.packets)

    def __len__(self) -> int:
        return len(self.packets)

    def by_handler(self, handler: Any) -> list[Packet]:
        label = str(getattr(handler, "value", handler))
        return [p for p in self.packets if p.handler == label]

    def first(self, handler: Any) -> Packet | None:
        """Return the first packet for ``handler``, or ``None``."""
        matches = self.by_handler(handler)
        return matches[0] if matches else None

    def flatten(self, handler: Any = None) -> dict[str, Any]:
        """Merge member bodies into a single mapping (last write wins).

        With ``handler`` given, only packets carrying that ``body-handler``
        contribute. A device response queue can hold replies to several
        earlier commands, so filtering matters when you want one answer.
        """
        packets = self.by_handler(handler) if handler is not None else self.packets
        merged: dict[str, Any] = {}
        for packet in packets:
            merged.update(packet.body)
        return merged

    @property
    def handlers(self) -> list[str]:
        """Every ``body-handler`` present, in order."""
        return [p.handler for p in self.packets if p.handler]


def encode_batch(
    owner_id: str,
    ping_interval: int,
    subpackets: Sequence[SubPacket],
    *,
    separator: str = ARRAY_SPLIT,
    marker: str = MARKER_STRING,
) -> str:
    """Build a batch body targeting one device.

    ``owner_id`` is the target device's ID. Captured responses key the block
    that way (``<<68b6b33d3b38=&200>=&{...}>``), which matches the note that
    the device ID is only needed on the wire when a packet is batched.

    ``marker`` defaults to ``&`` because firmware emits the sub-packet array
    string-marked, even though the spec's worked example leaves it bare. Pass
    ``marker=""`` for the bare form.

    ``separator`` exists because the two worked examples in `network.md`
    disagree: the "Sub Parsing" section comma-separates sub-packets while the
    "Add A Device Group And Turn On Desk Light" example concatenates them.
    """
    if not subpackets:
        raise EncodeError("a batch must contain at least one sub-packet")
    key_block = encode_block(_check_string(str(owner_id)), str(int(ping_interval)))
    members = separator.join(sp.encode() for sp in subpackets)
    return (
        f"{BLOCK_START}{key_block}{BLOCK_SPLIT}{marker}"
        f"{ARRAY_START}{members}{ARRAY_END}{BLOCK_END}"
    )


def parse_groups(value: Any) -> dict[str, str]:
    """Decode a ``DVGR`` value into ``{group_id: group_name}``.

    Group lists arrive as an array of blocks rather than plain strings:
    ``<DVGR=&{<545d577b=&My-Room>}>``.
    """
    groups: dict[str, str] = {}
    items = value if isinstance(value, (list, tuple)) else [value]
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if text.startswith(BLOCK_START) and text.endswith(BLOCK_END):
            try:
                key, name = parse_block(text)
            except ParseError:
                continue
            if key:
                groups[key] = "" if name is None else str(name)
        else:
            groups[text] = ""
    return groups


# ==========================================================================
# Low-level scanning
# ==========================================================================


def _match_delim(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Return the index of the delimiter closing the one at ``start``."""
    if start >= len(text) or text[start] != open_ch:
        raise ParseError(f"expected {open_ch!r}", text, start)
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    raise ParseError(f"unterminated {open_ch!r}", text, start)


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not nested inside ``<>`` or ``{}``."""
    parts: list[str] = []
    depth = 0
    current = 0
    for i, ch in enumerate(text):
        if ch in (BLOCK_START, ARRAY_START):
            depth += 1
        elif ch in (BLOCK_END, ARRAY_END):
            depth -= 1
        elif ch == ARRAY_SPLIT and depth == 0:
            parts.append(text[current:i])
            current = i + 1
    parts.append(text[current:])
    return parts


def _iter_groups(text: str, open_ch: str, close_ch: str) -> Iterator[str]:
    """Yield each top-level ``open_ch ... close_ch`` group, skipping separators.

    Stray type markers between groups are tolerated: firmware may mark each
    sub-packet even though the spec's example does not.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == open_ch:
            end = _match_delim(text, i, open_ch, close_ch)
            yield text[i : end + 1]
            i = end + 1
        elif ch in " \t\r\n" or ch == ARRAY_SPLIT or ch in MARKERS:
            i += 1
        else:
            raise ParseError(f"unexpected character {ch!r}", text, i)


def _decode_scalar(marker: str, text: str) -> Any:
    text = text.strip()
    if marker == MARKER_NUMBER:
        try:
            return int(text, 10)
        except ValueError as exc:
            raise ParseError(f"invalid base-10 value {text!r}") from exc
    if marker == MARKER_HEX:
        try:
            return int(text, 16)
        except ValueError as exc:
            raise ParseError(f"invalid base-16 value {text!r}") from exc
    return text


def parse_value(text: str) -> Any:
    """Decode a marked value such as ``!12345``, ``$FF`` or ``&{a,b}``."""
    if not text:
        return None
    marker, rest = text[0], text[1:]
    if marker not in MARKERS:
        # Untyped payload (batch arrays and raw bodies arrive here).
        return text
    rest = rest.strip()
    if rest.startswith(ARRAY_START):
        end = _match_delim(rest, 0, ARRAY_START, ARRAY_END)
        inner = rest[1:end].strip()
        if not inner:
            return []
        return [_decode_scalar(marker, item) for item in _split_top_level(inner)]
    return _decode_scalar(marker, rest)


def parse_block(text: str) -> tuple[str, Any]:
    """Decode a single ``<KEY=VALUE>`` block into a ``(key, value)`` pair.

    An empty block ``<>`` decodes to ``("", None)``.
    """
    text = text.strip()
    if not (text.startswith(BLOCK_START) and text.endswith(BLOCK_END)):
        raise ParseError(f"not a block: {text!r}")
    inner = text[1:-1]
    if not inner:
        return "", None
    if inner.startswith(BLOCK_START):
        # Nested-key form, only used by the batch sender block.
        key_end = _match_delim(inner, 0, BLOCK_START, BLOCK_END)
        key = inner[: key_end + 1]
        if inner[key_end + 1 : key_end + 2] != BLOCK_SPLIT:
            raise ParseError("expected '=' after nested key block", inner, key_end + 1)
        return key, inner[key_end + 2 :]
    split = inner.find(BLOCK_SPLIT)
    if split < 0:
        raise ParseError(f"block has no '=' separator: {text!r}")
    return inner[:split].strip(), parse_value(inner[split + 1 :])


def parse_body(text: str) -> dict[str, Any]:
    """Decode a concatenation of blocks into a mapping.

    >>> parse_body("<SPLT=!{255,0,0}><SPON=!1>")
    {'SPLT': [255, 0, 0], 'SPON': 1}
    """
    result: dict[str, Any] = {}
    text = (text or "").strip()
    if not text or text == EMPTY_BODY:
        return result
    for group in _iter_groups(text, BLOCK_START, BLOCK_END):
        key, value = parse_block(group)
        if key:
            result[key] = value
    return result


def looks_like_batch(text: str) -> bool:
    """Cheap structural test for the batch form ``<<...>=...>``."""
    stripped = (text or "").strip()
    return stripped.startswith(BLOCK_START + BLOCK_START)


def parse_batch(text: str, *, strict: bool = False) -> Batch:
    """Decode a batch packet.

    Each member packet inherits the batch-level ``device-id`` and
    ``ping-interval`` before its own headers are applied, matching the parsed
    output shown in `network.md`.

    The batch value is accepted in every form the spec implies:

    * ``{...}``   -- bare array, as in the Sub Parsing worked example
    * ``&{...}``  -- string-marked array, as in the spec's own template
    * ``<...>``   -- one inline body, no array wrapper
    * empty       -- an empty response queue

    Only a genuinely unrecognisable value raises, and then only under
    ``strict``; otherwise an empty batch comes back so a stray frame cannot
    take down a polling loop.
    """
    raw = text or ""
    text = raw.strip()
    if not (text.startswith(BLOCK_START) and text.endswith(BLOCK_END)):
        raise ParseError(f"not a batch: {text[:60]!r}")

    key_text, value_text = parse_block(text)
    if not key_text.startswith(BLOCK_START):
        raise ParseError("batch key is not a nested block")

    sender_id, interval_raw = parse_block(key_text)
    try:
        ping_interval = int(interval_raw) if interval_raw is not None else None
    except (TypeError, ValueError):
        ping_interval = None

    shared: dict[str, Any] = {Header.DEVICE_ID.value: sender_id}
    if ping_interval is not None:
        shared[Header.PING_INTERVAL.value] = ping_interval

    value_text = (value_text or "").strip()
    # The spec's template marks the sub-packet array as a string (`&{...}`)
    # while its worked example leaves it bare. Tolerate any type marker.
    if value_text[:1] in MARKERS:
        value_text = value_text[1:].strip()

    packets: list[Packet] = []

    if not value_text or value_text == EMPTY_BODY:
        # Empty response queue.
        pass
    elif value_text.startswith(ARRAY_START):
        end = _match_delim(value_text, 0, ARRAY_START, ARRAY_END)
        inner = value_text[1:end].strip()
        if inner:
            for group in _iter_groups(inner, ARRAY_START, ARRAY_END):
                try:
                    packets.append(_parse_subpacket(group, shared))
                except ParseError:
                    if strict:
                        raise
    elif value_text.startswith(BLOCK_START):
        # A single un-wrapped body rather than an array of sub-packets.
        packets.append(
            Packet(
                headers=dict(shared),
                body=parse_body(value_text),
                raw_body=value_text,
            )
        )
    elif strict:
        raise ParseError(
            f"unrecognised batch value {value_text[:60]!r}", raw
        )

    return Batch(
        sender_id=sender_id,
        ping_interval=ping_interval,
        packets=packets,
        raw=raw,
    )


def _parse_subpacket(text: str, shared: Mapping[str, Any]) -> Packet:
    inner = text[1:-1].strip()
    if inner[:1] in MARKERS:
        inner = inner[1:].strip()

    headers: dict[str, Any] = dict(shared)

    if inner.startswith(ARRAY_START):
        header_end = _match_delim(inner, 0, ARRAY_START, ARRAY_END)
        header_text = inner[1:header_end].strip()
        if header_text[:1] in MARKERS:
            header_text = header_text[1:].strip()
        body_text = inner[header_end + 1 :].lstrip().lstrip(ARRAY_SPLIT).strip()
        if header_text:
            for block in _iter_groups(header_text, BLOCK_START, BLOCK_END):
                key, value = parse_block(block)
                if key:
                    headers[key] = value
    else:
        # Some frames carry a body with no header array at all.
        body_text = inner

    if body_text[:1] in MARKERS:
        body_text = body_text[1:].strip()

    return Packet(headers=headers, body=parse_body(body_text), raw_body=body_text)


def parse_response(text: str, *, strict: bool = False) -> Batch:
    """Decode any device response, batched or not, as a :class:`Batch`.

    Non-batched responses become a single-member batch with an empty sender ID
    so that callers only need one code path. Unless ``strict`` is set, a frame
    that cannot be decoded yields an empty batch with the original text on
    ``.raw`` rather than raising — a polling loop should survive one odd
    response.
    """
    raw = text or ""
    text = raw.strip()
    if looks_like_batch(text):
        try:
            return parse_batch(text, strict=strict)
        except ParseError:
            if strict:
                raise
            return Batch(sender_id="", ping_interval=None, packets=[], raw=raw)
    try:
        body = parse_body(text)
    except ParseError:
        if strict:
            raise
        return Batch(sender_id="", ping_interval=None, packets=[], raw=raw)
    return Batch(
        sender_id="",
        ping_interval=None,
        packets=[Packet(headers={}, body=body, raw_body=text)] if body else [],
        raw=raw,
    )


def normalise(values: Mapping[str, Any]) -> dict[str, Any]:
    """Re-key a decoded body from wire labels to :class:`~pyrava.const.Var`.

    Unknown labels are preserved verbatim so firmware additions do not vanish.
    """
    out: dict[str, Any] = {}
    for key, value in values.items():
        var = VAR_BY_LABEL.get(key)
        out[var.name.lower() if var else key] = value
    return out
