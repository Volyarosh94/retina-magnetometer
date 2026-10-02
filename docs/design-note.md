# Design note: RM3100 magnetometers for RETINA

30 September 2026, revised 1 October 2026. Covers the three parts of the
magnetometer work:

| Part | What | Where |
| --- | --- | --- |
| A | A node app that reads an RM3100 over I2C, stores and shows what it measures, works out its orientation and reports its health | this repository, `retina_magnetometer/` |
| B | A simulated RM3100 that the app drives without a code change | this repository, `rm3100_sim/` |
| C | Magnetometers beside the fleet simulator's radar nodes, detection on the server, and the `/sim` map | the retina-simulation and retina-server pull requests |

The READMEs say how to use each part. This note records why things are the way
they are:

- the assumptions made along the way;
- the main design choices, and the alternatives turned down;
- what the detector measured;
- what only hardware can settle;
- what a real node needs;
- how it was verified.

## Assumptions

Nobody from the RETINA team was asked during the build. Wherever the
requirements left a choice open, the documented defaults and the existing code
decided it, and every such choice is listed here. Each is a setting or a small
change, and any of them may be overturned.

**Part A, the node app**

| # | Assumption | Why | To change it |
| --- | --- | --- | --- |
| A1 | The sensor is on the Pi's header bus, `/dev/i2c-1`, at one of 0x20–0x23 | That is where a breakout on GPIO 2/3 appears; the app probes all four addresses | `MAGNETOMETER_BUS`, `MAGNETOMETER_I2C_ADDRESS` |
| A2 | 1 Hz, polled, at 200 cycles | 200 is the chip's reset value and the datasheet's reference point (15 nT noise). The detector's shortest time scale is 2 s, so 1 Hz resolves a pass, and a week of samples is about 23 MB. Poll mode keeps up to about 87 Hz at 200 cycles, continuous mode up to 147 Hz | `MAGNETOMETER_SAMPLE_RATE_HZ`, `_CYCLE_COUNT`, `_MODE` |
| A3 | Seven days of raw samples, a year of per-minute summaries, and at most 1 GB of data | Retention has to be bounded, and no figures were given; these fit a node's SD card with room to spare | `MAGNETOMETER_RAW_RETENTION_DAYS`, `_ROLLUP_RETENTION_DAYS`, `_MAX_DB_MB` |
| A4 | The page is read-only and unauthenticated on the LAN, like tar1090 and blah2's page. Settings are environment variables | Writes on an unauthenticated port would let anyone on the network reconfigure the sensor; on a node, settings belong behind retina-gui's login | See [adding-to-a-retina-node.md](adding-to-a-retina-node.md), section 5 |
| A5 | Port 3030, on the `blah2` bridge | Next to retina-spectrum's 3020 in retina-node's port table | the compose entry |
| A6 | The node's position is retina-node's merged `config.yml` (`location.rx`) | It is where retina-tracker reads it; environment variables override it | `MAGNETOMETER_LATITUDE`, `_LONGITUDE`, `_ALTITUDE_M` |
| A7 | The heading assumes the downward axis is vertical | One field vector fixes two of the three rotations. Only an accelerometer would fix the third, and the page says so | Fit an accelerometer |
| A8 | Health is published as `status.json` in retina-telemetry's format, and there is no container health check | Mender waits for healthy containers, and a node without a sensor must not block the stack's install | — |
| A9 | The node's clock is right. Retention ages data by it, and every sample is stamped with it | owl-os disciplines the clock with chrony, for the radar. A clock set ahead ages data out early: a week ahead empties the raw samples at the next prune, a year ahead the minute summaries too. One stepped back writes the samples that follow over any stored at the same milliseconds, and their minutes are summarised again | Check `chronyc tracking` before the first run ([hardware-verification.md](hardware-verification.md), item 22) |
| A10 | A power cut may cost the last half minute of samples, never the database: SQLite runs in WAL mode with `synchronous=NORMAL` | The setting SQLite's documentation gives as the best balance for WAL mode, which it calls safe from corruption. `FULL` would sync the WAL at every flush, every 5 s, making it the card's most frequent sync, to save at most half a minute of data | `PRAGMA synchronous` in `storage.py` |

**Part B, the simulator**

| # | Assumption | Why | To change it |
| --- | --- | --- | --- |
| B1 | The app reaches the simulated chip over TCP, one JSON line per I2C transfer | The simulated data has to feed the app with no code change, so the app changes one variable and nothing else | — |
| B2 | The daily variation is fitted to the FRD and BSL observatories | They are the INTERMAGNET observatories either side of Greenville: FRD at 38.2° N, 77.4° W, and BSL at 30.4° N, 89.6° W. So the fit stands for roughly 30–38° N in eastern North America, and is a plausible shape elsewhere, not a prediction | a scenario's `diurnal` block |
| B3 | A UAP is a permanent dipole of 1e9 A·m² with a fixed direction per pass, on a straight, level track | The specified magnetic model. There is no induced magnetisation and no eddy currents | a scenario's `uap_pass` events |
| B4 | Aircraft and drones contribute nothing | The specified model: their moments are orders of magnitude smaller | — |
| B5 | Table 3-1's noise (30, 20 and 15 nT at 50, 100 and 200 cycles) is the standard deviation of one reading on one axis, white and Gaussian, as the registers report it: rounding to counts included | PNI gives the figure as "Noise", with no statistic and no bandwidth, and it can only have been measured on the output, which is in counts. So the simulator adds Gaussian noise of √(σ² − LSB²/12) before rounding (σ the table's figure, LSB one count), and the rounded output has the table's spread. The fleet's magnetometers do the same. Detection range and the false-alarm calibration both scale with this reading | a scenario's `noise_scale` |

