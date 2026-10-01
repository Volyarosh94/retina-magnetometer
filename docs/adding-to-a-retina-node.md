# Adding the magnetometer to a RETINA node

What it takes for this container to run on a node alongside the radar stack.
Nothing here changes retina-gui; the last section lists the optional follow-ups
that would.

## 1. The node must expose I2C (owl-os)

owl-os ships with the Pi's I2C interface off: `dtparam=i2c_arm=on` is
commented out in the Pi 5 image's `config.txt`, and nothing loads the `i2c-dev`
module, so there is no `/dev/i2c-1`. An owl-os change and release is needed:

- `dtparam=i2c_arm=on` in `config.txt` (optionally `dtparam=i2c_arm_baudrate=400000`;
  the default is 100 kHz, which is plenty at 1 Hz);
- `i2c-dev` loaded at boot (a line in `/etc/modules-load.d/`).

Until a node has both, the container still runs: its status page and
`status.json` say `no_bus` and what to enable, and the rest of the stack is
unaffected.

## 2. The sensor

An RM3100 breakout on the Pi's header I2C pins (GPIO 2/3, 3.3 V, ground), or
over Qwiic/STEMMA QT on a board that has the connector. Four things worth
getting right before it is screwed down:

- **Supply**: 2.0–3.6 V. The Pi's 3.3 V rail, never 5 V (ArduPilot users saw a
  ~1.3x gain error from boards run above the 3.7 V absolute maximum).
- **Address**: 0x20–0x23 by strapping; the app probes all four.
- **Distance**: a steel box or a fan motor is a small magnet whose field
  falls as 1/r³, so a few metres usually settle it. A cable does not: one
  conductor carrying 1 A puts 200 nT on the sensor at a metre and still 40 nT
  at five, falling only as 1/r. Its return beside it cancels most of that (a
  pair falls as 1/r², a twisted pair much faster), so power should run as a
  twisted pair with its own return, never back through the mast or the
  ground. Next to the Pi, the SDR and the power supply the total can be
  hundreds of nT, and it moves as their load does. Mount the sensor as far
  from them as the cable allows (HamSCI stations run 30 m of differential I2C
  to put it in the garden) and away from anything that moves or switches.
- **Orientation**: level, with a known axis pointing north if possible. The app
  works out the downward axis and the heading anyway, but a documented mounting
  makes that a check rather than a discovery.

## 3. The service in retina-node

[`deploy/retina-node-service.yml`](../deploy/retina-node-service.yml) is the
entry for retina-node's `docker-compose.yml`, commented line by line. Adding a
service there follows the precedent of retina-telemetry and retina-spectrum:

1. Paste the entry under `services:`; add `MAGNETOMETER_V` to the header's
   release list.
2. Add `-e 's/\${MAGNETOMETER_V:-\([^}]*\)}/\1/g'` to
   `scripts/build_mender_artifact.sh`. Its guard fails the artifact build
   otherwise, which is the point of the guard.
3. Add `# MAGNETOMETER_V=...` (and, if wanted, `MAGNETOMETER_PORT`,
   `MAGNETOMETER_SAMPLE_RATE_HZ`, `MAGNETOMETER_CYCLE_COUNT`) to `.env.example`.
