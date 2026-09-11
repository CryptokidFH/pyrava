"""Tests pinned to the worked examples in network.md and animation.md.

Run with: python -m pytest tests/ -q     (or plain `python tests/test_protocol.py`)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pyrava import (
    AnimationScript,
    Command,
    EncodeError,
    HexInt,
    SubPacket,
    disassemble,
    encode_batch,
    encode_block,
    encode_body,
    parse_batch,
    parse_body,
    rgb,
)
from pyrava.client import BaravaDevice, new_sender_id
from pyrava.const import Handler, Zone
from pyrava.errors import BaravaError, CompileError


# ---------------------------------------------------------------- key/value

def test_scalar_decoding_matches_docs():
    assert parse_body("<NUMBER_VALUE=!12345>") == {"NUMBER_VALUE": 12345}
    assert parse_body("<HEX_VALUE=$FF>") == {"HEX_VALUE": 255}
    assert parse_body("<STRING_VALUE=&my string>") == {"STRING_VALUE": "my string"}


def test_array_decoding_matches_docs():
    assert parse_body("<NUMBER_ARRAY=!{1,2,3,4,5}>") == {
        "NUMBER_ARRAY": [1, 2, 3, 4, 5]
    }
    assert parse_body("<HEX_ARRAY=${00,FF,0F,CF}>") == {
        "HEX_ARRAY": [0, 255, 15, 207]
    }
    assert parse_body("<STRING_ARRAY=&{hello,world,stuff}>") == {
        "STRING_ARRAY": ["hello", "world", "stuff"]
    }


def test_empty_body():
    assert parse_body("<>") == {}
    assert encode_body() == "<>"


def test_encoding_round_trips():
    for source in (
        "<NUMBER_VALUE=!12345>",
        "<STRING_VALUE=&my string>",
        "<NUMBER_ARRAY=!{1,2,3,4,5}>",
        "<STRING_ARRAY=&{hello,world,stuff}>",
    ):
        assert encode_body(parse_body(source)) == source


def test_hex_marker_is_opt_in():
    assert encode_block("FCLR", HexInt(0xFF00FF)) == "<FCLR=$FF00FF>"
    assert encode_block("FCLR", 0xFF00FF) == "<FCLR=!16711935>"


def test_spectrum_body_matches_docs():
    body = encode_body({"SPLT": [255, 0, 0, 0, 255, 0], "SPON": 1})
    assert body == "<SPLT=!{255,0,0,0,255,0}><SPON=!1>"


def test_reserved_characters_are_rejected():
    with pytest.raises(EncodeError):
        encode_block("DVNM", "name<with>delims")


def test_heterogeneous_arrays_are_rejected():
    with pytest.raises(EncodeError):
        encode_body({"MIXED": [1, "two"]})


# -------------------------------------------------------------------- batch

DOC_BATCH = (
    "<<xxyyzz112233=&200>={{{<HEADER1=!1234>,<HEADER2=$FF>},<BODY1=&hello>},"
    "{{<HEADER_ABC=&world>},<BODY2=!4567>}}>"
)


def test_batch_decoding_matches_docs():
    batch = parse_batch(DOC_BATCH)
    assert batch.sender_id == "xxyyzz112233"
    assert batch.ping_interval == 200
    assert len(batch) == 2

    first, second = batch.packets
    assert first.headers == {
        "device-id": "xxyyzz112233",
        "ping-interval": 200,
        "HEADER1": 1234,
        "HEADER2": 255,
    }
    assert first.body == {"BODY1": "hello"}
    assert second.headers == {
        "device-id": "xxyyzz112233",
        "ping-interval": 200,
        "HEADER_ABC": "world",
    }
    assert second.body == {"BODY2": 4567}


def test_batch_accepts_the_separatorless_variant():
    """The group/desk-light example concatenates sub-packets without commas."""
    source = (
        "<<xxyyzz112233=&100>={{{<body-handler=&DVAGR>},"
        "<GRNM=&group-name><GRID=&group-id>}"
        "{{<body-handler=&DSKST>},<DSTT=!1>}}>"
    )
    batch = parse_batch(source)
    assert [p.handler for p in batch] == ["DVAGR", "DSKST"]
    assert batch.packets[0].body == {"GRNM": "group-name", "GRID": "group-id"}
    assert batch.packets[1].body == {"DSTT": 1}


def test_batch_encoding_round_trips():
    encoded = encode_batch(
        "xxyyzz112233",
        100,
        [
            SubPacket("DVAGR", {"GRNM": "group-name", "GRID": "group-id"}),
            SubPacket("DSKST", {"DSTT": 1}),
        ],
    )
    batch = parse_batch(encoded)
    assert [p.handler for p in batch] == ["DVAGR", "DSKST"]
    assert batch.ping_interval == 100
    assert batch.flatten() == {
        "GRNM": "group-name",
        "GRID": "group-id",
        "DSTT": 1,
    }


def test_batch_flatten_and_lookup():
    batch = parse_batch(DOC_BATCH)
    assert batch.flatten() == {"BODY1": "hello", "BODY2": 4567}
    assert batch.packets[0].get("BODY1") == "hello"


# ------------------------------------------------------------------- client

def test_wildcard_sender_id_is_refused():
    with pytest.raises(BaravaError, match="wildcard|keep-alive|not a valid"):
        BaravaDevice("192.0.2.10", sender_id="*")


def test_generated_sender_ids_are_unique_and_clean():
    a, b = new_sender_id(), new_sender_id()
    assert a != b
    assert not set(a) & set("<>={},!&$")


def test_rgb_packing():
    assert rgb(255, 0, 0) == 0xFF0000
    assert rgb(0, 255, 0) == 0x00FF00
    with pytest.raises(ValueError):
        rgb(256, 0, 0)


# ---------------------------------------------------------------- animation

def test_gradient_rotate_script_compiles_to_expected_bytes():
    """The first practical example in animation.md, in the dialect the
    hardware actually speaks.

    animation.md writes this as BUILD_GRADIENT (no operands) followed by
    APPEND_GRADIENT per stop. Themes captured from the device's own app
    instead use FILL_ZONE (no operands) followed by BUILD_GRADIENT carrying
    (pos, r, g, b) per stop -- see test_captured_themes_recompile_exactly.
    Where the document and the wire disagree, the wire wins.
    """
    script = AnimationScript()
    with script.header(0):
        script.select_zone(0)
        script.select_zone(3)
        script.gradient((0, 255, 0, 0), (255, 0, 0, 255))
        script.deselect_all()
    with script.thread(0):
        with script.main():
            with script.atomic():
                script.select_zone(0)
                script.select_zone(3)
                script.rotate_left(5)
                script.deselect_zone(0)
                script.deselect_zone(3)

    assert script.compile() == bytes(
        [
            Command.OPEN_HEADER, 0,
            Command.SELECT_ZONE, 0,
            Command.SELECT_ZONE, 3,
            Command.FILL_ZONE,
            Command.BUILD_GRADIENT, 0, 255, 0, 0,
            Command.BUILD_GRADIENT, 255, 0, 0, 255,
            Command.MAP_LINEAR,
            Command.DESELECT_ALL,
            Command.CLOSE_HEADER,
            Command.OPEN_THREAD, 0,
            Command.START_SCOPE_MAIN,
            Command.START_SCOPE_ATOMIC,
            Command.SELECT_ZONE, 0,
            Command.SELECT_ZONE, 3,
            Command.ROTATE_LEFT, 5,
            Command.DESELECT_ZONE, 0,
            Command.DESELECT_ZONE, 3,
            Command.END_SCOPE_ATOMIC,
            Command.END_SCOPE_MAIN,
            Command.CLOSE_THREAD,
        ]
    )


def test_loop_parameter_is_two_bytes():
    script = AnimationScript()
    with script.header(0):
        script.select_zone(4)
    with script.thread(0):
        with script.main():
            with script.loop(5):
                with script.atomic():
                    script.select_zone(4)
                    script.rotate_left(5)
                    script.deselect_zone(4)

    data = script.compile()
    # A naive byte search would match a zone parameter, so anchor on the
    # preceding START_SCOPE_MAIN opcode instead.
    index = data.index(bytes([Command.START_SCOPE_MAIN, Command.START_SCOPE_LOOP])) + 1
    assert data[index : index + 3] == bytes([Command.START_SCOPE_LOOP, 0x00, 0x05])
    assert "START_SCOPE_LOOP(iterations=5)" in "\n".join(disassemble(data))


def test_hex_payload_is_uppercase_and_even_length():
    script = AnimationScript()
    with script.header(0):
        script.select_zone(4)
        script.set_rgb(255, 128, 0)
    with script.thread(0):
        script.rotate_left(1)
    payload = script.to_hex()
    assert payload.isupper() or payload.isdigit()
    assert len(payload) % 2 == 0
    assert bytes.fromhex(payload) == script.compile()


def test_zone_bounds_are_enforced():
    script = AnimationScript()
    with pytest.raises(CompileError, match="zone 9 out of range"):
        with script.header(0):
            script.select_zone(9)


def test_unclosed_scope_is_caught():
    script = AnimationScript()
    script.emit(Command.OPEN_HEADER, 0)
    script._scopes.append("header")
    with pytest.raises(CompileError, match="unclosed scope"):
        script.compile()


def test_arity_is_enforced():
    script = AnimationScript(strict=False)
    with pytest.raises(CompileError, match="takes 3 parameter"):
        script.emit(Command.SET_RGB, 255)
    with pytest.raises(CompileError, match="takes 0 parameter"):
        script.emit(Command.FILL_ZONE, 255)  # captures show no operands


def test_disassembler_round_trips():
    script = AnimationScript()
    with script.header(0):
        script.gradient((0, 255, 0, 0), (255, 0, 0, 255))
    with script.thread(0):
        with script.main():
            script.rotate_right(3)

    lines = disassemble(script.to_hex())
    # The device's own app emits no leading SCRIPT_HEADER byte, so the
    # stream now starts at OPEN_HEADER.
    assert "OPEN_HEADER(thread_index=0)" in lines[0]
    assert any("ROTATE_RIGHT(amount=3)" in line for line in lines)

    with_prefix = disassemble(script.to_hex(script_header=True))
    assert with_prefix[0] == "SCRIPT_HEADER"


def test_mismatched_header_and_thread_warns():
    script = AnimationScript()
    with script.header(0):
        script.select_all()
    with script.thread(1):
        script.rotate_left(1)
    with pytest.warns(UserWarning, match="no matching"):
        script.compile()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ------------------------------------------------- endpoint & queue semantics

INFO_BODY = "<DVNM=&Desk Lamp><DVID=&68b6b33d3b38><DVVR=&1.0.0><DVTP=!0><DVST=!1>"
HEAT_BODY = "<HTCT=!6930><HTST=!7000><HTHH=!100>"


class _DeferredDevice:
    """Mock firmware: each reply is flushed on the NEXT request, not this one."""

    def __init__(self):
        self.queue = []
        self.log = []

    def send(self, headers, body):
        self.log.append((headers.get("body-handler"), body, dict(headers)))
        flush, self.queue = self.queue, []
        handler = headers.get("body-handler")
        if handler == "DVIFO":
            self.queue.append("{{<body-handler=&DVIFO>}," + INFO_BODY + "}")
        elif handler == "HTIFO":
            self.queue.append("{{<body-handler=&HTIFO>}," + HEAT_BODY + "}")
        if not flush:
            return ""
        sid = headers["device-id"]
        interval = headers["ping-interval"]
        return f"<<{sid}=&{interval}>={{{','.join(flush)}}}>"

    def close(self):
        pass


def _device(**kwargs):
    fake = _DeferredDevice()
    device = BaravaDevice("192.0.2.10", transport=fake, **kwargs)
    # Tests hit a mock transport with no real device to protect, so drop the
    # inter-upload throttle to keep the suite fast.
    device.min_animation_gap = 0
    return device, fake


def test_default_endpoint_is_the_real_one():
    from pyrava.transport import HttpTransport

    assert HttpTransport("192.168.1.249").url == (
        "http://192.168.1.249:8080/barava-host-post"
    )


def test_deferred_reply_is_collected_by_polling():
    device, fake = _device()
    info = device.get_device_info()
    assert info["DVID"] == "68b6b33d3b38"
    assert device.device_id == "68b6b33d3b38"
    # command, then a bare queue-pop ping
    assert [h for h, _, _ in fake.log] == ["DVIFO", None]
    assert fake.log[1][1] == ""  # plain pings carry no body


def test_setters_do_not_poll():
    device, fake = _device()
    device.set_fill_color((255, 0, 0))  # RGB tuple -> converted to hue
    assert [h for h, _, _ in fake.log] == ["COLFL"]


def test_session_id_travels_as_device_id_header():
    device, fake = _device(sender_id="~A1B2C3D4E5F6")
    device.ping()
    headers = fake.log[0][2]
    assert headers["device-id"] == "~A1B2C3D4E5F6"
    assert headers["ping-interval"] == "1000"  # documented default
    assert "body-handler" not in headers


def test_generated_ids_match_the_app_shape():
    ident = new_sender_id()
    assert ident.startswith("~") and len(ident) == 13
    assert ident[1:].isupper() or ident[1:].isdigit()


def test_heater_temperature_scaling():
    device, _ = _device()
    assert device.read_temperature_f() == 69.30
    assert device.read_temperatures_f() == {"current": 69.30, "target": 70.00}


def test_deprecated_celsius_aliases_convert_correctly():
    """These used to relabel raw Fahrenheit as Celsius; now they convert."""
    device, _ = _device()
    with pytest.warns(DeprecationWarning):
        assert device.read_temperature_c() == pytest.approx((69.30 - 32) * 5 / 9, abs=0.01)
    with pytest.warns(DeprecationWarning):
        assert device.read_target_temperature_c() == pytest.approx(
            (70.00 - 32) * 5 / 9, abs=0.01
        )
    with pytest.warns(DeprecationWarning):
        both = device.read_temperatures_c()
    assert both["current"] == pytest.approx((69.30 - 32) * 5 / 9, abs=0.01)
    assert both["target"] == pytest.approx((70.00 - 32) * 5 / 9, abs=0.01)


def test_flatten_filters_by_handler():
    batch = parse_batch(
        "<<~ABC=&2000>={{{<body-handler=&DVIFO>}," + INFO_BODY + "},"
        "{{<body-handler=&HTIFO>}," + HEAT_BODY + "}}>"
    )
    assert batch.handlers == ["DVIFO", "HTIFO"]
    assert "HTCT" not in batch.flatten("DVIFO")
    assert batch.flatten("HTIFO")["HTCT"] == 6930
    assert batch.first("HTIFO").get("HTHH") == 100


def test_mdns_name_yields_device_id():
    from pyrava.discovery import DiscoveredService

    service = DiscoveredService("barava68b6b33d3b38", "192.168.1.249", 8080)
    assert service.device_id == "68b6b33d3b38"
    assert DiscoveredService("printer", "10.0.0.5", 80).device_id is None


# ------------------------------------------------- response shape tolerance

_BODY = "<DVNM=&Desk Lamp><DVID=&68b6b33d3b38><DVST=!1>"


@pytest.mark.parametrize(
    "frame",
    [
        # bare array, as in the Sub Parsing worked example
        "<<~ABC=&2000>={{{<body-handler=&DVIFO>}," + _BODY + "}}>",
        # string-marked array, as in the spec's own template
        "<<~ABC=&2000>=&{{{<body-handler=&DVIFO>}," + _BODY + "}}>",
        # every level marked
        "<<~ABC=&2000>=&{&{&{<body-handler=&DVIFO>},&" + _BODY + "}}>",
    ],
)
def test_batch_value_marker_is_optional(frame):
    from pyrava import parse_response

    batch = parse_response(frame)
    assert batch.handlers == ["DVIFO"]
    assert batch.flatten()["DVID"] == "68b6b33d3b38"


@pytest.mark.parametrize(
    "frame",
    [
        "<<~ABC=&2000>=&{}>",     # empty queue, marked
        "<<~ABC=&2000>={}>",      # empty queue, bare
        "<<~ABC=&2000>=<>>",      # empty queue, empty block
        "",                        # no content at all
        "not a packet at all",     # junk
    ],
)
def test_empty_and_junk_frames_do_not_raise(frame):
    from pyrava import parse_response

    batch = parse_response(frame)
    assert batch.flatten() == {}
    assert batch.raw == frame


def test_inline_body_without_array_wrapper():
    from pyrava import parse_response

    batch = parse_response("<<~ABC=&2000>=" + _BODY + ">")
    assert batch.sender_id == "~ABC"
    assert batch.ping_interval == 2000
    assert batch.flatten()["DVNM"] == "Desk Lamp"


def test_strict_mode_still_raises():
    from pyrava import ParseError, parse_response

    with pytest.raises(ParseError):
        parse_response("<<~ABC=&2000>=???>", strict=True)


# ------------------------------------------- captured firmware 1.0.1 frame

CAPTURED = (
    "<<68b6b33d3b38=&200>=&{{{<body-handler=&DVIFO>},<DVNM=&Barava>"
    "<DVGR=&{<545d577b=&My-Room>}><FCLR=!0><FBRT=!255><DCLR=!292><DBRT=!10>"
    "<DVTP=!0><DDSN=&02.12.2026.17.57.49.1.2000><DPIN=!0><DVVR=&1.0.1>"
    "<SPON=!0><HTVR=!23><CHRV=!1><HSTT=!0><FSTT=!1><DSTT=!0><DVST=!0>"
    "<DMTT=!1><VHSM=!1><DPEN=!0>}}>"
)


class _Replay:
    def __init__(self, raw=CAPTURED):
        self.raw = raw
        self.sent = []

    def send(self, headers, body):
        self.sent.append((dict(headers), body))
        return self.raw

    def close(self):
        pass


def test_captured_frame_decodes_fully():
    from pyrava import parse_response

    batch = parse_response(CAPTURED)
    assert batch.device_id == "68b6b33d3b38"
    assert batch.ping_interval == 200
    assert batch.handlers == ["DVIFO"]
    info = batch.flatten("DVIFO")
    assert info["DVNM"] == "Barava"
    assert info["DVVR"] == "1.0.1"
    assert info["DDSN"] == "02.12.2026.17.57.49.1.2000"
    assert info["HTVR"] == 23
    assert len(info) == 20


def test_device_id_comes_from_the_key_block_not_dvid():
    """Firmware 1.0.1 omits DVID from DVIFO entirely."""
    fake = _Replay()
    device = BaravaDevice("192.0.2.10", transport=fake)
    info = device.get_device_info()
    assert "DVID" not in info
    assert device.device_id == "68b6b33d3b38"
    assert device.name == "Barava"


def test_reported_interval_is_adopted():
    """The device advertises 200ms, below the vendor's 500ms floor, so we
    record what it said but don't actually ping that fast."""
    fake = _Replay()
    device = BaravaDevice("192.0.2.10", transport=fake, ping_interval=2000)
    device.get_device_info()
    assert device.reported_ping_interval == 200
    assert device.ping_interval == 500  # clamped to MIN_PING_INTERVAL_MS

    fake2 = _Replay()
    pinned = BaravaDevice(
        "192.0.2.10", transport=fake2, ping_interval=2000,
        follow_device_interval=False,
    )
    pinned.get_device_info()
    assert pinned.reported_ping_interval == 200
    assert pinned.ping_interval == 2000


