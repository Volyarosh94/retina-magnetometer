"""Where the node is, for the orientation reference.

The node's configured receiver position lives in retina-node's merged
config.yml under ``location.rx`` (written by retina-gui's tower step). This
app mounts that file read-only and reads the three numbers; the
``MAGNETOMETER_LATITUDE/LONGITUDE/ALTITUDE_M`` variables override it, which is
how a standalone run or the simulator demo supplies one.

The file is retina-gui's, not this app's, so nothing in it may stop the app:
an unreadable file, YAML that does not parse (or holds a date that is not
one), a document of another shape or a number too big for a float is logged
and treated as no location.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

from retina_magnetometer.config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    altitude_m: float
    source: str


def _number(value) -> float | None:
    """A finite float, or None for anything else: text, a boolean, NaN, or an
    integer too large for a float (YAML has no limit on them)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def from_node_config(path: Path) -> Location | None:
    try:
        return _from_node_config(path)
    except Exception as exc:
        # Whatever the file holds: PyYAML raises more than YAMLError (a date
        # like 2026-13-45 is a ValueError), and the file is not this app's.
        log.warning("could not read a location from node config %s: %s", path, exc)
        return None


def _from_node_config(path: Path) -> Location | None:
    try:
        document = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return None
    # A fresh node has the keys with null values. Anything other than a
    # mapping where one belongs (a list, or text) is a shape this app does not
    # know, and says so.
    rx = document
    for key in ("location", "rx"):
        if not isinstance(rx, dict):
            break
        rx = rx.get(key)
    if not isinstance(rx, dict):
        if rx is not None:
            log.warning("node config %s does not hold location.rx as a mapping; no location read from it", path)
        return None
    latitude, longitude = _number(rx.get("latitude")), _number(rx.get("longitude"))
    if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    # retina-node records altitude in metres; an unset one is treated as sea
    # level, which moves the reference field by ~25 nT per km — far below
    # what orientation is sensitive to. One outside the range the environment
    # variable accepts is a mistake, and is treated the same way.
    altitude = _number(rx.get("altitude")) or 0.0
    if not -500 <= altitude <= 10_000:
        log.warning("node config %s gives an altitude of %g m; using sea level", path, altitude)
        altitude = 0.0
    return Location(latitude, longitude, altitude, f"node config ({path})")


def resolve(config: Config) -> Location | None:
    """The environment's position if given, else the node's, else None."""
    if config.latitude is not None and config.longitude is not None:
        return Location(config.latitude, config.longitude, config.altitude_m or 0.0, "environment")
    return from_node_config(config.node_config)
