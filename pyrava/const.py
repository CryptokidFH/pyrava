"""Protocol constants for the Barava network protocol.

Derived from `network.md` (ESP verno 1.0.0, 10/11/24).
"""

from __future__ import annotations

from enum import Enum, IntEnum

# --------------------------------------------------------------------------
# mDNS
# --------------------------------------------------------------------------

SERVICE_TYPE = "_barava._tcp.local."

# --------------------------------------------------------------------------
# Syntax delimiters
# --------------------------------------------------------------------------

BLOCK_START = "<"
BLOCK_SPLIT = "="
BLOCK_END = ">"

ARRAY_START = "{"
ARRAY_SPLIT = ","
ARRAY_END = "}"

MARKER_NUMBER = "!"  # base 10
MARKER_STRING = "&"
MARKER_HEX = "$"  # base 16, parsed to decimal

MARKERS = (MARKER_NUMBER, MARKER_STRING, MARKER_HEX)

#: Characters that cannot appear inside a raw string value without breaking
#: the (delimiter-based, un-escaped) wire format.
RESERVED_CHARS = frozenset("<>={},!&$")


# --------------------------------------------------------------------------
# HTTP-level packet headers
# --------------------------------------------------------------------------


class Header(str, Enum):
    """Packet header keys."""

    DEVICE_ID = "device-id"
    BATCHED_PACKET = "batched-packet"
    BODY_HANDLER = "body-handler"
    PING_INTERVAL = "ping-interval"
    DEV_KEY = "dev-key"  # DEPRECATED -- use DVSTP / Privilege instead

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


# --------------------------------------------------------------------------
# Privilege levels
# --------------------------------------------------------------------------


class Privilege(IntEnum):
    USER = 0
    BETA = 1
    ALPHA = 2
    DEV = 3


# --------------------------------------------------------------------------
# Heater health (HTHH)
# --------------------------------------------------------------------------


class HeaterHealth(IntEnum):
    """``HTHH``: whether the heater reports a fault.

    Confirmed against the app: its "Heater Status" readout shows "Ok" when
    this is 0. Only 0 has actually been correlated against the app; ``FAULT``
    (any nonzero value the app might show as an error) is inferred, not yet
    observed. Use :func:`~pyrava.client.describe_heater_health` rather than
    constructing this directly -- it degrades gracefully if a fault turns out
    to use a code other than 1.
    """

    OK = 0
    FAULT = 1

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return "OK" if self is HeaterHealth.OK else "Fault"


# --------------------------------------------------------------------------
# LED zones
# --------------------------------------------------------------------------


class Zone(IntEnum):
    """Addressable LED zone indices, confirmed by lighting each in turn.

    Five zones: a top ring, a middle ring split into inner/outer, and a
    bottom ring split into inner/outer.
    """

    BOTTOM_INNER = 0
    MIDDLE_INNER = 2
    MIDDLE_OUTER = 3
    TOP = 4
    BOTTOM_OUTER = 1


# --------------------------------------------------------------------------
# Event handlers (the "body-handler" value)
# --------------------------------------------------------------------------


class Handler(str, Enum):
    """Function event handlers."""

    # --- user mode -------------------------------------------------------
    DEVICE_INFO = "DVIFO"
    DISCONNECT_WIFI = "DSCWF"
    SET_DEVICE_NAME = "DVSNM"
    ADD_DEVICE_GROUP = "DVAGR"
    REMOVE_DEVICE_GROUP = "DVRGR"
    WIPE_DEVICE_GROUPS = "WIPGR"
    SET_DEVICE_STATE = "DVSTT"
    SET_HEATER_STATE = "HTSTT"
    SET_FILL_COLOR = "COLFL"
    SET_FILL_STATE = "COLST"
    SET_FILL_BRIGHTNESS = "COLBR"
    SET_DESK_COLOR = "DSKCL"
    SET_DESK_BRIGHTNESS = "DSKBR"
    SET_DESK_STATE = "DSKST"
    COMPILE_ANIMATION = "CMPAM"
    TOGGLE_SPECTRUM = "SSPEC"
    RUN_UPDATE_CHECK = "CHUPD"
    GET_HEATER_INFO = "HTIFO"
    WIPE_DEVICE_INFO = "WPDVI"
    RUN_UPDATE = "UPDVC"
    SET_DEVICE_TYPE = "DVSTP"

    # --- developer mode --------------------------------------------------
    DEV_UPDATE_HEATER_CONFIG = "_HSCFG"
    DEV_DUMP_HEATER_REGISTERS = "_DHTCF"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


#: Handlers that require privilege > USER.
DEV_HANDLERS = frozenset(
    {Handler.DEV_UPDATE_HEATER_CONFIG, Handler.DEV_DUMP_HEATER_REGISTERS}
)


# --------------------------------------------------------------------------
# Event variables (block keys inside a packet body)
# --------------------------------------------------------------------------