def test_above_floor_interval_is_adopted_verbatim():
    """A device-advertised value at or above the floor is used as-is."""
    raw = CAPTURED.replace("<68b6b33d3b38=&200>", "<68b6b33d3b38=&800>")
    fake = _Replay(raw)
    device = BaravaDevice("192.0.2.10", transport=fake, ping_interval=2000)
    device.get_device_info()
    assert device.reported_ping_interval == 800
    assert device.ping_interval == 800


def test_group_blocks_decode_to_a_mapping():
    from pyrava import parse_groups

    fake = _Replay()
    device = BaravaDevice("192.0.2.10", transport=fake)
    device.get_device_info()
    assert device.groups == {"545d577b": "My-Room"}
    assert parse_groups(["<a1=&Kitchen>", "<b2=&Den>"]) == {
        "a1": "Kitchen",
        "b2": "Den",
    }


def test_outbound_batch_matches_the_device_dialect():
    """Keyed on the device ID, sub-packet array string-marked."""
    fake = _Replay()
    device = BaravaDevice("192.0.2.10", transport=fake, sender_id="~A1B2C3D4E5F6")
    device.register()
    device.send_batch([SubPacket("DSKST", {"DSTT": 1})])
    body = fake.sent[-1][1]
    # register() absorbed the device's advertised 200ms and clamped it to
    # the 500ms floor, so that is what goes back out in the key block.
    assert body.startswith("<<68b6b33d3b38=&500>=&{")
    assert fake.sent[-1][0]["batched-packet"] == ""
    # the session ID still identifies us in the HTTP header
    assert fake.sent[-1][0]["device-id"] == "~A1B2C3D4E5F6"
    round_tripped = parse_batch(body)
    assert round_tripped.handlers == ["DSKST"]


