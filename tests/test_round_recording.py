"""Round recordings: a run's round broadcasts and labels written to one binary file, loaded back and replayed.

Assertions are on what a reader of the file gets back (csi.round_recording) and on the dashboard payload a replay
produces, never on the bytes the writer lays down (except to build files a reader must refuse).
"""
import json
import math
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from csi.dashboard import DashboardState
from csi.room import Walk, load_room
from csi.replay import ReplayError, rounds_dashboard
from csi.round_recording import RecordingFormatError, RoundRecorder, load_rounds
from csi.server import DashboardServer
from csi.simulate import simulate_gateways, simulate_rounds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dashboard as cli  # noqa: E402

TR8 = ROOT / "tests" / "fixtures" / "room_tr8.json"
ROOM = load_room(TR8)
T0 = 1_700_000_000.0


def test_a_recording_gives_back_every_broadcast_with_its_host_time_and_source_and_the_labels(tmp_path):
    heard = list(simulate_rounds(ROOM, duration_s=0.5, t0=T0))
    path = tmp_path / "run.rounds"
    with RoundRecorder(path, ROOM, ["gw1", "gw2"], notes="bench, door shut") as rec:
        rec.label("empty", host_ts=T0)
        for k, (host_ts, data) in enumerate(heard):
            rec.write("gw1" if k % 3 else "gw2", host_ts, data)
        rec.label("walking", host_ts=T0 + 0.25, mark="A3")
    loaded = load_rounds(path)
    assert [(b.host_ts, b.source, b.data) for b in loaded.broadcasts] == \
        [(host_ts, "gw1" if k % 3 else "gw2", data) for k, (host_ts, data) in enumerate(heard)]
    assert [(lab.host_ts, lab.label, lab.extra) for lab in loaded.labels] == \
        [(T0, "empty", {}), (T0 + 0.25, "walking", {"mark": "A3"})]
    assert loaded.sources == ("gw1", "gw2")
    assert loaded.notes == "bench, door shut"
    assert loaded.room == ROOM
    assert loaded.truncated_bytes == 0


WALK = Walk(((1.0, 1.5), (3.0, 1.5)), speed_mps=0.4, loop=True)   # up and down the n8-n4 line


def judged(state, host_ts):
    payload = state.snapshot(host_ts)
    return {row["id"]: row["state"] for row in payload["links"]}, payload["status"]["room"]["state"]


def labelled_sim_run(path, **sim):
    """A simulated 8-node run (a walker after 14 s quiet, a node dying, lossy links) fed live to a DashboardState
    through a recorder; returns the live (link states, room state) every 0.5 s of host time."""
    stream = simulate_rounds(ROOM, WALK, duration_s=20.0, quiet_first_s=14.0, dead_after={"n3": 17.0},
                             drop_prob=0.05, seed=3, t0=T0, **sim)
    live, seen, next_check = DashboardState(ROOM, mode="auto"), [], T0 + 0.5
    with RoundRecorder(path, ROOM, ["gw1"]) as rec:
        rec.label("empty", host_ts=T0)
        rec.label("walking", host_ts=T0 + 14.0)
        for host_ts, data in rec.tee({"gw1": stream})["gw1"]:
            while host_ts >= next_check:
                seen.append(judged(live, next_check))
                next_check += 0.5
            live.push_broadcast(host_ts, data)
    return seen


def test_a_labelled_8_node_recording_replays_to_the_live_link_states_and_room_state(tmp_path):
    live = labelled_sim_run(tmp_path / "run.rounds")
    recording = load_rounds(tmp_path / "run.rounds")
    replay, seen, next_check = DashboardState(recording.room, mode="auto"), [], T0 + 0.5
    for host_ts, data in recording.streams(speed=math.inf)["gw1"]:
        while host_ts >= next_check:
            seen.append(judged(replay, next_check))
            next_check += 0.5
        replay.push_broadcast(host_ts, data)
    assert seen == live and len(seen) == 39
    links_end, room_end = live[-1]
    assert room_end == "MOTION" and links_end["n8<-n4"] == "MOTION" and links_end["n4<-n3"] == "NO_SIGNAL"
    assert recording.label_at(T0 - 1) is None
    assert (recording.label_at(T0 + 5).label, recording.label_at(T0 + 15).label) == ("empty", "walking")


