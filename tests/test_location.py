"""The orientation reference position: environment first, then node config."""

import logging

import pytest

from retina_magnetometer.config import from_env
from retina_magnetometer.location import from_node_config, resolve


def test_environment_wins(tmp_path):
    node = tmp_path / "config.yml"
    node.write_text("location:\n  rx:\n    latitude: 10\n    longitude: 20\n    altitude: 5\n")
    c = from_env(
        {"MAGNETOMETER_LATITUDE": "34.85", "MAGNETOMETER_LONGITUDE": "-82.39", "MAGNETOMETER_NODE_CONFIG": str(node)}
    )
    here = resolve(c)
    assert (here.latitude, here.longitude, here.source) == (34.85, -82.39, "environment")


def test_reads_retina_node_merged_config(tmp_path):
    node = tmp_path / "config.yml"
    node.write_text(
        "node_id: ret-test\nlocation:\n  rx:\n    latitude: 34.9\n    longitude: -82.2\n    altitude: 280\n    name: roof\n"
    )
    here = resolve(from_env({"MAGNETOMETER_NODE_CONFIG": str(node)}))
    assert (here.latitude, here.longitude, here.altitude_m) == (34.9, -82.2, 280.0)
    assert "node config" in here.source


def test_unset_location_in_a_fresh_node_config(tmp_path):
    node = tmp_path / "config.yml"
    node.write_text("location:\n  rx:\n    latitude: null\n    longitude: null\n    altitude: null\n")
    assert from_node_config(node) is None


def test_missing_or_broken_file(tmp_path):
    assert from_node_config(tmp_path / "absent.yml") is None
    broken = tmp_path / "broken.yml"
    broken.write_text("location: [unclosed")
    assert from_node_config(broken) is None


def test_unset_altitude_is_sea_level(tmp_path):
    node = tmp_path / "config.yml"
    node.write_text("location:\n  rx:\n    latitude: 1.5\n    longitude: 2.5\n")
    assert from_node_config(node).altitude_m == 0.0


@pytest.mark.parametrize(
    "text",
    [
        'location: "somewhere"\n',
        "- one\n- two\n",
        "42\n",
        "location:\n  - rx\n",
        "location:\n  rx: [34.9, -82.2]\n",
        "location:\n  rx: somewhere\n",
    ],
)
def test_a_node_config_of_another_shape_is_no_location(tmp_path, caplog, text):
    # retina-gui writes this file; nothing in it may stop the app (it used to
    # end it with an AttributeError, and restart: always made that a loop).
    node = tmp_path / "config.yml"
    node.write_text(text)
    with caplog.at_level(logging.WARNING, logger="retina_magnetometer.location"):
        assert from_node_config(node) is None
        assert resolve(from_env({"MAGNETOMETER_NODE_CONFIG": str(node)})) is None
    assert "location.rx" in caplog.text


def test_unset_keys_are_no_location_without_a_warning(tmp_path, caplog):
    node = tmp_path / "config.yml"
    for text in ("", "node_id: ret-1\n", "location:\n", "location:\n  rx:\n"):
        node.write_text(text)
        with caplog.at_level(logging.WARNING, logger="retina_magnetometer.location"):
            assert from_node_config(node) is None
    assert caplog.text == ""


def test_a_file_that_is_not_text_or_not_a_file_is_no_location(tmp_path):
    binary = tmp_path / "config.yml"
    binary.write_bytes(b"\xff\xfe\x00location")
    assert from_node_config(binary) is None
    # Docker creates a directory where a bind-mounted file is missing.
    directory = tmp_path / "missing.yml"
    directory.mkdir()
    assert from_node_config(directory) is None


def test_numbers_that_are_not_finite_or_not_plausible(tmp_path):
    node = tmp_path / "config.yml"
    node.write_text("location:\n  rx:\n    latitude: .nan\n    longitude: 20\n")
    assert from_node_config(node) is None
    node.write_text("location:\n  rx:\n    latitude: 10\n    longitude: .inf\n")
    assert from_node_config(node) is None
    # A bad altitude would skew the reference field without a word; sea level
    # is what an unset one means already.
    for altitude in (".nan", "-.inf", "50000"):
        node.write_text(f"location:\n  rx:\n    latitude: 10\n    longitude: 20\n    altitude: {altitude}\n")
        assert from_node_config(node).altitude_m == 0.0


@pytest.mark.parametrize(
    "text",
    [
        # PyYAML turns this into a date, and raises ValueError for a month 13.
        "updated: 2026-13-45\nlocation:\n  rx:\n    latitude: 10\n    longitude: 20\n",
        "location:\n  rx:\n    latitude: 2026-02-30\n    longitude: 20\n",
    ],
)
def test_a_date_that_is_not_one_anywhere_in_the_file_is_no_location(tmp_path, caplog, text):
    node = tmp_path / "config.yml"
    node.write_text(text)
    with caplog.at_level(logging.WARNING, logger="retina_magnetometer.location"):
        assert from_node_config(node) is None
    assert "could not read a location" in caplog.text


def test_integers_too_big_for_a_float_are_no_number(tmp_path):
    node = tmp_path / "config.yml"
    node.write_text("location:\n  rx:\n    latitude: " + "9" * 400 + "\n    longitude: 20\n")
    assert from_node_config(node) is None
    node.write_text("location:\n  rx:\n    latitude: 10\n    longitude: 20\n    altitude: " + "9" * 400 + "\n")
    assert from_node_config(node).altitude_m == 0.0  # as an unset altitude
