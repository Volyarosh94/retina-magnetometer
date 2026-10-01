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
and recovers from. A bad setting, an unreadable database or a data directory
it cannot write does not stop it either; each is reported on the page.

## Sampling

Two modes, both from the RM3100 manual (PNI UM16 §5):

- **poll** (default): the app triggers each measurement itself (POLL), waits for
  data-ready (DRDY, read from the STATUS register) and reads the nine result
  bytes. Samples fall on a grid aligned to the clock, so at 1 Hz every sample
  is on the second and nodes' timestamps line up. Each is stamped at the middle
  of its conversion. Poll mode keeps rates up to what one measurement and its
  transfers take: about 87 Hz at the default 200 cycles, allowing for a chip
  5 % slower than the datasheet and a 100 kHz bus. A faster setting runs at
  that limit, with a configuration warning.
- **continuous**: the chip measures on its own at a TMRC rate (600 Hz halving
  down to 0.075 Hz) and the app reads each result as DRDY rises, stamping it as
  it reads it. The requested rate is rounded to the nearest TMRC step, and the
  conversion time is its only cap.

The cycle count sets gain, noise and the fastest possible rate. From the
datasheet (Table 3-1) and PNI's gain formula (0.3671·CC + 1.5 counts/µT):

| Cycle count | Gain (counts/µT) | Noise (nT) | One count (nT) | Max 3-axis rate | Poll mode's limit |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 50 | 19.9 | 30 | 50.4 | ~533 Hz | ~160 Hz |
| 100 | 38.2 | 20 | 26.2 | ~284 Hz | ~125 Hz |
| **200** (default) | **74.9** | **15** | **13.3** | **~147 Hz** | **~87 Hz** |
| 400 | 148.3 | ~11 (extrapolated) | 6.7 | ~75 Hz | ~54 Hz |
| 800 | 295.2 | ~8.4 (extrapolated) | 3.4 | ~38 Hz | ~31 Hz |

