"""Logging and replay of the dashboard state stream, seen only through the server's HTTP interface.

A live run is logged to a file; a fresh replay server serves that file through the same /api/state and
/api/events endpoints. Session replays feed a simulator-recorded session through the live pipeline.
"""
import json
import math
import time

import pytest

from csi.replay import ReplayError, ReplayServer, load_log, session_dashboard
from csi.room import Walk
from csi.server import DashboardServer
from tests.test_evaluate import record_plan
from tests.test_dashboard_server import (LABELS, RIGHT, ROOM, calibrated_thresholds, get_json, grid,
                                         links_by_id, read_events, room_streams)


@pytest.fixture
def run_server():
    """Start servers on ephemeral ports; all are stopped at teardown."""
    started = []

    def _start(server):
        server.start()
        started.append(server)
        return server

    yield _start
    for server in started:
        server.stop()


def walk_left_to_right(seconds: float = 3.0):
    walk = Walk((ROOM.wall_point("left"), ROOM.wall_point("right")), speed_mps=0.5)
    return room_streams(walk, duration_s=seconds, seed=4)


@pytest.fixture(scope="module")
def walk_log(tmp_path_factory):
    """A live simulated run of a person walking left to right, logged to a file. Returns the log path."""
    log = tmp_path_factory.mktemp("log") / "walk.jsonl"
    server = DashboardServer(ROOM, walk_left_to_right(), calibrated_thresholds(), port=0, log=log,
                             log_meta={"source": "simulate-room"})
    server.start()
    try:
        assert server.wait_finished(timeout=60)
    finally:
        server.stop()
    return log


def logged_payloads(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()[1:]]


def logged_span_s(path) -> float:
    times = [p["status"]["time_s"] for p in logged_payloads(path)]
    return times[-1] - times[0]


def states_of(payload: dict) -> dict:
    return {row["id"]: row["state"] for row in payload["links"]}


def hot_cell(payload: dict):
    """(row, col) of the hottest cell, or None when the room is cold."""
    if payload["heatmap"]["max"] <= 0:
        return None
    g = grid(payload)
    return divmod(int(g.argmax()), g.shape[1])


def test_log_has_a_header_then_one_line_per_event_with_the_same_payloads(tmp_path, run_server):
    log = tmp_path / "run.jsonl"
    server = run_server(DashboardServer(ROOM, walk_left_to_right(2.0), calibrated_thresholds(), port=0, log=log,
                                        log_meta={"source": "simulate-room"}))
    assert server.wait_finished(timeout=60)

    events = read_events(server)
    lines = log.read_text().splitlines()

    header = json.loads(lines[0])
    assert header["format"] == "csi-dashboard-log"
    assert header["room"]["width_m"] == ROOM.width_m and header["room"]["depth_m"] == ROOM.depth_m
    assert {n["name"] for n in header["room"]["receivers"] + header["room"]["senders"]} == {"rx1", "rx2", "tx0", "tx1"}
    assert set(header["thresholds"]) == LABELS
    assert header["source"] == "simulate-room"
    assert header["started"] > 0
    assert [json.loads(line) for line in lines[1:]] == events
    assert events[-1]["status"]["finished"] is True


def test_replayed_log_serves_the_logged_states_and_hot_cell_track_in_order(walk_log, run_server):
    logged = logged_payloads(walk_log)
    server = run_server(ReplayServer(load_log(walk_log), speed=math.inf))
    assert server.wait_finished(timeout=30)

    events = read_events(server)

    assert len(events) == len(logged)
    assert [states_of(e) for e in events] == [states_of(e) for e in logged]
    track = [hot_cell(e) for e in events]
    assert track == [hot_cell(e) for e in logged]
    hot = [cell for cell in track if cell is not None]
    assert hot and hot[-1][1] > hot[0][1]          # the walk moves left to right
    for i in range(0, len(logged), 3):              # sampled frames match in full
        assert events[i] == logged[i]
    assert events[-1]["status"]["finished"] is True
    assert get_json(server)["status"]["finished"] is True


def test_replay_at_original_speed_takes_about_the_logged_time(walk_log, run_server):
    span = logged_span_s(walk_log)
    started = time.monotonic()
    server = run_server(ReplayServer(load_log(walk_log), speed=1.0))
    assert server.wait_finished(timeout=span + 30)
    elapsed = time.monotonic() - started
    assert 0.8 * span <= elapsed <= span + 1.5


