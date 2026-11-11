"""The dashboard server on round broadcast sources, and dashboard.py --simulate.

Sources are (host time, round broadcast bytes) streams from the simulated transceiver rig. Assertions are on the
HTTP interface (/api/state) and on the CLI contract.
"""
import json
import sys
import urllib.request
from pathlib import Path

import pytest

from csi.room import Walk, load_room
from csi.server import DashboardServer
from csi.simulate import simulate_rounds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dashboard as cli  # noqa: E402

TR8 = ROOT / "tests" / "fixtures" / "room_tr8.json"
ROOM = load_room(TR8)
T0 = 1_700_000_000.0


def get_state(server):
    with urllib.request.urlopen(server.url + "/api/state", timeout=10) as response:
        return json.loads(response.read())


def test_the_server_reads_round_broadcast_sources_into_link_states():
    walk = Walk(((1.0, 1.5), (3.0, 1.5)), speed_mps=0.4, loop=True)   # up and down the n8-n4 line
    stream = simulate_rounds(ROOM, walk, duration_s=17.0, quiet_first_s=14.0, seed=5, t0=T0)
    server = DashboardServer(ROOM, {"gateway": stream}, mode="auto", port=0)
    server.start()
    try:
        assert server.wait_finished(timeout=60)
        payload = get_state(server)
    finally:
        server.stop()
    states = {row["id"]: row["state"] for row in payload["links"]}
    assert len(states) == 56
    assert states["n8<-n4"] == "MOTION" and states["n4<-n8"] == "MOTION"
    assert states["n1<-n2"] == "EMPTY"                         # the top wall, 1.5 m from the walker
    assert payload["status"]["room"]["state"] == "MOTION"
    assert payload["status"]["receivers"] == {"gateway": "finished"}
    assert payload["status"]["broadcasts"]["accepted"] == 8 * 17 * 20
    assert sum(payload["status"]["broadcasts"]["rejected"].values()) == 0
    assert payload["status"]["uncalibrated_links"] == []      # auto mode: every link judges itself


def test_a_source_mixing_bad_bytes_keeps_running_and_counts_them():
    good = list(simulate_rounds(ROOM, duration_s=1.0, t0=T0))
    stream = [good[0], (good[0][0], b"\x09garbage")] + good[1:]
    server = DashboardServer(ROOM, {"gateway": iter(stream)}, mode="auto", port=0)
    server.start()
    try:
        assert server.wait_finished(timeout=30)
        payload = get_state(server)
    finally:
        server.stop()
    assert payload["status"]["broadcasts"]["rejected"]["short"] == 1
    assert sum(payload["status"]["broadcasts"]["rejected"].values()) == 1
    assert payload["status"]["broadcasts"]["accepted"] == len(good)
    assert payload["status"]["receivers"] == {"gateway": "finished"}


def test_cli_simulate_serves_the_transceiver_room_in_auto_mode():
    args = cli.build_parser().parse_args(["--simulate", "--room", str(TR8), "--http-port", "0"])
    cli.check_source(args)
    assert cli.server_mode(args) == "auto"
    server = cli.build_server(args)
    server.start()
    try:
        payload = get_state(server)
        for _ in range(100):
            if payload["status"]["broadcasts"]["accepted"] >= 16:
                break
            server.wait_finished(timeout=0.05)
            payload = get_state(server)
    finally:
        server.stop()
    assert len(payload["links"]) == 56
    assert payload["status"]["broadcasts"]["accepted"] >= 16
    assert {n["role"] for n in payload["nodes"]} == {"transceiver"}


@pytest.mark.parametrize("extra, message", [
    (["--thresholds", "links.json"], "adaptive"),
    (["--raw-log", "raw.tsv"], "--raw-log"),
    (["--simulate-room"], "one source"),
])
def test_cli_simulate_refuses_what_does_not_apply(extra, message, capsys):
    args = cli.build_parser().parse_args(["--simulate", "--room", str(TR8), *extra])
    with pytest.raises(SystemExit) as err:
        cli.check_source(args)
    assert message in str(err.value)


def test_cli_simulate_needs_a_room_with_transceivers():
    args = cli.build_parser().parse_args(["--simulate", "--room", str(ROOT / "tests" / "fixtures" / "room_rig4.json")])
    cli.check_source(args)
    with pytest.raises(SystemExit) as err:
        cli.build_server(args)
    assert "transceiver" in str(err.value)
