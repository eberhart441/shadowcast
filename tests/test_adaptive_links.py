"""Auto mode end to end: LinkMonitor, DashboardState payload, HTTP, CLI mode choice, noisy calibration.

Driven by simulated (receiver, packet) streams through the text seam (csi.simulate), never a real port.
"""
import dataclasses
import json
import sys
from pathlib import Path

import pytest

from csi.dashboard import DashboardState
from csi.links import UNCALIBRATED, WARMUP, LinkId, LinkMonitor, calibrate_links_report
from csi.live_calibration import RequestError, parse_request
from csi.room import load_room
from csi.server import default_walk
from csi.simulate import SENDER_MACS, Segment, simulate_links, simulate_room_links

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dashboard as cli  # noqa: E402

ROOM = load_room()
T0 = 1_700_000_000.0
LABELS = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01"}


# Segments after the first continue the device clock (device_offset_us): a clock that goes back reads as a receiver
# reboot, which restarts the adaptive baseline (see test_a_reboot_with_a_new_gain_is_relearned_after_a_new_warm_up).
def empty(seconds, seed=1, t0=T0):
    return list(simulate_room_links(person=None, duration_s=seconds, seed=seed, t0=t0,
                                    device_offset_us=round((t0 - T0) * 1e6)))


def walk(seconds, seed=2, t0=T0):
    return list(simulate_room_links(person=default_walk(ROOM), duration_s=seconds, seed=seed, t0=t0,
                                    device_offset_us=round((t0 - T0) * 1e6)))


def rebooted(packets, factor, after_host_ts):
    """Packets from ``after_host_ts`` on come from receivers that rebooted: their device clock starts again near 0 and
    their re-rolled gain scales the amplitudes by ``factor``."""
    import numpy as np
    out, offset = [], None
    for receiver, pkt in packets:
        if pkt.host_ts >= after_host_ts:
            offset = pkt.device_ts if offset is None else offset
            pkt = dataclasses.replace(pkt, amplitude=np.asarray(pkt.amplitude) * factor,
                                      device_ts=pkt.device_ts - offset + 1000)
        out.append((receiver, pkt))
    return out


def feed(state, packets):
    for receiver, pkt in packets:
        state.push(receiver, pkt)


def by_id(payload):
    return {row["id"]: row for row in payload["links"]}


# ---------------------------------------------------------------- LinkMonitor and DashboardState (auto)


def test_auto_mode_with_no_thresholds_warms_up_then_every_link_is_usable():
    monitor = LinkMonitor({}, mode="auto")
    states = {}
    for receiver, pkt in empty(20, seed=3):
        state = monitor.push(receiver, pkt)
        states.setdefault(LinkId(receiver, pkt.mac).label, []).append((pkt.host_ts - T0, state))
    for label in LABELS:
        early = {s for t, s in states[label] if t < 9.0}
        late = {s for t, s in states[label] if t > 12.0}
        assert early == {WARMUP}, label
        assert UNCALIBRATED not in late and late == {"EMPTY"}, label


def test_auto_payload_reports_mode_elevated_level_and_room_state_and_is_json_serialisable():
    state = DashboardState(ROOM, mode="auto")
    feed(state, empty(16, seed=4))
    payload = state.snapshot()
    json.dumps(payload)
    rows = by_id(payload)
    for label in LABELS:
        row = rows[label]
        assert row["mode"] == "auto"
        assert row["elevated"] is False
        assert row["level"] is not None and row["level"] < 1.0
    assert payload["status"]["room"]["state"] == "EMPTY"