def test_batch_marker_can_be_disabled():
    body = encode_batch("68b6b33d3b38", 200, [SubPacket("DSKST", {"DSTT": 1})],
                        marker="")
    assert body.startswith("<<68b6b33d3b38=&200>={")
    assert parse_batch(body).handlers == ["DSKST"]


# ----------------------------------------------------------- discovery bits

def test_device_id_extraction_variants():
    from pyrava.discovery import DiscoveredService

    def ident(**kw):
        kw.setdefault("host", "1.2.3.4")
        kw.setdefault("port", 8080)
        return DiscoveredService(**kw).device_id

    # "barava" is itself made of hex letters; the prefix must be stripped
    # before searching or the match slides left by one character.
    assert ident(name="barava68b6b33d3b38") == "68b6b33d3b38"
    assert ident(name="Barava-68B6B33D3B38") == "68b6b33d3b38"
    assert ident(name="x", server="barava-68b6b33d3b38.local") == "68b6b33d3b38"
    assert ident(name="Barava Light", properties={"mac": "68:b6:b3:3d:3b:38"}) == (
        "68b6b33d3b38"
    )
    assert ident(name="HP LaserJet", server="printer.local") is None


def test_barava_hint_detection():
    from pyrava.discovery import DiscoveredService

    yes = DiscoveredService("Barava Light", "1.2.3.4", 8080)
    no = DiscoveredService("Sonos Kitchen", "1.2.3.5", 1400)
    assert yes.looks_like_barava
    assert not no.looks_like_barava


