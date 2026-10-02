# Adding the magnetometer to a RETINA node

What it takes for this container to run on a node beside the radar stack.
Nothing here changes retina-gui; section 5 lists the optional follow-ups that
would.

## 1. The node must expose I2C (owl-os)

owl-os ships with the Pi's I2C interface off: `dtparam=i2c_arm=on` is
commented out in the Pi 5 image's `config.txt`, and nothing loads the
`i2c-dev` module, so there is no `/dev/i2c-1`. An owl-os change and release
needs to add:

- `dtparam=i2c_arm=on` in `config.txt` (the default 100 kHz is plenty at
  1 Hz);
- `i2c-dev` loaded at boot, through a line in `/etc/modules-load.d/`.

Until then the container still runs. Its page and `status.json` report
`no_bus` and what to enable, and the rest of the stack is unaffected.

## 2. The sensor

An RM3100 breakout goes on the Pi's header I2C pins (GPIO 2/3, 3.3 V, ground),
or on Qwiic/STEMMA QT where the board has the connector.

- Power it from the 3.3 V rail, never 5 V. ArduPilot users saw a ~1.3x gain
  error from boards run above the 3.7 V absolute maximum.
- The address is 0x20–0x23 by strapping, and the app probes all four.
- Mount it as far from the Pi, the SDR and the power supply as the cable
  allows. A wire carrying 1 A puts 200 nT on the sensor at a metre, so power
  should run as a twisted pair with its own return. HamSCI stations run 30 m
  of differential I2C to keep the sensor clear of the house.
- Mount it level, with a known axis pointing north if possible, so the app's
  orientation estimate becomes a check.

## 3. The service in retina-node

[`deploy/retina-node-service.yml`](../deploy/retina-node-service.yml) is the
entry for retina-node's `docker-compose.yml`, commented line by line. Adding
it follows the precedent of retina-telemetry and retina-spectrum:

1. Paste the entry under `services:`, and add `MAGNETOMETER_V` to the
   header's release list.
2. Add `-e 's/\${MAGNETOMETER_V:-\([^}]*\)}/\1/g'` to
   `scripts/build_mender_artifact.sh`. Its guard fails the artifact build
   without it.
3. Add `# MAGNETOMETER_V=...` (and, if wanted, `MAGNETOMETER_PORT`,
   `MAGNETOMETER_SAMPLE_RATE_HZ`, `MAGNETOMETER_CYCLE_COUNT`) to
   `.env.example`.
4. Add a row for port 3030 to the README's web interface table.
5. Tag a release of this repository, bump the pin in retina-node, and Mender
   carries the image to the nodes. The entry pulls
   `ghcr.io/offworldlabs/retina-magnetometer`, so the repository has to live
   in offworldlabs before the first release.

The entry's choices:

- It binds `/dev` and adds a device cgroup rule instead of a `devices:`
  entry. `devices: [/dev/i2c-1]` makes `docker compose up` fail on a node
  without the device, which today is every node, and a failed `up` fails the
  Mender install of the whole stack. The bind always succeeds, a bus that
  appears later is picked up without recreating the container, and the rule
  `c 89:1 rmw` lets the container open `/dev/i2c-1` and no other I2C device.
- It mounts private `/dev/shm` and `/dev/mqueue` and puts the null device over
  `/dev/console`, so the `/dev` bind does not share the host's shared memory
  (the SDRplay API's files), message queues or console.
- It has no `HEALTHCHECK` and does not crash-loop. Mender's update module
  waits for every container to be running and healthy or without a health
  check, and the inventory marks a node degraded if a container is
  restarting. A node without a sensor shows that on the status page and
  installs normally.
- It runs as root in the container with every capability dropped, a
  read-only root filesystem and `no-new-privileges`. `/dev/i2c-1` is
  `root:i2c 0660` on the host with a group id that varies by image, and root
  owns it without any capability.
- It has its own port (3030) on the `blah2` bridge, like tar1090 and
  blah2_web, its data under `${DATA_DIR}/retina-magnetometer`, and the merged
  `config.yml` read-only for `location.rx`, the same mount retina-tracker
  uses. It has no `depends_on`, so it keeps recording while the radar
  services restart.

Settings are environment variables. On a Mender node, config-merger rewrites
the manifest's `.env` on every run, so values set there by hand do not
survive. Until config-merger emits them, a node runs the compose entry's
defaults, the same limitation retina-telemetry's DNS settings have.

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

Retention runs on the node's clock, so check it before the first run. Then
open `http://<node>:3030`. For the first
power-up of a new sensor, work through
[hardware-verification.md](hardware-verification.md).

## 5. Optional follow-ups elsewhere

None of these is needed for the container to run.

- retina-gui: a service card on the home page with `data-port="3030"` (cards
  are hard-coded in `templates/index.html`). The card could also read
  `status.json`, as the telemetry card reads retina-telemetry's.
- node-infra: a route for the support tunnel, which today carries only the
  GUI's hostname and the blah2 paths.
- config-merger: emit `MAGNETOMETER_*` settings from a `magnetometer:`
  section of `user.yml`, so that retina-gui becomes the settings page, behind
  its own login.
