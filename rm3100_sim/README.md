# The RM3100 simulator (Part B)

A simulated RM3100 that the node app (Part A) drives exactly as it drives the
real chip. The simulation is at the **register level**: the app's driver
writes POLL, polls STATUS for DRDY, burst-reads the result registers, runs the
self test, and the simulator answers each I2C transfer the way the chip would,
with the measurement registers filled from a physical model of the field at a
site. Pointing the app at it is one variable, `MAGNETOMETER_BUS=tcp://host:9100`
instead of `/dev/i2c-1`. The app's `tcp://` transport
(`retina_magnetometer/rm3100/remote.py`) carries each I2C transfer here, and
nothing above that transport knows the difference: the driver and the sampler
see the same transfers and the same failures as on `/dev/i2c-1`. A test
enforces that the app never imports this package.

```bash
python -m rm3100_sim serve --scenario demo            # the chip on :9100
python -m rm3100_sim generate --scenario uap-flyby --start 2026-09-30T12:00Z --duration 1h > day.csv
python -m rm3100_sim backfill --scenario demo --days 7 --data-dir ./data
python -m rm3100_sim describe uap-flyby               # a scenario, fully resolved
python -m rm3100_sim list                             # the built-in scenarios
```

`serve`, `generate` and `describe` take `--scenario` (a built-in name or a
YAML path), `--seed` and `--start`, which override the file. The same
scenario, seed and start always give the same data: `generate` output is
identical byte for byte, and the live server's value at a given instant is
identical in every run.

`backfill` takes `--scenario` and `--seed` and places the history itself: it
ends where the database's history begins, or now in an empty database. That is
the oldest sample, or the oldest minute summary if those reach back further,
as they do on a node older than its raw retention (seven days of samples, a
year of minutes). So it can run while the app samples, or long after, and
never writes a sample or a summary over anything the app recorded. Where the
history meets a minute summary it stops at that minute's start, which can
leave up to a minute without samples at the seam. Its session is stamped
where its history starts, so the app's own session stays the newest.

Arguments are checked before anything is written: the cycle count
must be one the app accepts (30 to 1000), the rate no faster than the chip
can measure all three axes at that cycle count (146.6 Hz at 200), days at
most the five years WMM2025 covers, the retention and size settings within
the app's own limits, and durations and speeds positive (speeds up to
100,000). A bad one is a usage error and an unreadable scenario file a
scenario error, never a traceback or an empty file.

## The chip (`device.py`)

The register model implements what the manual (PNI UM16, V16.0) specifies:

- **POLL** starts a single measurement of the requested axes; DRDY rises after
  the conversion time for their cycle counts (6.8 ms for three axes at 200).
- **CMM** starts and stops continuous mode, which measures every TMRC interval,
  capped by the conversion time. Writing TMRC or *reading* CMM while it runs
  ends it, as the manual says; a driver that does either would find out here.
- **Cycle counts** (CCX/CCY/CCZ) set each axis's gain, conversion time and
  noise, and reset to 200 when the chip is power-cycled.
- **DRDY** is cleared by reading the results and by any register write (the
  HSHAKE DRC1 and DRC0 defaults). Reading results while DRDY is low returns the
  old values and sets NACK2.
- **Refused writes** are NACKed on the wire, as the manual says the chip does
  (UM16 §4.5.1): a POLL during continuous mode or a CMM write during a POLL
  (HSHAKE NACK1), and a write to an undefined register (NACK0), 0x0A aside
  (below). The transfer fails with errno 121, EREMOTEIO, which is what i2c-dev
  reports for a NACK.
- **BIST**, armed with STE and run by the next POLL, reports per-axis pass
  bits; a scenario can kill one axis's coil.
- **REVID** reads 0x22. Addresses other than the strapped one (0x20–0x23)
  NACK.