def test_ipv6_addresses_are_not_dropped():
    """inet_ntoa raises on 16-byte packing; that used to discard the service."""
    import socket as _socket

    from pyrava.discovery import _addresses_of

    class _Info:
        addresses = [
            _socket.inet_pton(_socket.AF_INET6, "fe80::1"),
            _socket.inet_aton("192.168.1.249"),
        ]

    assert _addresses_of(_Info()) == ["fe80::1", "192.168.1.249"]


# ------------------------------------------------------------ CLI plumbing

def test_cli_colour_parsing():
    from pyrava.__main__ import _parse_rgb

    assert _parse_rgb("#FF8000") == (0xFF, 0x80, 0x00)
    assert _parse_rgb("FF8000") == (0xFF, 0x80, 0x00)
    assert _parse_rgb("255,128,0") == (255, 128, 0)
    with pytest.raises(ValueError):
        _parse_rgb("1,2")


def test_cli_boolish():
    import argparse

    from pyrava.__main__ import _boolish

    assert _boolish("on") is True
    assert _boolish("OFF") is False
    with pytest.raises(argparse.ArgumentTypeError):
        _boolish("maybe")


def test_cli_parser_builds_every_subcommand():
    from pyrava.__main__ import build_parser

    parser = build_parser()
    for command in ["discover", "info", "heater", "set", "raw", "watch", "doctor"]:
        args = parser.parse_args([command] if command != "raw" else [command, "DVIFO"])
        assert callable(args.func)


def test_version_is_single_sourced():
    """pyproject reads __version__ via dynamic metadata; keep them in step."""
    import pyrava

    assert pyrava.__version__.count(".") == 2


# ------------------------------------------------------- polling keepalive

class _DeferredEventDevice:
    """Mock firmware: replies queue and flush one ping later, like the real
    device. Used to prove keepalive=[...] produces continuous callbacks."""

    INFO = "<DVNM=&Desk Lamp><DVID=&x>"
    HEAT = "<HTCT=!6930><HTST=!7000>"

    def __init__(self):
        self.queue = []
        self.calls = []

    def send(self, headers, body):
        self.calls.append(body)
        flush, self.queue = self.queue, []
        wants_info = headers.get("body-handler") == "DVIFO" or "DVIFO" in body
        wants_heat = headers.get("body-handler") == "HTIFO" or "HTIFO" in body
        if wants_info:
            self.queue.append("{{<body-handler=&DVIFO>}," + self.INFO + "}")
        if wants_heat:
            self.queue.append("{{<body-handler=&HTIFO>}," + self.HEAT + "}")
        if not flush:
            return ""
        return f"<<abc123456789=&200>=&{{{','.join(flush)}}}>"

    def close(self):
        pass


def test_bare_ping_keepalive_never_triggers_callbacks():
    """This was the reported bug: keepalive=None requests nothing new, so
    subscriptions never fire even though the loop is running."""
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    seen = []
    session = device.poll(interval=0.03, enforce_interval_floor=False)
    session.on(Handler.DEVICE_INFO, lambda p: seen.append(p.body))
    time.sleep(0.15)
    session.stop()
    assert seen == []
    assert all(body == "" for body in fake.calls)


def test_keepalive_list_produces_continuous_callbacks():
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    device.device_id = "68b6b33d3b38"  # normally set by register()
    seen = []
    session = device.poll(
        interval=0.03, keepalive=[Handler.DEVICE_INFO, Handler.GET_HEATER_INFO],
        enforce_interval_floor=False,
    )
    session.on(Handler.DEVICE_INFO, lambda p: seen.append(("info", p.body)))
    session.on(Handler.GET_HEATER_INFO, lambda p: seen.append(("heat", p.body)))
    time.sleep(0.2)
    session.stop()
    kinds = {k for k, _ in seen}
    assert kinds == {"info", "heat"}
    assert len(seen) >= 4  # several ticks' worth, not a one-off


def test_keepalive_single_handler_still_works():
    """The pre-existing single-handler form must keep working unchanged."""
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    seen = []
    session = device.poll(interval=0.03, keepalive=Handler.GET_HEATER_INFO,
                          enforce_interval_floor=False)
    session.on(Handler.GET_HEATER_INFO, lambda p: seen.append(p.body))
    time.sleep(0.15)
    session.stop()
    assert len(seen) >= 1
    assert seen[0]["HTCT"] == 6930


def test_unregistered_batch_warning_is_throttled(caplog):
    """send_batch before device_id is known should warn once, not per tick."""
    import logging

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)  # never registered
    with caplog.at_level(logging.WARNING, logger="pyrava"):
        for _ in range(3):
            device.send_batch([SubPacket("DVIFO", {})])
    warnings_seen = [r for r in caplog.records if "batching before" in r.message]
    assert len(warnings_seen) == 1


# --------------------------------------------------------------- CLI watch

def test_redraw_plain_fallback_when_not_a_tty(capsys, monkeypatch):
    from pyrava.__main__ import _Redraw

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    redraw = _Redraw()  # ansi=None -> auto-detect -> False, since not a tty
    assert redraw.ansi is False
    redraw.render(["a", "b"])
    redraw.render(["a (updated)", "b"])
    out = capsys.readouterr().out
    assert out.count("a") >= 2  # scrolled, not redrawn in place
    assert "\x1b[" not in out


def test_redraw_ansi_path_moves_cursor_and_clears_lines(capsys):
    from pyrava.__main__ import _Redraw

    redraw = _Redraw(ansi=True)
    redraw.render(["one", "two"])
    redraw.render(["one (updated)", "two"])
    out = capsys.readouterr().out
    assert "\x1b[2A" in out  # moves up by the line count of the first block
    assert "\x1b[2K" in out  # clears each line before rewriting