@pytest.mark.slow
def test_a_reboot_with_a_new_gain_is_relearned_after_a_new_warm_up():
    # The old test scaled the amplitudes with no reboot and expected the level to be relearned in about 30 s.
    # The baseline is now frozen while a link is elevated (someone moving stays detected for up to 5 minutes),
    # so a scale change is relearned quickly only when the scorer restarts, which a receiver reboot (the event
    # that re-rolls the gain) always does.
    packets = empty(60, seed=5)
    reboot_at = T0 + 20.0
    state = DashboardState(ROOM, mode="auto")
    seen_after = {label: [] for label in LABELS}
    for i, (receiver, pkt) in enumerate(rebooted(packets, 2.5, reboot_at)):  # score x6 after the reboot
        state.push(receiver, pkt)
        if i % 100 == 0 and pkt.host_ts > reboot_at:
            rows = by_id(state.snapshot(pkt.host_ts))
            for label in LABELS:
                seen_after[label].append((pkt.host_ts - reboot_at, rows[label]["state"]))
    for label in LABELS:
        early = {s for t, s in seen_after[label] if 0.5 < t < 9.0}
        late = [s for t, s in seen_after[label] if t > 14.0]
        assert early == {WARMUP}, (label, early)
        assert len(late) > 20 and set(late) == {"EMPTY"}, (label, set(late))


@pytest.mark.slow
def test_a_person_walking_makes_the_room_motion_via_at_least_two_links():
    state = DashboardState(ROOM, mode="auto")
    feed(state, empty(14, seed=6))
    room_states, max_elevated = set(), 0
    for receiver, pkt in walk(30, seed=7, t0=T0 + 14.0):
        state.push(receiver, pkt)
        if int(pkt.host_ts * 10) % 5 == 0:  # sample the payload every 0.5 s
            payload = state.snapshot(pkt.host_ts)
            room_states.add(payload["status"]["room"]["state"])
            max_elevated = max(max_elevated, len(payload["status"]["room"]["elevated"]))
    assert "MOTION" in room_states
    assert max_elevated >= 2


def test_set_threshold_turns_that_link_to_fixed_and_leaves_the_others_adaptive():
    state = DashboardState(ROOM, mode="auto")
    feed(state, empty(12, seed=8))
    state.set_threshold("rx1<-00", 1e9)  # a fixed threshold no score can reach
    feed(state, walk(15, seed=9, t0=T0 + 12.0))
    rows = by_id(state.snapshot())
    assert rows["rx1<-00"]["mode"] == "fixed"
    assert rows["rx1<-00"]["state"] == "EMPTY"
    assert rows["rx2<-00"]["mode"] == "auto"


def test_fixed_mode_default_keeps_the_old_threshold_states_and_reports_mode_fixed():
    state = DashboardState(ROOM, {"rx1<-00": 1e9})
    feed(state, empty(12, seed=10))
    rows = by_id(state.snapshot())
    assert rows["rx1<-00"]["mode"] == "fixed"
    assert rows["rx2<-00"]["mode"] == "fixed"
    assert rows["rx2<-00"]["state"] == UNCALIBRATED


# ---------------------------------------------------------------- CLI mode choice


def test_cli_fixed_is_the_default_for_live_sources_with_or_without_a_thresholds_file(tmp_path):
    parser = cli.build_parser()
    for argv in (["--port", "rx1=/dev/cu.x"], ["--simulate-room"]):
        assert cli.server_mode(parser.parse_args(argv)) == "fixed", argv
    path = tmp_path / "links.json"
    path.write_text('{"rx1<-00": 3.0}')
    with_file = parser.parse_args(["--simulate-room", "--thresholds", str(path)])
    assert cli.server_mode(with_file) == "fixed"


def test_cli_auto_only_with_the_auto_threshold_flag():
    parser = cli.build_parser()
    assert cli.server_mode(parser.parse_args(["--port", "rx1=/dev/cu.x", "--auto-threshold"])) == "auto"
    assert cli.server_mode(parser.parse_args(["--simulate-room", "--auto-threshold"])) == "auto"


def test_cli_simulate_room_without_a_file_is_calibrated_fixed_not_auto():
    args = cli.build_parser().parse_args(["--simulate-room"])
    streams, thresholds = cli.build_sources(args, ROOM)
    assert thresholds is not None and set(thresholds) == LABELS  # the simulated empty-room calibration
    assert all(value > 0 for value in thresholds.values())
    _, auto_thresholds = cli.build_sources(cli.build_parser().parse_args(["--simulate-room", "--auto-threshold"]), ROOM)
    assert auto_thresholds is None


