"""Find out what mDNS can actually see from this machine.

    python examples/mdns_debug.py

Runs four checks and prints what each one finds. Paste the output if
discovery still comes up empty.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrava.discovery import (
    CANDIDATE_TYPES,
    browse,
    list_service_types,
    scan,
)

TIMEOUT = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0


def local_addresses() -> list[str]:
    """Every IPv4 address this host has, so we can spot extra adapters."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    # The address actually used to reach the LAN.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        addrs.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(addrs)


print("=" * 68)
print("1. Local network interfaces")
print("=" * 68)
addrs = local_addresses()
for addr in addrs:
    print(f"   {addr}")
if len(addrs) > 1:
    print("\n   More than one adapter. VPN and hypervisor adapters routinely")
    print("   swallow multicast. If discovery fails, pin the real one:")
    print(f'      discover_devices(interfaces=["{addrs[0]}"])')

print()
print("=" * 68)
print("2. Is anything answering mDNS at all?")
print("=" * 68)
try:
    types = list_service_types(timeout=TIMEOUT)
except Exception as exc:
    types = []
    print(f"   FAILED: {exc}")
if types:
    print(f"   {len(types)} service type(s) advertised:")
    for t in types:
        print(f"      {t}")
else:
    print("   Nothing. mDNS isn't reaching this machine at all.")
    print("   Usual causes: firewall blocking inbound UDP 5353 for python.exe,")
    print("   a VPN adapter, or AP client isolation. This is not a")
    print("   library problem -- no Python change will find the device.")

print()
print("=" * 68)
print("3. The service types we try by default")
print("=" * 68)
for candidate in CANDIDATE_TYPES:
    hits = browse(TIMEOUT / len(CANDIDATE_TYPES), service_type=candidate)
    mark = "FOUND" if hits else "  -  "
    print(f"   [{mark}] {candidate}")
    for service in hits:
        print(f"            {service}  id={service.device_id}")

print()
print("=" * 68)
print("4. Everything on the network, Barava-looking first")
print("=" * 68)
try:
    services = scan(TIMEOUT)
except Exception as exc:
    services = []
    print(f"   FAILED: {exc}")
if services:
    for service in services:
        flag = " <-- looks like Barava" if service.looks_like_barava else ""
        print(f"   {service}{flag}")
        print(f"        server={service.server!r} id={service.device_id}")
        if service.properties:
            print(f"        TXT={service.properties}")
else:
    print("   Nothing resolved.")

print()
print("=" * 68)
print("If the device appears above under a type we don't try, pass it:")
print('   discover_devices(service_type="_thattype._tcp.local.")')
print("Meanwhile, connecting by IP works and needs no mDNS:")
print('   BaravaDevice("192.168.1.249")')
print("=" * 68)