def test_watch_command_populates_status_from_deferred_replies(monkeypatch):
    """Same fixture shape as the earlier keepalive tests; checks cmd_watch's
    own callbacks build the right summary strings, not just that *a*
    callback fires."""
    monkeypatch.setattr("pyrava.client.MIN_PING_INTERVAL_MS", 10)
    import argparse
    import threading
    import time

    from pyrava import __main__ as cli

    info = ("<DVNM=&Desk Lamp><DVVR=&1.0.1><DVST=!1><FSTT=!1>"
            "<FCLR=!16744448><FBRT=!200><DSTT=!0><DBRT=!10>")
    heat = "<HTCT=!6930><HTST=!7000><HTHH=!100>"

    class Fake:
        def __init__(self):
            self.queue = []

        def send(self, headers, body):
            flush, self.queue = self.queue, []
            if headers.get("body-handler") == "DVIFO" or "DVIFO" in body:
                self.queue.append("{{<body-handler=&DVIFO>}," + info + "}")
            if headers.get("body-handler") == "HTIFO" or "HTIFO" in body:
                self.queue.append("{{<body-handler=&HTIFO>}," + heat + "}")
            if not flush:
                return ""
            return f"<<68b6b33d3b38=&200>=&{{{','.join(flush)}}}>"

        def close(self):
            pass

    device = BaravaDevice("192.0.2.10", transport=Fake())
    device.register()

    captured = {}
    original_connect = cli._connect
    cli._connect = lambda args: device
    try:
        args = argparse.Namespace(
            host="192.0.2.10", port=8080, timeout=5.0, json=False, interval=0.02
        )

        def stop_soon():
            time.sleep(0.15)
            import _thread
            _thread.interrupt_main()

        threading.Thread(target=stop_soon, daemon=True).start()
        # Patch _Redraw to just capture the final rendered state instead of
        # writing escape codes into the test's stdout.
        from pyrava import __main__ as cli_mod

            
        class _Capture:
            def __init__(self, ansi=None):
                pass

            def render(self, lines):
                captured["lines"] = lines

            def finish(self, message):
                captured["finished"] = message

        monkey = cli_mod._Redraw
        cli_mod._Redraw = _Capture
        try:
            try:
                cli.cmd_watch(args)
            except KeyboardInterrupt:
                pass
        finally:
            cli_mod._Redraw = monkey
    finally:
        cli._connect = original_connect

    lines = "\n".join(captured["lines"])
    assert "Desk Lamp" in lines
    assert "fill on" in lines
    assert "69.30" in lines and "70.00" in lines
    assert captured["finished"] == "stopped"


# ----------------------------------------------------------- heater health

def test_describe_heater_health_known_values():
    from pyrava import describe_heater_health

    assert describe_heater_health(0) == "OK"
    assert describe_heater_health(1) == "Fault"


def test_describe_heater_health_unknown_and_missing():
    """A fault code we haven't seen yet should still read sensibly."""
    from pyrava import describe_heater_health

    assert describe_heater_health(2) == "Fault (code 2)"
    assert describe_heater_health(None) == "unknown"
    assert describe_heater_health("garbage") == "Fault (code garbage)"


def test_heater_health_enum_matches_observed_mapping():
    from pyrava import HeaterHealth

    assert HeaterHealth.OK == 0
    assert HeaterHealth.FAULT == 1
    assert str(HeaterHealth.OK) == "OK"
    assert str(HeaterHealth.FAULT) == "Fault"


def _heater_device(health_raw):
    class Fake:
        def send(self, headers, body):
            return (
                "<<abc123456789=&200>=&{{{<body-handler=&HTIFO>},"
                f"<HTCT=!6930><HTST=!7000><HTHH=!{health_raw}>"
                "}}>"
            )
        def close(self):
            pass

    return BaravaDevice("192.0.2.10", transport=Fake())


def test_device_heater_status_ok():
    device = _heater_device(0)
    assert device.heater_status() == "OK"
    assert device.is_heater_ok() is True


def test_device_heater_status_fault():
    device = _heater_device(1)
    assert device.heater_status() == "Fault"
    assert device.is_heater_ok() is False


def test_device_heater_status_missing_field():
    class Fake:
        def send(self, headers, body):
            return "<<abc123456789=&200>=&{{{<body-handler=&HTIFO>},<HTCT=!6930>}}>"
        def close(self):
            pass

    device = BaravaDevice("192.0.2.10", transport=Fake())
    assert device.heater_status() == "unknown"
    assert device.is_heater_ok() is None


def test_cli_heater_status_line(capsys):
    import argparse

    from pyrava import __main__ as cli

    device = _heater_device(1)
    original = cli._connect
    cli._connect = lambda args: device
    try:
        args = argparse.Namespace(
            host="192.0.2.10", port=8080, timeout=5.0, json=False
        )
        cli.cmd_heater(args)
    finally:
        cli._connect = original
    out = capsys.readouterr().out
    assert "status   Fault" in out


def test_cli_watch_shows_status_not_raw_code(monkeypatch):
    """The redrawn watch block should show 'status Fault', not 'health 1'."""
    monkeypatch.setattr("pyrava.client.MIN_PING_INTERVAL_MS", 10)
    import argparse
    import threading
    import time

    from pyrava import __main__ as cli

    info = "<DVNM=&Lamp><DVVR=&1.0.1>"
    heat = "<HTCT=!6930><HTST=!7000><HTHH=!1>"

    class Fake:
        def __init__(self):
            self.queue = []

        def send(self, headers, body):
            flush, self.queue = self.queue, []
            if headers.get("body-handler") == "DVIFO" or "DVIFO" in body:
                self.queue.append("{{<body-handler=&DVIFO>}," + info + "}")
            if headers.get("body-handler") == "HTIFO" or "HTIFO" in body:
                self.queue.append("{{<body-handler=&HTIFO>}," + heat + "}")
            if not flush:
                return ""
            return f"<<68b6b33d3b38=&200>=&{{{','.join(flush)}}}>"

        def close(self):
            pass

    device = BaravaDevice("192.0.2.10", transport=Fake())
    device.register()

    captured = {}
    original_connect = cli._connect
    original_redraw = cli._Redraw
    cli._connect = lambda args: device

    class _Capture:
        def __init__(self, ansi=None):
            pass

        def render(self, lines):
            captured["lines"] = lines

        def finish(self, message):
            captured["finished"] = message

    cli._Redraw = _Capture
    try:
        args = argparse.Namespace(
            host="192.0.2.10", port=8080, timeout=5.0, json=False, interval=0.02
        )

        def stop_soon():
            time.sleep(0.15)
            import _thread
            _thread.interrupt_main()

        threading.Thread(target=stop_soon, daemon=True).start()
        try:
            cli.cmd_watch(args)
        except KeyboardInterrupt:
            pass
    finally:
        cli._connect = original_connect
        cli._Redraw = original_redraw

    lines = "\n".join(captured["lines"])
    assert "status Fault" in lines
    assert "health 1" not in lines


# ------------------------------------------------------- fill hue / colour