The noise is one standard deviation of a single reading on one axis, as the
registers report it, rounding to counts included; the
[design note](../docs/design-note.md#assumptions) (B5) says why it is read that
way. The 400 and 800 rows extend the datasheet's noise curve; the 8.4 nT at 800
agrees with the 8.7 nT Regoli et al. (2018) measured at 800 cycles. The rate
column is derived from the datasheet's maximum single-axis rates. Poll mode's
limit adds 5 % to the conversion time, and 4.3 ms to each sample for its
transfers and the host's own share.

At start-up, and whenever it has lost the sensor, the app probes addresses
0x20–0x23 for REVID 0x22 (or only the configured address), stops continuous
mode in case an earlier run left it on, writes the cycle count and reads it
back, runs the built-in self test, and starts the configured mode. A failed
attempt waits before the next, from 1 s doubling to 30 s, and the wait starts
again from 1 s once a sample has got through.

No sample is stored until what the app set on the chip has been read back
after it was measured: the cycle count, and TMRC (poll mode, which has no use
for TMRC, sets it to 0x9F, a value no reset leaves). That is after every sample
at up to 1 Hz, and once a second for each second's samples at faster rates. A
brown-out resets the chip to its defaults without any transfer failing: in poll
mode it goes on measuring, at 200 cycles whatever the app set, and a reset
between a measurement and the read of its results hands over zeros. A
read-back that finds the chip reset drops every sample since the last good one
(the health page lists a sensor reset and how many were dropped) and
configures the chip again.

In continuous mode a sample is also held until a later one shows continuous
mode was still running, since CMM cannot be read without ending it. A reset
ends continuous mode: DRDY staying low for three sample periods (and 20 ms)
counts as a failed read, three in a row make the app find the sensor again,
and the sample it could not confirm is dropped. A clean stop delivers every
sample it can confirm and takes the chip out of continuous mode.

## Storage

One SQLite file, `/data/magnetometer.sqlite`, in WAL mode:

| Table | Holds | Kept |
| --- | --- | --- |
| `samples` | every measurement: time (ms), x, y, z in nT | `MAGNETOMETER_RAW_RETENTION_DAYS` (7) |
| `minutes` | per UTC minute: count, min/mean/max of x, y, z and \|B\| | `MAGNETOMETER_ROLLUP_RETENTION_DAYS` (365) |
| `sessions` | each time sampling (re)starts: cycle count, gain, rate, mode, bus, address | always |

Retention is bounded twice: by time, and by size (`MAGNETOMETER_MAX_DB_MB`,
1024). Time is the node's clock: every 10 minutes, the rows older than their
retention go. The size is the data's own, the pages in use, so a reader
holding the file open cannot make the cap delete more: past the cap the oldest
go until the data are back under 90 % of it. First the raw samples the minute
summaries already cover, then the oldest minutes, and raw samples not yet
summarised only when nothing else is left; when both must give way, the raw
samples keep half of the room. The freed pages go back to the filesystem at
the same prune, 4 MiB at a time with a checkpoint after each, unless a reader
holds an old snapshot. They then stay in the file, where new writes reuse
them, until a prune finds no such reader, and a cap the reader held up is
tried again within a minute. Measured: a week of raw samples at 1 Hz is about
23 MB (24.5 MB with its minute summaries; the samples take ten times as much
at 10 Hz), and a year of minute summaries about 66 MB whatever the rate.

When the cap is smaller than the retentions need at the configured rate (about
38.5 B a raw sample and 125 B a minute summary), the history is shorter than
configured, the raw samples' first. The health document's `config_notes`, and
the Storage card, say how much the cap holds: for example, about 3.7 days of
raw samples at 10 Hz with 200 MB. It is information only: the state is
unaffected.

A database that cannot be read at all (an SD card fault) is moved aside as
`magnetometer.sqlite.unreadable-<time>` and a new one started; the app never
deletes these, and the Storage card shows how many there are and what they
take. One written by a newer version of the app, as after a rollback, is left
as it is and reported, and nothing is stored until it is dealt with.

Samples are written in one transaction every `MAGNETOMETER_FLUSH_INTERVAL_S`
(5 s) rather than one per sample, which matters on an SD card. They go through
one connection kept open, so a flush appends to the WAL without a sync; the
database file is written and synced only at checkpoints (each prune's, every
10 minutes, and SQLite's own when the WAL reaches 1,000 pages): about 36 syncs
an hour at 1 Hz ([hardware-verification.md](../docs/hardware-verification.md),
item 20). A clean stop (`docker stop`) flushes and closes the database, leaving
one file; a power cut can lose what the kernel had not yet written out, about
the last half minute, never the database. The last hour (at most 200,000
samples) is also held in memory, so the live views never wait for the disk
and never miss the seconds not yet flushed.

The file is plain SQLite; `sqlite3 magnetometer.sqlite "SELECT * FROM minutes
ORDER BY t_ms DESC LIMIT 5"` works on it directly.

## The page

- **Tiles**: the latest |B|, X, Y and Z.
- **Field over time**: one row per component and one for |B|, each on its own
  scale (the components differ by tens of thousands of nT). Windows of 10 min,
  1 h (the default), 6 h, 24 h, 7 d and 30 d; the address (`#window=<s>`) and
  the browser keep the choice. When a point stands for more than one sample,
  the line is the mean and the band behind it spans the minimum to the
  maximum, so a short spike stays visible in a month-long view. From 24 h up,
  each row's scale fits the mean line so the daily variation shows; the band
  runs off the edge where a spike passed, the note under the chart says so,
  and hovering a point gives its full range. A scale narrower than 10 nT is
  widened to 10 nT. Drag to zoom in; the zoomed range is fetched again at full
  resolution. Double-click to go back.
- **Orientation**: a compass showing where the sensor's horizontal axes point,
  the numbers behind it, and what the estimate cannot know (below).
- **Sensor health**, **Storage**, **Configuration**: the health document
  (below), the storage figures with any problems, notes and files kept aside,
  and the settings in force with their errors and warnings.

The page polls: the chart every 2 s for windows up to an hour, every 30 s up to
a day and every 5 minutes beyond, each refresh fetching only the points since
the newest it holds, and the whole window again after every thirty; health
every 2 s; orientation every 10 s. It follows retina-gui's design tokens and
runs the Plotly release retina-gui runs, vendored so it works on a network
with no internet.

## Orientation

The app takes the median of the last minute of samples and compares it with
WMM2025 at the node's location (`location.rx` in retina-node's `config.yml`, or
`MAGNETOMETER_LATITUDE`/`LONGITUDE`/`ALTITUDE_M`). It reports:

- **the measured field** in the sensor's frame, which needs no model;
- **the magnitude against the model** (48,564 nT at Greenville, SC, on
  30 September 2026). More than 5 % off points at a hard-iron offset from
  nearby steel or electronics, a gain error, or a local anomaly;
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
downward axis becomes ambiguous and the page says that too. Where the field's
horizontal part is under 2,000 nT (near a magnetic pole, NOAA's blackout zone
for compasses, or with two dead axes) there is no heading to give, and the
page says why. An accelerometer beside the sensor would give full attitude.

## Health

`/api/health` and `/data/status.json` carry the same document (the second
rewritten every 5 s, written whole and renamed into place, in
retina-telemetry's format: `schema`, `written_at`, `state`, `detail`, `errors`,
and the fields below).

| State | Meaning |
| --- | --- |
| `starting` | looking for the sensor, or found it and waiting for the first sample |
| `ok` | sampling |
| `degraded` | sampling, but the last read failed, the self test failed, a setting was replaced by a safe one, storage is failing, poll mode is missing more than one tick in ten over the last minute, or something inside the app failed (the detail says which) |
| `stalled` | no sample for five sample periods (at least 10 s): since the last sample, since the sensor was first found (the count does not start again when it is found again), or since start-up |
| `no_bus` | the I2C device does not exist or cannot be opened; the detail says what to enable |
| `no_sensor` | the bus is there but no RM3100 answered |
| `config_error` | a `MAGNETOMETER_*` value has no safe reading; the detail names it and sampling is off |

A value with a safe reading is used that way instead, listed in
`config_warnings`, and the state is `degraded`: a poll rate above poll mode's
limit runs at the limit, half a location is ignored, a host that cannot be
listened on gives way to 127.0.0.1, and a port that is not a port number to
3030.

Alongside: the last successful sample and its age, samples taken, read errors
(total, and in a row), internal errors (counted apart from read errors), the
last twenty errors with their times (sensor resets and storage problems among
them; `errors` holds the latest five), how many times the sensor was found
again, the self-test result, the configured, effective and measured rates
(configured is the rate as set; in poll mode, effective leaves out the ticks
it missed), storage use, and the configuration's `config_errors` (sampling is
off), `config_warnings` (settings replaced by a safe reading; the state is
`degraded`) and `config_notes` (what valid settings add up to, such as the
history a small size cap holds; the state is unaffected).

