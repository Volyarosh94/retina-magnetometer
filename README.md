# retina-magnetometer

RM3100 magnetometer support for RETINA nodes, in two parts that share one
repository and one image:

- **The node app** (`retina_magnetometer/`, Part A). A container that reads a
  PNI RM3100 over I2C, keeps its measurements on the node with bounded
  retention, and shows the three axes and |B| from ten minutes to thirty days
  in a browser, with the sensor's orientation worked out from the Earth's field
  and its health. [Part A README](retina_magnetometer/README.md)
- **The RM3100 simulator** (`rm3100_sim/`, Part B). A register-level model of
  the chip, served over TCP, whose measurement registers are filled from a
  physical field model: WMM2025 for the site, the solar-quiet daily variation,
  storms, local disturbances, UAP passes as magnetic dipoles, sensor noise from
  the datasheet, and I2C faults, all scripted by scenario files and
  reproducible from a seed. The node app runs against it with one variable
  changed and no code changed. [Part B README](rm3100_sim/README.md)

Part C, magnetometer nodes in the fleet simulation and on the server's `/sim`
map, is in the retina-simulation and retina-server pull requests; the
[design note](docs/design-note.md) covers all three.

## Try it

No hardware needed. With Docker Compose 2.x:

```bash
docker compose up --build          # the app, driven by the simulator
docker compose run --rm backfill   # optional: a week of simulated history
```

Then open <http://localhost:3030>. The demo scenario mounts the simulated
sensor upside down and turned 37° east of north, which the orientation card
should work out on its own; flies a UAP past it every few minutes; parks a car
beside it for half an hour every two hours; and makes the I2C bus drop some
transfers for twenty seconds every half hour, which the health card reports.
`SIM_SCENARIO=faults docker compose up` runs the failure modes on their own.

## Develop

The project is a [uv](https://docs.astral.sh/uv/) project with a committed
lock, per the org's Python app standard.

```bash
uv sync                                   # Python 3.12 venv with the dev tools
uv run python -m rm3100_sim serve &       # the simulated sensor on :9100
MAGNETOMETER_BUS=tcp://127.0.0.1:9100 MAGNETOMETER_DATA_DIR=./data \
MAGNETOMETER_LATITUDE=34.85 MAGNETOMETER_LONGITUDE=-82.39 \
  uv run python -m retina_magnetometer    # the app on :3030
tools/check.sh                            # every gate CI runs
```

`tools/check.sh` runs pre-commit (ruff, the shared ruff standard, the dead-code
gate) and the tests with coverage. CI also builds the image for arm64 and
amd64, and brings the demo up and checks, through the app's own API, that it
found the simulated sensor, is sampling it, and recovered the mounting.

## Documents

| Document | What it covers |
| --- | --- |
| [retina_magnetometer/README.md](retina_magnetometer/README.md) | Part A: what the app does, its configuration, API, storage and health |
| [rm3100_sim/README.md](rm3100_sim/README.md) | Part B: the chip model, the field model, scenario files, the command line |
| [docs/design-note.md](docs/design-note.md) | Design choices, rejected alternatives, assumptions, what needs hardware |
| [docs/adding-to-a-retina-node.md](docs/adding-to-a-retina-node.md) | How this container joins a node's retina-node stack |
| [docs/hardware-verification.md](docs/hardware-verification.md) | The bring-up checklist for a physical RM3100 |

## Layout

```
retina_magnetometer/        Part A, the node app
  rm3100/                   the driver: registers, I2C transports, the device
  sampler.py                acquisition thread and recovery
  recorder.py, storage.py   buffering, SQLite, retention
  orientation.py            mounting from the field
  health.py                 health model and status.json
  web/                      Flask page and JSON API
rm3100_sim/                 Part B, the simulator
  device.py                 the register-level chip model
  physics.py                the field model
  scenario.py, scenarios/   scenario files
  server.py                 the TCP server
tests/                      pytest; tests/sim for the simulator
deploy/                     the service entry for retina-node's compose file
```
