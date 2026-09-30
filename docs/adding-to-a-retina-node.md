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
- **Distance**: the field of a steel box, a fan or a DC cable falls as 1/r³,
  but next to the Pi, the SDR and the power supply it can be hundreds of nT.
  Mount the sensor as far from them as the cable allows (HamSCI stations run
  30 m of differential I2C to put it in the garden) and away from anything
  that moves or switches.
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
   nodes inside the artifact.

The choices in that entry, and why:

- **`/dev` bind mount plus a device cgroup rule, not `devices:`.** A
  `devices: [/dev/i2c-1]` entry makes `docker compose up` fail on any node
  without that device, and today that is every node; a failed `up` fails the
  Mender install of the whole stack. Binding `/dev` always succeeds, the rule
  `c 89:* rmw` limits the container to I2C character devices, and a bus that
  appears later (after the owl-os change) is picked up without recreating the
  container: the app retries with backoff.
- **No `HEALTHCHECK` and no crash loop.** Mender's update module waits for every
  container to be `running` and either healthy or without a health check, and
  the inventory marks a node degraded if any container is restarting. A node
  without a sensor is a fact for the status page, not a failed deployment.
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

## 4. Checking it on the node

```bash
ls -l /dev/i2c-1                     # the bus exists (after the owl-os change)
i2cdetect -y 1                       # something at 0x20-0x23 (i2c-tools)
docker logs retina-magnetometer      # "RM3100 at 0x20 on /dev/i2c-1 ..."
curl -s localhost:3030/api/health | jq '.state, .sensor, .self_test'
cat /data/retina-node/retina-magnetometer/status.json
```

Then open `http://<node>:3030`. The first power-up of a new sensor is worth the
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
