"""Print the exact bytes going to and from a device.

Run this and paste the output when the parser disagrees with your firmware:

    python examples/dump_frames.py 192.168.1.249
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrava import BaravaDevice, Handler

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.249"
device = BaravaDevice(host)
print(f"endpoint   {device.transport.url}")
print(f"session id {device.sender_id}\n")

for label, call in [
    ("DVIFO", lambda: device.request(Handler.DEVICE_INFO, wait=False)),
    ("ping 1", device.ping),
    ("ping 2", device.ping),
    ("HTIFO", lambda: device.request(Handler.GET_HEATER_INFO, wait=False)),
    ("ping 3", device.ping),
]:
    batch = call()
    headers, body = device.last_request
    print(f"--- {label} ---")
    print(f"  sent     headers={headers}")
    print(f"           body={body!r}")
    print(f"  received {device.last_raw!r}")
    print(f"  parsed   sender={batch.sender_id!r} interval={batch.ping_interval} "
          f"handlers={batch.handlers}")
    for packet in batch:
        print(f"           {packet.handler}: {packet.body}")
    print()
