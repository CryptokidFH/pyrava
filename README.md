# pyrava

A Python client for Barava smart devices, built from the vendor's `network.md`
and `animation.md`, then corrected against firmware 1.0.1 on real hardware.

```bash
pip install pyrava              # core client, zero dependencies
pip install "pyrava[all]"       # + mDNS discovery and faster HTTP
```

The core client talks HTTP through the standard library, so a plain install
pulls in nothing. Two optional extras:

| Extra | Brings | Needed for |
| --- | --- | --- |
| `zeroconf` | `discovery` | `discover_devices()`; not needed if you connect by IP |
| `requests` | `fast` | connection reuse, lower latency at short ping intervals |

Requires Python 3.8+.

## Command line

```bash
pyrava discover                       # list devices via mDNS
pyrava info --host 192.168.1.249      # dump device state
pyrava heater --host 192.168.1.249    # temperatures, scaled
pyrava set --host 192.168.1.249 --power on --fill-color '#FF8000'
pyrava raw --host 192.168.1.249 DVIFO # send any handler, see the raw frames
pyrava watch --host 192.168.1.249     # live status, redrawn in place
pyrava doctor                         # diagnose mDNS when discovery is empty
```

Add `-v` to log every frame in both directions, or `--json` for
machine-readable output.

`pyrava watch` redraws a fixed status block in place rather than scrolling —
device state, light state, and heater temperatures, refreshed as the device
answers. Data appears from the second tick onward (replies are deferred by
one ping; see below). `--json` switches to one JSON object per line instead,
since a redrawn screen isn't machine-readable. Output that isn't a real
terminal (piped to a file, an unsupported console) falls back to plain
scrolling automatically.

## Quick start

```python
from pyrava import discover_devices

devices = discover_devices(timeout=5)      # mDNS, then DVIFO on each hit
light = devices[0]

print(light.device_id, light.name)   # from the response key block
print(light.groups)                  # {'545d577b': 'My-Room'}
light.set_state(True)
light.set_fill_color((255, 40, 0))      # RGB tuple -> hue, see below
light.set_fill_brightness(180)
```

Already know the address:

```python
from pyrava import BaravaDevice

light = BaravaDevice("192.168.1.42")
light.register()                # DVIFO; populates light.device_id
print(light.get_device_info())
```

## How the connection actually works

Everything goes to one endpoint:

```
POST http://{device_ip}:8080/barava-host-post
```

Real HTTP headers, packet as a raw body. That's the default; you shouldn't
need to configure it.

Discovery is the slow step, which is why the vendor app sits on a loading
indicator: nothing can be transmitted or synced until the mDNS query resolves.

1. Browse `_barava._tcp.local.` for device addresses.
2. Ping a resolved address with your **own** session ID in the `device-id`
   header and ask for `DVIFO`. That registers you in the device's queue and
   returns the device's real ID in `DVID`.
3. Keep pinging at your declared `ping-interval`. The device never initiates —
   it parks responses in a per-sender queue and flushes them, batched, on your
   next ping. Miss your interval and it unregisters you.

### Replies arrive late

This is not a matched request/response protocol. A command's reply lands in
your session queue and comes back on a *later* ping, so the HTTP response to
one command frequently carries the answer to an earlier one.

`request()` handles this: it sends the command, then fires bare queue-pop pings
until a packet tagged with your handler comes back.

```python
light.request(Handler.DEVICE_INFO)                 # polls for the DVIFO reply
light.request(Handler.SET_FILL_COLOR, {...}, wait=False)   # setter, no reply
light.ping()                                       # pop the queue, send nothing
```

A plain ping sends no `body-handler` header and an empty body. Setters use
`wait=False` already, so they cost one round trip.

Because a single response can hold replies to several commands, filter before
reading:

```python
batch = light.request(Handler.GET_HEATER_INFO)
batch.handlers                      # ['DVIFO', 'HTIFO']
batch.flatten(Handler.GET_HEATER_INFO)   # only the heater packet's variables
batch.first(Handler.GET_HEATER_INFO)     # the packet itself
```