4. Add a row for port 3030 to the README's web interface table.
5. Release as usual: tag this repository `vX.Y.Z` to publish
   `ghcr.io/offworldlabs/retina-magnetometer:vX.Y.Z` (arm64 and amd64), bump
   the pin in retina-node, tag retina-node, and Mender carries the image to the
   nodes inside the artifact. The repository has to be in offworldlabs first:
   see [where the image comes from](#where-the-image-comes-from).

The choices in that entry, and why:

- **`/dev` bind mount plus a device cgroup rule, not `devices:`.** A
  `devices: [/dev/i2c-1]` entry makes `docker compose up` fail on any node
  without that device, and today that is every node; a failed `up` fails the
  Mender install of the whole stack. Binding `/dev` always succeeds, and a bus
  that appears later (after the owl-os change) is picked up without
  recreating the container: the app retries with backoff. The bind shows the
  container every device node on the host; the device cgroup decides which it
  may open. The rule `c 89:1 rmw` adds `/dev/i2c-1` (i2c-dev is major 89 and
  the minor is the adapter's number, so the rule changes with
  `MAGNETOMETER_BUS`) to the devices Docker and runc always allow: null,
  zero, full, random, urandom, tty, console, ptmx, the pts terminals and
  `/dev/net/tun`. The cgroup governs device nodes only.
- **Private `/dev/shm` and `/dev/mqueue`, and no console.** The bind is
  recursive, so it also brings the filesystems the host mounts under `/dev`,
  and Docker leaves out its own once `/dev` is bound. Without the entry's two
  `tmpfs` mounts, the container would share the host's `/dev/shm`, where blah2
  and retina-spectrum keep the SDRplay API's shared memory, and the host's
  message queues: files owned by root, which is who the app runs as. Docker
  mounts the two over the bind's, empty and private. The host's
  `/dev/console`, which the default device rules admit and which is root's,
  has the null device mounted over it. What the bind still brings: every other
  host device node, which only the cgroup's allow-list opens; the host's
  `/dev/pts`, whose terminals the cgroup admits, so that their file
  permissions are what protect them (a root login's terminal is open to the
  container's root); and any other filesystem the host mounts under `/dev`,
  such as `/dev/hugepages` where the kernel has huge pages. Item 21 of
  [hardware-verification.md](hardware-verification.md) checks all of this on
  a node.
- **No `HEALTHCHECK` and no crash loop.** Mender's update module waits for every
  container to be `running` and either healthy or without a health check, and
  the inventory marks a node degraded if any container is restarting. A node
  without a sensor is a fact for the status page, not a failed deployment, and
  so is what the app may find at start. A data directory it cannot write is
  reported on the page and in the log (`status.json` lives in that directory
  too); a database it cannot read is moved aside, a new one begun, and the
  page says so; a `config.yml` it does not understand is logged, and the page
  shows no location. The app keeps running either way.
- **Root in the container, with every capability dropped**, a read-only root
  filesystem and `no-new-privileges`. `/dev/i2c-1` is `root:i2c 0660` on the
  host, with a group id that varies by image, and Docker creates the data
  directory root-owned; root is the owner of both without any capability. The
  page it serves is read-only.
- **Its own port (3030) on the `blah2` bridge**, like tar1090 and blah2_web,
  rather than host networking: it needs nothing from the host's network.
- **Data under `${DATA_DIR}/retina-magnetometer`**, bounded by the app itself
  (7 days raw, a year of minute summaries, 1 GB cap by default).
- **The merged `config.yml` read-only**, for `location.rx`, which the
  orientation check needs; the same mount retina-tracker uses.
- **No `depends_on`**: it should keep recording while the radar services restart.

Settings are environment variables. On a Mender node, config-merger rewrites
the manifests `.env` on every run, so values set there by hand do not survive;
until config-merger emits them, the defaults in the compose entry are what a
node runs, the same limitation retina-telemetry's DNS settings have.

### Where the image comes from

The entry pulls `ghcr.io/offworldlabs/retina-magnetometer`. The release
workflow, `.github/workflows/release.yml`, publishes to
`ghcr.io/<owner of the repository>/retina-magnetometer`, so the two meet only
once the retina-magnetometer repository lives in offworldlabs: transferred
there, or forked into it, before the first release. A tag pushed from any
other account publishes under that account, where no node looks.

A release runs every gate first (`tools/check.sh`, as CI does), refuses a tag
that does not match the version in `pyproject.toml`, and publishes the tag and
`latest` for arm64 and amd64. Each image carries labels naming the repository,
the commit and the tag that built it.

## 4. Checking it on the node

```bash
ls -l /dev/i2c-1                     # the bus exists (after the owl-os change)
i2cdetect -y 1                       # something at 0x20-0x23 (i2c-tools)
chronyc tracking                     # "Leap status : Normal": the clock is synchronised
docker logs retina-magnetometer      # "RM3100 at 0x20 on /dev/i2c-1 ..."
curl -s localhost:3030/api/health | jq '.state, .sensor, .self_test'
cat /data/retina-node/retina-magnetometer/status.json
docker exec retina-magnetometer ls -A /dev/shm /dev/mqueue   # both empty
docker exec retina-magnetometer ls -l /dev/console           # 1, 3: the null device
```

The clock matters because retention runs on it: a node whose clock is days
ahead deletes history early (item 22 of the checklist says how much). Then
open `http://<node>:3030`. The first power-up of a new sensor is worth the
full checklist in [hardware-verification.md](hardware-verification.md).

## 5. Optional follow-ups elsewhere

None of these is needed for the container to run; each makes it more
reachable.

- **retina-gui**: a service card on the home page with `data-port="3030"`
  (cards are hard-coded in `templates/index.html`), and the architecture doc's
  home-page section updated with it. A card could also read
  `status.json`, the way the telemetry card reads retina-telemetry's.
- **node-infra**: a route for the support tunnel. It only carries the GUI's
  hostname and the blah2 paths today, so the page is not reachable remotely
  until it has one.
- **config-merger**: emit `MAGNETOMETER_*` settings from a `magnetometer:`
  section of `user.yml`, so retina-gui can change them; retina-gui would then
  be the settings page for them, behind its own login.
- **retina-node's compose header**: its release-variable list is already
  missing `SPECTRUM_V` and `TELEMETRY_V`; worth fixing in the same change.
