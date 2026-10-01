"""Environment configuration: defaults, parsing, and errors that do not crash."""

import socket
from pathlib import Path

import pytest

from retina_magnetometer import config as config_module
from retina_magnetometer.config import from_env
from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.driver import RM3100
from retina_magnetometer.storage import EMPTY_DB_BYTES, MINUTE_ROW_BYTES, SAMPLE_ROW_BYTES


def env(**values):
    return {f"MAGNETOMETER_{k}": str(v) for k, v in values.items()}


def test_defaults_are_a_working_node_setup():
    c = from_env({})
    assert c.bus == "/dev/i2c-1"
    assert c.i2c_address is None  # probe all four
    assert c.mode == "poll" and c.sample_rate_hz == 1.0 and c.cycle_count == 200
    assert c.data_dir == Path("/data") and c.db_path == Path("/data/magnetometer.sqlite")
    assert c.port == 3030
    assert c.errors == ()


def test_everything_can_be_set():
    c = from_env(
        env(
            BUS="tcp://rm3100-sim:9100",
            I2C_ADDRESS="0x21",
            I2C_FRAMING="stop",
            MODE="continuous",
            SAMPLE_RATE_HZ="10",
            CYCLE_COUNT="400",
            SELF_TEST="false",
            DATA_DIR="/tmp/mag",
            RAW_RETENTION_DAYS="2",
            ROLLUP_RETENTION_DAYS="30",
            MAX_DB_MB="256",
            LATITUDE="34.85",
            LONGITUDE="-82.39",
            ALTITUDE_M="300",
            PORT="8099",
        )
    )
    assert c.errors == ()
    assert (c.bus, c.i2c_address, c.repeated_start, c.mode) == ("tcp://rm3100-sim:9100", 0x21, False, "continuous")
    assert (c.sample_rate_hz, c.cycle_count, c.self_test) == (10.0, 400, False)
    assert (c.raw_retention_days, c.rollup_retention_days, c.max_db_mb) == (2.0, 30.0, 256.0)
    assert (c.latitude, c.longitude, c.altitude_m, c.port) == (34.85, -82.39, 300.0, 8099)


@pytest.mark.parametrize(
    "values,fragment",
    [
        (env(CYCLE_COUNT="lots"), "not an integer"),
        (env(CYCLE_COUNT="5000"), "outside"),
        (env(SAMPLE_RATE_HZ="fast"), "not a number"),
        (env(SAMPLE_RATE_HZ="400"), "faster than a 200-cycle measurement allows"),
        (env(MODE="burst"), "must be one of"),
        (env(I2C_ADDRESS="0x40"), "not an RM3100 address"),
        (env(I2C_ADDRESS="banana"), "must be auto"),
        (env(SELF_TEST="maybe"), "true or false"),
        (env(RAW_RETENTION_DAYS="30", ROLLUP_RETENTION_DAYS="7"), "must not be shorter"),
        (env(LATITUDE="95"), "outside"),
    ],
)
def test_bad_values_are_reported_not_raised(values, fragment):
    c = from_env(values)
    assert any(fragment in e for e in c.errors), c.errors


def test_bad_values_fall_back_to_safe_defaults():
    c = from_env(env(CYCLE_COUNT="lots", SAMPLE_RATE_HZ="400"))
    assert c.cycle_count == 200 and c.sample_rate_hz == 1.0


def test_rate_ceiling_follows_the_cycle_count():
    assert from_env(env(SAMPLE_RATE_HZ="100", CYCLE_COUNT="200")).errors == ()
    assert from_env(env(SAMPLE_RATE_HZ="100", CYCLE_COUNT="800")).errors  # ~37 Hz max at 800


@pytest.mark.parametrize("cycle_count", [50, 200, 400, 1000])
def test_poll_mode_runs_a_rate_it_cannot_keep_at_the_most_it_can(cycle_count):
    # A poll-mode sample is a measurement the host waits for: the conversion,
    # a STATUS polling step and the transfers. A rate between that and the
    # conversion limit would lose every other tick. Such a rate was accepted
    # before poll mode had a limit of its own, so a node configured with it
    # keeps sampling, at the limit, and says so; continuous mode, which the
    # chip times itself, takes the rate as it is.
    poll_limit = RM3100.max_poll_rate_hz(cycle_count)
    conversion_limit = reg.max_xyz_rate_hz(cycle_count)
    between = float(f"{(poll_limit + conversion_limit) / 2:.2f}")
    below = f"{poll_limit * 0.99:.2f}"
    kept = from_env(env(SAMPLE_RATE_HZ=below, CYCLE_COUNT=cycle_count))
    assert kept.errors == () and kept.warnings == () and kept.requested_rate_hz is None
    poll = from_env(env(SAMPLE_RATE_HZ=between, CYCLE_COUNT=cycle_count))
    assert poll.errors == ()
    (warning,) = poll.warnings
    assert f"sampling at {poll_limit:.3g} Hz of the {between:g} Hz configured" in warning
    assert "continuous mode" in warning
    assert poll.sample_rate_hz == poll_limit and poll.requested_rate_hz == between
    continuous = from_env(env(SAMPLE_RATE_HZ=between, CYCLE_COUNT=cycle_count, MODE="continuous"))
    assert continuous.errors == continuous.warnings == () and continuous.sample_rate_hz == between