**Part C, the fleet and the server**

| # | Assumption | Why | To change it |
| --- | --- | --- | --- |
| C1 | A UAP is any object the world spawned as `object_type == "anomalous"`. An airliner that turns anomalous mid-flight (a hijack or spoof scenario) stays non-magnetic | That is the world's existing anomaly class; a hijacked airliner is still an airliner | `magnetometer.py` |
| C2 | At most one magnetometer per roof, beside a radar node and named after it (`mag-<radar node>`). Eight by default, spread across the fleet | Magnetometers "should always be located alongside a radar node"; two radar nodes on one roof share one site | `mag_n_nodes`, `mag_placement` |
| C3 | Detection runs on the server, over the whole network | The daily variation and storms are common to the network, and only the network can take them away (below) | — |
| C4 | Readings reach the server on a push of their own, as ground truth and ADS-B already do. Nothing touches TCP 3012 or `/v1/nodes` | The server treats every TCP node as a radar node, and the `/v1/nodes` contract must not change | — |
| C5 | Magnetometer data is for administrators only, on a polled endpoint, never on the aircraft WebSocket. Positions go through the same `public_latlon` rule as radar nodes | The socket is unauthenticated and broadcast. A magnetometer sits on a radar node's roof, and must not publish a location the radar node itself would not | — |
| C6 | The default threshold is 5.5σ | On the model fitted to the benchmark, that is about one false alarm every 19 days across eight magnetometers, against one every 1.5 days at 5σ, and every pass within 1 km is found | `mag_threshold_sigma` |
| C7 | The settings behave like the existing simulation keys: they live in memory, and the state snapshot restores them. The threshold and the common-mode switch are the server's own and take effect at once; the fleet applies the rest in-process at its next poll (every 5 s) | Settings "following the style of the existing simulation settings" | — |
| C8 | The simulator sends the truth behind each reading (the UAP field at the node) beside the reading | It is used to score events, never to find them | — |

**Conventions**

- This repository follows the node repositories: commit subjects
  `YYYYMMDD - Subject`, branches `YYYYMMDD-subject`, and uv with a committed
  lock per the org's Python app standard. retina-simulation and retina-server
  follow their own convention (plain imperative subjects, `feat/` branches).
