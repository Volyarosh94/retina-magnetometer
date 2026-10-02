# Design note: RM3100 magnetometers for RETINA

30 September 2026, revised 2 October 2026.

The work has three parts. Part A, the node app (`retina_magnetometer/`), reads
an RM3100 over I2C, stores and charts what it measures, works out the sensor's
orientation and reports its health. Part B, the simulator (`rm3100_sim/`), is
a simulated RM3100 that the app drives with no code change. Part C, in the
retina-simulation and retina-server pull requests, puts magnetometers beside
the fleet simulator's radar nodes, runs detection on the server and shows
them on the `/sim` map. The READMEs explain how to use each part.

## Assumptions

No one on the RETINA team was asked during the build. Where the requirements
left a choice open, the existing code and its documented defaults decided it.
Each choice is a setting or a small change.

| # | Assumption | To change it |
| --- | --- | --- |
| A1 | The sensor is on `/dev/i2c-1` at one of 0x20–0x23; the app probes all four | `MAGNETOMETER_BUS`, `_I2C_ADDRESS` |
| A2 | 1 Hz in poll mode at 200 cycles, the chip's reset value (15 nT noise) | `_SAMPLE_RATE_HZ`, `_MODE`, `_CYCLE_COUNT` |
| A3 | Seven days of raw samples, a year of minute summaries, a 1 GB cap | `_RAW_RETENTION_DAYS`, `_ROLLUP_RETENTION_DAYS`, `_MAX_DB_MB` |
| A4 | The page is read-only and unauthenticated on the LAN, like tar1090; settings are environment variables | [adding-to-a-retina-node.md](adding-to-a-retina-node.md), section 5 |
| A5 | Port 3030 on the `blah2` bridge | the compose entry |
| A6 | The position is `location.rx` in retina-node's merged `config.yml`, where retina-tracker reads it | `_LATITUDE`, `_LONGITUDE`, `_ALTITUDE_M` |
| A7 | The heading assumes the downward axis is vertical; one field vector fixes only two of three rotations | an accelerometer |
| A8 | Health is published as `status.json` in retina-telemetry's format, with no container health check | — |
| A9 | The node's clock is right (owl-os runs chrony); retention ages data by it | `chronyc tracking` ([hardware-verification.md](hardware-verification.md), item 22) |
| A10 | A power cut can lose the last half minute of samples, never the database (WAL, `synchronous=NORMAL`) | `PRAGMA synchronous` in `storage.py` |
| B1 | The daily variation is fitted to the FRD and BSL observatories, so it stands for about 30–38° N in eastern North America | a scenario's `diurnal` block |
| B2 | A UAP is a permanent 1e9 A·m² dipole with a fixed direction, on a straight, level track; aircraft and drones contribute nothing | a scenario's `uap_pass` events |
| B3 | Table 3-1's noise (30, 20, 15 nT at 50, 100, 200 cycles) is one axis's standard deviation as the registers report it, rounding included | a scenario's `noise_scale` |
| C1 | A UAP is a world object spawned as `object_type == "anomalous"`; an airliner that turns anomalous stays non-magnetic | `magnetometer.py` |
| C2 | At most one magnetometer per roof, beside a radar node; eight by default, spread across the fleet | `mag_n_nodes`, `mag_placement` |
| C3 | Detection runs on the server, over the whole network | — |
| C4 | Readings reach the server on a push of their own; nothing touches TCP 3012 or `/v1/nodes` | — |
| C5 | Magnetometer data is for administrators, polled, and never on the aircraft WebSocket | — |
| C6 | The default threshold is 5.5σ | `mag_threshold_sigma` |

## Part A: the node app

The app uses Flask, waitress and Plotly, as retina-gui does, so it runs and
deploys like its neighbours with no build step. Plotly is vendored because
nodes may not reach a CDN.

It has its own RM3100 driver in user space (i2c-dev through smbus2). The
driver probes, checks REVID 0x22, stops continuous mode in case an earlier run
left it on, writes the cycle counts and reads them back, runs the built-in
self test, and polls STATUS for data-ready, which needs no DRDY wire. The I2C
framing (repeated start or STOP) is a setting, because the manual and working
drivers disagree.

The app never exits because of the hardware. A missing bus or sensor, NACK
bursts and a sensor that browns out are states it reports and recovers from,
backing off from 1 s to 30 s. A brown-out resets the chip without any transfer
failing, so the app reads back the cycle count and TMRC before storing
samples, and drops those taken since the last good read-back if the chip has
reset. A bad setting, an unreadable database or an unwritable data directory
is reported on the page; only a port it cannot listen on ends the process.

One SQLite file in WAL mode holds raw samples, per-minute summaries and a
session log, bounded by age and by size. Writes are batched every 5 s. Charts
are downsampled on the server to min/mean/max per point, so a 30-day view of
2.6 million samples arrives as about 2,000 points and a five-second
disturbance still shows.

