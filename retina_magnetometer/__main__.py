"""Run the node app: ``python -m retina_magnetometer``.

Three threads and a web server in one process: the sampler talks to the
sensor, the recorder writes to disk and keeps status.json current, and
waitress serves the page and the API. SIGTERM (``docker stop``) stops the
sampler, flushes what is buffered, closes the database and exits; nothing
buffered is lost on a clean stop.

What the app finds at start does not stop it: a container that exits is
restarted into the same problem, and the page that would explain it never
comes up. A bad setting is reported on the page and in status.json; a data
directory that cannot be written, on the page and in the log (status.json
lives there too); a database that cannot be read is moved aside and a new one
begun; a node config of the wrong shape is logged and gives no location. A
listen address that cannot be used when the server binds to it (one that is
not this machine's) is replaced by 127.0.0.1, and that is reported too. The
rest keeps running. Only a port it cannot listen on ends the process, as
there is then no page to report on.
"""

from __future__ import annotations

import errno
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

# What a listen address that is not this machine's (EADDRNOTAVAIL), or not of
# a family it has (EAFNOSUPPORT), fails with when the server binds to it. A
# port in use or a privileged port fails otherwise, and no other address would
# help: those end the process as before.
_ADDRESS_ERRORS = (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT)


def main() -> int:
    config = config_module.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for problem in config.errors:
        log.error("configuration: %s", problem)
    for problem in config.warnings:
        log.warning("configuration: %s", problem)

    health = Health(config)
    # Never raises: a database it cannot open is reported, and tried again.
    storage = Storage(
        config.db_path,
        raw_retention_days=config.raw_retention_days,
        rollup_retention_days=config.rollup_retention_days,
        max_db_mb=config.max_db_mb,
    )
    recorder = Recorder(storage, health, config)
    # Sessions go through the recorder, like the samples, so the sampler never
    # waits on the disk or fails with it.
    sampler = Sampler(config, health, recorder.add, on_session=recorder.start_session)
    here = location_module.resolve(config)
    if here is None:
        log.warning("node location unknown: orientation will report the field direction only")
    else:
        log.info("location %.4f, %.4f, %.0f m from %s", here.latitude, here.longitude, here.altitude_m, here.source)

    app = create_app(Services(config=config, health=health, recorder=recorder, storage=storage, location=here))
    server = _listen(app, config, health)

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
    log.info("serving on %s", _served(server))
    try:
        server.run()
    finally:
        sampler.stop()
        recorder.stop()
        # The last checkpoint: the database is left whole in its one file,
        # the WAL taken into it and removed, for a copy or a backup.
        storage.close()
        log.info("stopped")
    return 0


def _listen(app, config: config_module.Config, health: Health):
    """The server, on the configured address or, if that is not one this
    machine can listen on, on 127.0.0.1 with a warning on the page."""
    try:
        return create_server(app, host=config.host, port=config.port, threads=8, ident="retina-magnetometer")
    except (OSError, ValueError) as exc:
        # ValueError: a name that resolved when the settings were read, and
        # no longer does.
        about_the_address = isinstance(exc, ValueError) or exc.errno in _ADDRESS_ERRORS
        if config.host == "127.0.0.1" or not about_the_address:
            raise
        message = (
            f"MAGNETOMETER_HOST={config.host!r} cannot be listened on ({exc}): the page is served on 127.0.0.1 only"
        )
        log.warning("configuration: %s", message)
        health.config_warning(message)
        return create_server(app, host="127.0.0.1", port=config.port, threads=8, ident="retina-magnetometer")


def _served(server) -> str:
    """Where the page is served: one address, or several (waitress listens on
    each address a name such as localhost resolves to)."""
    listen = getattr(server, "effective_listen", None) or [(server.effective_host, server.effective_port)]
    return ", ".join(f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}" for host, port in listen)


if __name__ == "__main__":
    sys.exit(main())
