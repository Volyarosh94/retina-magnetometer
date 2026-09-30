# The node app (Part A)

A container for a RETINA node that reads a PNI RM3100 magnetometer over I2C,
stores what it measures, and shows it in a browser on port 3030:

- the three axes and |B| over any window from ten minutes to thirty days, with
  zoom that fetches the zoomed range at full resolution;
- the sensor's **orientation**, worked out from the Earth's field against the
  World Magnetic Model at the node's location;
- the sensor's **health**: when it last sampled, how many reads have failed and
  how, what the self test said, and what the storage is doing.

It is configured through `MAGNETOMETER_*` environment variables, keeps its data
in one SQLite file under `/data`, and never exits because of the hardware: no
bus, no sensor, a flaky bus and a sensor that resets are all states it reports
and recovers from.

## Sampling

Two modes, both from the RM3100 manual (PNI UM16 §5):

- **poll** (default): the app triggers each measurement itself (POLL), waits for
  data-ready (DRDY, read from the STATUS register) and reads the nine result
  bytes. Samples fall on a grid aligned to the clock, so at 1 Hz every sample
  is on the second and nodes' timestamps line up. Each is stamped at the middle
  of its conversion. Any rate up to the conversion limit works.
- **continuous**: the chip measures on its own at a TMRC rate (600 Hz halving
  down to 0.075 Hz) and the app reads each result as DRDY rises. The requested
  rate is rounded to the nearest TMRC step.

The cycle count sets gain, noise and the fastest possible rate. From the
datasheet (Table 3-1) and PNI's gain formula (0.3671·CC + 1.5 counts/µT):

| Cycle count | Gain (counts/µT) | Noise (nT) | One count (nT) | Max 3-axis rate |
| ---: | ---: | ---: | ---: | ---: |
| 50 | 19.9 | 30 | 50.4 | ~533 Hz |
| 100 | 38.2 | 20 | 26.2 | ~284 Hz |
| **200** (default) | **74.9** | **15** | **13.3** | **~147 Hz** |
| 400 | 148.3 | ~11 (extrapolated) | 6.7 | ~75 Hz |
| 800 | 295.2 | ~8.4 (extrapolated) | 3.4 | ~38 Hz |

The 400 and 800 rows extend the datasheet's noise curve; the 8.4 nT at 800
agrees with the 8.7 nT Regoli et al. (2018) measured at 800 cycles. The rate
column is derived from the datasheet's maximum single-axis rates.

At start-up, and whenever it has lost the sensor, the app probes addresses
0x20–0x23 for REVID 0x22 (or only the configured address), runs the built-in
self test, writes the cycle count and reads it back. Once a minute it reads the
cycle count again: a brown-out resets it to 200 without any transfer failing,
and every value after that would carry the wrong gain.

## Storage

One SQLite file, `/data/magnetometer.sqlite`, in WAL mode:

| Table | Holds | Kept |
| --- | --- | --- |
| `samples` | every measurement: time (ms), x, y, z in nT | `MAGNETOMETER_RAW_RETENTION_DAYS` (7) |
| `minutes` | per UTC minute: count, min/mean/max of x, y, z and \|B\| | `MAGNETOMETER_ROLLUP_RETENTION_DAYS` (365) |
| `sessions` | each time sampling (re)starts: cycle count, gain, rate, mode, bus, address | always |

Retention is bounded twice: by time, and by size (`MAGNETOMETER_MAX_DB_MB`,
1024). When the file passes the cap, the oldest raw samples go first, then the
oldest minutes, and the space is returned to the filesystem. Measured: a week
of raw samples at 1 Hz is about 25 MB (ten times that at 10 Hz), and a year of
minute summaries about 66 MB whatever the rate.

Samples are written in one transaction every `MAGNETOMETER_FLUSH_INTERVAL_S`
(5 s) rather than one per sample, which matters on an SD card; a clean stop
(`docker stop`) flushes first, and a power cut can lose at most that interval.
The last hour is also held in memory, so the live views never wait for the
disk and never miss the seconds not yet flushed.

