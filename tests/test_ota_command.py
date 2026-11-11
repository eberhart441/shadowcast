"""The host's side of the maintenance command, the maintenance network and the node's report (csi/ota.py).

The commands are pinned to tests/fixtures/ota_command.json, which the node's decoder (firmware/core/ota_core.c)
replays too; the derived network passphrase and the report tag were computed with openssl.
"""
import json
from pathlib import Path

import pytest

from csi import ota
from csi.round_auth import TEST_KIT_KEY, tag_ok

FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures" / "ota_command.json").read_text())
SHA = bytes.fromhex(FIXTURE["valid"][0]["command"]["image_sha256"])


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda c: c["name"])
def test_the_host_builds_the_golden_command(case):
    c = case["command"]
    data = ota.maintenance_command(c["command"], c["targets"], TEST_KIT_KEY, port=c["port"],
                                   ap_channel=c["ap_channel"], issued=c["issued"], image_size=c["image_size"],
                                   image_sha256=bytes.fromhex(c["image_sha256"]), round_no=c["round"])
    assert data.hex() == case["hex"]
    assert tag_ok(data, TEST_KIT_KEY)


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda c: c["name"])
def test_the_host_reads_the_golden_command(case):
    c = case["command"]
    got = ota.decode_command(bytes.fromhex(case["hex"]))
    assert (got.command, got.round, got.port, got.ap_channel, got.issued, got.image_size) == (
        c["command"], c["round"], c["port"], c["ap_channel"], c["issued"], c["image_size"])
    assert got.image_sha256.hex() == c["image_sha256"]
    assert list(got.targets) == c["targets"]


@pytest.mark.parametrize("case", FIXTURE["invalid"], ids=lambda c: c["name"])
def test_a_malformed_command_is_refused_with_the_nodes_reason(case):
    with pytest.raises(ota.CommandError) as e:
        ota.decode_command(bytes.fromhex(case["hex"]))
    assert e.value.reason == case["reason"]


def test_the_host_will_not_build_a_command_a_node_would_refuse():
    targets = [f"02:00:00:00:{i:02X}:5C" for i in range(1, 15)]
    with pytest.raises(ValueError, match="13"):
        ota.maintenance_command(ota.OTA_CMD_UPDATE, targets, TEST_KIT_KEY, port=8070, ap_channel=36, issued=1,
                                image_size=10, image_sha256=SHA)
    with pytest.raises(ValueError, match="overlaps"):
        ota.maintenance_command(ota.OTA_CMD_UPDATE, targets[:1], TEST_KIT_KEY, port=8070, ap_channel=4, issued=1,
                                image_size=10, image_sha256=SHA, sensing_channel=6)
    with pytest.raises(ValueError, match="serial"):
        ota.maintenance_command(ota.OTA_CMD_REPORT, ["n1"], TEST_KIT_KEY, port=8070, ap_channel=36, issued=1)


@pytest.mark.parametrize("sensing,ap,clear", [(6, 36, True), (6, 1, True), (6, 11, True), (6, 2, False),
                                              (6, 6, False), (1, 5, False), (6, 0, True), (6, 20, False)])
def test_a_maintenance_network_must_not_overlap_the_sensing_channel(sensing, ap, clear):
    assert ota.channels_clear(sensing, ap) is clear


def test_the_maintenance_network_is_derived_from_the_kit_key():
    assert ota.maintenance_network(TEST_KIT_KEY) == ("shadowcast-88a46367", "8c47bb2ddd61da6ee9ecc57d")


LINE = ("ota-report 1 serial=02:00:00:00:02:5C issued=1794000000 outcome=committed slot=ota_1 "
        "image=abababababababab version=v0.3-18-g1a2b3c4")


def test_a_report_with_the_kits_tag_is_read():
    report = ota.parse_report(LINE + " tag=ccad49fd7ccc466e", TEST_KIT_KEY)
    assert report == ota.Report(serial="02:00:00:00:02:5C", issued=1794000000, outcome="committed", slot="ota_1",
                                image="abababababababab", version="v0.3-18-g1a2b3c4")
    assert report.ok


@pytest.mark.parametrize("line", [LINE + " tag=ccad49fd7ccc466f", LINE, LINE.replace("ota_1", "ota_0")
                                  + " tag=ccad49fd7ccc466e", "hello", ""])
def test_a_report_without_the_kits_tag_is_refused(line):
    with pytest.raises(ota.ReportError):
        ota.parse_report(line, TEST_KIT_KEY)


def test_only_committed_already_and_checked_in_are_good_outcomes():
    good = {o for o in ota.OUTCOMES if ota.Report("s", 1, o, "ota_0", "-", "v").ok}
    assert good == {"committed", "already", "checked_in"}
    assert "rolled_back" in ota.OUTCOMES and "refused_image" in ota.OUTCOMES
