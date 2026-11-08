"""Nodes carry their board's USB serial (its base MAC) from the room config; it is optional."""
import json
from pathlib import Path

from csi.room import load_room

COMMITTED = Path(__file__).resolve().parents[1] / "config" / "room.json"


def write_room(tmp_path, nodes):
    path = tmp_path / "room.json"
    path.write_text(json.dumps({"room": {"width_m": 2.0, "depth_m": 1.0}, "nodes": nodes}))
    return path


def test_serial_is_read_per_node_and_defaults_to_empty(tmp_path):
    room = load_room(write_room(tmp_path, {
        "rx1": {"role": "receiver", "x": 0, "y": 0, "serial": "38:44:BE:A5:18:B0"},
        "rx2": {"role": "receiver", "x": 2, "y": 0},
        "tx0": {"role": "sender", "mac": "1a:00:00:00:00:00", "x": 2, "y": 1, "serial": "38:44:BE:A5:41:BC"},
    }))
    serials = {n.name: n.serial for n in (*room.receivers, *room.senders)}
    assert serials == {"rx1": "38:44:BE:A5:18:B0", "rx2": "", "tx0": "38:44:BE:A5:41:BC"}


def test_port_hint_is_not_read_into_the_node(tmp_path):
    room = load_room(write_room(tmp_path, {
        "rx1": {"role": "receiver", "x": 0, "y": 0, "port_hint": "/dev/cu.usbmodem1101"},
    }))
    assert not hasattr(room.receivers[0], "port_hint")
    assert room.receivers[0].serial == ""


def test_committed_config_carries_the_known_serials_and_no_port_hints():
    config = json.loads(COMMITTED.read_text())
    serials = {name: spec.get("serial") for name, spec in config["nodes"].items()}
    assert serials["rx1"] == "38:44:BE:A5:18:B0"
    assert serials["rx2"] == "10:BD:A3:CC:85:BC"
    assert serials["tx0"] == "38:44:BE:A5:41:BC"
    assert serials["tx1"] == "38:44:BE:A5:33:9C"
    assert not any("port_hint" in spec for spec in config["nodes"].values())
