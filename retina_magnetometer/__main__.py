"""Run the node app: ``python -m retina_magnetometer``.

Three threads and a web server in one process: the sampler talks to the
sensor, the recorder writes to disk and keeps status.json current, and
waitress serves the page and the API. SIGTERM (``docker stop``) stops the
sampler, flushes what is buffered and exits; nothing buffered is lost on a
clean stop.
"""

from __future__ import annotations

import logging
import signal
import sys

from waitress import create_server

from retina_magnetometer import config as config_module
from retina_magnetometer import location as location_module
from retina_magnetometer.health import Health
from retina_magnetometer.recorder import Recorder
from retina_magnetometer.sampler import Sampler
from retina_magnetometer.storage import Storage
from retina_magnetometer.web import Services, create_app

log = logging.getLogger("retina_magnetometer")


def main() -> int:
    config = config_module.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for problem in config.errors:
        log.error("configuration: %s", problem)

    health = Health(config)
    storage = Storage(
        config.db_path,
        raw_retention_days=config.raw_retention_days,
        rollup_retention_days=config.rollup_retention_days,
        max_db_mb=config.max_db_mb,
    )
    recorder = Recorder(storage, health, config)
    sampler = Sampler(config, health, recorder.add, on_session=storage.start_session)
    here = location_module.resolve(config)
    if here is None:
        log.warning("node location unknown: orientation will report the field direction only")
    else:
        log.info("location %.4f, %.4f, %.0f m from %s", here.latitude, here.longitude, here.altitude_m, here.source)

    app = create_app(Services(config=config, health=health, recorder=recorder, storage=storage, location=here))
    server = create_server(app, host=config.host, port=config.port, threads=8, ident="retina-magnetometer")

    def stop(signum, _frame):
        # waitress's run() treats SystemExit as "close every channel and
        # return", which is prompt even with browsers holding keep-alive
        # connections open; docker stop allows ten seconds before SIGKILL.
        log.info("signal %d: shutting down", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    recorder.start()
    sampler.start()
    log.info("serving on http://%s:%d", config.host, config.port)
    try:
        server.run()
    finally:
        sampler.stop()
        recorder.stop()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