def test_rgb_to_hue_matches_textbook_primaries():
    from pyrava import rgb_to_hue

    assert rgb_to_hue(255, 0, 0) == pytest.approx(0, abs=0.5)
    assert rgb_to_hue(0, 255, 0) == pytest.approx(120, abs=0.5)
    assert rgb_to_hue(0, 0, 255) == pytest.approx(240, abs=0.5)
    assert rgb_to_hue(255, 255, 0) == pytest.approx(60, abs=0.5)


def test_rgb_to_hue_rejects_out_of_range_channels():
    from pyrava import rgb_to_hue

    with pytest.raises(ValueError):
        rgb_to_hue(256, 0, 0)
    with pytest.raises(ValueError):
        rgb_to_hue(0, -1, 0)


def test_rgb_no_longer_used_for_fill_still_works_standalone():
    """rgb() itself is unchanged -- it's just not what set_fill_color uses."""
    from pyrava import rgb

    assert rgb(255, 0, 0) == 0xFF0000


def test_set_fill_hue_sends_raw_value_and_validates_range():
    device, fake = _device()
    device.set_fill_hue(255)
    assert fake.log[-1][1] == "<FCLR=!255>"

    with pytest.raises(ValueError, match="0-360"):
        device.set_fill_hue(511)
    with pytest.raises(ValueError, match="0-360"):
        device.set_fill_hue(-1)


def test_set_fill_color_tuple_sends_hue_not_packed_rgb():
    """The original bug: (0,0,255) used to pack to 255 and read as a hue by
    accident, or (0,80,255) packed to 20735 and got rejected by the device.
    Now it should send an actual computed hue for the requested colour."""
    device, fake = _device()

    device.set_fill_color((0, 0, 255))  # pure blue
    sent = fake.log[-1][1]
    assert sent == "<FCLR=!240>"  # blue's textbook hue

    device.set_fill_color((255, 0, 0))  # pure red
    assert fake.log[-1][1] == "<FCLR=!0>"


def test_set_fill_color_bare_number_is_raw_hue():
    device, fake = _device()
    device.set_fill_color(180)
    assert fake.log[-1][1] == "<FCLR=!180>"


def test_cli_fill_hue_argument_bypasses_conversion():
    import argparse

    from pyrava import __main__ as cli

    device, fake = _device()
    original = cli._connect
    cli._connect = lambda args: device
    try:
        args = argparse.Namespace(
            host="x", port=8080, timeout=5.0, json=False,
            power=None, desk=None, heater=None,
            fill_color=None, fill_hue=270.0,
            fill_brightness=None, desk_brightness=None, desk_temp=None,
        )
        cli.cmd_set(args)
    finally:
        cli._connect = original
    assert fake.log[-1][1] == "<FCLR=!270>"


def test_cli_fill_color_argument_converts_rgb_to_hue():
    import argparse

    from pyrava import __main__ as cli

    device, fake = _device()
    original = cli._connect
    cli._connect = lambda args: device
    try:
        args = argparse.Namespace(
            host="x", port=8080, timeout=5.0, json=False,
            power=None, desk=None, heater=None,
            fill_color="0,0,255", fill_hue=None,
            fill_brightness=None, desk_brightness=None, desk_temp=None,
        )
        cli.cmd_set(args)
    finally:
        cli._connect = original
    assert fake.log[-1][1] == "<FCLR=!240>"


# ------------------------------- vendor design notes: cyclical interface

def test_interval_floor_constants_match_the_design_notes():
    from pyrava.client import (
        DATA_POLL_INTERVAL_MS,
        DEFAULT_PING_INTERVAL_MS,
        MIN_PING_INTERVAL_MS,
    )

    # "no less than 500MS for maximum network and device stability"
    assert MIN_PING_INTERVAL_MS == 500
    # "otherwise it should be kept anywhere from 800-1000ms delay"
    assert 800 <= DEFAULT_PING_INTERVAL_MS <= 1000
    # "For data polling I.E heater temperature, it is acceptable to reach
    # this 500MS keepalive threshold"
    assert DATA_POLL_INTERVAL_MS == MIN_PING_INTERVAL_MS


def test_poll_clamps_below_floor_intervals():
    fake = _Replay()
    device = BaravaDevice("192.0.2.10", transport=fake)
    session = device.poll(interval=0.05)
    try:
        assert session.interval == 0.5
    finally:
        session.stop()


def test_poll_floor_can_be_waived_for_mocks():
    fake = _Replay()
    device = BaravaDevice("192.0.2.10", transport=fake)
    session = device.poll(interval=0.05, enforce_interval_floor=False)
    try:
        assert session.interval == 0.05
    finally:
        session.stop()


def test_urgent_enqueue_flushes_before_the_next_interval():
    """Passive commands wait for the tick; urgent ones force it early."""
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    device.device_id = "68b6b33d3b38"
    # A long interval, so anything arriving quickly must have been forced.
    session = device.poll(interval=5.0, enforce_interval_floor=False)
    try:
        time.sleep(0.1)
        calls_before = len(fake.calls)
        session.enqueue(Handler.SET_FILL_COLOR, {"FCLR": 240}, urgent=True)
        time.sleep(0.2)  # far less than the 5s interval
        assert len(fake.calls) > calls_before
        assert "FCLR" in fake.calls[-1]
    finally:
        session.stop()


def test_passive_enqueue_waits_for_the_tick():
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    device.device_id = "68b6b33d3b38"
    session = device.poll(interval=5.0, enforce_interval_floor=False)
    try:
        time.sleep(0.1)
        calls_before = len(fake.calls)
        session.enqueue(Handler.SET_FILL_COLOR, {"FCLR": 240})  # passive
        time.sleep(0.2)
        assert len(fake.calls) == calls_before  # still waiting for the tick
    finally:
        session.stop()


def test_callback_return_value_is_queued_as_a_response():
    """The design notes say handler callbacks should produce a response."""
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    device.device_id = "68b6b33d3b38"
    session = device.poll(
        interval=0.03, keepalive=Handler.GET_HEATER_INFO,
        enforce_interval_floor=False,
    )
    try:
        session.on(
            Handler.GET_HEATER_INFO,
            lambda p: SubPacket("DVIFO", {}),  # reply to every heater packet
        )
        time.sleep(0.25)
    finally:
        session.stop()
    assert any("DVIFO" in body for body in fake.calls)


def test_callback_returning_none_queues_nothing():
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    device.device_id = "68b6b33d3b38"
    session = device.poll(
        interval=0.03, keepalive=Handler.GET_HEATER_INFO,
        enforce_interval_floor=False,
    )
    try:
        session.on(Handler.GET_HEATER_INFO, lambda p: None)
        time.sleep(0.15)
    finally:
        session.stop()
    # Only the keepalive handler should ever appear, never a batch.
    assert not any("DVIFO" in body for body in fake.calls)


def test_stop_is_prompt_even_with_a_long_interval():
    """stop() wakes the interval wait rather than blocking for a full tick."""
    import time

    fake = _DeferredEventDevice()
    device = BaravaDevice("192.0.2.1", transport=fake)
    session = device.poll(interval=30.0, enforce_interval_floor=False)
    time.sleep(0.05)
    started = time.monotonic()
    session.stop()
    assert time.monotonic() - started < 2.0