`sender_id` identifies *your script*, not the device, and can be anything
stable for the session. `new_sender_id()` generates one; the vendor app makes a
fresh one at every launch, which is why the IDs you captured kept changing.

```python
light = BaravaDevice("192.168.1.42", sender_id="my-script", ping_interval=200)
```

Shorter intervals mean faster updates (worth it for heater telemetry) at the
cost of more traffic.

### Never use `*` as a sender ID

The library raises on it. A wildcard in the sender block tells every listening
device that the keep-alive interval is being set, and sending it fires a
network interrupt to extend the keep-alive signal. Use a concrete ID.

## Commands

```python
light.set_state(True)                   # DVSTT
light.set_fill_color((255, 40, 0))      # COLFL -- RGB, converted to hue
light.set_fill_hue(240)                 # COLFL -- raw hue, 0-360, no conversion
light.set_fill_state(True)              # COLST
light.set_fill_brightness(180)          # COLBR
light.set_desk_color(292)               # DSKCL -- colour temp, not RGB
light.set_desk_brightness(140)          # DSKBR
light.set_desk_state(True)              # DSKST
light.set_heater_state(True)            # HTSTT
light.set_device_name("Desk Lamp")      # DVSNM
light.add_group("office", "g7")         # DVAGR
light.remove_group("g7")                # DVRGR
light.wipe_groups()                     # WIPGR
light.set_spectrum(True, [255,0,0, 0,255,0])   # SSPEC
light.get_heater_info()                 # HTIFO
light.read_temperature_f()              # HTCT / 100 -> 69.3 (Fahrenheit)
light.read_temperatures_f()             # current + target, one exchange
light.heater_status()                   # HTHH -> "OK" / "Fault" / "Fault (code N)"
light.is_heater_ok()                    # HTHH == 0 -> True / False / None
light.check_for_updates()               # CHUPD
light.wipe_device()                     # WPDVI -- factory reset
```

Anything not wrapped is one call away:

```python
from pyrava import Handler, Var
light.request(Handler.SET_FILL_COLOR, {Var.FILL_COLOR: 0xFF0000})
```

## Batching

A batch is **many packets aimed at one device**, delivered as one request — not
one packet fanned out to many devices. One round trip instead of three.

The batch key block is the **target device's** ID, so `register()` (or any
prior request) must have run first — that's where the ID comes from. Your own
session ID stays in the `device-id` HTTP header.

```python
from pyrava import SubPacket, Handler, Var

light.send_batch([
    SubPacket(Handler.SET_DESK_STATE, {Var.DESK_STATE: 1}),
    SubPacket(Handler.SET_DESK_BRIGHTNESS, {Var.DESK_BRIGHTNESS: 200}),
    SubPacket(Handler.SET_FILL_COLOR, {Var.FILL_COLOR: 0x00FF80}),
])
```

The target device ID never goes on the wire outside the packet headers; only
batches carry an inline ID, in the sender block.

## Animations

Scripts compile to bytecode, hex-encode, and go out via `CMPAM`. The builder
tracks scopes so unclosed blocks and out-of-range zones fail at compile time
rather than on the device.

```python
from pyrava import AnimationScript, disassemble

script = AnimationScript()

with script.header(0):                    # runs atomically, one tick
    script.select_zones(0, 3)
    script.gradient((0, 255, 0, 0), (255, 0, 0, 255), smooth=True)
    script.deselect_all()                 # don't leak selection to other threads

with script.thread(0):                    # one command per tick
    with script.main():                   # repeats forever
        with script.atomic():             # ...unless wrapped like this
            script.select_zones(0, 3)
            script.rotate_left(5)
            script.deselect_zones(0, 3)

light.upload_animation(script)
print("\n".join(disassemble(script.compile())))
```