- The image is `ghcr.io/offworldlabs/retina-magnetometer`, released the way
  retina-telemetry is, once this repository lives in offworldlabs
  ([What a real node needs](#what-a-real-node-needs), item 4).

## Part A: the node app

### What it is built on, and why

- **Flask, waitress and Plotly, as retina-gui is.** retina-gui is Flask and
  draws with Plotly 3.7.0. A page on a node should look, run and deploy like
  its neighbours, without a build step. Plotly's basic bundle is vendored,
  because nodes sit on networks that may not reach a CDN.
- **Its own RM3100 driver, from user space (i2c-dev through smbus2).** The
  driver probes, identifies (REVID 0x22), stops continuous mode in case a
  previous run left it on, writes the cycle counts and reads them back, runs
  the built-in self test, and then polls STATUS for data-ready. Polling STATUS
  needs no GPIO line, where the DRDY pin would need a wire and a GPIO the
  container would have to be given. The framing, repeated start or STOP, is a
  setting, because the manual and the field disagree
  ([hardware-verification.md](hardware-verification.md), item 4).
- **It recovers; it never exits.** No bus, no sensor, NACK bursts and a
  sensor that browns out and resets are all states it reports and leaves on
  its own. It backs off (from 1 s, doubling to 30 s), finds the sensor again
  after three failures in a row, and reads back what it set on the chip (the
  cycle count, and TMRC) before any sample is stored, after each one at up to
  1 Hz and once a second at faster rates. A brown-out resets the chip without
  a single transfer failing, so the samples since the last good read-back are
  dropped rather than stored at the wrong gain, or as zeros. What it finds at
  start does not stop it either. A setting with no safe reading turns
  sampling off and says why on the page; one with a safe reading is used that
  way, with a warning. A database it cannot read is moved aside and kept, and
  a data directory it cannot write is reported until it can. Only a port it
  cannot listen on ends it, as there would be no page to report on.
- **One SQLite file.** It holds raw samples, per-minute summaries (count and
  min/mean/max per axis and of |B|) and a session log, in WAL mode with
  incremental vacuum. Retention is bounded twice, by age and by size. Over
  the cap the oldest data goes first, raw samples the minute summaries already
  cover before the summaries themselves. The size counted is the data's own,
  so a reader holding the file open cannot make the cap delete more. Writes
  are batched every 5 s through one connection kept open, so a flush appends
  to the WAL without a sync; the database file is written and synced only at
  checkpoints. A clean stop flushes and closes the database; a power cut can
  lose about the last half minute, never the database (A10).
- **Charts that keep spikes.** Downsampling happens on the server, as
  min/mean/max per point. A 30-day view of 2.6 million samples arrives as
  ~2,000 points, and a five-second disturbance still shows in it as a band.
  From a day up the scales fit the mean line, so passes do not flatten the
  daily variation; the band runs off the scale where they were. Buckets sit
  on a fixed grid, so a refresh fetches only what is new since the last one.
  A zoom fetches the zoomed range again at full resolution.
- **Orientation from the Earth's field.** The app compares WMM2025 (through
  pygeomag, which ships NOAA's coefficient file unchanged and reproduces all
  100 official test values) with the last minute of samples. From that it
  gives:
  - which axis points down;
  - a lower bound on the tilt;
  - the heading against magnetic and true north;
  - an uncertainty combining the model's declination error, the tilt bound
    and any magnitude mismatch.

  Where the field's horizontal part is under 2,000 nT, NOAA's blackout zone
  for compasses near the magnetic poles, it gives no heading, and says why.
- **A deployment that cannot break a node.** Two choices do this:
  - a `/dev` bind with a device cgroup rule, not `devices:`;
  - no container health check.

  The container runs as root with every capability dropped, a read-only root
  filesystem and `no-new-privileges`. Over the `/dev` bind it has a private
  `/dev/shm` and `/dev/mqueue` and no host console, and its cgroup rule admits
  `/dev/i2c-1` alone. [adding-to-a-retina-node.md](adding-to-a-retina-node.md)
  explains each choice.

### Turned down

The requirements suggested two things to build on: Grafana with a time-series
database, and HamSCI's RM3100 Pi magnetometer software. Both were weighed, and
neither was used.

**Grafana with a time-series database** (InfluxDB or Prometheus, with Grafana,
on the Pi). Grafana would draw the charts, but the charts are a small part of
what the app does. It is two more services, each with its own storage, memory and image to
pin in retina-node's compose file and carry in the Mender artifact, on a Pi
that already runs the radar stack. It is also a second web UI beside
retina-gui's, with a login of its own unless anonymous access is turned on.
And neither of them knows the RM3100: the driver, the recovery, the health
model and the orientation would all still have to be written, as a collector
feeding the database. SQLite holds 1–10 Hz with room to spare, in one file to
back up or inspect, and keeps the min/mean/max minute summaries that a
month-long chart needs. Grafana fits better on the server: retina-server's
`docs/alerting.md` defers metrics history and dashboards (VictoriaMetrics with
Grafana) to a later monitoring project. If that comes, a node's minute
summaries can feed it from the SQLite file or `/api/series`.

**HamSCI's RM3100 software.** The Personal Space Weather Station
magnetometers run `runMag` ([HamSCI/rm3100-runMag](https://github.com/HamSCI/rm3100-runMag),
Dave Witten's C program). It polls the RM3100 over i2c-dev, by default once a
second, reads the MCP9808 thermometers on its board pair, and appends one JSON
object per sample to a log file it rolls at 00:00 UTC. HamSCI's newer
[mag-recorder](https://github.com/HamSCI/mag-recorder) is a Python supervisor
around Witten's `mag-usb`, which reads the sensor through a USB-to-I2C
adapter rather than the Pi's header. It spools JSON lines and uploads a daily
file to the PSWS network, and its README lists its validation against a real
RM3100 as still to come. Both write files for the PSWS archive. Neither draws
the data on the node, works out the sensor's orientation from the field, or
reports its health the way RETINA's services do. Neither runs its driver
against a simulated chip either: mag-recorder's simulator stands in for
`mag-usb` with synthetic lines, so the code that talks to the sensor never
runs, and that code is what Part B has to exercise. runMag's self test is
marked not implemented, and its cycle-count read-back is an option run once,
before sampling; here the self test runs whenever the sensor is found, and no
sample is stored before the cycle count has been read back. Wrapping runMag
would have meant parsing its log lines and supervising a second process, to
save a driver of a few hundred lines of Python.

What this app took from HamSCI is evidence of what works on hardware. runMag
expects REVID 0x22, writes the register pointer and reads with a STOP between,
polls by default and converts with PNI's gain formula; this driver's
defaults, or its `stop` framing option, do the same
([hardware-verification.md](hardware-verification.md), items 2 and 4).
runMag's undocumented "NOS" register (0x0A) is why the simulator accepts a
write there. The node guide's advice on distance, and the checklist's on
temperature, come from HamSCI's stations: the sensor buried in a pipe for a
steady temperature, at the end of 30 m or more of differential I2C, with a
thermometer beside it.

The other alternatives:

| Alternative | Why not |
| --- | --- |
| The Linux IIO driver (`rm3100-i2c`, in the kernel since 5.0) | Raspberry Pi OS kernels do not build it, and it has no identity check, no self test and no read-back of the cycle count. Each node would also need a device-tree overlay |
| Flat CSV or Parquet files | No range queries, and retention and compaction would have to be written by hand |
| FastAPI with a React front end | Unlike the node's other pages; needs a build step |
| Pushing samples to the page over a WebSocket | At 1 Hz and a handful of viewers, polling every 2 s is simpler to serve, each refresh brings only the new points, and it survives proxies and reconnections for free |
| A Docker `HEALTHCHECK` | Mender's install waits for every container to be healthy, so a node without a sensor fitted would fail to install the whole stack |
| `devices: [/dev/i2c-1]` in compose | `docker compose up` fails on a node without the device, which today is every node |
| A settings form on the page | See A4 |
| Hard- and soft-iron calibration | It needs the sensor turned through a sphere, which is impossible once it is mounted on a mast. The magnitude check flags a large offset instead, and the fix is to move the sensor |

## Part B: the simulator

### What it is built on, and why

- **A register-level model of the chip.** It sits behind the same bus
  interface the driver uses on a real node. Every transfer the driver makes is
  answered as the chip would answer it, following the manual:
  - POLL, CMM and TMRC;
  - DRDY set and cleared by the manual's rules;
  - conversion times per cycle count;
  - writes the chip cannot carry out NACKed on the wire, with the HSHAKE NACK
    bits;
  - the self test;
  - REVID;
  - the other addresses NACKing.

  I2C faults come back as the errno a Linux adapter returns. A scenario can
  also brown the chip out: its registers return to their defaults without a
  single transfer failing. The app points at the model with one variable,
  `MAGNETOMETER_BUS=tcp://host:9100`, and a test enforces that the app never
  imports the simulator.
- **A field that is the sum of documented sources:**
  - WMM2025;
  - the crust;
  - the solar-quiet daily variation (fitted to FRD and BSL, blended by
    season and solar cycle, with day-to-day variability);
  - storms with pulsations;
  - local steps (a car parked by the mast);
  - UAP passes as dipoles.

  The sensor sees that field through its mounting, a hard-iron offset and
  per-axis gain errors, plus noise, and it is quantised to the chip's counts.
  The noise is sized so that, rounding included, it is the datasheet's for the
  cycle count: Table 3-1's figure was measured on the chip's output, which is
  in counts (B5).
- **Reproducible by construction.** Every random number is a hash of the
  seed, a stream name and an index; nothing draws from a shared generator. So
  the field at an instant does not depend on how often, or in what order, the
  app asks for it. Adding, removing or reordering an event changes no other
  event's numbers: each event's draws are keyed by its own identity, its id
  or its type and time.
- **Scenarios in YAML, strictly validated.** An unknown key is an error that
  names its path. A typo that is silently ignored produces data that looks
  right and is not.

### Turned down

| Alternative | Why not |
| --- | --- |
| Replaying CSV files into the app's storage | The driver would never run, and neither would its timing or its faults, which is the part most likely to be wrong on hardware |
| A mock at the driver's Python API | The same objection, and the app would need a code path for it, where the simulated data has to feed it with none |
| Linux `i2c-stub`, or a CUSE character device | A kernel module or root on the host, no macOS, and neither can model conversion time, DRDY or NACKs |
| A random walk or a sinusoid for the field | No WMM direction for the orientation check to recover, and not the real shape of the daily variation that detection has to survive |

## Part C: magnetometers in the simulated network

retina-server's design note on the simulated magnetometers
(`docs/design-notes/2026-09-30-simulated-magnetometers.md`) has a table of
each Part C requirement and where it is met, and the fuller account of the
choices below: the settings and how they join the Physics page, flybys, and
each step of the detector with what was tried first.

### Where things run

```
retina-simulation                          retina-server
─────────────────                          ─────────────
SimulationWorld ── anomalous objects ─┐
                                      ▼
MagnetometerArray.sample()  per world step (1 Hz):
  WMM2025 + crust + Sq + storm
  + Σ UAP dipoles + RM3100 noise,
  quantised
        │  POST /api/sim/magnetometer/push       services/sim_magnetometer.py
        └──── every 1 s, simulator key ────────▶ MagnetometerNetwork.ingest()
                                                   static level, noise, common mode,
                                                   matched filter, events, scorecard
                                                        │
   /sim map (admin) ◀──── GET /api/sim/magnetometers ───┘  every 2 s
   Physics Layer    ────▶ PUT /api/simulation/config (mag_*)
                    ────▶ POST /api/sim/magnetometers/flyby
```

Every magnetometer route (the push, the two reads and the flyby) exists only
under `SYNTHETIC_FLEET_ENABLED`. `/api/simulation/config` (GET and PUT) is
mounted everywhere, as it always was, but without the flag it neither shows
nor accepts the `mag_*` settings. Nothing reaches:

- the radar pipeline;
- `/v1/nodes` (its generated contract is unchanged);
- the aircraft WebSocket;
- the ground-truth or anomaly stores that radar scoring reads.

A flyby rides in the runtime config as `mag_flyby`, with a number and the time
of the request, but it is a one-off command: the route leaves the config's
`_updated_at` stamp alone, so the Physics page does not read it as somebody
else's change of settings. The fleet looks for it at every poll and flies
each request once. The UAP is an ordinary anomalous world object on a
straight, level track, so the radar nodes see it too.

### The detector

Once a second, for each magnetometer:

1. **Static level.** The trimmed mean (middle half) of its last 30 minutes,
   first set from two minutes of readings. It is refreshed every 30 s, on the
   same second at every node, so the refresh itself is common to the network.
2. **Noise.** Measured from the node's own residual over the last
   10 minutes: the standard deviation of first differences, outliers clipped
   (never tighter than the sensor's own noise allows), divided by √2, and
   never below 0.3 times the datasheet figure. It starts again when the cycle
   count changes or the common mode is switched in or out, and the node
   scores nothing until it has a minute of the new residual. An estimate from
   fewer than ten minutes of readings is less certain: over n readings the
   statistic is 9·F(9, 2(n − 1)), not χ²₉, and on one minute noise alone
   crosses what χ²₉ calls 5.5σ 23 times as often as on ten. So every
   statistic is first mapped to the one with the same tail probability over
   ten minutes of readings, and the false-alarm rate holds from the first
   scored minute.
3. **Common mode.** Each node has the per-axis median of the *other* nodes'
   deviations subtracted from it:
   - the daily variation is 30–90 nT and a storm about 105 nT at the Dst
     minimum (up to about 113 nT with the pulsations), the same everywhere
     to within a few nT across a fleet;
   - the median keeps a UAP near one node from leaking into the others;
   - leaving the node out keeps its own noise, and its own UAP, from pulling
     on its own baseline;
   - a node that may be carrying a pass (an event open, or a score of 3σ) is
     left out of the others' medians, and so is every node within 3 km of
     one, because a pass is felt before it alarms.

   A node clear of every event that finds no one clear sets those scores
   aside first; one at or beside an event, which is measuring the pass, takes
   clear nodes only. However few the others, the median follows them, and
   each window is scored against the extra noise a median of few carries.
   With no one left, a node holds its own recent common mode, for three
   minutes at most; where a hold ends, and where one clear of every event
   begins one, no window spans the step. The common mode is subtracted only
   while six or more magnetometers report: a median of fewer lets one node's
   UAP into the others, so it is then left in.
4. **Matched filter.** A dipole on a straight line produces, on every axis, a
   combination of the three Anderson functions f_n(u) = uⁿ/(1+u²)^(5/2),
   n = 0, 1, 2, of u = (t − t₀)/τ. Here t₀ is the time of closest approach
   and τ is the slant range at closest approach divided by the speed. The
   residual in a window of ±4τ is projected onto those functions, after
   removing a constant and a slope. This runs at six scales: τ = 2, 4, 8, 16,
   32 and 64 s, which at 100 m/s is 0.2 to 6.4 km. A window must lie within
   one unbroken stretch of readings: readings more than 5 s apart leave a
   hole, and so do a restart of the noise estimate, the fleet's clock
   stepping back and the ends of a hold, and no window at any scale spans
   one. Windows count readings, so a fleet running slow stretches each scale
   a little.
5. **Score.** Under noise alone, the captured energy over the noise variance
   is χ² with 9 degrees of freedom. Its tail probability is expressed as a
   one-sided Gaussian score in σ.
6. **Events.** An event opens when the best score crosses the threshold.
   - **The peak.** It follows the unclipped statistic to the window best
     matched to the pass. The score stops at 40σ, well before a close pass
     does.
   - **Closing.** The event closes once its peak's scale, and every shorter
     one, has stayed under the threshold minus one for five readings, and the
     peak's window has passed. The longer scales still hold a strong pass for
     minutes after it has gone, which is why they are left out. It also
     closes when its node can no longer be followed: a restart, a hold past
     its limit, a re-placement, or the fleet's clock stepping back.
   - **What follows.** No window that overlaps the event may open another.

   It carries:
   - the time of closest approach;
   - the pass's own time scale τ, its slant range at closest approach over
     its speed, in seconds, refined between the six search scales;
   - the strength (the fitted signal's peak, in nT, at that τ);
   - the score;
   - a confidence;
   - how often noise alone would score as high;
   - a range band, from the strength, the dipole model and the score: a
     strength read near the threshold has taken in the noise that helped it
     over, and its band allows for that;
   - whether the model behind the confidence held when it opened
     (`calibrated`: the common mode subtracted, no storm, readings a second
     apart).
7. **Scoring.** The simulator's truth labels each event true when the UAP's
   own field could have raised the statistic that fired: at least one noise
   variance of energy in the event's peak window, after the constant and slope
   the detector removes. That gives a live false-alarm rate per node-hour
   searched, with a 95 % interval. It also counts the passes of 20 nT or more
   as they happen, and how many of them a true event was open for. The
   scorecard describes one configuration, and starts again when the
   threshold, the common mode actually subtracted, the sensor setting or the
   storm changes.

The method is magnetic anomaly detection with Anderson functions (Anderson
1949; Sheinker et al. 2009), applied as a matched subspace detector over a
bank of time scales, after the network has removed what it has in common.

### Turned down

| Alternative | Why not |
| --- | --- |
| A threshold on \|B\| or on each axis's deviation | The daily variation and storms are several times the noise. A threshold above them misses everything beyond about a kilometre, and still alarms on every storm |
| A high-pass filter and a threshold, per node | Storm pulsations have periods of 45–600 s, the same time scales as a pass, so they get through with it. It also throws away the shape of a pass, which is what separates it from noise |
| Detection inside the simulator | The simulator would be grading itself. A real network would detect where the data from all its nodes meets, which is the server |
| One time scale | A matched filter is matched to one τ; passes range from seconds (close and fast) to minutes (far and slow) |
| A learned classifier | There is no labelled real data to train it on, and its false-alarm rate could not be stated in advance |
| Registering magnetometers as TCP nodes | See C4 |
| Magnetometer data on the aircraft WebSocket | See C5 |

### What it measured

`scripts/magnetometer_false_alarms.py` in retina-server runs the real code
path: the simulator's readings go through the server's ingest, as the live
route sends them, each push stamped with its simulated time. Its networks are
magnetometers on a grid about 20 km apart around Greenville, SC, at 200
cycles, with the common mode subtracted: eight, except where other sizes are
named. Every run is seeded and reproducible, and every threshold runs over
the same simulated days, the default and the one below it over three times
as many. The numbers here come from one run at its defaults, about an hour
and three quarters on eight processes.

**False alarms.** No UAP anywhere: WMM2025, crust, the daily variation and
datasheet noise, so every event is a false alarm. Node-hours are node-hours
searched, as the live scorecard counts them. The model is the detector's
(`false_alarm_rate`), described below.

| Threshold | Node-hours | False alarms | Per node-hour | 95 % interval | Model |
| ---: | ---: | ---: | ---: | --- | ---: |
| 3.0σ | 3,065 | 17,455 | 5.70 | 5.61–5.78 | 5.71 |
| 3.5σ | 3,065 | 4,163 | 1.36 | 1.32–1.40 | 1.34 |
| 4.0σ | 3,065 | 716 | 0.234 | 0.217–0.251 | 0.238 |
| 4.5σ | 3,065 | 107 | 0.035 | 0.029–0.042 | 0.032 |
| 5.0σ | 9,194 | 25 | 0.0027 | 0.0018–0.0040 | 0.0034 |
| **5.5σ** (default) | **9,194** | **1** | **0.00011** | **0.0000028–0.00061** | **0.00027** |
| 6.0σ | 3,065 | 0 | 0 | 0–0.0012 | 0.000017 |

**The model.** Noise alone crosses a threshold of z at A · z² · P(Z > z) per
node-hour, with A = 470.

- **Why z².** The detector searches over two parameters, time and time
  scale. The rate of distinct excursions of such a search falls as z·φ(z),
  which is ≈ z²·P(Z > z), more slowly than the tail probability alone
  (Siegmund & Worsley 1995, *Ann. Statist.* 23; Worsley 2001, *Adv. Appl.
  Prob.* 33, for χ² fields).
- **How A was fitted.** By Poisson maximum likelihood across all the
  thresholds at once. They share their simulated days, so their counts are
  not independent, and A's interval comes from drawing whole seed-days
  again. The model's expected counts (17,499, 4,105, 730, 99.1, 31.0, 2.48
  and 0.05) sit inside the measured 95 % interval at every threshold, and
  A's own interval is 463–477. With the exponent left free, the fit gives
  2.03 (1.87–2.20).

A simpler model, with a fixed number of independent looks per hour, was the
first one tried. Fitted to the same counts, it gives 0.93 of the measured
rate at 3σ and too little by 1.3, 1.6, 2.3 and 2.1 times at 3.5, 4, 4.5 and
5σ, so it was dropped.

**What 5.5σ means.** At 2.7 × 10⁻⁴ false alarms per node-hour (measured: 1
in 9,194 node-hours), eight magnetometers raise one about every 19 days on
the model; at 5σ they would raise one about every 1.5 days (measured: 25 in
9,194 node-hours). An event at the
threshold carries a confidence of 0.9997: the probability that noise alone
would not have produced it at that node within an hour. What the lower
threshold buys in reach is under **Reach**, below.

**Other network sizes.** The model is fitted at eight magnetometers. With six
or seven each median is of fewer others, and noisier, which the score allows
for; with twelve, of more. At 3.5σ:

| Magnetometers | Node-hours | False alarms | Per node-hour | 95 % interval | Model expects |
| ---: | ---: | ---: | ---: | --- | ---: |
| 6 | 1,006 | 1,402 | 1.39 | 1.32–1.47 | 1,347 |
| 7 | 1,006 | 1,390 | 1.38 | 1.31–1.46 | 1,347 |
| 12 | 1,149 | 1,569 | 1.37 | 1.30–1.43 | 1,539 |

Over the same simulated days, six alarm 2.1 % more than eight, seven 1.3 %
more, twelve 0.4 % less.

**No daily cycle.** Sixteen seeded runs of the same two days, 30 September
and 1 October 2026, on seeds of their own, at 3.5σ: 6,137 node-hours and
8,367 false alarms, binned by local solar time at the network's centre, the clock
the simulated daily variation keeps. Three tests were fixed before any run:
χ² over the 24 hours, the first four harmonics of the day, and daytime
(06–18 local solar time) against night. Alarms can cluster in time, so each p
also comes from turning every run's clock by a random amount, which keeps
the clustering and breaks only the tie to the time of day:

- χ² = 28.6 on 23 degrees of freedom (p = 0.19; 0.31 with the clocks turned);
- the four harmonics, 11.4 on 8 degrees of freedom (p = 0.18; 0.28);
- daytime against night, 4,157 against 4,210 alarms (p = 0.49; 0.53).

**A storm.** An intense storm (Dst −150 nT, with pulsations of 45–600 s), at
5.5σ, through its whole course: the sudden commencement, the main phase to
the Dst minimum six hours in (about −142 nT), and 30 hours of recovery, each
phase counted on its own. Eight storms at each size:

| Magnetometers | Common mode | Node-hours | False alarms | Per node-hour | 95 % interval |
| ---: | --- | ---: | ---: | ---: | --- |
| 8 | removed (default) | 2,322 | 0 | 0 | 0–0.0016 |
| 8 | not removed | 2,322 | 2,426 | 1.04 | 1.00–1.09 |
| 7 | removed | 2,031 | 0 | 0 | 0–0.0018 |
| 7 | not removed | 2,031 | 2,139 | 1.05 | 1.01–1.10 |
| 6 | removed | 1,741 | 0 | 0 | 0–0.0021 |
| 6 | not removed | 1,741 | 1,833 | 1.05 | 1.01–1.10 |

With the common mode left in, most of the false alarms come in the main
phase and the first twelve hours of recovery: 2,368 of the 2,426 at eight
magnetometers, and one in the eighteen hours after. With it removed there
are none, in any phase, at any size. This is why detection runs over the
network. The settings keep the switch, so the
difference can be shown live.

**Detection probability.** One 1e9 A·m² pass at a time, 300 m above the
sensor, on a random heading with a random dipole direction, 60 passes per
cell, the same passes at every threshold. At 5.5σ:

| Slant range at closest approach | 30 m/s | 100 m/s | 300 m/s |
| ---: | ---: | ---: | ---: |
| 0.5 km | 60/60 | 60/60 | 60/60 |
| 1.0 km | 60/60 | 60/60 | 60/60 |
| 1.5 km | 60/60 | 57/60 | 19/60 |
| 2.0 km | 49/60 | 9/60 | 0/60 |
| 2.5 km | 7/60 | 0/60 | 0/60 |
| 3.0 km | 0/60 | 0/60 | 0/60 |
| 3.5 km | 0/60 | 0/60 | 0/60 |

**Reach.** The slant range out to which nine passes in ten are found, and one
in two, in km:

| Threshold | 30 m/s | 100 m/s | 300 m/s |
| ---: | --- | --- | --- |
| 5.0σ | 1.88 / 2.26 | 1.54 / 1.80 | 1.10 / 1.50 |
| **5.5σ** (default) | **1.77 / 2.23** | **1.53 / 1.78** | **1.07 / 1.37** |
| 6.0σ | 1.68 / 2.16 | 1.51 / 1.74 | 1.07 / 1.34 |

With datasheet noise, a magnetometer is a local tripwire with a reach of about
1.5 km, about 1.8 km for slow passes and 1.1 km for fast ones. A slow pass
stays in view longer, and the matched filter adds up that time. The field falls as 1/r³, so each
doubling of the moment adds only 26 % to the reach. Eight magnetometers each
watch a circle about 3 km across, about 57 km² between them. The radars watch
tens of kilometres in every direction.

**How well a detection is measured.** For the passes found at 5.5σ, the
strength reads 1.01 times the true peak field (median; 0.91 to 1.46 for nine
in ten), and 1.30 below 8σ, where the fit takes in the noise that helped the
statistic over the threshold. The strength and τ are measured at the pass's
own time scale, its slant range at closest approach over its speed, refined
between the six scales the detector searches, and the published τ reads
1.00 times the pass's own (median; 0.68 to 1.20 for nine in ten). The first
version fitted the strength at the peak's scale, one of the six, and a pass
whose τ fell between two scales read low: 0.85 times the true peak on the
map's default flyby (600 m off and 300 m up at 120 m/s), where it now reads
0.999 (0.943 to 1.037). The time of closest approach is 1 s from the truth
(median), and the range band holds the true slant range for 98.6 % of them.
The band's constants (`BAND_NEAR = (-0.056, -0.56)`,
`BAND_FAR = (0.627, 2.53)`) are fitted by the benchmark to the passes it
found at 3.5, 5, 5.5 and 6σ: the narrowest band that keeps 98 % of every
score bin's passes inside each end, 98 % being the least at which a band
fitted on half the seeds held nine passes in ten of the other half in every
bin. It holds 99.3, 98.8, 98.6 and 98.1 % of the passes found at those
thresholds, and far over near is 1.29 for a strong detection, 1.69 at 5.5σ
and 2.50 at 3.5σ.

**Where it is weakest.** Core placement, an option (spread is the default),
puts magnetometers close enough for one pass to reach several, which is
where the common mode is hardest to keep clean. In generated fleets with
magnetometers within 3 km of one another, every pass is found at its own
node, but about one pass in 22 raises a false alarm at another, 0.7–11 km
from the pass's own: 57 false alarms in 1,048 passes, quiet as often as in a
storm, and none at the pass's own node. A node counts as clear until it
scores 3σ, but a pass is already in its readings before that, so a far
node's median of the few clear others left can carry the pass, and a hold
can then freeze it. Taking clear nodes only for a node at or beside an event
also finds about one pass in a hundred fewer there (3,060 of 3,408, against
3,092 of 3,412). Spread placement, the default, puts magnetometers 4–118 km
apart, so no two are that close; on the benchmark's grid, about 20 km apart,
the 1,260 detection passes raised no false alarm at another node, and the
storms at six, seven and eight magnetometers none at all with the common
mode removed. The next step is to take a node's readings out of the others'
medians back to before it scored 3σ (a look-back exclusion), and to begin
holds clean.

### On the map

The layer appears only on `/sim`, and only where the server runs a fleet:

- **Badges.** A small badge beside each magnetometer's radar node, on the
  map's quality scale with a shape per state: a green square when quiet, an
  amber diamond within 1.5σ of the threshold, a red square that pulses while
  detecting, grey while warming up, and a hollow grey square when silent. The
  badges are DivIcons in the marker pane, because a second interactive canvas
  would swallow the map's clicks, and their class is not `node-marker`, which
  the end-to-end suite counts.
- **The range band.** While a detection is open, and fainter for a minute
  after, the ring its strength and score imply for the configured dipole
  moment.
- **A summary card.** The live false-alarm rate per node-hour searched, with
  its 95 % interval, how many passes were found, and the latest events; why,
  when the noise model does not hold; and how many pushes the server has
  refused or entries it has skipped, when it has.
- **A panel per magnetometer.** Ten minutes of detection score against the
  threshold, of the residual field, and of the field as measured. The time
  scales it searches: all six, or which of them, and when the rest come.
  Every field of the current event, including the truth behind it. A button
  that flies a UAP past it.
- **A toast** when a detection starts (more than three at once share one).

The settings are a section of the Physics Layer page (`/sim/physics`):

- how many magnetometers, and where;
- the cycle count;
- a storm;
- the threshold, with the false-alarm rate the model predicts beside the
  measured one where the model holds (the common mode subtracted, which
  takes six or more magnetometers, no storm, and readings a second apart), and
  a note where it does not;
- the common-mode switch;
- a flyby form.

The section has its own Apply, and joins the page's existing check for a
config changed elsewhere: its unapplied edits count there as the page's own
do. The design note in retina-server lists the behaviours of that check that
predate this work and were left as they are.

## What only hardware can settle

Nothing here has touched an RM3100. The driver was tested byte by byte against
a scripted bus and against the simulator's register model. Both encode the
manual, and the manual contradicts itself in places.
[hardware-verification.md](hardware-verification.md) lists 25 checks for the
first power-up and the days after it, each with how to make it and what the
code assumes meanwhile. The ones most likely to need a change are:

- **Identity.** The manual gives no REVID value; every driver that checks it
  uses 0x22.
- **I2C framing on the Pi 5's RP1 adapter.** Repeated start or STOP; both are
  proven elsewhere, neither here. It is a setting.
- **DRDY rules.** Especially whether the pointer write before a STATUS read
  clears it.
- **Conversion times and the rate limits.** The continuous-mode cap, and
  poll mode's allowance of 4.3 ms a sample for its transfers and the host,
  which is an estimate.
- **Resets.** That a brown-out puts the registers back to the manual's
  defaults (TMRC 0x96), which is how poll mode tells a reset chip from a
  configured one at the default cycle count.
- **Noise, gain and axis polarity.** Some boards are ~1.3× off PNI's gain
  formula, and some builds reverse an axis.
- **Permissions.** Whether the cgroup rule and root-without-capabilities open
  `/dev/i2c-1` on owl-os.

The detector's numbers come from simulated data, and a real network will
differ:

- **Real noise may not be white.** The simulator's is, and the datasheet
  gives one figure with no bandwidth. PNI calls the measurements stable over
  temperature and free from offset drift (UM16 §2); the 0.4 %/°C in its
  Table 3-3 is the coils' DC resistance, not a drift of the reading. But the
  chip has no temperature sensor, and PNI's figures were taken at room
  temperature (Table 3-1's footnote). Any slow drift, thermal or not, makes
  noise measured from first differences understate what the long scales see,
  and the long scales would then alarm more often. A first deployment should log temperature beside the sensor,
  as HamSCI's stations do ([hardware-verification.md](hardware-verification.md),
  item 15).
- **Real sites have magnetic clutter.** Cars, lifts, trains, power lines and
  the node's own electronics produce local, dipole-like signals: false alarms
  that no common mode removes.
- **A real common mode is less common.** Across tens of kilometres, the
  induced field differs with ground conductivity, so storms leave larger
  residuals than here.
- **Real clocks drift.** The leave-one-out median compares nodes at the same
  second, so nodes need disciplined clocks. owl-os runs chrony for the radar;
  it is worth confirming on each node (`chronyc tracking`,
  [hardware-verification.md](hardware-verification.md), item 22).

The false-alarm model's coefficient must therefore be measured again on real
data before a threshold on a real network means anything. The method carries
over: run the detector over a stretch of recorded readings with nothing in
them, and fit the coefficient, as the benchmark script does over simulated
readings.

## What a real node needs

[adding-to-a-retina-node.md](adding-to-a-retina-node.md) has the detail.
In short:

1. **owl-os.** I2C is off in today's image. It needs
   `dtparam=i2c_arm=on` and `i2c-dev` loaded at boot, which is an owl-os
   change and release. The clock, which retention runs on (A9), is already
   disciplined by chrony.
2. **The sensor.** On the Pi's 3.3 V header pins (never 5 V), as far from the
   Pi, the SDR and the power supply as the cable allows, with power run as
   twisted pairs, level, and with a known axis north if possible.
3. **retina-node.** The service entry
   ([`deploy/retina-node-service.yml`](../deploy/retina-node-service.yml)), a
   `sed` clause in the Mender artifact script, the `.env.example` lines and a
   row in the port table.
4. **A release, from offworldlabs.** The service entry pulls
   `ghcr.io/offworldlabs/retina-magnetometer`, and
   `.github/workflows/release.yml` publishes to
   `ghcr.io/<owner of the repository>/retina-magnetometer`. So the repository
   has to be transferred to offworldlabs, or forked into it, before the first
   release: a tag pushed from any other account publishes under that account,
   where no node looks. Then tag `vX.Y.Z` (the release checks it against
   `pyproject.toml`'s version and runs every gate first), bump the pin in
   retina-node, and Mender carries the image. The image's labels name the
   repository and commit that built it.
5. **Optional.** A retina-gui card for port 3030, a node-infra tunnel route,
   and config-merger emitting `MAGNETOMETER_*` settings, so that retina-gui
   becomes the settings page.

For detection on a real network, one piece is missing on purpose. Magnetometer
readings have no way to reach the server: `/v1/nodes` carries radar data only,
and its contract was not to change. That would be a new, versioned contract,
alongside the radar one. The node app already keeps one sample per second on
the grid of the second, stamped at mid-conversion, which is what a network
detector needs from it.

## How it was verified

- **retina-magnetometer.** 837 tests, 96 % coverage with branches, and
  pre-commit with the shared ruff standard and the dead-code gate. The demo
  (`docker compose up`) was brought up, and it recovered the simulated
  mounting: the downward axis, and the heading to 37.1° east of true north
  (the mounting is 37°). A week of backfill at 1 Hz, written while the app
  sampled, took 55 s and 28 MB, and a clean stop then left the database in
  one file. The node entry, run as written on a machine without I2C,
  reported `no_bus` and what to enable, read its location from the node
  config, ran with no capabilities, a read-only root filesystem and
  `no-new-privileges`, saw none of the host's shared memory, message queues
  or console, and stopped cleanly on SIGTERM.
- **retina-simulation.** Its suite (280 tests) with the magnetometer tests:
  the field's parts against the specified numbers, WMM2025 and the
  datasheet; placement (one per roof, spread, core, seeded random);
  reproducibility, and that placement and noise never touch the world's
  random state, so a seed's radar scene is unchanged; the push payload;
  output noise at the datasheet's figure, rounding included; one reading per
  world step, pushed to a local server that is slow, failing, refusing, or
  down for longer than the hold, without ever holding up the world; failed
  readings kept from stopping the radar fleet and counted in STATS; the
  config poll's wiring; the seed from the generator's stamp; flybys with
  ten-minute leads at up to 1000 m/s.
- **retina-server.** The backend's magnetometer test files, more than 250
  detector tests, with the rest of the backend suite; the dashboard's
  typecheck, lint and 1,770 tests; and pre-commit. The detector module is
  fully covered, and its tests run the simulator's
  readings through it: a quiet network raises nothing, and its false alarms
  match the model behind every confidence; a close pass is found once, with
  its truth, and measured at its best window; a distant pass, or noise beside
  a UAP that does not move, is not claimed; a storm is removed by the common
  mode, at six and seven magnetometers too; a pass over a cluster, across a
  ring or over the dense core they build raises no false alarm elsewhere; a
  hole, a restart, a hold, a clock stepping back and a fleet running slow
  are each handled. The benchmark above is the measurement.
- **End to end.** A local server with a fleet: a flyby requested from the
  `/sim` map, detected by the magnetometer it passed, and shown on the map.
  The screenshots are with the pull requests.