def test_direct_calls_do_not_race_the_poll_thread():
    """A session pings on a background thread while your code calls setters;
    both go through one transport, so they must be serialised."""
    import threading
    import time

    overlaps = []
    guard = threading.Lock()
    in_flight = [0]

    class SlowTransport:
        def send(self, headers, body):
            with guard:
                in_flight[0] += 1
                if in_flight[0] > 1:
                    overlaps.append(True)
            time.sleep(0.005)  # widen the race window
            with guard:
                in_flight[0] -= 1
            return "<<abc123456789=&500>=&{{{<body-handler=&HTIFO>},<HTCT=!6930>}}>"

        def close(self):
            pass

    device = BaravaDevice("192.0.2.1", transport=SlowTransport())
    device.device_id = "abc123456789"
    session = device.poll(interval=0.01, enforce_interval_floor=False)
    try:
        for _ in range(15):
            device.set_fill_hue(200)
            time.sleep(0.003)
    finally:
        session.stop()

    assert not overlaps


# ------------------------------- captured themes (app, firmware 1.0.1)

#: Three ANDT payloads captured from the device's own app.
THEME_OFF = "0200170f040f020f030f000f01070100050a06"
THEME_RGB = (
    "0200170f040f020f030f000f0107010005170f0429ff0000"
    "170f022900ff15170f03290900ff0a06"
)
THEME_RGB_PLUS_GRADIENT = (
    "0200170f040f020f030f000f0107010005170f0011120007003f125504003112aa"
    "00043614170f0429ff0007170f022900ffd4170f03290d00ff0a06"
)


def test_captured_themes_recompile_exactly():
    """Byte-exact reproduction is the proof the bytecode model is right.

    These three payloads came off real firmware; if build_theme() emits the
    same bytes, the opcode arities, the zone order and the omission of the
    leading SCRIPT_HEADER are all correct.
    """
    device = BaravaDevice("192.0.2.1")

    off = device.build_theme()
    assert off.to_hex().lower() == THEME_OFF

    rgb_theme = device.build_theme(
        {4: (0xFF, 0x00, 0x00), 2: (0x00, 0xFF, 0x15), 3: (0x09, 0x00, 0xFF)}
    )
    assert rgb_theme.to_hex().lower() == THEME_RGB

    combined = device.build_theme(
        solids={4: (0xFF, 0x00, 0x07), 2: (0x00, 0xFF, 0xD4), 3: (0x0D, 0x00, 0xFF)},
        gradients={0: ((0x00, 0x07, 0x00, 0x3F),
                       (0x55, 0x04, 0x00, 0x31),
                       (0xAA, 0x00, 0x04, 0x36))},
    )
    assert combined.to_hex().lower() == THEME_RGB_PLUS_GRADIENT


def test_captured_themes_disassemble_without_desync():
    """Every byte must be consumed; a wrong arity would leave a tail."""
    for payload in (THEME_OFF, THEME_RGB, THEME_RGB_PLUS_GRADIENT):
        lines = disassemble(payload)
        assert lines
        assert not any("??" in line or "unknown" in line.lower() for line in lines)


def test_empty_theme_is_the_all_off_theme():
    """No zones set == the app's blank theme, not an empty script."""
    device = BaravaDevice("192.0.2.1")
    assert device.build_theme().to_hex().lower() == THEME_OFF


def test_zone_order_matches_the_app_header():
    from pyrava import ZONE_ORDER

    assert ZONE_ORDER == (4, 2, 3, 0, 1)
    header = disassemble(THEME_OFF)
    selected = [
        int(line.split("=")[1].rstrip(")"))
        for line in header if "SELECT_ZONE" in line
    ]
    assert tuple(selected) == ZONE_ORDER


def test_set_zone_colors_uploads_to_cmpam():
    device, fake = _device()
    device.set_zone_colors({4: (255, 0, 0)})
    handler, body, _ = fake.log[-1]
    assert handler == "CMPAM"
    assert "ANDT" in body


def test_clear_zones_sends_the_off_theme():
    device, fake = _device()
    device.clear_zones()
    body = fake.log[-1][1]
    assert THEME_OFF.upper() in body


def test_set_zone_gradient_targets_one_zone():
    device, fake = _device()
    device.set_zone_gradient(0, (0, 7, 0, 63), (255, 0, 4, 54))
    payload = parse_body(fake.log[-1][1])["ANDT"]
    lines = "\n".join(disassemble(payload))
    assert "FILL_ZONE" in lines
    assert lines.count("BUILD_GRADIENT") == 2
    assert "MAP_LINEAR" in lines


def test_zone_names_match_confirmed_mapping():
    """Confirmed by physically lighting each ring in turn."""
    from pyrava import Zone

    assert Zone.TOP == 4
    assert Zone.MIDDLE_INNER == 2
    assert Zone.MIDDLE_OUTER == 3
    assert Zone.BOTTOM_INNER == 0
    assert Zone.BOTTOM_OUTER == 1


def test_zone_enum_works_as_a_plain_int_in_theme_building():
    """Zone is an IntEnum so it's a drop-in dict key / int wherever a raw
    zone index was previously used."""
    from pyrava import Zone

    device = BaravaDevice("192.0.2.1")
    by_enum = device.build_theme({Zone.TOP: (255, 0, 0)}).to_hex()
    by_int = device.build_theme({4: (255, 0, 0)}).to_hex()
    assert by_enum == by_int


# ---------------------------------------------------- gradients from N colors

def test_generate_gradient_stops_matches_captured_spacing():
    """i * (256 // N), confirmed by two independent 3-stop captures."""
    from pyrava import generate_gradient_stops

    stops = generate_gradient_stops([(255, 80, 0), (255, 0, 160), (41, 0, 255)])
    assert [pos for pos, *_ in stops] == [0, 85, 170]
    assert stops == [
        (0, 255, 80, 0),
        (85, 255, 0, 160),
        (170, 41, 0, 255),
    ]


def test_generate_gradient_stops_scales_to_other_counts():
    from pyrava import generate_gradient_stops

    two = generate_gradient_stops([(255, 0, 0), (0, 0, 255)])
    assert [pos for pos, *_ in two] == [0, 128]

    five = generate_gradient_stops([(0, 0, 0)] * 5)
    assert [pos for pos, *_ in five] == [0, 51, 102, 153, 204]


def test_generate_gradient_stops_requires_at_least_two_colors():
    from pyrava import generate_gradient_stops

    with pytest.raises(ValueError, match="at least two"):
        generate_gradient_stops([(255, 0, 0)])


def test_captured_single_zone_gradient_recompiles_exactly():
    """Confirms FILL_ZONE + BUILD_GRADIENT works scoped to one zone alone,
    not just as part of a multi-zone theme."""
    CAPTURED = (
        "0200170f040f020f030f000f0107010005170f04111200ff8800"
        "1255ff00a012aa2900ff140a06"
    )
    device = BaravaDevice("192.0.2.1")
    script = device.build_theme(gradients={
        Zone.TOP: ((0, 255, 136, 0), (85, 255, 0, 160), (170, 41, 0, 255)),
    })
    assert script.to_hex().lower() == CAPTURED