def small_recording(path):
    with RoundRecorder(path, ROOM, ["gw1"]) as rec:
        rec.label("empty", host_ts=T0)
        for host_ts, data in simulate_rounds(ROOM, duration_s=0.2, t0=T0):
            rec.write("gw1", host_ts, data)
    return path.read_bytes()


def test_a_recording_of_another_format_version_is_refused_naming_the_version(tmp_path):
    blob = bytearray(small_recording(tmp_path / "run.rounds"))
    blob[8:10] = (2).to_bytes(2, "little")             # the version follows the 8-byte magic
    (tmp_path / "v2.rounds").write_bytes(bytes(blob))
    with pytest.raises(RecordingFormatError, match="format version 2; this build reads version 1 only"):
        load_rounds(tmp_path / "v2.rounds")


@pytest.mark.parametrize("content, message", [
    (b"", "not a round recording"),
    (b"host_ts,device_ts,rssi\n1.0,2,3\n", "not a round recording"),
    (b"SCROUNDS\x01\x00\xff\x00\x00\x00{", "header is cut short"),
])
def test_a_file_that_is_not_a_round_recording_is_refused_cleanly(tmp_path, content, message):
    path = tmp_path / "x.rounds"
    path.write_bytes(content)
    with pytest.raises(RecordingFormatError, match=message) as err:
        load_rounds(path)
    assert str(path) in str(err.value)


def test_a_missing_file_is_refused_cleanly(tmp_path):
    with pytest.raises(RecordingFormatError, match="cannot read"):
        load_rounds(tmp_path / "nothing.rounds")


def test_a_record_kind_the_version_does_not_define_is_refused(tmp_path):
    blob = small_recording(tmp_path / "run.rounds")
    (tmp_path / "odd.rounds").write_bytes(blob + bytes([7]) + bytes(8) + bytes([0]) + bytes(4))
    with pytest.raises(RecordingFormatError, match="kind 7"):
        load_rounds(tmp_path / "odd.rounds")


def test_a_last_record_cut_short_is_left_out_and_counted(tmp_path):
    blob = small_recording(tmp_path / "run.rounds")
    whole = load_rounds(tmp_path / "run.rounds")
    (tmp_path / "cut.rounds").write_bytes(blob[:-5])
    cut = load_rounds(tmp_path / "cut.rounds")
    assert cut.broadcasts == whole.broadcasts[:-1]
    assert cut.truncated_bytes == 14 + len(whole.broadcasts[-1].data) - 5   # its 14-byte record head + what is left


def test_the_recorder_refuses_unknown_sources_and_labels(tmp_path):
    with RoundRecorder(tmp_path / "run.rounds", ROOM, ["gw1"]) as rec:
        with pytest.raises(ValueError, match="gw9"):
            rec.write("gw9", T0, b"\x01")
        with pytest.raises(ValueError, match="invalid label"):
            rec.label("dancing", host_ts=T0)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds


def replay_times(recording, speed, clock=None):
    """(wall offset from the first broadcast, recorded host time) for each broadcast of the only source."""
    clock = clock or FakeClock()
    (stream,) = recording.streams(speed, clock=clock, sleep=clock.sleep).values()
    start, out = None, []
    for host_ts, _ in stream:
        start = clock.now if start is None else start
        out.append((clock.now - start, host_ts))
    return out


@pytest.mark.parametrize("speed", [1.0, 4.0])
def test_a_replay_paces_the_recorded_gaps_at_1x_and_nx(tmp_path, speed):
    with RoundRecorder(tmp_path / "run.rounds", ROOM, ["gw1"]) as rec:
        for host_ts, data in simulate_rounds(ROOM, duration_s=1.0, t0=T0):
            rec.write("gw1", host_ts, data)
    times = replay_times(load_rounds(tmp_path / "run.rounds"), speed)
    assert len(times) == 160
    first = times[0][1]
    for wall, host_ts in times:
        assert wall == pytest.approx((host_ts - first) / speed, abs=1e-9)
    assert times[-1][0] == pytest.approx(0.99 / speed, abs=0.01)