def test_cli_auto_threshold_forces_auto_even_with_a_file(tmp_path):
    path = tmp_path / "links.json"
    path.write_text('{"rx1<-00": 3.0}')
    args = cli.build_parser().parse_args(["--port", "rx1=/dev/cu.x", "--thresholds", str(path), "--auto-threshold"])
    assert cli.server_mode(args) == "auto"


# ---------------------------------------------------------------- noisy calibration


def mixed_clip(seed=40):
    """10 s empty, then 15 s of someone walking: a clip the quiet-room rules would refuse."""
    quiet = list(simulate_links(senders=SENDER_MACS, receivers=("rx1", "rx2"),
                                scenario=[Segment("empty", 10)], seed=seed))
    busy = list(simulate_links(senders=SENDER_MACS, receivers=("rx1", "rx2"),
                               scenario=[Segment("motion", 15)], seed=seed + 1))
    t_end = quiet[-1][1].host_ts
    shifted = [(r, dataclasses.replace(p, host_ts=p.host_ts + t_end - busy[0][1].host_ts + 0.01,
                                       device_ts=p.device_ts + int(round((t_end - busy[0][1].host_ts + 0.01) * 1e6))))
               for r, p in busy]
    return quiet + shifted


@pytest.mark.slow
def test_quiet_mode_refuses_a_busy_clip_but_noisy_mode_calibrates_it_with_a_warning():
    clip = mixed_clip()
    quiet = calibrate_links_report(clip, margin=1.25)
    assert quiet.skipped  # the quiet-room rules refuse links for movement or too little quiet data
    noisy = calibrate_links_report(clip, margin=1.25, noisy=True)
    assert noisy.calibrations, "noisy mode must calibrate (never refuse for movement)"
    assert all(t > 0 for t in noisy.thresholds.values())
    for label, why in noisy.skipped.items():  # whatever is still skipped is not refused for movement
        assert "movement" not in why and "quiet" not in why, (label, why)
    for cal in noisy.calibrations.values():
        assert "noisy" in cal.warning


def test_calibration_requests_default_to_the_quiet_rules_and_noisy_is_opt_in():
    # the quiet rules are the default now: the noisy p97 rule lets movement in the clip lift the threshold
    assert parse_request({})["noisy"] is False
    assert parse_request({"noisy": True})["noisy"] is True
    with pytest.raises(RequestError):
        parse_request({"noisy": "yes"})


def test_the_default_calibration_records_a_minute_of_empty_room():
    request = parse_request({})
    assert request["clips"] * request["seconds"] >= 60


# ---------------------------------------------------------------- HTTP seam


def test_http_auto_mode_without_thresholds_serves_adaptive_links_and_a_threshold_round_trip():
    import urllib.request

    from csi.server import DashboardServer
    from csi.simulate import simulate_room

    streams = simulate_room(None, room=ROOM, duration_s=16, seed=11)
    server = DashboardServer(ROOM, streams, None, port=0, mode="auto")
    server.start()
    try:
        assert server.wait_finished(timeout=30)
        with urllib.request.urlopen(server.url + "/api/state", timeout=5) as resp:
            payload = json.loads(resp.read())
        assert {row["mode"] for row in payload["links"]} == {"auto"}
        assert "UNCALIBRATED" not in {row["state"] for row in payload["links"]}
        assert payload["status"]["uncalibrated_links"] == []
        assert sorted(payload["status"]["adaptive_links"]) == sorted(LABELS)
        assert payload["status"]["room"]["state"] in {"EMPTY", "MOTION", "WARMUP"}
        status, body = server.command("/api/threshold", {"link": "rx1<-00", "value": 3.0})
        assert status == 200 and body["threshold"] == 3.0
        status, body = server.command("/api/threshold", {"link": "rx1<-00", "value": None})
        assert status == 200 and body["threshold"] is None
    finally:
        server.stop()