A measurement is the scenario's field at the middle of the conversion, rotated
into the sensor's frame, plus Gaussian noise, clipped at ±800 µT and quantised
with PNI's gain formula. The datasheet's noise (Table 3-1: 30, 20 and 15 nT at
50, 100 and 200 cycles) can only have been measured on the chip's output,
which is in counts, so it already includes the rounding: at 200 cycles one
count is 13.3 nT. The noise added before rounding is what is left once the
rounding's share (one count over √12) is taken out, so the values the
registers report have the table's spread at every cycle count; adding the
table's figure and then rounding would overshoot it by 3 % at 200 cycles and
15 % at 30. The noise for a conversion is a pure function of the seed, the
axis and the time, so it does not depend on how often or in what order the app
asks.

Faults from the scenario act on the transfers: NACKs; a disconnect, during
which every transfer NACKs and after which the chip is power-cycled, whether or
not a transfer arrived while it was away; DRDY that never rises; and a
brown-out, a power dip too short for any transfer to fail, which puts the
registers back to their defaults (cycle counts 200, continuous mode off) and
loses a conversion under way. Nothing on the bus shows a brown-out: only
reading the registers back, or noticing that DRDY has stopped rising in
continuous mode, can.

Where the manual is silent, or its letter would break what works in the
field, the model picks a reading. The one the app depends on is in the
[hardware checklist](../docs/hardware-verification.md): DRC0 clears DRDY when
a write's first data byte arrives, not as its register address arrives, as the
manual's text has it (R07 §5.6.2, p.36). A pointer-only write, the first half
of every register read, therefore leaves DRDY alone; otherwise STATUS polling,
which every maintained driver relies on, could never see it rise.

Nothing in the app depends on the rest:

- In a multi-byte write, bytes before a refused one have taken effect. The
  manual says only that the chip NACKs, and that the address increments after
  each byte.
- A write to a read-only register is refused like one to an undefined
  register.
- Register 0x0A is accepted although Table 5-1 leaves it out. HamSCI's
  rm3100-runMag writes it as an undocumented "NOS" register, without checking
  whether the chip acknowledged it, so the model stores the value and ignores
  it rather than refuse software that uses it. What the chip does with it is
  unknown.
- Nothing clears a NACK bit but a power cycle, and NACK0 is already set in the
  reset value the manual gives (0x1B), so only the wire shows that a write was
  refused.
- A cycle count of 0, which the register accepts, counts nothing and reads 0.

### The wire protocol (`server.py`)

One JSON object per line each way, over TCP:

```
-> {"op": "transfer", "address": 32, "write": "24", "read": 9}
<- {"ok": true, "read": "00a10f..."}
<- {"ok": false, "errno": 121, "error": "no acknowledge from 0x20 (injected)"}
```

`write` and `read` are hex; a failure carries the errno a Linux i2c-dev adapter
returns (121, EREMOTEIO, for a NACK), which the app's client raises as
`OSError`. The errnos are Linux's numbers whatever the server runs on: EPROTO
is 71, as on Linux, even on macOS, where it is 100. `{"op": "hello"}` names
the server and scenario. It is readable with `nc`, on purpose. A request line
longer than 4096 bytes gets a single EPROTO reply, so the replies stay in step
with the requests, and a fault inside the model comes back as EIO (5) without
closing the connection.

## The field (`physics.py`)

North/east/down (NED), in nanotesla, summed from independent sources:

