"""mDNS discovery for Barava devices.

`network.md` says devices advertise under ``_barava._tcp.local.``, but that has
never been confirmed against hardware, and vendors routinely advertise under a
generic type (``_http._tcp``) with a product-specific instance name. So
:func:`browse` takes a list of candidate types, and :func:`scan` enumerates
whatever is actually on the network when none of them match.

Discovery only yields an address; the device's own ID is learned afterwards
from the key block of any response (see :func:`pyrava.client.discover_devices`).
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .const import SERVICE_TYPE
from .errors import DiscoveryError
from .transport import DEFAULT_PORT

__all__ = [
    "DiscoveredService",
    "browse",
    "scan",
    "list_service_types",
    "CANDIDATE_TYPES",
]

_ID_PATTERN = re.compile(r"[0-9a-fA-F]{12}")
#: A 12-hex run not touching other hex characters, so "barava68b6..." can't
#: contribute its own a/b/e letters to the match.
_ISOLATED_ID = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{12}(?![0-9a-fA-F])")

#: Types to try, in order. The documented one first, then the generic types a
#: device like this plausibly registers under.
CANDIDATE_TYPES: tuple[str, ...] = (
    SERVICE_TYPE,
    "_barava-host._tcp.local.",
    "_http._tcp.local.",
    "_arduino._tcp.local.",
    "_esp._tcp.local.",
)

#: Substring used to recognise a Barava instance among generic services.
NAME_HINT = "barava"


@dataclass
class DiscoveredService:
    """A device found on the local network, before any protocol handshake."""

    name: str
    host: str
    port: int
    properties: dict[str, str] = field(default_factory=dict)
    addresses: list[str] = field(default_factory=list)
    service_type: str = ""
    server: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def device_id(self) -> str | None:
        """Best-effort device ID from the service name or TXT records.

        Convenience only; the authoritative ID is the key block of any
        response, which the client reads automatically.
        """
        for key in ("device-id", "deviceid", "id", "mac"):
            value = self.properties.get(key, "").replace(":", "").replace("-", "")
            if value and _ID_PATTERN.fullmatch(value):
                return value.lower()
        for text in (self.name, self.server):
            stem = text.replace(":", "").replace("-", "").replace("_", "").lower()
            stem = stem.split(".")[0]
            # Strip the product prefix first: "barava" is itself made of hex
            # letters, so searching the whole string finds a bogus match.
            if stem.startswith(NAME_HINT):
                candidate = stem[len(NAME_HINT) :]
                if _ID_PATTERN.fullmatch(candidate):
                    return candidate
            match = _ISOLATED_ID.search(stem)
            if match:
                return match.group(0)
        return None

    @property
    def looks_like_barava(self) -> bool:
        blob = f"{self.name} {self.server} {self.service_type}".lower()
        return NAME_HINT in blob

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        suffix = f" [{self.service_type}]" if self.service_type else ""
        return f"{self.name} @ {self.host}:{self.port}{suffix}"


def _require_zeroconf():
    try:
        import zeroconf
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DiscoveryError(
            "mDNS discovery needs the 'zeroconf' package: pip install zeroconf"
        ) from exc
    return zeroconf


def _addresses_of(info: Any) -> list[str]:
    """Extract printable addresses, IPv4 or IPv6.

    ``socket.inet_ntoa`` raises on 16-byte IPv6 packing, which previously
    caused a device advertising AAAA records to be dropped silently.
    """
    parsed = getattr(info, "parsed_addresses", None)
    if callable(parsed):
        try:
            return [str(a) for a in parsed()]
        except Exception:  # pragma: no cover - defensive
            pass
    out: list[str] = []
    for packed in getattr(info, "addresses", None) or []:
        family = socket.AF_INET6 if len(packed) == 16 else socket.AF_INET
        try:
            out.append(socket.inet_ntop(family, packed))
        except (OSError, ValueError):
            continue
    return out


def _properties_of(info: Any) -> dict[str, str]:
    props: dict[str, str] = {}
    for key, value in (getattr(info, "properties", None) or {}).items():
        try:
            k = key.decode() if isinstance(key, bytes) else str(key)
            v = value.decode() if isinstance(value, bytes) else str(value)
        except UnicodeDecodeError:
            continue
        props[k.lower()] = v
    return props


def browse(
    timeout: float = 5.0,
    *,
    service_type: str | Sequence[str] = CANDIDATE_TYPES,
    minimum: int = 0,
    interfaces: Iterable[str] | None = None,
    require_hint: bool = False,
) -> list[DiscoveredService]:
    """Browse one or more service types for up to ``timeout`` seconds.

    ``service_type`` accepts a single type or a sequence; all are browsed
    concurrently. ``interfaces`` pins Zeroconf to specific local IPs, which
    matters on machines with VPN or hypervisor adapters that otherwise
    swallow multicast. ``require_hint`` keeps only instances whose name looks
    like a Barava device, useful when browsing generic types.

    Returns as soon as ``minimum`` services have answered, if ``minimum`` is
    greater than zero; otherwise waits the full timeout.
    """
    zc_mod = _require_zeroconf()
    types = [service_type] if isinstance(service_type, str) else list(service_type)
    if not types:
        raise DiscoveryError("no service types to browse")

    found: dict[str, DiscoveredService] = {}

    class _Listener(zc_mod.ServiceListener):
        def _resolve(self, zc, type_: str, name: str) -> None:
            # Keep this short: it runs on the browser thread, and a long
            # blocking resolve here stalls every other callback.
            info = zc.get_service_info(type_, name, timeout=3000)
            if info is None:
                return
            addresses = _addresses_of(info)
            suffix = "." + type_
            trimmed = name[: -len(suffix)] if name.endswith(suffix) else name
            service = DiscoveredService(
                name=trimmed or name,
                host=addresses[0] if addresses else "",
                port=info.port or DEFAULT_PORT,
                properties=_properties_of(info),
                addresses=addresses,
                service_type=type_,
                server=(getattr(info, "server", "") or "").rstrip("."),
            )
            # Record even without an address so callers can see it existed.
            found[f"{type_}|{name}"] = service

        def add_service(self, zc, type_: str, name: str) -> None:
            self._resolve(zc, type_, name)

        def update_service(self, zc, type_: str, name: str) -> None:
            self._resolve(zc, type_, name)

        def remove_service(self, zc, type_: str, name: str) -> None:
            found.pop(f"{type_}|{name}", None)

    kwargs: dict[str, Any] = {}
    if interfaces:
        kwargs["interfaces"] = list(interfaces)

    zc = zc_mod.Zeroconf(**kwargs)
    try:
        browser = zc_mod.ServiceBrowser(zc, types, _Listener())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if minimum and len([s for s in found.values() if s.host]) >= minimum:
                break
            time.sleep(0.1)
        browser.cancel()
    finally:
        zc.close()

    results = [s for s in found.values() if s.host]
    if require_hint:
        results = [s for s in results if s.looks_like_barava]
    return sorted(results, key=lambda s: (s.service_type, s.name))


def list_service_types(timeout: float = 5.0) -> list[str]:
    """Enumerate every mDNS service type being advertised locally."""
    zc_mod = _require_zeroconf()
    try:
        from zeroconf import ZeroconfServiceTypes
    except ImportError as exc:  # pragma: no cover
        raise DiscoveryError("this zeroconf build cannot enumerate types") from exc
    return sorted(ZeroconfServiceTypes.find(timeout=timeout))


def scan(
    timeout: float = 5.0,
    *,
    hint: str = NAME_HINT,
    interfaces: Iterable[str] | None = None,
) -> list[DiscoveredService]:
    """Enumerate every service type, then browse them all.

    Slower than :func:`browse` but makes no assumption about the advertised
    type. Anything matching ``hint`` sorts first.
    """
    types = list_service_types(timeout=timeout)
    if not types:
        return []
    services = browse(timeout, service_type=types, interfaces=interfaces)
    hint = hint.lower()
    return sorted(
        services,
        key=lambda s: (hint not in f"{s.name} {s.server}".lower(), s.name),
    )