`loop(n)` gives a finite repeat. Header and thread indices must match or the
two halves fall out of sync — the compiler warns when they don't. Board
Revision 1 has 5 zones, so 5 threads maximum, and the engine targets 41 Hz.

Transforms are destructive: applying the inverse will not restore a buffer.

## The cyclical model

The vendor's design notes (`barava_network_impl.md`) describe the interface as
a **cycle**, not a request/response API: a keepalive loop runs continuously in
both directions, device state updates ride on it, and your commands are
appended to the pending keepalive packet rather than sent as separate
exchanges. `poll()` is that cycle, and it's the intended primary mode:

```python
session = light.poll(keepalive=[Handler.DEVICE_INFO, Handler.GET_HEATER_INFO])
session.on(Handler.GET_HEATER_INFO, update_my_ui)
session.enqueue(Handler.SET_FILL_COLOR, {Var.FILL_COLOR: 240}, urgent=True)
```

Commands come in two priorities, matching the notes:

| | Goes out | Use for |
| --- | --- | --- |
| `enqueue(...)` | next tick, merged into the keepalive | passive updates |
| `enqueue(..., urgent=True)` | immediately, still merged | colour, privilege, anything user-visible |

One-shot calls like `light.set_fill_hue(240)` still work and are fine for
scripts. When a session is running they're serialised against it, so they
won't interleave — but they are a separate exchange rather than a merged one,
so prefer `enqueue(urgent=True)` inside a running cycle.

### Timing

The notes give a hard floor of **500ms** between keepalives "for maximum
network and device stability", with 800–1000ms preferred and 500ms reserved
for data polling like heater temperature. `pyrava` enforces that:

* `DEFAULT_PING_INTERVAL_MS` is 1000.
* `poll(interval=...)` is clamped to 500ms, with a warning.
* Firmware 1.0.1 advertises **200ms** in its response key block, below the
  floor. `follow_device_interval` records it as `reported_ping_interval` but
  clamps the value actually used, rather than pinging 2.5x faster than the
  vendor says is safe.

## Receiving events

```python
session = light.poll(interval=0.2, keepalive=[Handler.DEVICE_INFO, Handler.GET_HEATER_INFO])
session.on(Handler.GET_HEATER_INFO, lambda p: print(p.get(Var.HEATER_CUR_TEMP)))
session.enqueue(Handler.SET_FILL_COLOR, {Var.FILL_COLOR: 0xFF0000})
...
session.stop()
```

`keepalive` controls what goes out each tick when nothing else is queued:

* `None` (default) — a bare ping. Pops whatever's already queued but
  requests nothing new; your callbacks only fire for replies to commands
  sent some other way.
* a single handler — re-sent every tick.
* **a list of handlers — all re-sent together as one batch every tick.**
  This is what you want for a general watch loop with several
  `session.on(...)` subscriptions; a bare ping alone never triggers a reply,
  since the device only answers requests it's actually received.

Replies are deferred by one ping (see above), so a handler passed here starts
showing up in callbacks on the *second* tick, not the first.

Queued commands (`session.enqueue(...)`) ride out on the next tick alongside
any keepalive handlers, and everything the device returns is dispatched by
handler.

Callbacks may return a reply, which the notes describe as the normal shape of
a mirrored handler ("callbacks ... should always produce a client response").
Return `None` for no reply, or a `SubPacket` (or several) to queue one:

```python
session.on(Handler.DEVICE_INFO, lambda p: SubPacket(Handler.GET_HEATER_INFO, {}))
```

## Privilege

Levels are `USER`, `BETA`, `ALPHA`, `DEV`, set with a `BARAVA-...` key via
`DVSTP` and persistent across sessions. An invalid key immediately revokes
elevated privileges and forces the device to user mode, so don't call this
speculatively:

```python
import os
light.set_device_type(os.environ["BARAVA_KEY"])
print(light.privilege)
```

The `dev-key` header is deprecated in favour of this.

## When discovery finds nothing

