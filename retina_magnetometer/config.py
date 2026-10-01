"""Configuration, from environment variables.

Tunables come from the environment, not from the node's config.yml, the same
split retina-telemetry makes: config.yml is retina-gui's document, and
Compose already hands every other service its settings as variables. The one
thing read from config.yml is the node's own location (see ``location.py``),
because that is a fact about the node rather than a setting of this app.

A bad value never stops the app. One with no safe reading is reported as a
configuration error on the status page and in status.json, the sampler stays
off, and the web UI keeps serving: a container that crash-loops over a typo
marks the whole node degraded in Mender and tells the operator less than one
line on a page would. One with a safe reading is used that way and reported
as a warning, and sampling goes on: a poll rate above what poll mode can keep
runs at the most it can (a node that sampled with it before must not stop
after an upgrade), half of a location from the environment is ignored, and an
address or port the page cannot be served on gives way to a safe one (where
the page is served has nothing to do with sampling).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.driver import RM3100

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
    # What MAGNETOMETER_SAMPLE_RATE_HZ asked for, when poll mode could not
    # keep it and samples at ``sample_rate_hz`` instead.
    requested_rate_hz: float | None = None
    # Values with no safe reading: sampling stays off.
    errors: tuple[str, ...] = field(default=())
    # Values used in a safe reading other than the one given: sampling goes on.
    warnings: tuple[str, ...] = field(default=())
    # What valid values add up to that the operator may not expect (the size
    # cap holding less history than the retentions ask for): not a fault.
    notes: tuple[str, ...] = field(default=())

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
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }


# What the data take in the database, measured as the size cap measures them,
# (page_count - freelist_count) * page_size before and after, on the app's own
# schema and write path (storage.Storage: 4 KiB pages, incremental
# auto-vacuum): a day of 1 Hz readings, quantised as the driver hands them
# over and written a minute per transaction through write_samples, took
# 38.45 B a sample, and a day at 10 Hz the same; the roll-up of a week of
# them took 124.75 B a minute summary, as did a month of minute rows written
# straight into the table (124.68). The empty database is seven pages.
SAMPLE_BYTES = 38.45
MINUTE_BYTES = 124.75
EMPTY_DB_BYTES = 7 * 4096


def _span(days: float) -> str:
    if days < 1.0:
        return f"{days * 24:.1f} hours"
    if days < 10.0:
        return f"{days:.1f} days"
    return f"{days:.0f} days"


def _days(days: float) -> str:
    return f"{days:g} day" if days == 1 else f"{days:g} days"


def _capacity_note(max_db_mb: float, stored_rate_hz: float, raw_days: float, rollup_days: float) -> str | None:
    """What the size cap leaves of the history the retentions ask for, when
    that is less than all of it; None when it all fits.

    The cap deletes down to 90 % of itself (storage.Storage._cap): first the
    raw samples the minute summaries already cover, and while the summaries
    must give way as well, the raw samples keep half of that room. Between
    prunes the data grow back towards the cap, so this is the least the
    database holds, not the most.
    """
    room = max_db_mb * 1024 * 1024 * 0.9 - EMPTY_DB_BYTES
    sample_day = stored_rate_hz * 86_400 * SAMPLE_BYTES
    minute_day = 1440 * MINUTE_BYTES
    raw_need, minutes_need = raw_days * sample_day, rollup_days * minute_day
    if raw_need + minutes_need <= room:
        return None
    if minutes_need <= room / 2:
        raw_kept, minutes_kept = room - minutes_need, minutes_need
    elif raw_need <= room / 2:
        raw_kept, minutes_kept = raw_need, room - raw_need
    else:
        raw_kept = minutes_kept = room / 2
    raw = f"{_span(raw_kept / sample_day)} of raw samples at {stored_rate_hz:.3g} Hz, not the {_days(raw_days)} configured"
    minutes = f"{_span(minutes_kept / minute_day)} of minute summaries, not the {_days(rollup_days)} configured"
    held = f"{PREFIX}MAX_DB_MB={max_db_mb:g} holds about"
    if raw_kept >= raw_need:
        return f"{held} {minutes}; the raw samples keep their {_days(raw_days)}"
    if minutes_kept >= minutes_need:
        return f"{held} {raw}; the minute summaries keep their {_days(rollup_days)}"
    return f"{held} {raw}, and about {minutes}"


def _listen_problem(host: str, port: int) -> str | None:
    """Why the page could not be served on ``host``, or None if it can be.

    The lookup waitress makes when it creates the server (getaddrinfo, for a
    passive socket), so an IP address needs no name service and a name is
    looked up exactly as the server would look it up. Whether an address
    belongs to this machine only shows when the server binds to it.
    """
    # waitress reads "*" as every interface, and takes an IPv6 address in
    # brackets.
    if host == "*":
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except (OSError, ValueError) as exc:  # gaierror, or a name the IDNA codec refuses
        return str(exc)
    return None


class _Reader:
    def __init__(self, env: Mapping[str, str]):
        self.env = env
        self.errors: list[str] = []
        self.warnings: list[str] = []

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
    requested_rate: float | None = None
    ceiling = reg.max_xyz_rate_hz(cycle_count)
    # In poll mode the host waits out every measurement, so the conversion is
    # not all a sample costs; a rate in between would lose every other tick.
    poll_ceiling = RM3100.max_poll_rate_hz(cycle_count)
    if rate > ceiling:
        r.errors.append(
            f"{PREFIX}SAMPLE_RATE_HZ={rate:g} is faster than a {cycle_count}-cycle measurement allows "
            f"({ceiling:.0f} Hz); lower the rate or the cycle count"
        )
        rate = 1.0
    elif mode == "poll" and rate > poll_ceiling:
        # Accepted before poll mode had a limit of its own, so a node may
        # have been sampling with it: it goes on, at the most poll mode keeps.
        r.warnings.append(
            f"{PREFIX}SAMPLE_RATE_HZ={rate:g} is more than poll mode can keep up with at {cycle_count} cycles, "
            f"where each sample waits for its measurement: sampling at {poll_ceiling:.3g} Hz of the {rate:g} Hz "
            f"configured; continuous mode goes up to {ceiling:.0f} Hz"
        )
        requested_rate, rate = rate, poll_ceiling

    raw_days = r.number("RAW_RETENTION_DAYS", 7.0, 0.01, 3650)
    rollup_days = r.number("ROLLUP_RETENTION_DAYS", 365.0, 1, 36500)
    max_db_mb = r.number("MAX_DB_MB", 1024.0, 16, 1_000_000)
    if rollup_days < raw_days:
        r.errors.append(f"{PREFIX}ROLLUP_RETENTION_DAYS must not be shorter than RAW_RETENTION_DAYS")
        rollup_days = max(365.0, raw_days)

    framing = r.choice("I2C_FRAMING", "repeated-start", ("repeated-start", "stop"))

    # The environment overrides the node's position only as a latitude and
    # longitude pair (location.resolve). Half of one would otherwise be
    # dropped without a word; it is still dropped, so that nothing downstream
    # can use it, but the page says so.
    latitude = r.optional_number("LATITUDE", -90, 90)
    longitude = r.optional_number("LONGITUDE", -180, 180)
    altitude = r.optional_number("ALTITUDE_M", -500, 10_000)
    location = ("LATITUDE", "LONGITUDE", "ALTITUDE_M")
    given = [name for name in location if r.raw(name) is not None]
    missing = [name for name in location[:2] if r.raw(name) is None]
    if given and missing:
        r.warnings.append(
            f"{' and '.join(PREFIX + name for name in given)} {'is' if len(given) == 1 else 'are'} set without "
            f"{' and '.join(PREFIX + name for name in missing)}, and ignored: the environment overrides the "
            "node's location only with a latitude and a longitude together"
        )
        latitude = longitude = altitude = None

    # Where the page is served. waitress resolves the host and binds when the
    # server is created, so an address it cannot use would otherwise end the
    # app there. It falls back to this machine only, never to every interface
    # the operator did not ask for; a port to the default one.
    port_text = r.raw("PORT")
    port = 3030
    if port_text is not None:
        try:
            port = int(port_text, 0)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            r.warnings.append(f"{PREFIX}PORT={port_text!r} is not a port number (1..65535): the page is served on 3030")
            port = 3030
    host = r.raw("HOST") or "0.0.0.0"
    if r.raw("HOST") is not None:
        problem = _listen_problem(host, port)
        if problem is not None:
            r.warnings.append(
                f"{PREFIX}HOST={host!r} cannot be listened on ({problem}): the page is served on 127.0.0.1 only"
            )
            host = "127.0.0.1"

    self_test = r.boolean("SELF_TEST", True)
    flush_interval = r.number("FLUSH_INTERVAL_S", 5.0, 0.2, 300)
    orientation_window = r.number("ORIENTATION_WINDOW_S", 60.0, 5, 3600)
    log_level = r.choice("LOG_LEVEL", "info", ("debug", "info", "warning", "error")).upper()

    # How much of the history the size cap leaves, at the rate samples are
    # stored at: in continuous mode, the TMRC step's. Only for a configuration
    # that samples; with an error the values above may be stand-ins.
    notes: list[str] = []
    if not r.errors:
        stored_rate = rate
        if mode == "continuous":
            stored_rate = reg.effective_continuous_rate_hz(reg.tmrc_for_rate(rate), cycle_count)
        note = _capacity_note(max_db_mb, stored_rate, raw_days, rollup_days)
        if note is not None:
            notes.append(note)

    return Config(
        bus=r.raw("BUS") or "/dev/i2c-1",
        i2c_address=address,
        repeated_start=framing == "repeated-start",
        mode=mode,
        sample_rate_hz=rate,
        cycle_count=cycle_count,
        self_test=self_test,
        data_dir=Path(r.raw("DATA_DIR") or "/data"),
        raw_retention_days=raw_days,
        rollup_retention_days=rollup_days,
        max_db_mb=max_db_mb,
        flush_interval_s=flush_interval,
        node_config=Path(r.raw("NODE_CONFIG") or "/config/config.yml"),
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude,
        orientation_window_s=orientation_window,
        host=host,
        port=port,
        log_level=log_level,
        requested_rate_hz=requested_rate,
        errors=tuple(r.errors),
        warnings=tuple(r.warnings),
        notes=tuple(notes),
    )