The file is plain SQLite; `sqlite3 magnetometer.sqlite "SELECT * FROM minutes
ORDER BY t_ms DESC LIMIT 5"` works on it directly.

## The page

- **Tiles**: the latest |B|, X, Y and Z.
- **Field over time**: one row per component and one for |B|, each on its own
  scale (the components differ by tens of thousands of nT). Windows of 10 min,
  1 h, 6 h, 24 h, 7 d and 30 d. When a point stands for more than one sample,
  the line is the mean and the band behind it spans the minimum to the maximum,
  so a short spike stays visible in a month-long view. Drag to zoom in; the
  zoomed range is fetched again at full resolution. Double-click to go back.
- **Orientation**: a compass showing where the sensor's horizontal axes point,
  the numbers behind it, and what the estimate cannot know (below).
- **Sensor health**, **Storage**, **Configuration**.

The page polls: the chart every 2 s for windows up to an hour, every 30 s up to
a day and every 5 minutes beyond; health every 2 s; orientation every 10 s. It follows
retina-gui's design tokens and runs the Plotly release retina-gui runs, vendored
so it works on a network with no internet.

## Orientation

The app takes the median of the last minute of samples and compares it with
WMM2025 at the node's location (`location.rx` in retina-node's `config.yml`, or
`MAGNETOMETER_LATITUDE`/`LONGITUDE`/`ALTITUDE_M`). It reports:

- **the measured field** in the sensor's frame, which needs no model;
- **the magnitude against the model** (48,564 nT at Greenville, SC). More than
  5 % off points at a hard-iron offset from nearby steel or electronics, a gain
  error, or a local anomaly;
- **which axis points down**, and a lower bound on how far it is tilted: at
  mid-latitudes the field dips steeply (62° at Greenville), so only one axis can
  make the right angle with it;
