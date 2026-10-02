# The RM3100 simulator (Part B)

A simulated RM3100 that the node app drives exactly as it drives the real
chip. It works at the register level: the app's driver writes POLL, polls
STATUS for DRDY, reads the result registers and runs the self test, and the
simulator answers each I2C transfer as the chip would, with the measurement
registers filled from a model of the field at a site.

The app connects with one variable, `MAGNETOMETER_BUS=tcp://host:9100`, and
nothing above its `tcp://` transport knows the difference. A test checks that
the app never imports this package.

## Command line

```bash
python -m rm3100_sim serve --scenario demo            # the chip on :9100
python -m rm3100_sim generate --scenario uap-flyby --start 2026-09-30T12:00Z --duration 1h > day.csv
python -m rm3100_sim backfill --scenario demo --days 7 --data-dir ./data
python -m rm3100_sim describe uap-flyby               # a scenario, fully resolved
python -m rm3100_sim list                             # the built-in scenarios
```

`serve`, `generate` and `describe` take `--scenario` (a built-in name or a
YAML path), `--seed` and `--start`, which override the file. The same
scenario, seed and start always give the same data. `--speed` runs scenario
time faster than real time (a day in 24 minutes at 60) without changing the
chip's conversion timing.

`backfill` writes simulated history into the app's database, ending where the
existing history begins, so it can run while the app samples and never
overwrites anything the app recorded. The app's own retention and size cap
then apply to it. A refused run leaves the database untouched. A week at
1 Hz takes about a minute on a laptop.

## The chip (`device.py`)

The register model follows the manual (PNI UM16, V16.0):

- POLL starts a single measurement, and DRDY rises after the conversion time
  (6.8 ms for three axes at 200 cycles).
- CMM starts and stops continuous mode, which measures every TMRC interval.
  Writing TMRC or reading CMM while it runs ends it, as the manual says.
- The cycle counts set each axis's gain, conversion time and noise, and reset
  to 200 on a power cycle.
- Reading the results or writing any register clears DRDY.
- Writes the chip cannot carry out are NACKed on the wire (errno 121,
  EREMOTEIO, as i2c-dev reports it) and set the HSHAKE NACK bits.
- The self test reports per-axis pass bits; a scenario can kill one axis's
  coil.
- REVID reads 0x22, and addresses other than the strapped one NACK.

A measurement is the scenario's field at the middle of the conversion,
rotated into the sensor's frame, plus Gaussian noise, quantised with PNI's
gain formula. The datasheet's noise (Table 3-1: 30, 20 and 15 nT at 50, 100
and 200 cycles) can only have been measured on the output, which is in
counts, so it already includes the rounding. The noise added before rounding
is what remains once the rounding's share is taken out, so the registers
report the table's spread at every cycle count.

Faults act on the transfers: NACK bursts, a disconnect ending in a power
cycle, DRDY that never rises, and a brown-out, which resets the registers
without any transfer failing.

Where the manual is silent or contradicts working drivers, the model picks a
reading. The one the app depends on is that a pointer-only write, the first
half of every register read, leaves DRDY alone; otherwise STATUS polling could
never see it rise. The
[hardware checklist](../docs/hardware-verification.md) (item 8) has the
details. The model also accepts a write to register 0x0A, which HamSCI's
runMag uses as an undocumented "NOS" register, and ignores the value.

### The wire protocol (`server.py`)

One JSON object per line each way, over TCP:

```
-> {"op": "transfer", "address": 32, "write": "24", "read": 9}
<- {"ok": true, "read": "00a10f..."}
<- {"ok": false, "errno": 121, "error": "no acknowledge from 0x20 (injected)"}
```

`write` and `read` are hex, and a failure carries the Linux errno, which the
app's client raises as `OSError`. It is readable with `nc`.

## The field (`physics.py`)

North/east/down, in nanotesla, summed from independent sources:

