# Changelog

## 0.4.3 (unreleased)

### Added

- `punch_color(rgb)`: optional saturation boost for muted source colours
  (e.g. from screen capture). Hue and brightness are left alone by default;
  `min_value=` optionally floors brightness.

### Notes

- A fourth gradient capture -- a theme the app describes as a single "dimmed
  blue" -- decoded to three fully saturated stops at ~20-25% brightness and
  recompiles byte-exactly. This confirms two things: gradient stops really
  are plain RGB, and **dark colours are fully representable**. An earlier
  note in 0.4.2 suggested those near-black-looking captured values were
  suspicious; they were not, and that reading was wrong.
- Consequently, washed-out output tracks low *saturation*, not low
  brightness. `punch_color()` therefore does not floor brightness by
  default, which would have brightened away exactly the dim-but-saturated
  colours the device renders well.

## 0.4.2 (unreleased)

### Added

- `punch_color(rgb)`: optional caller-side saturation boost + brightness
  floor, for when muted source colours (e.g. from screen capture) render
  palely on the diffused ring LEDs. Not applied automatically anywhere.

### Notes

- Investigated a report of gradients looking washed out. Hardware probing
  confirmed the gradient path is correct: stops are literal RGB and render
  faithfully (a grey ramp renders as grey, which rules out any hue/HSV
  misread). The paleness was faithful rendering of genuinely muted source
  colours, not a decode bug. `punch_color()` is the opt-in remedy.

## 0.4.1 (unreleased)

Device-safety fixes prompted by the firmware author's notes, after a rapid
sequence of zone uploads wedged a device mid-run.

### Fixed

- `examples/zone_probe.py` uploaded a full animation per zone with no pacing,
  which could hang the device partway and then time out in cleanup. It now
  relies on the new upload throttle, handles a mid-run transport failure per
  zone instead of aborting, and only attempts cleanup if something was lit.

### Added

- Animation uploads are throttled: `upload_animation()` and everything built
  on it space consecutive uploads at least `min_animation_gap` seconds apart
  (default 1s; set to 0 to disable). The firmware author warns that loading a
  compiled animation forces heavy context switching and that flooding the
  device can wedge it.
- The animation compiler refuses a zero rotation amount (`rotate_left(0)`
  etc.), reported by the firmware author to deadlock the animation engine.

### Changed

