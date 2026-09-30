# The RM3100 simulator (Part B)

A simulated RM3100 that the node app (Part A) drives exactly as it drives the
real chip. The simulation is at the **register level**: the app's driver
writes POLL, polls STATUS for DRDY, burst-reads the result registers, runs the
self test, and the simulator answers each I2C transfer the way the chip would,
with the measurement registers filled from a physical model of the field at a
site. Pointing the app at it is one variable, `MAGNETOMETER_BUS=tcp://host:9100`
instead of `/dev/i2c-1`; not one line of the app knows the difference, and a
test enforces that the app never imports this package.

```bash
python -m rm3100_sim serve --scenario demo            # the chip on :9100
python -m rm3100_sim generate --scenario uap-flyby --start 2026-09-30T12:00Z --duration 1h > day.csv
python -m rm3100_sim backfill --scenario demo --days 7 --data-dir ./data
python -m rm3100_sim describe uap-flyby               # a scenario, fully resolved
python -m rm3100_sim list                             # the built-in scenarios
```

`--scenario` takes a built-in name or a YAML path; `--seed` and `--start`
override the file. The same scenario, seed and start always give the same data:
`generate` output is identical byte for byte, and the live server's value at a
given instant is identical in every run.

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
  old values and sets NACK2; a POLL during continuous mode, or a CMM write
  during a POLL, is ignored and sets NACK1; a write to an undefined register
  sets NACK0.
- **BIST**, armed with STE and run by the next POLL, reports per-axis pass
  bits; a scenario can kill one axis's coil.
- **REVID** reads 0x22. Addresses other than the strapped one (0x20–0x23)
  NACK.

A measurement is the scenario's field at the middle of the conversion, rotated
into the sensor's frame, plus Gaussian noise at the datasheet level for that
axis's cycle count, clipped at ±800 µT and quantised with PNI's gain formula.
The noise for a conversion is a pure function of the seed, the axis and the
time, so it does not depend on how often or in what order the app asks.

Where the manual is silent, the model follows what the field supports and the
[hardware checklist](../docs/hardware-verification.md) lists it. The main one:
a pointer-only write (the first half of every register read) does not clear
DRDY, or STATUS polling, which every maintained driver relies on, could never
see it rise.

### The wire protocol (`server.py`)

One JSON object per line each way, over TCP:

```
-> {"op": "transfer", "address": 32, "write": "24", "read": 9}
<- {"ok": true, "read": "00a10f..."}
<- {"ok": false, "errno": 121, "error": "no acknowledge from 0x20 (injected)"}
```

`write` and `read` are hex; a failure carries the errno a Linux i2c-dev adapter
returns (121, EREMOTEIO, for a NACK), which the app's client raises as
`OSError`. `{"op": "hello"}` names the server and scenario. It is readable with
`nc`, on purpose.

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

The UAP model is the one the task gives: moment 1e9 A·m², falling as 1/r³.
Aircraft and drones are left out, their moments being far smaller. A pass is
described by its geometry at closest approach, which is what decides what a
magnetometer sees: horizontal distance (to the right of track), altitude
above the sensor, speed, heading, and the dipole's direction. That direction is
drawn once per pass from the seed unless the scenario fixes it (`along_track`,
or a vector), and stays fixed for the pass. For speed, each pass is evaluated
only while it contributes more than 0.001 nT.

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
start: now                    # or an ISO 8601 time: 2026-09-30T12:00:00Z
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
    at: +5m                   # relative to start, or ISO 8601
    every: 15m                # optional repeat; `count` bounds it (default: a week's worth)
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
```

Times are `+90s`, `+5m`, `+2h`, `+1d`, `+01:30:00` from the start, or absolute
ISO 8601. Durations take the same relative forms.

### Built-in scenarios

| Name | What it shows |
| --- | --- |
| `quiet-day` | The baseline: WMM2025, crust, the daily variation, datasheet noise |
| `uap-flyby` | Passes every 15 minutes at slant ranges of about 0.6, 1.5 and 3 km: well inside the sensor's reach, at its edge, and beyond it. Sensor upside down, turned 37° east of north |
| `storm` | An intense storm (Dst −250 nT) with pulsations, from ten minutes in |
| `faults` | Every 20 minutes: a minute of 30 % NACKs, a 30 s disconnect ending in a power cycle, 20 s of stuck DRDY |
| `demo` | What `docker compose up` runs: UAP passes, a car parked for half an hour every two hours, a short NACK burst every half hour, the rotated mounting |

## Reproducibility

Nothing draws from a shared random generator. Every random number is a hash of
(seed, stream name, index): the noise of each axis is keyed by the conversion
time on a 0.1 ms grid, the daily variation's day-to-day spread by the local
day, a pass's dipole direction by its event and repeat number, a NACK by the
transfer count. Adding an event therefore changes nothing else's numbers, and
the same seed gives the same field at the same instant however the app samples
it. `generate` with the same arguments writes identical files; the tests check
that, and that a different seed changes the noise but not the field.

## Speed

`--speed` runs scenario time faster than real time (a day in 24 minutes at
60) without changing the chip's conversion timing, so the app's driver sees
hardware timing while the chart shows a day's variation. For history, use
`backfill`: a week at 1 Hz takes under a minute.