| Source | Model | Size at Greenville, SC |
| --- | --- | --- |
| Main field | WMM2025 at the site (pygeomag) | \|B\| 48,564 nT, dip 62.3°, declination −7.0° |
| Crust | a fixed offset per scenario | 42, −18, 65 nT in the built-ins |
| Daily variation | solar-quiet variation fitted to the FRD and BSL observatories, by season and solar activity, with day-to-day variability | 20–90 nT peak to peak |
| Storms | sudden commencement, main phase, recovery, and Pc4–Pc5 pulsations | as configured |
| Local steps | an offset for a while, such as a car parked by the mast | tens of nT |
| UAP passes | magnetic dipoles on straight, level tracks | 200 nT at 1 km on axis, 1.6 nT at 5 km, for 1e9 A·m² |

The sensor sees that field through its mounting, a hard-iron offset and
per-axis gain errors, all set by the scenario. Aircraft and drones are left
out, as their moments are far smaller. The model has no induced magnetisation,
no 1/f sensor noise and no temperature dependence, and the daily variation fit
stands for about 30–38° N in eastern North America.

## Scenario files

YAML, validated strictly: an unknown key or a wrong type is an error naming
the key path. Every field except `site` is optional.

```yaml
name: my-scenario
description: One line for `list`
seed: 42                      # everything random follows from this
start: now                    # or an ISO 8601 time within 2025-2029
site: {latitude: 34.85, longitude: -82.39, altitude_m: 300}
field:
  crustal_offset_nt: [42.0, -18.0, 65.0]   # north, east, down
  diurnal: {enabled: true, f107: 120, variability: 0.25}
sensor:
  address: 0x20
  mounting: {yaw_deg: 37, pitch_deg: 0, roll_deg: 180}   # roll 180 = upside down
  noise_scale: 1.0            # times the datasheet noise
  hard_iron_nt: [0, 0, 0]
  gain_error: [0.0, 0.0, 0.0]
events:
  - type: uap_pass
    at: +5m                   # relative to start, or ISO 8601
    every: 15m                # optional repeat; `count` bounds it
    closest_approach_m: 800   # horizontal, to the right of track
    altitude_m: 400           # above the sensor
    speed_mps: 120
    heading_deg: 90
    moment_am2: 1.0e+9
    moment_direction: random  # or along_track, or [north, east, down]
  - {type: storm, at: +1h, dst_min_nt: -150, main_phase_h: 6, recovery_h: 12}
  - {type: step, at: +20m, duration: 30m, delta_nt: [35, -12, 48]}
faults:
  - {type: nack, at: +10m, duration: 60s, probability: 0.3}
  - {type: disconnect, at: +4m, duration: 30s}
  - {type: stuck_drdy, at: +7m, duration: 20s}
  - {type: brownout, at: +9m}
```

Times are relative (`+90s`, `+5m`, `+2h`, `+1d`, `+01:30:00`) or absolute
ISO 8601. A bare `14:00` is refused as ambiguous, since YAML would read it as
the number 840. The start must fall within 2025–2029, the years WMM2025
covers.

### Built-in scenarios

| Name | What it shows |
| --- | --- |
| `quiet-day` | WMM2025, crust, the daily variation and datasheet noise |
| `uap-flyby` | passes every 15 minutes at slant ranges of about 0.6, 1.5 and 3 km, with the sensor upside down and turned 37° east of north |
| `storm` | an intense storm (Dst −250 nT) with pulsations |
| `faults` | every 20 minutes: NACKs, a disconnect, stuck DRDY and a brown-out |
| `demo` | what `docker compose up` runs: UAP passes, a parked car, short NACK bursts and the rotated mounting |

## Reproducibility

Every random number is a hash of the seed, a stream name and an index, never
a draw from a shared generator. An event's numbers are keyed by its `id`, or
else its type and `at`, so adding or removing events changes no other
event's numbers, and the same seed gives the same field at the same instant
however the app samples it.
