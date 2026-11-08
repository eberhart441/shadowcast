"""Boards are found by USB serial (the base MAC), never by /dev name. Fake list_ports results only: no port is opened."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from csi.ports import PortResolutionError, match_ports, resolve_receiver_ports
from csi.room import load_room

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "room_rig4.json"
RX1_SERIAL = "38:44:BE:A5:18:B0"
RX2_SERIAL = "10:BD:A3:CC:85:BC"
TX0_SERIAL = "38:44:BE:A5:41:BC"
TX1_SERIAL = "38:44:BE:A5:33:9C"


def usb(device, serial):
    return SimpleNamespace(device=device, serial_number=serial, vid=0x303A)


def room_with_serials(tmp_path, **serials):
    """A copy of the frozen rig4 fixture with the given node serials added (rx1, rx2, tx0, tx1)."""
    config = json.loads(FIXTURE.read_text())
    for name, serial in serials.items():
        config["nodes"][name]["serial"] = serial
    path = tmp_path / "room.json"
    path.write_text(json.dumps(config))
    return load_room(path)


@pytest.fixture
def room(tmp_path):
    return room_with_serials(tmp_path, rx1=RX1_SERIAL, rx2=RX2_SERIAL, tx0=TX0_SERIAL, tx1=TX1_SERIAL)


def test_receivers_resolve_to_their_device_by_serial_whatever_the_name(room):
    listed = [usb("/dev/cu.usbmodem1101", RX1_SERIAL.lower().replace(":", "")),
              usb("/dev/cu.usbmodem2101", RX2_SERIAL)]
    assert resolve_receiver_ports(room, listed) == {"rx1": "/dev/cu.usbmodem1101", "rx2": "/dev/cu.usbmodem2101"}


def test_senders_are_not_needed_for_resolution(room):
    listed = [usb("/dev/cu.usbmodem1101", RX1_SERIAL), usb("/dev/cu.usbmodem2101", RX2_SERIAL)]
    assert set(resolve_receiver_ports(room, listed)) == {"rx1", "rx2"}


def test_a_missing_receiver_is_named_with_its_serial(room):
    with pytest.raises(PortResolutionError) as info:
        resolve_receiver_ports(room, [usb("/dev/cu.usbmodem1101", RX1_SERIAL)])
    message = str(info.value)
    assert "rx2" in message and RX2_SERIAL in message
    assert "rx1" not in message


def test_every_missing_receiver_is_listed(tmp_path):
    room = room_with_serials(tmp_path, rx1=RX1_SERIAL, rx2=RX2_SERIAL)
    with pytest.raises(PortResolutionError) as info:
        resolve_receiver_ports(room, [])
    assert "rx1" in str(info.value) and "rx2" in str(info.value)


def test_a_receiver_without_a_serial_is_reported_as_missing(tmp_path):
    room = room_with_serials(tmp_path, rx1=RX1_SERIAL)  # rx2 has no serial
    with pytest.raises(PortResolutionError) as info:
        resolve_receiver_ports(room, [usb("/dev/cu.usbmodem1101", RX1_SERIAL)])
    assert "rx2" in str(info.value) and "no serial" in str(info.value)


def test_a_disabled_receiver_is_not_required(tmp_path):
    room = room_with_serials(tmp_path, rx1=RX1_SERIAL, rx2=RX2_SERIAL)  # rx3 is enabled: false in the fixture
    listed = [usb("/dev/a", RX1_SERIAL), usb("/dev/b", RX2_SERIAL)]
    assert set(resolve_receiver_ports(room, listed)) == {"rx1", "rx2"}


def test_two_devices_with_one_serial_fail_rather_than_guess(room):
    listed = [usb("/dev/a", RX1_SERIAL), usb("/dev/b", RX1_SERIAL), usb("/dev/c", RX2_SERIAL)]
    with pytest.raises(PortResolutionError) as info:
        resolve_receiver_ports(room, listed)
    assert "rx1" in str(info.value)


def test_match_report_pairs_each_device_with_its_node_and_flags_the_rest(room):
    listed = [usb("/dev/cu.usbmodem1101", RX1_SERIAL),
              usb("/dev/cu.usbmodem9999", "AA:BB:CC:DD:EE:FF"),
              SimpleNamespace(device="/dev/cu.Bluetooth-Incoming-Port", serial_number=None, vid=None)]
    report = match_ports(room, listed)
    assert report.devices == (("/dev/cu.usbmodem1101", RX1_SERIAL, "rx1"),
                              ("/dev/cu.usbmodem9999", "AA:BB:CC:DD:EE:FF", ""))
    assert report.absent == ("rx2", "tx0", "tx1")
    assert report.unset == ()


def test_match_report_lists_nodes_without_a_serial(tmp_path):
    room = room_with_serials(tmp_path, rx1=RX1_SERIAL)
    report = match_ports(room, [usb("/dev/cu.usbmodem1101", RX1_SERIAL)])
    assert report.absent == ()  # a node with no serial cannot be "not connected": it is unset
    assert report.unset == ("rx2", "tx0", "tx1")


def load_ports_script():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import ports as ports_cli
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    return ports_cli


def test_ports_script_lists_matches_and_flags_and_exits_zero(monkeypatch, capsys, tmp_path):
    from serial.tools import list_ports

    room_file = tmp_path / "room.json"
    config = json.loads(FIXTURE.read_text())
    config["nodes"]["rx1"]["serial"] = RX1_SERIAL
    config["nodes"]["rx2"]["serial"] = RX2_SERIAL
    room_file.write_text(json.dumps(config))
    monkeypatch.setattr(list_ports, "comports", lambda: [usb("/dev/cu.usbmodem1101", RX1_SERIAL),
                                                         usb("/dev/cu.usbmodem7777", "AA:BB:CC:DD:EE:FF")])
    ports_cli = load_ports_script()
    assert ports_cli.main(["--room", str(room_file)]) == 0
    out = capsys.readouterr().out.lower()
    assert "/dev/cu.usbmodem1101" in out and RX1_SERIAL.lower() in out and "rx1" in out
    assert "/dev/cu.usbmodem7777" in out and "no node" in out
    assert "rx2" in out and "not connected" in out


def test_ports_script_exits_zero_with_nothing_connected(monkeypatch, capsys):
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda: [])
    ports_cli = load_ports_script()
    assert ports_cli.main(["--room", str(FIXTURE)]) == 0
    assert "no usb serial" in capsys.readouterr().out.lower()
