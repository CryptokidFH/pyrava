"""A Python interface for Barava smart devices.

Quick start::

    from pyrava import discover_devices

    devices = discover_devices(timeout=5)
    light = devices[0]
    light.set_state(True)
    light.set_fill_color((255, 40, 0))

Or connect directly if you already know the address::

    from pyrava import BaravaDevice

    light = BaravaDevice("192.168.1.42")
    print(light.get_device_info())
"""

from __future__ import annotations

from .animation import Command, AnimationScript, disassemble
from .client import (
    BaravaDevice,
    PollingSession,
    describe_heater_health,
    discover_devices,
    new_sender_id,
    rgb,
    rgb_to_hue,
)
from .const import Handler, Header, HeaterHealth, Privilege, Var
from .discovery import CANDIDATE_TYPES, DiscoveredService, browse, list_service_types, scan
from .errors import (
    BaravaError,
    CompileError,
    DiscoveryError,
    EncodeError,
    ParseError,
    TransportError,
)
from .packet import (
    Batch,
    HexInt,
    Packet,
    SubPacket,
    encode_batch,
    encode_block,
    encode_body,
    parse_batch,
    parse_body,
    parse_groups,
    parse_response,
)
from .transport import HttpTransport, Transport

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # client
    "BaravaDevice",
    "PollingSession",
    "describe_heater_health",
    "discover_devices",
    "new_sender_id",
    "rgb",
    "rgb_to_hue",
    # discovery
    "CANDIDATE_TYPES",
    "DiscoveredService",
    "browse",
    "list_service_types",
    "scan",
    # protocol constants
    "Handler",
    "Header",
    "HeaterHealth",
    "Privilege",
    "Var",
    # packets
    "Batch",
    "HexInt",
    "Packet",
    "SubPacket",
    "encode_batch",
    "encode_block",
    "encode_body",
    "parse_batch",
    "parse_body",
    "parse_groups",
    "parse_response",
    # animation
    "AnimationScript",
    "Command",
    "disassemble",
    # transport
    "HttpTransport",
    "Transport",
    # errors
    "BaravaError",
    "CompileError",
    "DiscoveryError",
    "EncodeError",
    "ParseError",
    "TransportError",
]