| Source | Model | Size at Greenville, SC |
| --- | --- | --- |
| Main field | **WMM2025** at the site's latitude, longitude and altitude, evaluated hourly and interpolated (pygeomag, which ships NOAA's coefficient file unchanged and reproduces all 100 official test values) | X 22,398, Y −2,761, Z 43,002 nT; \|B\| 48,564 nT; dip 62.3°, declination −7.0° |
| Crust | A fixed offset per scenario: the WMM describes the core field only, and local crust moves a real site by tens to ~100 nT | 42, −18, 65 nT in the built-ins |
| Daily variation | The solar-quiet (Sq) variation: four solar harmonics (24, 12, 8, 6 h) in local time, fitted to quiet days at the INTERMAGNET observatories either side of Greenville (FRD and BSL), for solar minimum and maximum and three seasons, blended by date and by F10.7. Each local day gets its own amplitude factor and phase shift, interpolated between local noons | 20–90 nT peak to peak, largest in Y, X lowest near local noon |
| Storms | A sudden commencement, a main phase pulling X towards 0.72·Dst, an exponential recovery, and Pc4–Pc5 pulsations (45–600 s) on the main phase | as configured; `storm` uses Dst −250 nT |
| Local steps | A fixed offset for a while, ramped in and out: a car parked by the mast | tens of nT |
| UAP passes | **Magnetic dipoles** on straight, level tracks: B = (μ0/4π)[3(m·r̂)r̂ − m]/r³ | 200 nT at 1 km on axis, 1.6 nT at 5 km, for 1e9 A·m² |

The sensor then sees that field through its mounting (yaw, pitch, roll from
north/east/down), a hard-iron offset and per-axis gain errors, all set by the
scenario.

A UAP is a magnetic dipole of 1e9 A·m², its field falling as 1/r³.
Aircraft and drones are left out, their moments being far smaller. A pass is
described by its geometry at closest approach, which is what decides what a
magnetometer sees: horizontal distance (to the right of track), altitude
above the sensor, speed, heading, and the dipole's direction. That direction is
drawn once per pass from the seed unless the scenario fixes it (`along_track`,
or a vector), and stays fixed for the pass. For speed, each pass and each
storm is evaluated only while it can contribute more than 0.001 nT.

What this model is not: the Sq fit is for about 35–40° N in North America and
is a plausible shape elsewhere, not a prediction; storms are shaped to stress
detectors, not to forecast; there is no induced (as opposed to permanent)
magnetisation, no temperature drift (the RM3100 has no temperature sensor and
its coil tempco is 0.4 %/°C), and no 1/f sensor noise.

## Scenario files

YAML, validated strictly: an unknown key or a wrong type is an error naming the
key path, because a typo that is silently ignored produces data that looks
right and is not. Every field except `site` is optional.

```yaml
name: my-scenario
description: One line for `list`
seed: 42                      # everything random follows from this
start: now                    # or an ISO 8601 time within 2025-2029: 2026-09-30T12:00:00Z
site:
  latitude: 34.85
  longitude: -82.39
  altitude_m: 300
field:
  crustal_offset_nt: [42.0, -18.0, 65.0]   # north, east, down
  diurnal:
    enabled: true
    f107: 120                 # solar activity: 70 quiet sun, 190 active
    variability: 0.25         # day-to-day amplitude spread, ±25 %
    phase_jitter_h: 1.0       # day-to-day timing spread
    scale: 1.0
sensor:
  address: 0x20               # 0x20..0x23
  mounting: {yaw_deg: 37, pitch_deg: 0, roll_deg: 180}   # roll 180 = upside down
  noise_scale: 1.0            # times the datasheet noise
  hard_iron_nt: [0, 0, 0]     # in the sensor's frame
  gain_error: [0.0, 0.0, 0.0] # fractional, per axis
  dead_axis: null             # x, y or z: that coil never oscillates
events:
  - type: uap_pass
    id: north-run             # optional: what this event's random numbers are keyed by
    at: +5m                   # relative to start, or ISO 8601
    every: 15m                # optional repeat; `count` bounds it (default: no end)
    closest_approach_m: 800   # horizontal, to the right of track
    altitude_m: 400           # above the sensor
    speed_mps: 120
    heading_deg: 90
    moment_am2: 1.0e+9        # YAML needs the sign in the exponent
    moment_direction: random  # or along_track, or [north, east, down]
  - type: storm
    at: +1h
    dst_min_nt: -150
    main_phase_h: 6
    recovery_h: 12
    commencement_nt: 25
    pulsation_nt: 6
  - type: step
    at: +20m
    duration: 30m
    delta_nt: [35, -12, 48]
    ramp_s: 20
faults:
  - type: nack                # each transfer NACKs with this probability
    at: +10m
    duration: 60s
    probability: 0.3
  - type: disconnect          # every transfer NACKs; the chip comes back power-cycled
    at: +4m
    duration: 30s
  - type: stuck_drdy          # conversions never raise DRDY
    at: +7m
    duration: 20s
  - type: brownout            # registers back to their defaults; no transfer fails
    at: +9m
```