- **the heading** of the first horizontal axis against magnetic and true north
  (WMM2025's declination), with an uncertainty that combines the model's
  declination error (0.35° at Greenville), the tilt bound and the magnitude
  mismatch.

A magnetometer on its own measures one vector, which fixes two of the three
rotations; turning the sensor about the field line changes nothing it can see.
The heading therefore assumes the downward axis is vertical, and the page says
so. Near the magnetic equator, where the field is nearly horizontal, the
downward axis becomes ambiguous and the page says that too. An accelerometer
beside the sensor would give full attitude.

## Health

`/api/health` and `/data/status.json` carry the same document (the second
rewritten atomically every 5 s, in retina-telemetry's format: `schema`,
`written_at`, `state`, `detail`, `errors`, and the fields below).

| State | Meaning |
| --- | --- |
| `starting` | looking for the sensor, or found it and waiting for the first sample |
| `ok` | sampling |
| `degraded` | sampling, but the last read failed or the self test failed |
| `stalled` | no sample for five sample periods (at least 10 s) |
| `no_bus` | the I2C device does not exist or cannot be opened; the detail says what to enable |
| `no_sensor` | the bus is there but no RM3100 answered |
| `config_error` | a `MAGNETOMETER_*` value is invalid; the detail names it and sampling is off |

Alongside: the last successful sample and its age, samples taken, read errors
(total, in a row, the last twenty with their times), how many times the sensor
was found again, the self-test result, the configured, effective and measured
rates, and storage use.

There is deliberately no Docker `HEALTHCHECK`: on a node, a container health
check that is red (no sensor fitted yet) holds up the Mender install of the
whole stack.

## API

All read-only JSON.

| Endpoint | Returns |
| --- | --- |
| `GET /api/series?window=<s>&points=<n>` | the chart data for the last `window` seconds |
| `GET /api/series?start=<ms>&end=<ms>&points=<n>` | the same for a range |
| `GET /api/latest` | the newest sample and its age |
| `GET /api/health` | the health document |
| `GET /api/orientation` | the orientation estimate, reference field and notes |
| `GET /api/config` | the effective configuration, location and recent sessions |
| `GET /healthz` | `ok` while the process serves requests |

`/api/series` returns `t` (ms) and, for each of `x`, `y`, `z` and `b` (|B|),
`min`/`mean`/`max` arrays of the same length, plus `n` (samples per point; 0
marks a gap, drawn as a break), `bucket_ms` (0 for raw samples) and `source`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAGNETOMETER_BUS` | `/dev/i2c-1` | `/dev/i2c-N`, `i2c:N`, or `tcp://host:port` for the simulator |
| `MAGNETOMETER_I2C_ADDRESS` | `auto` | `auto` probes 0x20–0x23; or one of them |
| `MAGNETOMETER_I2C_FRAMING` | `repeated-start` | `repeated-start` (one I2C_RDWR transaction) or `stop` (STOP between pointer and read, as PNI's manual draws it) |
| `MAGNETOMETER_MODE` | `poll` | `poll` or `continuous` |
| `MAGNETOMETER_SAMPLE_RATE_HZ` | `1` | samples per second; must not exceed the cycle count's limit |
| `MAGNETOMETER_CYCLE_COUNT` | `200` | 30–1000 |
| `MAGNETOMETER_SELF_TEST` | `true` | run BIST when the sensor is found |
| `MAGNETOMETER_DATA_DIR` | `/data` | database and status.json |
| `MAGNETOMETER_RAW_RETENTION_DAYS` | `7` | |
| `MAGNETOMETER_ROLLUP_RETENTION_DAYS` | `365` | at least the raw retention |
| `MAGNETOMETER_MAX_DB_MB` | `1024` | size cap on the database, WAL included |
| `MAGNETOMETER_FLUSH_INTERVAL_S` | `5` | seconds between disk writes |
| `MAGNETOMETER_NODE_CONFIG` | `/config/config.yml` | retina-node's merged config, read for `location.rx` |
| `MAGNETOMETER_LATITUDE`, `_LONGITUDE`, `_ALTITUDE_M` | unset | override the node's location |
| `MAGNETOMETER_ORIENTATION_WINDOW_S` | `60` | samples behind the orientation estimate |
| `MAGNETOMETER_HOST`, `MAGNETOMETER_PORT` | `0.0.0.0`, `3030` | where the page is served |
| `MAGNETOMETER_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

Changing a value means changing the container's environment and restarting
it. There is no settings form on the page: an unauthenticated LAN port is the
wrong place for writes, and on a node settings belong with retina-gui.

## Running it

- **Against the simulator**: see the [top-level README](../README.md).
- **On a RETINA node**: [docs/adding-to-a-retina-node.md](../docs/adding-to-a-retina-node.md),
  and [docs/hardware-verification.md](../docs/hardware-verification.md) for the
  first power-up of a real sensor.
- **Standalone on any Linux board with I2C**: `docker run -p 3030:3030 -v
  /dev:/dev --device-cgroup-rule 'c 89:* rmw' -v $PWD/data:/data -e
  MAGNETOMETER_LATITUDE=... -e MAGNETOMETER_LONGITUDE=...
  ghcr.io/offworldlabs/retina-magnetometer`.

## Tests

`uv run pytest` (or `tools/check.sh` for every gate). The driver is tested two
ways. Against a **scripted bus** (`tests/fakes.py`), every byte it puts on the
wire is asserted: register pointers without the SPI read bit, the six-byte
cycle-count write and its read-back, POLL then STATUS then the nine-byte burst,
the stop/TMRC/CMM order for continuous mode, the BIST sequence of the manual's
Figure 5-1. Against the **simulator's register model**, under a fake clock, the
same driver meets the chip's behaviour: conversion times, noise at each cycle
count, continuous-mode rate caps, DRDY rules, NACKs, a disconnect with a power
cycle, stuck DRDY. Above the driver: the sampler through every recovery path,
storage past its size cap, orientation for known mountings and its limits, the
API, and the app as a real process against the simulator over TCP, stopped with
SIGTERM to prove nothing buffered is lost.

What cannot be tested without a sensor is listed in
[docs/hardware-verification.md](../docs/hardware-verification.md).