class Var(str, Enum):
    """Functional event variables."""

    # --- user mode -------------------------------------------------------
    DEVICE_NAME = "DVNM"
    DEVICE_ID = "DVID"  # documented, but firmware 1.0.1 omits it from DVIFO
    DEVICE_TYPE = "DVTP"
    SENDER_ID = "SDID"
    DEVICE_GROUPS = "DVGR"
    DEVICE_STATE = "DVST"
    GROUP_NAME = "GRNM"
    GROUP_ID = "GRID"
    HEATER_STATE = "HSTT"
    FILL_COLOR = "FCLR"  # hue 0-360, not packed RGB (see rgb_to_hue)
    FILL_BRIGHTNESS = "FBRT"
    FILL_STATE = "FSTT"
    DESK_COLOR = "DCLR"  # colour temperature, not packed RGB (observed 292)
    DESK_BRIGHTNESS = "DBRT"
    DESK_STATE = "DSTT"
    ANIMATION_DATA = "ANDT"
    SPEC_PALLETE = "SPLT"
    SPEC_ON = "SPON"
    HEATER_SET_TEMP = "HTST"
    HEATER_CUR_TEMP = "HTCT"
    HEATER_HEALTH = "HTHH"
    RESET_REASON = "RSTR"
    DEVICE_VERSION = "DVVR"
    DEVICE_MUTE = "DMTT"
    HAS_CONFIG = "VHSM"
    HEAT_PID_OUT = "HPOT"
    VERSION_LATEST = "VRLT"
    UPDATE_FAILED = "UPFL"
    UPDATE_STARTED = "UPST"

    # --- observed on firmware 1.0.1 but absent from network.md ------------
    # These are real labels seen in DVIFO responses. Meanings are inferred
    # from context and observed values, so treat them as provisional.
    DEVICE_SERIAL = "DDSN"  # str, e.g. "02.12.2026.17.57.49.1.2000"
    DEVICE_PIN = "DPIN"  # int, 0 on an unpaired device
    HEATER_VERSION = "HTVR"  # int, e.g. 23
    CHARGER_REVISION = "CHRV"  # int, e.g. 1
    DEVICE_PAIR_ENABLED = "DPEN"  # int, 0/1

    # --- developer mode --------------------------------------------------
    HEAT_IDLE_TEMP = "_HIT"
    HEAT_ON_TEMP = "_HAT"
    HEAT_OFF_TEMP = "_HOT"
    PID_PROP = "_PRP"
    PID_DERI = "_DRI"
    PID_INTI = "_INT"
    HEAT_TOP_TEMP = "_TTE"
    HEAT_BOT_TEMP = "_BTE"
    HEAT_PAD_TEMP = "_PTE"
    HEAT_HEATER_ON = "_HTO"
    HEAT_LIM_TEMP = "_IST"
    FLASH_LEDS = "_FLD"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


#: Declared wire types, used for outbound coercion and inbound normalisation.
#: `str`/`int`/`list` mirror the "Type" column of the variable table.
VAR_TYPES: dict[str, type] = {
    Var.DEVICE_NAME: str,
    Var.DEVICE_ID: str,
    Var.DEVICE_TYPE: str,  # str in / int out -- see docs
    Var.SENDER_ID: str,
    Var.DEVICE_GROUPS: list,
    Var.DEVICE_STATE: int,
    Var.GROUP_NAME: str,
    Var.GROUP_ID: str,
    Var.HEATER_STATE: int,
    Var.FILL_COLOR: int,
    Var.FILL_BRIGHTNESS: int,
    Var.FILL_STATE: int,
    Var.DESK_COLOR: int,
    Var.DESK_BRIGHTNESS: int,
    Var.DESK_STATE: int,
    Var.ANIMATION_DATA: str,  # hex-encoded blob
    Var.SPEC_PALLETE: list,
    Var.SPEC_ON: int,
    Var.HEATER_SET_TEMP: int,
    Var.HEATER_CUR_TEMP: int,
    Var.HEATER_HEALTH: int,
    Var.RESET_REASON: int,
    Var.DEVICE_VERSION: str,
    Var.DEVICE_MUTE: int,
    Var.HAS_CONFIG: int,
    Var.HEAT_PID_OUT: int,
    Var.VERSION_LATEST: int,
    Var.UPDATE_FAILED: int,
    Var.UPDATE_STARTED: int,
    # observed on firmware, undocumented
    Var.DEVICE_SERIAL: str,
    Var.DEVICE_PIN: int,
    Var.HEATER_VERSION: int,
    Var.CHARGER_REVISION: int,
    Var.DEVICE_PAIR_ENABLED: int,
}

#: Reverse lookup, e.g. "FCLR" -> Var.FILL_COLOR
VAR_BY_LABEL: dict[str, Var] = {v.value: v for v in Var}
HANDLER_BY_LABEL: dict[str, Handler] = {h.value: h for h in Handler}
