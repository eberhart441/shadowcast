"""Round recordings: a run's round broadcasts and labels written to one binary file, loaded back and replayed.

Assertions are on what a reader of the file gets back (csi.round_recording) and on the dashboard payload a replay
produces, never on the bytes the writer lays down (except to build files a reader must refuse).
"""
import math
from pathlib import Path

import pytest

from csi.dashboard import DashboardState
from csi.room import Walk, load_room
from csi.round_recording import RecordingFormatError, RoundRecorder, load_rounds
from csi.simulate import simulate_rounds

ROOT = Path(__file__).resolve().parents[1]
ROOM = load_room(ROOT / "tests" / "fixtures" / "room_tr8.json")
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