def test_the_sources_of_one_replay_share_its_start(tmp_path):
    with RoundRecorder(tmp_path / "run.rounds", ROOM, ["gw1", "gw2"]) as rec:
        rec.write("gw1", T0, b"a")
        rec.write("gw2", T0 + 0.5, b"b")
    clock = FakeClock()
    streams = load_rounds(tmp_path / "run.rounds").streams(2.0, clock=clock, sleep=clock.sleep)
    start = clock.now
    assert next(streams["gw1"]) == (T0, b"a") and clock.now == start
    assert next(streams["gw2"]) == (T0 + 0.5, b"b") and clock.now == pytest.approx(start + 0.25)


def test_a_replay_at_full_speed_never_waits(tmp_path):
    small_recording(tmp_path / "run.rounds")
    times = replay_times(load_rounds(tmp_path / "run.rounds"), math.inf)
    assert len(times) == 32 and {wall for wall, _ in times} == {0.0}


def test_a_replay_speed_must_be_positive(tmp_path):
    small_recording(tmp_path / "run.rounds")
    with pytest.raises(ValueError, match="positive"):
        load_rounds(tmp_path / "run.rounds").streams(0)


def frames(log_path):
    """Per logged frame: host time, (link id, state) of every link, and the room state."""
    rows = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines()[1:]]
    return [(p["status"]["time_s"], sorted((r["id"], r["state"]) for r in p["links"]), p["status"]["room"]["state"])
            for p in rows]


