"""Round recordings: a run's round broadcasts and labels written to one binary file, loaded back and replayed.

Assertions are on what a reader of the file gets back (csi.round_recording) and on the dashboard payload a replay
produces, never on the bytes the writer lays down (except to build files a reader must refuse).
"""
import math
from pathlib import Path

import pytest

from csi.dashboard import DashboardState
from csi.room import Walk, load_room
from csi.round_recording import RoundRecorder, load_rounds
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