def test_the_conversion_limit_holds_in_both_modes():
    # Faster than any measurement at the cycle count: no safe reading, so a
    # configuration error as before.
    for mode in ("poll", "continuous"):
        c = from_env(env(SAMPLE_RATE_HZ="150", CYCLE_COUNT="200", MODE=mode))
        assert any("faster than a 200-cycle measurement allows (147 Hz)" in e for e in c.errors), c.errors


@pytest.mark.parametrize(
    "values,named",
    [
        (env(LATITUDE="34.85"), "MAGNETOMETER_LATITUDE is set without MAGNETOMETER_LONGITUDE, and ignored"),
        (env(LONGITUDE="-82.39"), "MAGNETOMETER_LONGITUDE is set without MAGNETOMETER_LATITUDE, and ignored"),
        (env(LONGITUDE="-82.39", ALTITUDE_M="300"), "are set without MAGNETOMETER_LATITUDE, and ignored"),
        (
            env(ALTITUDE_M="300"),
            "MAGNETOMETER_ALTITUDE_M is set without MAGNETOMETER_LATITUDE and MAGNETOMETER_LONGITUDE, and ignored",
        ),
    ],
)
def test_half_a_location_is_a_warning_and_ignored(values, named):
    # The environment overrides the node's position only as a latitude and
    # longitude pair. A lone value is dropped, as location.resolve would drop
    # it, but said, and sampling goes on: it needs no location.
    c = from_env(values)
    assert c.errors == ()
    assert any(named in w for w in c.warnings), c.warnings
    assert (c.latitude, c.longitude, c.altitude_m) == (None, None, None)


def test_a_whole_location_or_none_is_fine():
    for values in (
        env(LATITUDE="34.85", LONGITUDE="-82.39"),
        env(LATITUDE="34.85", LONGITUDE="-82.39", ALTITUDE_M="300"),
        env(LATITUDE="", LONGITUDE=""),
    ):
        c = from_env(values)
        assert c.errors == c.warnings == ()


def test_an_impossible_coordinate_is_still_an_error():
    c = from_env(env(LATITUDE="95", LONGITUDE="10"))
    assert any("MAGNETOMETER_LATITUDE=95 is outside" in e for e in c.errors), c.errors


@pytest.mark.parametrize(
    "host,listens_on",
    [
        ("0.0.0.0", "0.0.0.0"),
        ("127.0.0.1", "127.0.0.1"),
        ("::", "::"),
        ("::1", "::1"),
        ("192.168.1.20", "192.168.1.20"),
        # The forms waitress accepts too: the old inet_aton shorthands, every
        # interface as "*", IPv6 in brackets, and a name the system resolves.
        ("127.1", "127.1"),
        ("127.0.0.01", "127.0.0.01"),
        ("*", "*"),
        ("[::1]", "[::1]"),
        ("localhost", "localhost"),
    ],
)
def test_listen_addresses_waitress_can_use_are_taken_as_they_are(host, listens_on):
    c = from_env(env(HOST=host))
    assert c.errors == c.warnings == () and c.host == listens_on


def test_a_name_is_looked_up_the_way_the_server_will_look_it_up(monkeypatch):
    asked = []

    def resolver(host, port, *args, **kwargs):
        asked.append((host, port, kwargs))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", port))]

    monkeypatch.setattr(config_module.socket, "getaddrinfo", resolver)
    c = from_env(env(HOST="node-7.local", PORT="8099"))
    assert c.errors == c.warnings == () and c.host == "node-7.local"
    assert asked == [("node-7.local", 8099, {"type": socket.SOCK_STREAM, "flags": socket.AI_PASSIVE})]