def post(server, path, body):
    request = urllib.request.Request(server.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


def live_then_replay(tmp_path, sources):
    """Run ``sources`` live through the dashboard while recording them, then replay the recording at full speed.
    Returns (live frames, replay frames, live final broadcast counts, replay final counts, a command's status)."""
    with RoundRecorder(tmp_path / "run.rounds", ROOM, list(sources)) as rec:
        live = DashboardServer(ROOM, rec.tee(sources), mode="auto", port=0, log=tmp_path / "live.jsonl")
        live.start()
        try:
            assert live.wait_finished(timeout=60)
            live_counts = json.loads(live.state_json())["status"]["broadcasts"]
        finally:
            live.stop()
    replay = rounds_dashboard(tmp_path / "run.rounds", speed=math.inf, port=0, log=tmp_path / "replay.jsonl")
    replay.start()
    try:
        assert replay.wait_finished(timeout=60)
        replay_counts = json.loads(replay.state_json())["status"]["broadcasts"]
        status = post(replay, "/api/params", {"sensitivity": 2.0})
    finally:
        replay.stop()
    return frames(tmp_path / "live.jsonl"), frames(tmp_path / "replay.jsonl"), live_counts, replay_counts, status


def test_the_dashboard_replays_a_recording_frame_for_frame_like_the_live_run(tmp_path):
    stream = simulate_rounds(ROOM, WALK, duration_s=17.0, quiet_first_s=14.0, dead_after={"n6": 15.0}, seed=5, t0=T0)
    live_frames, replay_frames, live_counts, replay_counts, status = live_then_replay(tmp_path, {"gw1": stream})
    assert replay_frames == live_frames and len(live_frames) > 80
    assert live_frames[-1][2] == "MOTION" and dict(live_frames[-1][1])["n6<-n1"] == "NO_SIGNAL"
    assert replay_counts == live_counts
    assert status == 409                                                    # a replay is read-only


def test_a_two_gateway_recording_replays_with_the_same_duplicates_per_gateway(tmp_path):
    sources = simulate_gateways(ROOM, WALK, hears={"gw1": ["n1", "n2", "n3", "n4", "n5"],
                                                   "gw2": ["n4", "n5", "n6", "n7", "n8"]},
                                loss={"gw2": 0.1}, duration_s=16.0, quiet_first_s=14.0, seed=2, t0=T0)
    live_frames, replay_frames, live_counts, replay_counts, _ = live_then_replay(tmp_path, sources)
    assert replay_frames == live_frames
    assert replay_counts == live_counts
    assert live_counts["rejected"]["duplicate"] > 0 and set(live_counts["sources"]) == {"gw1", "gw2"}


def test_the_dashboard_refuses_an_unsupported_recording_as_a_replay_error(tmp_path):
    (tmp_path / "x.rounds").write_bytes(b"SCROUNDS\x09\x00\x02\x00\x00\x00{}")
    with pytest.raises(ReplayError, match="format version 9"):
        rounds_dashboard(tmp_path / "x.rounds", port=0)


# ---------------------------------------------------------------------------------------------- dashboard.py

def cli_args(*argv):
    args = cli.build_parser().parse_args([*argv, "--http-port", "0"])
    cli.check_source(args)
    return args


def run_until(server, accepted):
    payload = {}
    for _ in range(200):
        with urllib.request.urlopen(server.url + "/api/state", timeout=10) as response:
            payload = json.loads(response.read())
        if payload["status"]["broadcasts"]["accepted"] >= accepted or payload["status"]["finished"]:
            break
        server.wait_finished(timeout=0.05)
    return payload


def test_cli_records_a_simulated_run_with_its_labels_and_replays_it(tmp_path, capsys):
    path = tmp_path / "sim.rounds"
    args = cli_args("--simulate", "--room", str(TR8), "--record", str(path), "--label", "empty")
    built = cli.build(args)
    server = built.server
    server.start()
    try:
        run_until(server, 24)
        cli.read_labels(["walking mark=A3\n", "\n", "dancing\n"], built.recorder)
    finally:
        server.stop()
    out = capsys.readouterr().out
    assert "label walking" in out and "invalid label 'dancing'" in out
    recording = load_rounds(path)
    assert len(recording.broadcasts) >= 24 and recording.room == ROOM and recording.sources == ("sim-gateway",)
    assert [(lab.label, lab.extra) for lab in recording.labels] == [("empty", {}), ("walking", {"mark": "A3"})]

    args = cli_args("--replay-rounds", str(path), "--speed", "max")
    assert cli.server_mode(args) == "auto"
    replay = cli.build_server(args)
    replay.start()
    try:
        assert replay.wait_finished(timeout=30)
        payload = run_until(replay, 0)
    finally:
        replay.stop()
    assert payload["status"]["broadcasts"]["accepted"] == len(recording.broadcasts)
    assert payload["status"]["commands"]["available"] is False and len(payload["links"]) == 56


def test_cli_replays_a_real_rigs_recording_with_its_kit_key(tmp_path):
    from csi.round_auth import write_kit_key

    key = write_kit_key(tmp_path / "kit.key")
    path = tmp_path / "rig.rounds"
    with RoundRecorder(path, ROOM, ["gateway"]) as rec:
        for host_ts, data in simulate_rounds(ROOM, duration_s=0.5, t0=T0, kit_key=key):
            rec.write("gateway", host_ts, data)
    counts = {}
    for extra in ([], ["--kit-key", str(tmp_path / "kit.key")]):
        replay = cli.build_server(cli_args("--replay-rounds", str(path), "--speed", "max", *extra))
        replay.start()
        try:
            assert replay.wait_finished(timeout=30)
            counts[bool(extra)] = run_until(replay, 0)["status"]["broadcasts"]
        finally:
            replay.stop()
    assert counts[False]["rejected"]["auth"] == counts[True]["accepted"] == 8 * 10   # without it: the test key


@pytest.mark.parametrize("argv, message", [
    (["--simulate-room", "--record", "x.rounds"], "--record"),
    (["--simulate", "--label", "empty"], "--label needs --record"),
    (["--replay-rounds", "x.rounds", "--thresholds", "links.json"], "adaptive"),
    (["--replay-rounds", "x.rounds", "--raw-log", "raw.tsv"], "--raw-log"),
    (["--replay-rounds", "x.rounds", "--simulate"], "one source"),
])
def test_cli_refuses_record_and_replay_flags_that_do_not_apply(argv, message):
    args = cli.build_parser().parse_args(argv)
    with pytest.raises(SystemExit) as err:
        cli.check_source(args)
    assert message in str(err.value)


def test_cli_refuses_an_invalid_starting_label():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--simulate", "--record", "x.rounds", "--label", "dancing"])


def test_cli_replays_a_recording_with_the_possibly_occupied_hold_it_is_given(tmp_path):
    small_recording(tmp_path / "run.rounds")
    for argv, hold_s in (((), 60.0), (("--possibly-occupied-s", "15"), 15.0)):
        replay = cli.build_server(cli_args("--replay-rounds", str(tmp_path / "run.rounds"), "--speed", "max", *argv))
        replay.start()
        try:
            assert replay.wait_finished(timeout=30)
            assert run_until(replay, 0)["status"]["room"]["possibly_occupied_s"] == hold_s
        finally:
            replay.stop()