Times are `+90s`, `+5m`, `+2h`, `+1d`, `+01:30:00` from the start, or absolute
ISO 8601. Durations take the same relative forms. A two-part time such as
`14:00` is refused as ambiguous (YAML would otherwise read it as the number
840, fourteen minutes). Numbers must be finite. The start must fall within
2025-2029, the years WMM2025 covers. Faults and steps must last longer than
zero; `probability` belongs to `nack` alone, and a brownout takes no
`duration`. Repeats must be at least 1 ms apart, finer than anything the chip
or the app can tell apart, and an event or fault that repeats so often that
more than 1,000 of its occurrences would be under way at once is refused as a
slip.

### Built-in scenarios

| Name | What it shows |
| --- | --- |
| `quiet-day` | The baseline: WMM2025, crust, the daily variation, datasheet noise |
| `uap-flyby` | Passes every 15 minutes at slant ranges of about 0.6, 1.5 and 3 km: well inside the sensor's reach, at its edge, and beyond it. Sensor upside down, turned 37° east of north |
| `storm` | An intense storm (Dst −250 nT) with pulsations, from ten minutes in |
| `faults` | Every 20 minutes: a minute of 30 % NACKs, a 30 s disconnect ending in a power cycle, 20 s of stuck DRDY, a brown-out that resets the chip without a failed transfer |
| `demo` | What `docker compose up` runs: UAP passes, a car parked for half an hour every two hours, a short NACK burst every half hour, the rotated mounting |

## Reproducibility

Nothing draws from a shared random generator. Every random number is a hash of
(seed, stream name, index): the noise of each axis is keyed by the conversion
time on a 0.1 ms grid, the daily variation's day-to-day spread by the local
day, a pass's dipole direction and a storm's pulsations by the event's
identity and repeat number, a NACK by the transfer count. An event's identity
is its `id`, or else its type and its `at`, never its place in the list, so
adding, removing or reordering events changes no other event's numbers. Two
events that would draw from one identity (two random-direction passes at the
same `at`, say) are refused until one of them has an `id`; giving it to the
newcomer leaves the other's numbers as they were. Events that draw nothing
(steps, passes with a fixed dipole, storms without pulsations) may share an
`at` freely. The same seed gives the same
field at the same instant however the app samples it, and `generate` with the
same arguments writes identical files; the tests check both.

A different seed redraws exactly those numbers: the sensor noise, each day's
amplitude and timing of the daily variation, the direction of every pass whose
dipole is random, the storms' pulsations, and which transfers a NACK burst
fails. The main field, the crust, the average shape of the daily variation,
each storm's Dst, the steps, every pass's geometry, timing and strength, and
when faults happen stay as they are. The tests check both halves.

## Speed

`--speed` runs scenario time faster than real time (a day in 24 minutes at
60) without changing the chip's conversion timing, so the app's driver sees
hardware timing while the chart shows a day's variation. Events and faults
that repeat without a `count` never run out, however long or fast the server
runs: each occurrence is worked out when its time comes, and a run of power
cycles while no transfer was looking is counted in one step. For history, use
`backfill`: a week at 1 Hz takes about a minute on a laptop.

The model's years are 2025.0 to 2030.0, those WMM2025 covers. A scenario must
start inside them, and `generate` and `backfill` refuse a span that leaves
them. `serve` runs as long as it is left to, so it logs a warning instead, once,
when its simulated time reaches 2030.0: from there on the main field is
extrapolated, not modelled. At `--speed 60` from today that is years away; at
the maximum, 100,000, it is hours.