- The sub-floor ping-interval warning no longer asserts the device
  "advertises" 200ms as fact. The 200 is the second field of the response key
  block, which we interpret as the ping interval per `network.md`; the
  warning now says so and points to `follow_device_interval=False` if that
  reading is wrong. (The value's meaning is an open question for the vendor.)

## 0.4.0 (unreleased)

Decoded a fourth captured payload -- a single-zone gradient -- confirming
`FILL_ZONE`/`BUILD_GRADIENT` work identically whether scoped to one zone or
several, and revealing the stop-spacing convention.

### Added

- `generate_gradient_stops(colors)`: turns a flat list of RGB colours into
  positioned `(pos, r, g, b)` stops using `i * (256 // N)` spacing --
  confirmed against two independently captured gradients that both landed
  on exactly 0, 85, 170 for three stops, consistent with each zone being a
  physical ring (this spacing divides the ring evenly including the
  wraparound seam, rather than compressing into 0-255 and leaving one odd
  gap). Only independently confirmed at N=3.
- `set_gradient(colors, zones=...)`: builds and applies a generated gradient
  to one zone, several, a named group, or every zone (the default) in one
  call. The natural hook for a generated palette, e.g. k-means cluster
  centres from screen colours -- it's just a flat RGB list, no positions to
  work out.
- `ZONE_GROUPS`: named zone aliases -- `lava_lamp`, `downlamp`, `top_ooze`,
  `bottom_ooze`, `fluid` -- matching the lamp's own product theming. A group
  name now works anywhere a zone does, in `build_theme()`, `set_zone_colors()`,
  `set_zone_gradient()`, and `set_gradient()`. Groups may overlap; the later
  entry wins for any zone touched twice.
- `set_zone_state(key, color)` / `clear_zone(key)`: change or clear one zone
  or group while replaying every other zone as this session last set it, a
  client-side workaround for there being no per-zone update or state
  readback on the wire. `known_zone_colors` / `known_zone_gradients` expose
  the underlying shadow. Documented limitation: this only reflects what
  *this* `BaravaDevice` instance has itself sent -- it cannot see changes
  made by the app or another client.

## 0.3.1 (unreleased)

### Added

- `Zone` enum: `TOP`, `MIDDLE_INNER`, `MIDDLE_OUTER`, `BOTTOM_INNER`,
  `BOTTOM_OUTER`, confirmed by lighting each ring in turn on real hardware
  (`4, 2, 3, 0, 1` respectively). `ZONE_ORDER` now built from these names.
  `Zone` is an `IntEnum`, so existing code passing raw indices is unaffected.
- `examples/zone_probe.py` repurposed from discovery to verification: it now
  re-lights each named zone in sequence as a quick sanity check.

## 0.3.0 (unreleased)

Decoded three `CMPAM`/`ANDT` theme payloads captured from the device's own
app. All three now recompile byte-for-byte, which pins down several things
`animation.md` got wrong or left vague.

### Added

- `set_zone_colors()`, `set_zone_gradient()`, `clear_zones()` and
  `build_theme()` on `BaravaDevice`: per-zone RGB for the five addressable
  LED zones. These take real RGB, unlike the single-hue `FCLR` path.
- `ZONE_ORDER` = `(4, 2, 3, 0, 1)`, the order the app selects zones in a
  theme header. Five zones confirmed, matching the hardware's top ring,
  middle inner/outer and bottom inner/outer.
- `examples/zone_probe.py` lights one zone at a time so the indices can be
  mapped to physical rings.

### Fixed (breaking, animation bytecode)

- `FILL_ZONE` (0x11) takes **no** operands, not `(r, g, b)`. It opens a
  gradient build. `fill_zone()` lost its parameters.
- `BUILD_GRADIENT` (0x12) takes `(pos, r, g, b)` and is emitted once per
  stop, rather than taking none. `build_gradient()` gained parameters.
  `animation.md`'s worked example uses `APPEND_GRADIENT` (0x13) for this;
  the hardware does not. `append_gradient()` is kept but its role is now
  unconfirmed.
- `compile()` no longer prefixes `SCRIPT_HEADER` (0x00); captured themes
  start at `OPEN_HEADER`. Pass `script_header=True` for the old behaviour.
- `gradient()` now emits `FILL_ZONE, BUILD_GRADIENT*, MAP_*`, matching the app.

## 0.2.0 (unreleased)

Aligns the client with the vendor's `barava_network_impl.md`, which describes
a cyclical keepalive interface rather than the request/response model the
original `network.md` implied.

### Fixed

- **Polling was 2.5x faster than the vendor allows.** The notes set a hard
  500ms floor "for maximum network and device stability", but firmware 1.0.1
  advertises 200ms in its key block and `follow_device_interval` adopted that
  verbatim. The advertised value is still recorded as
  `reported_ping_interval`, but the value actually used is clamped to
  `MIN_PING_INTERVAL_MS`, with a one-time warning. `poll(interval=...)` is
  clamped too (`enforce_interval_floor=False` opts out, for mocks).
- `DEFAULT_PING_INTERVAL_MS` is now 1000 (was 2000); the notes recommend
  800-1000ms for general use. `MAX_POLL_DELAY` raised from 250ms to 500ms for
  the same reason.
- Transport access is serialised behind a lock. A running `PollingSession`
  pings on a background thread, so a direct setter call from your own thread
  could previously interleave two exchanges on one session.

### Added

- `enqueue(..., urgent=True)` implements the notes' two-tier response
  priority: passive commands ride the next keepalive, urgent ones are merged
  into the queued packet and force it out immediately. `PollingSession.stop()`
  is now prompt regardless of interval as a side effect.
- Handler callbacks may return a `SubPacket` (or an iterable of them) to be
  queued as a response, matching the mirrored-callback design. Returning
  `None` stays the no-reply default.

## 0.1.5 (unreleased)

### Fixed (breaking)

- `FCLR` is not a packed `0xRRGGBB` int. Testing against real hardware:
  `rgb(0,0,255)` (255) showed purple, `rgb(0,80,255)` (20735) showed red,
  `rgb(0,0,128)` (128) showed green — a packed-RGB read explains none of
  that. Values inside roughly 0-360 gave distinct plausible hues; values
  outside came back red regardless of magnitude, consistent with an
  out-of-range fallback. `FCLR` is now treated as a hue, 0-360.
  - New `BaravaDevice.set_fill_hue(degrees)` sends a raw hue with no
    conversion.
  - `set_fill_color()` now accepts an `(r, g, b)` tuple (converted to hue
    via the new `rgb_to_hue()`, saturation/lightness are lost) or a bare
    number (sent directly as hue). **It no longer accepts a packed int** —
    `light.set_fill_color(0xFF0000)` must become
    `light.set_fill_color((255, 0, 0))`.
  - CLI: `pyrava set --fill-color` now converts through hue; added
    `--fill-hue` for a raw, unconverted value.
  - The hue rotation itself (0=red, 120=green, 240=blue) is inferred from
    six data points, not confirmed against firmware source. New
    `examples/fill_color_probe.py` sweeps the range to verify or correct it.
  - `rgb()` is unchanged but no longer used for anything -- no currently
    confirmed field is known to use packed RGB.

## 0.1.4 (unreleased)

### Added

- `HTHH` (heater health) is now interpreted rather than left as a raw int.
  New `HeaterHealth` enum, `describe_heater_health()` helper, and
  `BaravaDevice.heater_status()` / `.is_heater_ok()`. Confirmed against the
  app: `0` shows there as "Heater Status: Ok". The fault side is inferred —
  only `0` has actually been observed — so `describe_heater_health()`
  degrades to `"Fault (code N)"` for any nonzero value rather than assuming
  `1` is the only fault code. `pyrava heater` and `pyrava watch` show the
  interpreted status instead of the raw number.

## 0.1.3 (unreleased)

### Changed

- `pyrava watch` now redraws a fixed status block (device, lights, heater,
  last-updated time) in place instead of printing a new line per event.
  Falls back to plain scrolling automatically when stdout isn't a real
  terminal (piped output, an unsupported console), and `--json` keeps the
  old one-object-per-line stream for scripting.

## 0.1.2 (unreleased)

### Fixed

- `pyrava watch` displayed nothing. Its polling loop sent bare queue-pop
  pings only — never an actual `DVIFO`/`HTIFO` request — and the device only
  replies to requests it's received, so the subscribed callbacks never had
  anything to fire on. `poll()`/`PollingSession` now accept `keepalive` as a
  list of handlers, batched and re-sent every tick; `watch` uses
  `keepalive=[Handler.DEVICE_INFO, Handler.GET_HEATER_INFO]`. Data now
  appears from the second tick onward (replies are still deferred by one
  ping). The single-handler and `None` forms of `keepalive` are unchanged.
- `send_batch()` logged its "batching before the device ID is known"
  warning on every call instead of once, which would have spammed a
  long-running `watch` session had it ever hit that path.

## 0.1.1 (unreleased)

### Fixed

- `HTCT`/`HTST` are Fahrenheit, not Celsius. `read_temperature_c()`,
  `read_temperatures_c()`, and `read_target_temperature_c()` previously
  returned the raw Fahrenheit value mislabeled as Celsius. They now
  convert correctly and are deprecated in favour of the new `_f`
  methods: `read_temperature_f()`, `read_temperatures_f()`,
  `read_target_temperature_f()`. The CLI's `heater` and `watch`
  commands print °F.

## 0.1.0

First release. Built from the vendor's `network.md` and `animation.md`, then
corrected against firmware 1.0.1 on real hardware.

### Protocol facts confirmed against hardware

- Single endpoint: `POST http://{ip}:8080/barava-host-post`, real HTTP headers
  with the packet as a raw body (not a JSON envelope).
- Responses are deferred. A command's reply lands in a per-session queue and
  comes back on a later ping, so `request()` polls until its handler appears.
- The response key block holds the *device's* ID and interval
  (`<<68b6b33d3b38=&200>=&{...}>`). `DVIFO` does not include `DVID`, making
  this the only source for the device ID.
- Outbound batches are keyed on the device ID and the sub-packet array is
  string-marked (`&{...}`).
- `DVGR` is an array of blocks (`&{<545d577b=&My-Room>}`), not strings.
- `DCLR` is a colour temperature, not packed RGB.
- `HTCT` / `HTST` are hundredths of a degree **Fahrenheit**.
- Session IDs are `~` plus 12 uppercase hex, regenerated per run.

### Undocumented variables observed

`DDSN`, `DPIN`, `HTVR`, `CHRV`, `DPEN` appear in `DVIFO` on firmware 1.0.1 but
are absent from the spec. Names in `Var` are inferred from their values.

### Still unverified

- `FCLR` colour layout (reads 0 on an idle device).
- Animation bytecode: U16 endianness for loop counters, L2 parameter widths.
- The `_barava._tcp.local.` service type; discovery tries several candidates
  and falls back to a full scan.
