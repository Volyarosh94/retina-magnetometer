# The node app (Part A)

A container for a RETINA node that reads a PNI RM3100 magnetometer over I2C,
stores what it measures and serves a page on port 3030. The page shows the
three axes and |B| over windows from ten minutes to thirty days, the sensor's
orientation, and its health.

## What it does

In poll mode (the default) the app triggers each measurement, waits for
data-ready in the STATUS register and reads the result. Samples fall on a
clock-aligned grid, so at 1 Hz every sample is on the second, stamped at the
middle of its conversion. In continuous mode the chip measures at its own
TMRC rate. At the default 200 cycles the noise is 15 nT and poll mode keeps up
to about 87 Hz.

When it finds the sensor, the app checks REVID 0x22, writes the cycle count
and reads it back, and runs the self test. It never exits because of the
hardware: a missing bus or sensor, a flaky bus and a sensor that resets are
states it reports and recovers from. A brown-out resets the chip without any
transfer failing, so no sample is stored until the cycle count and TMRC have
been read back after it, and samples taken after a reset are dropped.

Orientation compares the median of the last minute with WMM2025 at the node's
location. The app reports which axis points down, a lower bound on its tilt,
the heading against magnetic and true north with an uncertainty, and the
field's magnitude against the model, where more than 5 % off points at nearby
steel or a gain error. The heading assumes the downward axis is vertical,
because one field vector fixes only two of the three rotations.

## Running it

- Against the simulator, see the [top-level README](../README.md).
- On a RETINA node, see [docs/adding-to-a-retina-node.md](../docs/adding-to-a-retina-node.md),
  and [docs/hardware-verification.md](../docs/hardware-verification.md) for
  the first power-up of a real sensor.
- Standalone on any Linux board with I2C (bus 1 here):

  ```bash
  docker run -d --name retina-magnetometer -p 3030:3030 \
    -v /dev:/dev --device-cgroup-rule 'c 89:1 rmw' \
    --tmpfs /dev/shm:size=1m --tmpfs /dev/mqueue:size=64k -v /dev/null:/dev/console \
    -v "$PWD/data:/data" \
    -e MAGNETOMETER_LATITUDE=... -e MAGNETOMETER_LONGITUDE=... \
    ghcr.io/offworldlabs/retina-magnetometer
  ```

  `c 89:N` admits `/dev/i2c-N` alone; the tmpfs mounts and the null device
  keep the host's shared memory, message queues and console out.
  [deploy/retina-node-service.yml](../deploy/retina-node-service.yml) adds the
  rest of a node's hardening.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAGNETOMETER_BUS` | `/dev/i2c-1` | `/dev/i2c-N`, `i2c:N`, `N`, or `tcp://host:port` for the simulator |
| `MAGNETOMETER_I2C_ADDRESS` | `auto` | `auto` probes 0x20–0x23, or one of those addresses |
| `MAGNETOMETER_I2C_FRAMING` | `repeated-start` | or `stop`, a STOP between pointer and read as PNI's manual draws it |
| `MAGNETOMETER_MODE` | `poll` | `poll` or `continuous` |
| `MAGNETOMETER_SAMPLE_RATE_HZ` | `1` | 0.01–600; above poll mode's limit (~87 Hz at 200 cycles) it runs at the limit, with a warning |
| `MAGNETOMETER_CYCLE_COUNT` | `200` | 30–1000 |
| `MAGNETOMETER_SELF_TEST` | `true` | run the self test whenever the sensor is found |
| `MAGNETOMETER_DATA_DIR` | `/data` | database and `status.json` |
| `MAGNETOMETER_RAW_RETENTION_DAYS` | `7` | 0.01–3650 |
| `MAGNETOMETER_ROLLUP_RETENTION_DAYS` | `365` | 1–36,500, at least the raw retention |
| `MAGNETOMETER_MAX_DB_MB` | `1024` | 16–1,000,000; a size cap on the data |
| `MAGNETOMETER_FLUSH_INTERVAL_S` | `5` | 0.2–300 seconds between disk writes |
| `MAGNETOMETER_NODE_CONFIG` | `/config/config.yml` | retina-node's merged config, read for `location.rx` |
| `MAGNETOMETER_LATITUDE`, `_LONGITUDE`, `_ALTITUDE_M` | unset | override the location; latitude and longitude together |
| `MAGNETOMETER_ORIENTATION_WINDOW_S` | `60` | 5–3600 seconds of samples for the orientation |
| `MAGNETOMETER_HOST`, `MAGNETOMETER_PORT` | `0.0.0.0`, `3030` | where the page is served |
| `MAGNETOMETER_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |

A value with no safe reading is a configuration error, and sampling stays off
until it is fixed. A value with one is used that way, with a warning. To
change a value, change the container's environment and restart it; the page
has no settings form, because on a node the settings belong with retina-gui.

## API

All read-only JSON, except `/healthz`.

| Endpoint | Returns |
| --- | --- |
| `GET /api/series?window=<s>&points=<n>` | chart data for the last `window` seconds (10 s to 400 days) |
| `GET /api/series?start=<ms>&end=<ms>&points=<n>` | the same for a range, at most 400 days long |
| `GET /api/series?…&since=<ms>` | only the points from `since` on |
| `GET /api/latest` | the newest sample and its age |
| `GET /api/health` | the health document |
| `GET /api/orientation` | the orientation estimate, reference field and notes |
| `GET /api/config` | the effective configuration, the location and the latest sessions |
| `GET /healthz` | `ok` as plain text while the process serves requests |

`/api/series` returns `t` (ms) and `min`/`mean`/`max` arrays for `x`, `y`, `z`
and `b` (|B|), so a short spike still shows in a month-long view.

## Health

`/api/health` and `/data/status.json` carry the same document, in
retina-telemetry's format.

| State | Meaning |
| --- | --- |
| `starting` | looking for the sensor, or waiting for the first sample |
| `ok` | sampling |
| `degraded` | sampling, but with a failed read, a failed self test, a setting replaced by a safe one, failing storage or missed poll ticks |
| `stalled` | no sample for five sample periods (at least 10 s) |
| `no_bus` | the I2C device does not exist or cannot be opened; the detail says what to enable |
| `no_sensor` | the bus is there but no RM3100 answered |
| `config_error` | a setting has no safe reading; sampling is off |

The document also has the last successful sample, read error counts, recent
errors, the self-test result, the sample rates and storage use. There is no
Docker `HEALTHCHECK`: on a node without a sensor it would hold up the Mender
install of the whole stack.

## Storage

One SQLite file, `/data/magnetometer.sqlite`, in WAL mode, holds the raw
samples, a min/mean/max summary per minute, and a log of sampling sessions.
Every 10 minutes, rows older than their retention are deleted, and past the
size cap the oldest data goes first. A week of raw samples at 1 Hz is about
23 MB, and a year of minute summaries about 66 MB. Writes are batched every
5 s, and a power cut can lose about the last half minute, never the database.
A database that cannot be read is moved aside, never deleted, and a new one
is started.

## Tests

`uv run pytest`, or `tools/check.sh` for every gate. The driver is tested
against a scripted bus that checks every byte it sends, and against the
simulator's register model. What still needs a physical sensor is in
[docs/hardware-verification.md](../docs/hardware-verification.md).