Each failing storage operation (session, write, rollup, prune, stats,
status.json) is listed with the errors when it starts failing or fails another
way, and not again until it has gone 15 minutes without failing that way. It
is tried again after 5 s, 10, 20, 40, then every minute (never later than its
own schedule would run it). The Storage card shows them all together, and
clears when the last one works again. A database that cannot be opened is one
problem, whichever operation met it, and storage problems leave the
read-error counts alone.

There is deliberately no Docker `HEALTHCHECK`: on a node, a container health
check that is red (no sensor fitted yet) holds up the Mender install of the
whole stack.

## API

All read-only JSON, but for `/healthz`.

| Endpoint | Returns |
| --- | --- |
| `GET /api/series?window=<s>&points=<n>` | the chart data for the last `window` seconds (10 s to 400 days; 600 if not given) |
| `GET /api/series?start=<ms>&end=<ms>&points=<n>` | the same for a range, at most 400 days long |
| `GET /api/series?…&since=<ms>` | only the points from `since` on |
| `GET /api/latest` | the newest sample and its age |
| `GET /api/health` | the health document |
| `GET /api/orientation` | the orientation estimate, reference field and notes |
| `GET /api/config` | the effective configuration with its `errors`, `warnings` and `notes`, the location, and the five latest sessions |
| `GET /healthz` | `ok`, as plain text, while the process serves requests |

