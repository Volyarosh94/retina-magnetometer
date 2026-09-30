"""Configuration, from environment variables.

Tunables come from the environment, not from the node's config.yml, the same
split retina-telemetry makes: config.yml is retina-gui's document, and
Compose already hands every other service its settings as variables. The one
thing read from config.yml is the node's own location (see ``location.py``),
because that is a fact about the node rather than a setting of this app.

A bad value never stops the app. It is reported as a configuration error on
the status page and in status.json, the sampler stays off, and the web UI
keeps serving: a container that crash-loops over a typo marks the whole node
degraded in Mender and tells the operator less than one line on a page would.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from retina_magnetometer.rm3100 import registers as reg

PREFIX = "MAGNETOMETER_"


@dataclass(frozen=True)
class Config:
    # Sensor and bus
    bus: str = "/dev/i2c-1"
    i2c_address: int | None = None  # None: probe 0x20..0x23
    repeated_start: bool = True
    mode: str = "poll"  # "poll" (host-timed single measurements) or "continuous"
    sample_rate_hz: float = 1.0
    cycle_count: int = reg.DEFAULT_CYCLE_COUNT
    self_test: bool = True
    # Storage
    data_dir: Path = Path("/data")
    raw_retention_days: float = 7.0
    rollup_retention_days: float = 365.0
    max_db_mb: float = 1024.0
    flush_interval_s: float = 5.0
    # Orientation reference
    node_config: Path = Path("/config/config.yml")
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    orientation_window_s: float = 60.0
    # Web
    host: str = "0.0.0.0"
    port: int = 3030
    log_level: str = "INFO"
    errors: tuple[str, ...] = field(default=())

    @property
    def db_path(self) -> Path:
        return self.data_dir / "magnetometer.sqlite"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "status.json"

    def public(self) -> dict:
        """The effective configuration, for the UI and the API."""
        return {
            "bus": self.bus,
            "i2c_address": "auto (0x20-0x23)" if self.i2c_address is None else f"0x{self.i2c_address:02X}",
            "i2c_framing": "repeated start" if self.repeated_start else "stop between write and read",
            "mode": self.mode,
            "sample_rate_hz": self.sample_rate_hz,
            "cycle_count": self.cycle_count,
            "self_test": self.self_test,
            "data_dir": str(self.data_dir),
            "raw_retention_days": self.raw_retention_days,
            "rollup_retention_days": self.rollup_retention_days,
            "max_db_mb": self.max_db_mb,
            "flush_interval_s": self.flush_interval_s,
            "node_config": str(self.node_config),
            "orientation_window_s": self.orientation_window_s,
            "port": self.port,
            "errors": list(self.errors),
        }


class _Reader:
    def __init__(self, env: Mapping[str, str]):
        self.env = env
        self.errors: list[str] = []

    def raw(self, name: str) -> str | None:
        value = self.env.get(PREFIX + name)
        if value is None or value.strip() == "":
            return None
        return value.strip()

    def number(self, name: str, default: float, low: float, high: float) -> float:
        text = self.raw(name)
        if text is None:
            return default
        try:
            value = float(text)
        except ValueError:
            self.errors.append(f"{PREFIX}{name}={text!r} is not a number")
            return default
        if not low <= value <= high:
            self.errors.append(f"{PREFIX}{name}={text} is outside {low:g}..{high:g}")
            return default
        return value

    def optional_number(self, name: str, low: float, high: float) -> float | None:
        if self.raw(name) is None:
            return None
        sentinel = float("nan")
        value = self.number(name, sentinel, low, high)
        return None if value != value else value

    def integer(self, name: str, default: int, low: int, high: int) -> int:
        text = self.raw(name)
        if text is None:
            return default
        try:
            value = int(text, 0)
        except ValueError:
            self.errors.append(f"{PREFIX}{name}={text!r} is not an integer")
            return default
        if not low <= value <= high:
            self.errors.append(f"{PREFIX}{name}={text} is outside {low}..{high}")
            return default
        return value

    def choice(self, name: str, default: str, options: tuple[str, ...]) -> str:
        text = self.raw(name)
        if text is None:
            return default
        text = text.lower()
        if text not in options:
            self.errors.append(f"{PREFIX}{name}={text!r} must be one of {', '.join(options)}")
            return default
        return text

    def boolean(self, name: str, default: bool) -> bool:
        text = self.raw(name)
        if text is None:
            return default
        if text.lower() in ("1", "true", "yes", "on"):
            return True
        if text.lower() in ("0", "false", "no", "off"):
            return False
        self.errors.append(f"{PREFIX}{name}={text!r} is not true or false")
        return default


def from_env(env: Mapping[str, str] | None = None) -> Config:
    r = _Reader(os.environ if env is None else env)

    address_text = r.raw("I2C_ADDRESS")
    address: int | None = None
    if address_text is not None and address_text.lower() != "auto":
        try:
            address = int(address_text, 0)
        except ValueError:
            r.errors.append(f"{PREFIX}I2C_ADDRESS={address_text!r} must be auto or 0x20..0x23")
        else:
            if address not in reg.I2C_ADDRESSES:
                r.errors.append(f"{PREFIX}I2C_ADDRESS={address_text} is not an RM3100 address (0x20..0x23)")
                address = None

    cycle_count = r.integer("CYCLE_COUNT", reg.DEFAULT_CYCLE_COUNT, reg.MIN_CYCLE_COUNT, reg.MAX_CYCLE_COUNT)
    mode = r.choice("MODE", "poll", ("poll", "continuous"))
    rate = r.number("SAMPLE_RATE_HZ", 1.0, 0.01, 600.0)
    ceiling = reg.max_xyz_rate_hz(cycle_count)
    if rate > ceiling:
        r.errors.append(
            f"{PREFIX}SAMPLE_RATE_HZ={rate:g} is faster than a {cycle_count}-cycle measurement allows "
            f"({ceiling:.0f} Hz); lower the rate or the cycle count"
        )
        rate = 1.0

    raw_days = r.number("RAW_RETENTION_DAYS", 7.0, 0.01, 3650)
    rollup_days = r.number("ROLLUP_RETENTION_DAYS", 365.0, 1, 36500)
    if rollup_days < raw_days:
        r.errors.append(f"{PREFIX}ROLLUP_RETENTION_DAYS must not be shorter than RAW_RETENTION_DAYS")
        rollup_days = max(365.0, raw_days)

    framing = r.choice("I2C_FRAMING", "repeated-start", ("repeated-start", "stop"))

    return Config(
        bus=r.raw("BUS") or "/dev/i2c-1",
        i2c_address=address,
        repeated_start=framing == "repeated-start",
        mode=mode,
        sample_rate_hz=rate,
        cycle_count=cycle_count,
        self_test=r.boolean("SELF_TEST", True),
        data_dir=Path(r.raw("DATA_DIR") or "/data"),
        raw_retention_days=raw_days,
        rollup_retention_days=rollup_days,
        max_db_mb=r.number("MAX_DB_MB", 1024.0, 16, 1_000_000),
        flush_interval_s=r.number("FLUSH_INTERVAL_S", 5.0, 0.2, 300),
        node_config=Path(r.raw("NODE_CONFIG") or "/config/config.yml"),
        latitude=r.optional_number("LATITUDE", -90, 90),
        longitude=r.optional_number("LONGITUDE", -180, 180),
        altitude_m=r.optional_number("ALTITUDE_M", -500, 10_000),
        orientation_window_s=r.number("ORIENTATION_WINDOW_S", 60.0, 5, 3600),
        host=r.raw("HOST") or "0.0.0.0",
        port=r.integer("PORT", 3030, 1, 65535),
        log_level=r.choice("LOG_LEVEL", "info", ("debug", "info", "warning", "error")).upper(),
        errors=tuple(r.errors),
    )