The `_barava._tcp.local.` service type comes from the docs and has never been
confirmed against hardware, so `discover_devices()` tries several candidate
types and then falls back to enumerating every type on the network, keeping
anything whose name looks like a Barava device.

If it still comes back empty, find out why:

```bash
python examples/mdns_debug.py
```

That reports your local adapters, every service type answering on the network,
each candidate type tried individually, and every service it can resolve.

Two common outcomes:

**Nothing answers mDNS at all.** The cause is local, not in this library — a
firewall blocking inbound UDP 5353 for `python.exe`, a VPN or hypervisor
adapter capturing multicast, or client isolation on the access point. On a
multi-homed machine, pin the right adapter:

```python
discover_devices(interfaces=["192.168.1.50"])   # your LAN address
```

**The device shows up under a different type.** Pass it directly:

```python
discover_devices(service_type="_http._tcp.local.")
```

Either way, discovery is only a convenience. Connecting by IP needs no mDNS
and behaves identically:

```python
light = BaravaDevice("192.168.1.249")
light.register()
```

## Working with packets directly

```python
from pyrava import encode_body, parse_body, parse_batch, HexInt

encode_body({"SPLT": [255, 0, 0], "SPON": 1})   # '<SPLT=!{255,0,0}><SPON=!1>'
parse_body("<HEX_ARRAY=${00,FF,0F}>")           # {'HEX_ARRAY': [0, 255, 15]}
encode_body({"FCLR": HexInt(0xFF00FF)})         # '<FCLR=$FF00FF>'
```

Hex values decode to decimal on the way in, as the spec requires. `HexInt` is
only about formatting on the way out — the device treats `$FF` and `!255`
identically.

The format has no escaping, so string values containing `< > = { } , ! & $`
are unrepresentable and raise `EncodeError` rather than silently corrupting the
frame.

## Unit correction (0.1.x)

`HTCT`/`HTST` are **Fahrenheit**, not Celsius. An earlier version of
this README and library assumed Celsius and just relabeled the raw
value. `read_temperature_c()` / `read_temperatures_c()` /
`read_target_temperature_c()` still work but now do a real F→C
conversion and raise `DeprecationWarning`; switch to the `_f` methods:

```python
light.read_temperature_f()        # was read_temperature_c()
light.read_temperatures_f()       # was read_temperatures_c()
light.read_target_temperature_f() # was read_target_temperature_c()
```

## Fill colour correction (0.1.x)

`FCLR` is **not** a packed `0xRRGGBB` int, which earlier versions assumed.
Testing against real hardware: `rgb(0, 0, 255)` (== 255) showed purple,
`rgb(0, 80, 255)` (== 20735) showed red, and `rgb(0, 0, 128)` (== 128)
showed green. A packed-RGB read doesn't explain any of that. What does: every
value *inside* roughly 0–360 produced a distinct, plausible hue (128 →
green, 255 → violet, 360 → magenta near the wraparound seam), while every
value *outside* that range came back red regardless of size — consistent
with an out-of-range fallback, not a colour.

So `FCLR` is treated as a **hue, 0–360**, with brightness handled separately
by `FBRT` as it already was:

```python
light.set_fill_hue(240)             # raw hue, no conversion
light.set_fill_color((0, 0, 255))   # RGB -> hue via rgb_to_hue(); saturation/lightness are lost
```

**This rotation is inferred from six data points, not confirmed against
firmware source.** `examples/fill_color_probe.py` sweeps the range and asks
what you see at each value, if you want to verify or correct it:

```bash
python examples/fill_color_probe.py 192.168.1.249
```

`set_fill_color()` no longer accepts a bare packed int — there's no
non-ambiguous way to tell "a packed RGB int" from "a raw hue" apart, so
passing a plain number is now treated as the hue directly. If you had code
calling `light.set_fill_color(0xFF0000)`, change it to a tuple:
`light.set_fill_color((255, 0, 0))`.

## Confirmed against hardware