@pytest.mark.parametrize("host", ["no-such-host.invalid", "999.1.1.1", "0.0.0.0:3030", "my host"])
def test_an_address_that_does_not_resolve_is_a_warning_and_this_machine_only(monkeypatch, host):
    # The page must not stop sampling, and must not widen to every interface
    # when the operator asked for something narrower.
    def resolver(name, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    monkeypatch.setattr(config_module.socket, "getaddrinfo", resolver)
    c = from_env(env(HOST=host))
    assert c.errors == ()
    (warning,) = c.warnings
    assert f"MAGNETOMETER_HOST={host!r} cannot be listened on" in warning and "127.0.0.1 only" in warning
    assert c.host == "127.0.0.1"


def test_a_name_the_idna_codec_refuses_is_a_warning_too():
    # Raised before any lookup: a label longer than 63 characters.
    c = from_env(env(HOST="a" * 64 + ".example"))
    assert c.errors == () and c.host == "127.0.0.1"
    assert "label empty or too long" in c.warnings[0]


@pytest.mark.parametrize("port", ["0", "65536", "http", "30.30"])
def test_a_port_that_cannot_be_used_is_a_warning_and_the_default(port):
    c = from_env(env(PORT=port))
    assert c.errors == () and c.port == 3030
    (warning,) = c.warnings
    assert f"MAGNETOMETER_PORT={port!r} is not a port number" in warning


def test_warnings_reach_the_public_view():
    public = from_env(env(LATITUDE="34.85")).public()
    assert public["errors"] == [] and len(public["warnings"]) == 1


def held_days(cap_mb, rate_hz, share=None, minute_days=365):
    """What the size cap leaves for raw samples, from the row sizes the
    storage tests measure."""
    room = cap_mb * 1024 * 1024 * 0.9 - EMPTY_DB_BYTES
    raw_room = room * share if share is not None else room - minute_days * 1440 * MINUTE_ROW_BYTES
    return raw_room / (rate_hz * 86_400 * SAMPLE_ROW_BYTES)


def test_a_default_configuration_has_no_capacity_note():
    assert from_env({}).notes == ()
    assert from_env(env(SAMPLE_RATE_HZ="10")).notes == ()  # a week at 10 Hz is ~232 MB


def test_a_cap_too_small_for_the_raw_retention_says_how_much_it_holds():
    # The cap deletes the oldest raw samples first, the minute summaries
    # keep their year: the history is shorter than configured, on purpose or
    # not, and the operator should know which.
    c = from_env(env(MAX_DB_MB="200", SAMPLE_RATE_HZ="10"))
    (note,) = c.notes
    assert note == (
        f"MAGNETOMETER_MAX_DB_MB=200 holds about {held_days(200, 10):.1f} days of raw samples at 10 Hz, "
        "not the 7 days configured; the minute summaries keep their 365 days"
    )
    assert c.errors == c.warnings == ()


def test_a_cap_too_small_for_the_minute_summaries_cuts_both():
    # Once the summaries give way too, the raw samples keep half the room.
    c = from_env(env(MAX_DB_MB="16"))
    room = 16 * 1024 * 1024 * 0.9 - EMPTY_DB_BYTES
    minute_days = room / 2 / (1440 * MINUTE_ROW_BYTES)
    (note,) = c.notes
    assert note == (
        f"MAGNETOMETER_MAX_DB_MB=16 holds about {held_days(16, 1, share=0.5):.1f} days of raw samples at 1 Hz, "
        f"not the 7 days configured, and about {minute_days:.0f} days of minute summaries, not the 365 days configured"
    )


def test_raw_samples_that_fit_keep_their_retention_while_the_minutes_give_way():
    c = from_env(env(MAX_DB_MB="16", RAW_RETENTION_DAYS="1"))
    room = 16 * 1024 * 1024 * 0.9 - EMPTY_DB_BYTES
    minute_days = (room - 86_400 * SAMPLE_ROW_BYTES) / (1440 * MINUTE_ROW_BYTES)
    (note,) = c.notes
    assert note == (
        f"MAGNETOMETER_MAX_DB_MB=16 holds about {minute_days:.0f} days of minute summaries, "
        "not the 365 days configured; the raw samples keep their 1 day"
    )


def test_continuous_mode_counts_the_rate_the_chip_stores_at():
    # 37 Hz asks for TMRC 0x96, 37.5 Hz; a short history reads in hours.
    (note,) = from_env(env(MAX_DB_MB="100", MODE="continuous", SAMPLE_RATE_HZ="37")).notes
    assert "hours of raw samples at 37.5 Hz" in note


def test_no_capacity_note_while_the_configuration_has_errors():
    c = from_env(env(MAX_DB_MB="16", CYCLE_COUNT="lots"))
    assert c.errors and c.notes == ()


def test_notes_reach_the_public_view():
    public = from_env(env(MAX_DB_MB="16")).public()
    assert public["errors"] == public["warnings"] == [] and len(public["notes"]) == 1


def test_blank_values_mean_default():
    assert from_env(env(BUS="", PORT="  ")).bus == "/dev/i2c-1"


def test_public_view_names_the_framing_and_address():
    public = from_env(env(I2C_ADDRESS="auto")).public()
    assert public["i2c_address"] == "auto (0x20-0x23)"
    assert public["i2c_framing"] == "repeated start"
