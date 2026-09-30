"""The orientation reference position: environment first, then node config."""

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