| Detail | Value |
| --- | --- |
| Endpoint | `POST http://{ip}:8080/barava-host-post` |
| Request shape | Real HTTP headers, raw body — not a JSON envelope |
| `device-id` outbound | Your session ID, not the device's |
| Session ID shape | `~` plus 12 uppercase hex, regenerated per run |
| Response timing | Deferred to a later ping via the session queue |
| Response key block | The **device's** ID and interval: `<<68b6b33d3b38=&200>=&{...}>` |
| Batch array marker | String-marked (`&{...}`), not bare |
| Batch key | The target device's ID, not your session ID |
| `DVID` | Not sent — firmware 1.0.1 omits it from `DVIFO` |
| `DVGR` | Array of blocks: `&{<545d577b=&My-Room>}` |
| `DCLR` | Colour temperature, not packed RGB |
| `HTCT` scaling | Hundredths of a degree **Fahrenheit** (confirmed; not Celsius as first assumed) |
| `HTHH` | Boolean heater fault flag. `0` confirmed against the app's "Heater Status: Ok" readout; nonzero → fault is inferred, not yet observed. Use `heater_status()` / `is_heater_ok()` rather than the raw int |
| `FCLR` | Hue, roughly 0–360, not packed RGB. See "Fill colour correction" above — rotation inferred from six data points, values outside 0–360 fall back to red |

## Assumptions still worth verifying

Each guess below is isolated and easy to flip.

| Area | Assumption | Change it with |
| --- | --- | --- |
| Sub-packet separator | Comma. The Sub Parsing section uses one; the group/desk-light example doesn't. The parser accepts both | `encode_batch(..., separator="")` |
| Loop counter U16 | Big-endian; the spec doesn't say | `U16_BYTEORDER` in `animation.py` |
| `FCLR` rotation/zero-point | Standard HSL-ish (0=red, 120=green, 240=blue); only spot-checked, not independently verified | `examples/fill_color_probe.py`, then `rgb_to_hue()` in `client.py` |
| `DDSN` `DPIN` `HTVR` `CHRV` `DPEN` | Real labels seen in `DVIFO`, absent from the spec. Names in `Var` are inferred from their values | `const.py` |
| L2 param widths | 1 byte each; that column is blank in the table | `SPEC` in `animation.py` |
| HTTP method/path | `POST /` | `HttpTransport(..., method=..., path=...)` |

Turn on `logging.getLogger("pyrava").setLevel(logging.DEBUG)` to see every
frame in both directions — that's the fastest way to settle these against a
real device.

## Tests

```bash
python -m pytest tests/ -q
```

50 tests, pinned to the worked examples in both spec documents: every
key/value, array, and batch sample decodes to exactly the JSON the docs
show, and the practical gradient-rotate script compiles to the expected bytes.
A mock firmware that defers every reply by one ping covers the queue logic,
every plausible response shape is parsed in a parametrised test, and a real
captured firmware 1.0.1 frame is decoded field by field.

## When a frame won't parse

Responses decode leniently: an unrecognised frame yields an empty batch with
the original text on `.raw` rather than raising, so one odd response can't kill
a polling loop. Pass `strict=True` to `parse_response` when you'd rather see
the error.

To see exactly what your device sends:

```bash
python examples/dump_frames.py 192.168.1.249
```

Or inline, after any call:

```python
light.get_device_info()
print(light.last_request)   # (headers, body) that went out
print(light.last_raw)       # exact text that came back
```

## Layout

```
pyrava/
  const.py       handlers, variables, headers, privilege levels
  packet.py      the wire codec: blocks, arrays, batches
  discovery.py   mDNS browsing
  transport.py   HTTP, raw or JSON-enveloped
  client.py      BaravaDevice, PollingSession, discover_devices
  animation.py   bytecode compiler, builder, disassembler
  errors.py      exception hierarchy
  __main__.py    the `pyrava` command-line interface
```

## Releasing

```bash
python -m pytest tests/ -q
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

Version lives in one place, `pyrava/__init__.py`; `pyproject.toml` reads it
via `dynamic = ["version"]`.