def test_set_gradient_defaults_to_every_zone():
    device, fake = _device()
    device.set_gradient([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
    payload = parse_body(fake.log[-1][1])["ANDT"]
    lines = "\n".join(disassemble(payload))
    assert lines.count("FILL_ZONE") == 5  # every zone got the gradient


def test_set_gradient_targets_one_group():
    device, fake = _device()
    device.set_gradient([(255, 0, 0), (0, 255, 0)], zones="downlamp")
    payload = parse_body(fake.log[-1][1])["ANDT"]
    lines = "\n".join(disassemble(payload))
    assert lines.count("FILL_ZONE") == 2  # BOTTOM_INNER + BOTTOM_OUTER only


# --------------------------------------------------------------- zone groups

def test_zone_groups_match_requested_aliases():
    from pyrava import ZONE_GROUPS, Zone

    assert set(ZONE_GROUPS["lava_lamp"]) == {Zone.TOP, Zone.MIDDLE_INNER, Zone.MIDDLE_OUTER}
    assert set(ZONE_GROUPS["downlamp"]) == {Zone.BOTTOM_INNER, Zone.BOTTOM_OUTER}
    assert ZONE_GROUPS["top_ooze"] == (Zone.TOP,)
    assert ZONE_GROUPS["bottom_ooze"] == (Zone.MIDDLE_INNER,)
    assert ZONE_GROUPS["fluid"] == (Zone.MIDDLE_OUTER,)


def test_unknown_group_name_raises_a_helpful_error():
    device = BaravaDevice("192.0.2.1")
    with pytest.raises(ValueError, match="unknown zone group"):
        device.build_theme({"not_a_real_group": (255, 0, 0)})


def test_group_name_expands_to_member_zones_in_theme():
    device = BaravaDevice("192.0.2.1")
    by_group = device.build_theme({"top_ooze": (255, 0, 0)})
    by_zone = device.build_theme({Zone.TOP: (255, 0, 0)})
    assert by_group.to_hex() == by_zone.to_hex()


def test_overlapping_groups_last_write_wins():
    """lava_lamp includes TOP; top_ooze is also just TOP. Whichever is
    given second in the mapping should determine TOP's final colour."""
    device = BaravaDevice("192.0.2.1")
    script = device.build_theme({
        "lava_lamp": (255, 0, 0),
        "top_ooze": (0, 255, 0),  # should win for zone TOP specifically
    })
    lines = disassemble(script.to_hex())
    last_zone4_index = max(
        i for i, l in enumerate(lines) if "SELECT_ZONE(zone=4)" in l
    )
    assert "SET_RGB(r=0, g=255, b=0)" in lines[last_zone4_index + 1]


# --------------------------------------------------- partial zone updates

def test_set_zone_state_tracks_what_was_sent():
    device, fake = _device()
    device.set_zone_colors({Zone.TOP: (255, 0, 0)})
    assert device.known_zone_colors == {Zone.TOP: (255, 0, 0)}


def test_clear_zone_preserves_other_remembered_zones():
    device, fake = _device()
    device.set_zone_state("lava_lamp", (255, 80, 0))
    device.set_zone_state("downlamp", (0, 40, 255))
    assert set(device.known_zone_colors) == {
        Zone.TOP, Zone.MIDDLE_INNER, Zone.MIDDLE_OUTER,
        Zone.BOTTOM_INNER, Zone.BOTTOM_OUTER,
    }

    device.clear_zone("downlamp")
    assert Zone.BOTTOM_INNER not in device.known_zone_colors
    assert Zone.BOTTOM_OUTER not in device.known_zone_colors
    # lava_lamp's three zones must survive untouched
    for z in (Zone.TOP, Zone.MIDDLE_INNER, Zone.MIDDLE_OUTER):
        assert device.known_zone_colors[z] == (255, 80, 0)

    payload = parse_body(fake.log[-1][1])["ANDT"]
    lines = "\n".join(disassemble(payload))
    assert lines.count("SET_RGB(r=255, g=80, b=0)") == 3
    assert "SET_RGB(r=0, g=40, b=255)" not in lines


def test_set_zone_state_replaces_a_gradient_with_a_solid():
    device, fake = _device()
    device.set_zone_gradient(Zone.TOP, (0, 255, 0, 0), (128, 0, 0, 255))
    assert Zone.TOP in device.known_zone_gradients
    device.set_zone_state(Zone.TOP, (10, 20, 30))
    assert Zone.TOP not in device.known_zone_gradients
    assert device.known_zone_colors[Zone.TOP] == (10, 20, 30)


def test_clear_zones_resets_the_whole_shadow():
    device, fake = _device()
    device.set_zone_colors({Zone.TOP: (1, 2, 3)})
    assert device.known_zone_colors
    device.clear_zones()
    assert device.known_zone_colors == {}
    assert device.known_zone_gradients == {}


# ------------------------------------------------ device-safety guardrails

def test_zero_rotation_is_refused():
    """The firmware author reports zero rotation/hue deadlocks the engine."""
    from pyrava import AnimationScript
    from pyrava.errors import CompileError

    script = AnimationScript()
    with script.header(0):
        script.select_zone(4)
    with script.thread(0):
        with script.main():
            for method in ("rotate_left", "rotate_right", "rotate_up", "rotate_down"):
                with pytest.raises(CompileError, match="rotation amount 0"):
                    getattr(script, method)(0)


def test_nonzero_rotation_still_allowed():
    from pyrava import AnimationScript

    script = AnimationScript()
    with script.header(0):
        script.select_zone(4)
    with script.thread(0):
        with script.main():
            script.rotate_left(5)  # must not raise
    assert "ROTATE_LEFT" in "\n".join(disassemble(script.to_hex()))


def test_animation_uploads_are_throttled_by_default():
    """Consecutive uploads must be spaced to protect the device."""
    import time

    fake = _DeferredDevice()
    device = BaravaDevice("192.0.2.10", transport=fake)
    device.min_animation_gap = 0.3  # shorten for the test but keep it real

    start = time.monotonic()
    device.set_zone_colors({Zone.TOP: (255, 0, 0)})
    device.set_zone_colors({Zone.TOP: (0, 255, 0)})
    elapsed = time.monotonic() - start
    assert elapsed >= 0.3


def test_animation_throttle_can_be_disabled():
    import time

    fake = _DeferredDevice()
    device = BaravaDevice("192.0.2.10", transport=fake)
    device.min_animation_gap = 0

    start = time.monotonic()
    for _ in range(3):
        device.set_zone_colors({Zone.TOP: (255, 0, 0)})
    assert time.monotonic() - start < 0.5


def test_first_upload_is_not_delayed():
    """Throttle only spaces *consecutive* uploads; the first is immediate."""
    import time

    fake = _DeferredDevice()
    device = BaravaDevice("192.0.2.10", transport=fake)
    device.min_animation_gap = 5.0

    start = time.monotonic()
    device.set_zone_colors({Zone.TOP: (255, 0, 0)})
    assert time.monotonic() - start < 1.0
