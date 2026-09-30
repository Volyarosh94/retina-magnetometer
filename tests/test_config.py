"""Environment configuration: defaults, parsing, and errors that do not crash."""

from pathlib import Path

import pytest

from retina_magnetometer.config import from_env


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


def test_blank_values_mean_default():
    assert from_env(env(BUS="", PORT="  ")).bus == "/dev/i2c-1"


def test_public_view_names_the_framing_and_address():
    public = from_env(env(I2C_ADDRESS="auto")).public()
    assert public["i2c_address"] == "auto (0x20-0x23)"
    assert public["i2c_framing"] == "repeated start"