`/api/series` returns `t` (ms) and, for each of `x`, `y`, `z` and `b` (|B|),
`min`/`mean`/`max` arrays of the same length, plus `n` (samples per point; 0
marks a gap, drawn as a break), `bucket_ms` (0 for raw samples), `source`
(`memory`, `samples`, `samples+memory` or `minutes`), and the `start`, `end`
and `now` it answered for. `points` is held to 10–20,000 (1,500 if not given).
Values are rounded to 0.01 nT; buckets start at multiples of `bucket_ms`. If
the database cannot be read, the response holds the live buffer and a
`warning`. `start`, `end` and `since` are times in ms between 1970 and the year
3000; anything else is a 400, as is a window outside its range. JSON of 1 KB or
more is gzipped for clients that accept it.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAGNETOMETER_BUS` | `/dev/i2c-1` | `/dev/i2c-N`, `i2c:N`, `N`, or `tcp://host:port` for the simulator |
| `MAGNETOMETER_I2C_ADDRESS` | `auto` | `auto` probes 0x20–0x23; or one of them |
| `MAGNETOMETER_I2C_FRAMING` | `repeated-start` | `repeated-start` (one I2C_RDWR transaction) or `stop` (STOP between pointer and read, as PNI's manual draws it) |
| `MAGNETOMETER_MODE` | `poll` | `poll` or `continuous` |
| `MAGNETOMETER_SAMPLE_RATE_HZ` | `1` | samples per second, 0.01–600. Above the cycle count's conversion limit it is a configuration error; in poll mode, above what a measurement and its transfers allow (~87 Hz at 200 cycles) it runs at that limit, with a warning |
| `MAGNETOMETER_CYCLE_COUNT` | `200` | 30–1000 |
| `MAGNETOMETER_SELF_TEST` | `true` | run BIST whenever the sensor is found |
| `MAGNETOMETER_DATA_DIR` | `/data` | database and status.json |
| `MAGNETOMETER_RAW_RETENTION_DAYS` | `7` | 0.01–3650 |
| `MAGNETOMETER_ROLLUP_RETENTION_DAYS` | `365` | 1–36,500, and at least the raw retention |
| `MAGNETOMETER_MAX_DB_MB` | `1024` | 16–1,000,000: a size cap on the data (the pages in use); the file follows at the next prune, and the WAL is truncated at each. A cap smaller than the retentions need shortens the history, and `config_notes` says by how much |
| `MAGNETOMETER_FLUSH_INTERVAL_S` | `5` | 0.2–300 seconds between disk writes |
| `MAGNETOMETER_NODE_CONFIG` | `/config/config.yml` | retina-node's merged config, read for `location.rx` |
| `MAGNETOMETER_LATITUDE`, `_LONGITUDE`, `_ALTITUDE_M` | unset | override the node's location: latitude and longitude together (altitude optional); a lone value is ignored, with a warning |
| `MAGNETOMETER_ORIENTATION_WINDOW_S` | `60` | 5–3600 seconds of samples, whose median the orientation uses |
| `MAGNETOMETER_HOST`, `MAGNETOMETER_PORT` | `0.0.0.0`, `3030` | where the page is served. A host that cannot be listened on gives way to 127.0.0.1, and a port that is not 1–65535 to 3030, each with a warning; sampling is unaffected. A port that is already taken ends the process, as there would be no page to report on |
| `MAGNETOMETER_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

A number outside its range, or a value that is not one of the choices, is a
configuration error unless the table gives it a safe reading. Changing a value
means changing the container's environment and restarting it. There is no
settings form on the page: an unauthenticated LAN port is the wrong place for
writes, and on a node settings belong with retina-gui.

## Running it

- **Against the simulator**: see the [top-level README](../README.md).
- **On a RETINA node**: [docs/adding-to-a-retina-node.md](../docs/adding-to-a-retina-node.md),
  and [docs/hardware-verification.md](../docs/hardware-verification.md) for the
  first power-up of a real sensor.
- **Standalone on any Linux board with I2C** (bus 1 here):

  ```bash
  docker run -d --name retina-magnetometer -p 3030:3030 \
    -v /dev:/dev --device-cgroup-rule 'c 89:1 rmw' \
    --tmpfs /dev/shm:size=1m --tmpfs /dev/mqueue:size=64k -v /dev/null:/dev/console \
    -v "$PWD/data:/data" \
    -e MAGNETOMETER_LATITUDE=... -e MAGNETOMETER_LONGITUDE=... \
    ghcr.io/offworldlabs/retina-magnetometer
  ```

  `c 89:N` admits `/dev/i2c-N` alone (i2c-dev is major 89; another bus also
  needs `MAGNETOMETER_BUS=/dev/i2c-N`). The two tmpfs mounts keep out the
  host's shared memory and message queues, which the `/dev` bind would
  otherwise bring along, and the null device covers the host's console.
  [deploy/retina-node-service.yml](../deploy/retina-node-service.yml) adds the
  rest of a node's hardening: every capability dropped, a read-only root
  filesystem and `no-new-privileges`. Where the image is published is in
  [the node guide](../docs/adding-to-a-retina-node.md#where-the-image-comes-from).

## Tests

`uv run pytest` (or `tools/check.sh` for every gate). The driver is tested two
ways. Against a **scripted bus** (`tests/fakes.py`), every byte it puts on the
wire is asserted: register pointers without the SPI read bit, the six-byte
cycle-count write and its read-back, POLL then STATUS then the nine-byte burst,
the stop/TMRC/CMM order for continuous mode, the BIST sequence of the manual's
Figure 5-1 with continuous mode stopped first, and the acquisition order (stop,
cycle count, self test). Against the **simulator's register model**, under a
fake clock, the same driver meets the chip's behaviour: conversion times, noise
at each cycle count, continuous-mode rate caps, DRDY rules, NACKs and refused
writes, a disconnect with a power cycle, a brown-out, stuck DRDY.

Above the driver: the sampler through every recovery path in both modes,
including a brown-out kept out of storage whenever it lands; storage past its
size cap, with a reader holding a snapshot and with both tables giving way;
unreadable, newer and vanished databases; the housekeeping's failures, one
operation at a time, and its retries; a wall clock that steps back;
orientation for known mountings and its limits; the API, and the page's own
script (run under Node where it is installed); and the app as a real process
against the simulator over TCP, stopped with SIGTERM to prove nothing buffered
is lost, and started into an unreadable database, a data directory it cannot
write and an address that is not the machine's.

What cannot be tested without a sensor is listed in
[docs/hardware-verification.md](../docs/hardware-verification.md).