Orientation compares the median of the last minute with WMM2025 (pygeomag,
which reproduces all 100 of NOAA's test values) and reports the downward axis,
a lower bound on the tilt, and the heading against magnetic and true north
with an uncertainty.

The deployment cannot break a node: the compose entry binds `/dev` with a
device cgroup rule instead of a `devices:` entry, has no health check, and
runs the container with every capability dropped and a read-only root
filesystem. [adding-to-a-retina-node.md](adding-to-a-retina-node.md) explains
each choice.

## Part B: the simulator

The requirements allowed simulation at the register level or at the
measurement level. This one works at the register level, because a
measurement-level source would never run the driver, its timing or its fault
handling, the code most likely to be wrong on hardware. The chip model
answers each I2C transfer as the manual says the chip would: POLL, continuous
mode, DRDY, conversion times, refused writes, the self test and REVID. A
scenario can inject NACKs, a disconnect, a stuck DRDY or a brown-out. The app
reaches the model by setting `MAGNETOMETER_BUS=tcp://host:9100`, and a test
checks that it never imports the simulator.

The field is the sum of WMM2025, a crustal offset, the solar-quiet daily
variation, storms with pulsations, local steps such as a parked car, and UAP
passes as dipoles. The sensor sees it through its mounting, a hard-iron offset
and gain errors, then adds noise and rounds to counts. The datasheet's noise
can only have been measured on the output, which is in counts, so the noise
added before rounding is sized for the rounded output to match it (B3).

Every random number is a hash of the seed, a stream name and an index, so the
field at an instant does not depend on how often the app asks, and adding an
event changes no other event's numbers. Scenarios are YAML, strictly
validated.

## Part C: magnetometers in the simulated network

retina-server's design note on the simulated magnetometers
(`docs/design-notes/2026-09-30-simulated-magnetometers.md`) describes the
detector step by step and maps each requirement to where it is met.

retina-simulation samples every magnetometer once per world step (1 Hz): the
background field, the dipole field of each anomalous world object, and RM3100
noise. It pushes the readings, with the truth behind them for scoring, to
`POST /api/sim/magnetometer/push`. retina-server runs detection over the whole
network, and the `/sim` map polls `GET /api/sim/magnetometers` every 2 s to
show each magnetometer's state, recent readings and events. The settings
(count, placement, cycle count, a storm, the threshold, the common-mode switch
and a flyby) are `mag_*` keys on the Physics Layer page. All of it runs only
under `SYNTHETIC_FLEET_ENABLED`, and none of it reaches the radar pipeline,
`/v1/nodes` or the aircraft WebSocket.

The detector subtracts each node's static level and the network's common mode,
the median of the other nodes' deviations, which takes out the daily
variation and storms. It then runs a matched filter on the three Anderson
functions of a dipole pass at six time scales from 2 to 64 s (Anderson 1949;
Sheinker et al. 2009). Under noise alone the captured energy is χ² with 9
degrees of freedom, and its tail probability is reported as a score in σ. An
event carries the time of closest approach, the strength in nT, the
score, a confidence and a range band.

The benchmark, `scripts/magnetometer_false_alarms.py` in retina-server, sends
seeded readings through the server's real ingest path. Its network is eight
magnetometers about 20 km apart around Greenville, SC, at 200 cycles:

- With no UAP present, the default 5.5σ threshold raised 1 false alarm in
  9,194 node-hours. The detector's false-alarm model, A · z² · P(Z > z) per
  node-hour with A = 470, falls inside the measured 95 % interval at every
  threshold from 3σ to 6σ and predicts 0.00027 per node-hour at 5.5σ, about one false alarm every 19 days
  across eight magnetometers. At 5σ it is about one every 1.5 days (measured:
  25 in 9,194 node-hours).
- An intense storm (Dst −150 nT, with pulsations) raised no false alarm at six,
  seven or eight magnetometers with the common mode removed, and about one per
  node-hour with it left in.
- Every pass of a 1e9 A·m² dipole within 1 km was found. Nine in ten were found
  out to a slant range of 1.77, 1.53 and 1.07 km at 30, 100 and 300 m/s. A
  magnetometer is a local tripwire with a reach of about 1.5 km.
- Core placement, an option that puts magnetometers within 3 km of one
  another, is the known weak spot. About one pass in 22 there raises a false
  alarm at a neighbouring node, because a pass leaks into the neighbours'
  common mode before its own node scores high enough to be left out. The
  default spread placement keeps magnetometers 4–118 km apart, and on the
  benchmark's grid 1,260 passes raised none. The next step is to take a
  node's readings out of the others' medians back to before it scored 3σ.

## Alternatives turned down

Grafana with a time-series database would draw the charts, but the charts are
a small part of the app. It means two more services to pin in retina-node's
compose file and carry in the Mender artifact on a Pi that already runs the
radar stack, plus a second web UI with its own login unless anonymous access
is turned on. Neither knows the
RM3100, so the driver, recovery, health and orientation would still have to
be written. SQLite holds 1–10 Hz in one file and keeps the minute summaries a
month-long chart needs. Grafana fits better on the server, where
retina-server's `docs/alerting.md` already defers dashboards to a later
monitoring project; a node's minute summaries could feed it from
`/api/series`.

HamSCI's [rm3100-runMag](https://github.com/HamSCI/rm3100-runMag) and
[mag-recorder](https://github.com/HamSCI/mag-recorder) record the sensor to
files for the PSWS archive. Neither charts the data on the node, works out the
orientation or reports health the way RETINA's services do, and neither runs
its driver against a simulated chip, which Part B needs. runMag's self test
is marked not implemented, and its cycle-count read-back is an option run
once, before sampling. Wrapping it would mean parsing its log lines and supervising a
second process to save a few hundred lines of Python. The driver follows
HamSCI where it has hardware evidence: REVID 0x22, the `stop` framing option,
polling by default and PNI's gain formula.

The Linux IIO driver (`rm3100-i2c`, in the kernel since 5.0) is not built by Raspberry Pi OS kernels,
would need a device-tree overlay, and has no identity check, self test or
cycle-count read-back.

For Part C, a per-node threshold on |B| or on a high-pass filtered signal
would either miss everything beyond about a kilometre or alarm on every
storm: the daily variation and storms are several times the noise, and storm
pulsations share a pass's time scales. Only the network can remove what its
nodes have in common, so detection runs on the server. Running it in the
simulator would also have the simulator grading itself.

## What needs hardware

Nothing here has touched an RM3100. The driver was tested byte by byte
against a scripted bus and against the simulator's register model, and both
encode a manual that contradicts itself in places.
[hardware-verification.md](hardware-verification.md) lists 25 checks for the
first power-up and the days after, each with what the code assumes meanwhile.
The ones most likely to need a change are:

- the REVID value, which the manual does not give; every driver that checks it
  uses 0x22 (item 2);
- the I2C framing on the Pi 5's RP1 adapter, which is a setting (item 4);
- what clears DRDY, in particular whether the pointer write before a STATUS
  read does (item 8);
- the reset value of TMRC, which is how the app detects a brown-out (items 10
  and 23);
- poll mode's 4.3 ms allowance per sample for transfers and the host, an
  estimate (item 25);
- gain and axis polarity, since some boards read about 1.3× off PNI's gain
  formula and some builds reverse an axis (items 12 and 13);
- whether the cgroup rule opens `/dev/i2c-1` on owl-os (item 17).

The detector's numbers come from simulated data. Real noise may drift with
temperature, which would make the long time scales alarm more often, so a
first deployment should log temperature beside the sensor (item 15). Real
sites have magnetic clutter, such as cars and power lines, that no common mode
removes. The field a storm induces differs with ground conductivity, so across
tens of kilometres storms leave larger residuals than here. The false-alarm coefficient has to be fitted again on recorded quiet
data before a threshold on a real network means anything.

## What a real node needs

[adding-to-a-retina-node.md](adding-to-a-retina-node.md) has the detail.

1. owl-os has to enable I2C (`dtparam=i2c_arm=on` and `i2c-dev` loaded at
   boot), which is an owl-os change and release.
2. The sensor goes on the Pi's 3.3 V header pins, level, as far from the Pi,
   the SDR and the power supply as the cable allows.
3. retina-node needs the service entry
   ([`deploy/retina-node-service.yml`](../deploy/retina-node-service.yml)), a
   `sed` clause in the Mender artifact script, the `.env.example` lines and a
   row in the port table.
4. The entry pulls `ghcr.io/offworldlabs/retina-magnetometer`, and a release
   publishes under the account that owns the repository, so the repository
   has to move to offworldlabs before the first release.
5. Optionally, a retina-gui card for port 3030, a node-infra tunnel route, and
   config-merger emitting `MAGNETOMETER_*` settings so that retina-gui becomes
   the settings page.

Real readings have no way to reach the server yet. `/v1/nodes` carries radar
data only and was not to change, so detection on a real network needs a new,
versioned contract beside it. The app already keeps one sample per second on
the second, which is what a network detector needs.

## How it was tested

This repository has 837 tests at 96 % coverage with branches, and the demo
recovered the simulated mounting to 37.1° for a 37° mounting. retina-server
has more than 250 detector tests. End to end, a flyby requested from the `/sim` map was detected by the magnetometer
it passed and shown on the map. Screenshots of the app and of the `/sim` layer
are in [`docs/screenshots`](screenshots/).
