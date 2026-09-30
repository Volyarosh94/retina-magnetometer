"""Where the node is, for the orientation reference.

The node's configured receiver position lives in retina-node's merged
config.yml under ``location.rx`` (written by retina-gui's tower step). This
app mounts that file read-only and reads the three numbers; the
``MAGNETOMETER_LATITUDE/LONGITUDE/ALTITUDE_M`` variables override it, which is
how a standalone run or the simulator demo supplies one.
"""

from __future__ import annotations

import logging
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
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def from_node_config(path: Path) -> Location | None:
    try:
        document = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read node config %s: %s", path, exc)
        return None
    rx = ((document or {}).get("location") or {}).get("rx") or {}
    latitude, longitude = _number(rx.get("latitude")), _number(rx.get("longitude"))
    if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    # retina-node records altitude in metres; an unset one is treated as sea
    # level, which moves the reference field by ~25 nT per km — far below
    # what orientation is sensitive to.
    altitude = _number(rx.get("altitude")) or 0.0
    return Location(latitude, longitude, altitude, f"node config ({path})")


def resolve(config: Config) -> Location | None:
    """The environment's position if given, else the node's, else None."""
    if config.latitude is not None and config.longitude is not None:
        return Location(config.latitude, config.longitude, config.altitude_m or 0.0, "environment")
    return from_node_config(config.node_config)