@pytest.mark.parametrize("speed", [4.0, math.inf])
def test_accelerated_and_max_replays_finish_sooner(walk_log, run_server, speed):
    span = logged_span_s(walk_log)
    started = time.monotonic()
    server = run_server(ReplayServer(load_log(walk_log), speed=speed))
    assert server.wait_finished(timeout=span + 30)
    elapsed = time.monotonic() - started
    assert elapsed < span / 2
    assert get_json(server)["status"]["finished"] is True


def test_missing_log_is_a_clear_error(tmp_path):
    with pytest.raises(ReplayError, match="cannot read replay log .*nope.jsonl"):
        load_log(tmp_path / "nope.jsonl")


def test_corrupt_line_is_reported_by_its_number(walk_log, tmp_path):
    lines = walk_log.read_text().splitlines()
    lines[3] = lines[3][: len(lines[3]) // 2]          # line 4 is cut off mid-JSON
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(lines) + "\n")
    with pytest.raises(ReplayError, match="line 4"):
        load_log(bad)


def test_a_file_that_is_not_a_dashboard_log_is_rejected(tmp_path):
    other = tmp_path / "other.jsonl"
    other.write_text('{"hello": 1}\n')
    with pytest.raises(ReplayError, match="line 1 is not a dashboard log"):
        load_log(other)


def test_a_line_that_is_not_a_state_payload_is_reported_by_its_number(walk_log, tmp_path):
    lines = walk_log.read_text().splitlines()
    lines[2] = '{"links": []}'
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(lines) + "\n")
    with pytest.raises(ReplayError, match="line 3 is not a state payload"):
        load_log(bad)


# ---------------------------------------------------------------- recorded sessions

SESSION_FRAME_S = 1.0  # a 30 s session at the default 0.2 s frames is 30 s of SSE pacing per read


@pytest.fixture(scope="module")
def walking_session(tmp_path_factory):
    """A simulator-recorded session: 20 s empty, then 10 s of walking, with the real recorder."""
    return record_plan(tmp_path_factory.mktemp("sessions") / "room", [("empty", 20), ("walking", 10)])


def test_replayed_session_shows_motion_for_the_walking_recording(walking_session, run_server):
    server = run_server(session_dashboard(walking_session, ROOM, speed=math.inf, margin=1.5,
                                          frame_interval_s=SESSION_FRAME_S))
    assert server.wait_finished(timeout=60)

    events = read_events(server)
    states = [links_by_id(e)[RIGHT]["state"] for e in events]

    first_motion = states.index("MOTION")
    assert "EMPTY" in states[:first_motion]
    assert any(RIGHT in e["heatmap"]["contributing"] for e in events)
    final = events[-1]["status"]
    assert final["finished"] is True
    assert final["receivers"] == {"rx1": "finished"}
    assert any("single link" in m for m in final["messages"])


def test_session_replay_can_take_thresholds_given_instead_of_calibrating(walking_session, run_server):
    server = run_server(session_dashboard(walking_session, ROOM, speed=math.inf, thresholds={RIGHT: 1e9},
                                          frame_interval_s=SESSION_FRAME_S))  # nothing crosses this
    assert server.wait_finished(timeout=60)

    events = read_events(server)

    assert {links_by_id(e)[RIGHT]["state"] for e in events} <= {"WARMUP", "EMPTY"}
    assert all(RIGHT not in e["heatmap"]["contributing"] for e in events)


def test_session_replay_log_header_records_that_thresholds_were_calibrated_from_the_session(
        walking_session, tmp_path, run_server):
    log = tmp_path / "session.jsonl"
    server = run_server(session_dashboard(walking_session, ROOM, speed=math.inf, margin=1.5, log=log))
    assert server.wait_finished(timeout=60)
    server.stop()

    header = json.loads(log.read_text().splitlines()[0])
    assert header["thresholds_source"].startswith("calibrated on empty_")
    assert header["thresholds"][RIGHT] > 0
    assert header["source"].startswith("replay-session")


def test_session_without_an_empty_recording_cannot_be_calibrated(tmp_path):
    folder = record_plan(tmp_path / "walk-only", [("walking", 10)])
    with pytest.raises(ReplayError, match="no empty recording to calibrate on"):
        session_dashboard(folder, ROOM)


def test_a_folder_that_is_not_a_session_is_rejected(tmp_path):
    with pytest.raises(ReplayError, match="not a session folder"):
        session_dashboard(tmp_path, ROOM)
