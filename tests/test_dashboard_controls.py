"""Dashboard controls over HTTP: calibration on the live simulated stream, thresholds, saving, live parameters.

Fed by simulated per-receiver (host_ts, line) streams, as in tests/test_dashboard_server.py. Every assertion is
on an HTTP response or a payload, except the saved file, which is read back with load_thresholds (the contract
the next start relies on). Calibration streams are gated: the job is started over HTTP before any packet flows,
so the recording is the simulated stream and not a race with the pump.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from functools import lru_cache

import pytest

from csi.links import calibrate_links, load_thresholds
from csi.room import Walk, load_room
from csi.server import DashboardServer, controls_allowed, is_loopback
from csi.simulate import simulate_room, simulate_room_links

ROOM = load_room()
T0 = 1_700_000_000.0
LEFT = "rx2<-01"
RIGHT = "rx1<-00"
DIAG_B = "rx1<-01"
LABELS = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01"}
EMPTY_S = 60.0


@lru_cache(maxsize=None)
def calibrated_thresholds():
    return calibrate_links(list(simulate_room_links(room=ROOM, person=None, duration_s=25, seed=10)))


def post(url: str, path: str, body=None, content_type="application/json", raw: bytes | None = None):
    data = raw if raw is not None else json.dumps({} if body is None else body).encode()
    headers = {} if content_type is None else {"Content-Type": content_type}
    req = urllib.request.Request(url + path, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        text = err.read()
        try:
            return err.code, json.loads(text)
        except ValueError:
            return err.code, {"raw": text.decode(errors="replace")}


def get_json(server: DashboardServer, path: str = "/api/state") -> dict:
    with urllib.request.urlopen(server.url + path, timeout=5) as resp:
        return json.loads(resp.read())


def links_by_id(payload: dict) -> dict:
    return {row["id"]: row for row in payload["links"]}


def gated(stream, go: threading.Event):
    """Yields a stream's items once ``go`` is set; the header line before that is harmless."""
    for item in stream:
        if not go.is_set():
            go.wait()
        yield item


def chained(*streams):
    """Several simulated segments of one receiver, back to back; later segments lose their header line."""
    for i, stream in enumerate(streams):
        it = iter(stream)
        if i:
            next(it)
        yield from it


def wait_calibration(server, states=("done", "failed"), timeout=60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = get_json(server)
        if payload["status"]["calibration"]["state"] in states:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"calibration did not reach {states}: {get_json(server)['status']['calibration']}")


@pytest.fixture
def serve():
    started, gates = [], []

    def _serve(sources, thresholds=None, **kwargs):
        server = DashboardServer(ROOM, sources, thresholds, port=0, **kwargs)
        server.start()
        started.append(server)
        return server

    def _gate():
        go = threading.Event()
        gates.append(go)
        return go

    _serve.gate = _gate
    yield _serve
    for go in gates:
        go.set()
    for server in started:
        server.stop()


def empty_room(go=None, drop_after=None, duration_s=EMPTY_S, seed=12):
    """Simulated empty room per receiver; gated on ``go`` when given (calibration tests), else free-running."""
    streams = simulate_room(None, room=ROOM, duration_s=duration_s, seed=seed, drop_after=drop_after, t0=T0)
    if go is None:
        return streams
    return {name: gated(stream, go) for name, stream in streams.items()}


# ----------------------------------------------------------------- calibration on the live stream

@pytest.mark.slow
def test_calibrate_on_the_simulator_runs_the_clips_and_links_become_calibrated(serve):
    go = serve.gate()
    server = serve(empty_room(go), thresholds=None)
    status, body = post(server.url, "/api/calibrate", {"clips": 3, "seconds": 3, "lead_s": 0})
    assert status == 202
    assert body["calibration"]["state"] == "countdown" or body["calibration"]["state"] == "recording"
    go.set()

    payload = wait_calibration(server)
    calibration = payload["status"]["calibration"]

    assert calibration["state"] == "done", calibration["message"]
    assert set(calibration["result"]) == LABELS
    assert calibration["skipped"] == {}
    for label, row in calibration["result"].items():
        assert row["applied"] is True and "warning" not in row
        assert row["threshold"] > 0
        assert row["clean_s"] + row["rejected_s"] == pytest.approx(9, abs=1.5)   # 3 clips of 3 s each
        assert row["clean_s"] > 7
    assert set(payload["status"]["calibrated_links"]) == LABELS
    assert payload["status"]["uncalibrated_links"] == []
    assert {row["state"] for row in payload["links"]} != {"UNCALIBRATED"}
    assert links_by_id(payload)[RIGHT]["threshold"] == pytest.approx(calibration["result"][RIGHT]["threshold"], rel=1e-3)


@pytest.mark.slow
def test_a_link_with_no_packets_is_skipped_with_its_reason_and_the_others_are_kept(serve):
    go = serve.gate()
    server = serve(empty_room(go, drop_after={DIAG_B: 0.0}), thresholds=None)
    post(server.url, "/api/calibrate", {"clips": 3, "seconds": 3, "lead_s": 0})
    go.set()

    payload = wait_calibration(server)
    calibration = payload["status"]["calibration"]

    assert calibration["state"] == "done"
    assert DIAG_B in calibration["skipped"]
    assert "no packets" in calibration["skipped"][DIAG_B]
    assert DIAG_B not in calibration["result"]
    assert set(calibration["result"]) == LABELS - {DIAG_B}
    assert links_by_id(payload)[DIAG_B]["threshold"] is None     # no packets, so no threshold: still uncalibrated
    assert links_by_id(payload)[DIAG_B]["state"] == "NO_SIGNAL"
    assert links_by_id(payload)[RIGHT]["state"] != "UNCALIBRATED"
    assert any(DIAG_B in m and "no packets" in m for m in payload["status"]["messages"])


@pytest.mark.slow
def test_a_clip_that_is_mostly_movement_is_skipped_with_the_reason_and_keeps_the_previous_threshold(serve):
    go = serve.gate()
    # rx1 has someone walking for the first 27 s (clips 1 and 2 of 12 s each, with a 3 s pause), then an empty
    # room; rx2 is empty throughout. Only rx1's links see the walk, and only DIAG_B has most of its data rejected.
    walk = Walk((ROOM.wall_point("left"), ROOM.wall_point("right")), speed_mps=0.5, loop=True)
    rx1 = chained(simulate_room(walk, room=ROOM, duration_s=27, seed=31, t0=T0)["rx1"],
                  simulate_room(None, room=ROOM, duration_s=20, seed=32, t0=T0 + 27)["rx1"])
    rx2 = simulate_room(None, room=ROOM, duration_s=47, seed=33, t0=T0)["rx2"]
    server = serve({"rx1": gated(rx1, go), "rx2": gated(rx2, go)}, thresholds=None)
    status, _ = post(server.url, "/api/threshold", {"link": DIAG_B, "value": 0.6})
    assert status == 200
    post(server.url, "/api/calibrate", {"clips": 3, "seconds": 12, "lead_s": 0, "noisy": False})  # quiet-room rules
    go.set()

    payload = wait_calibration(server)
    calibration = payload["status"]["calibration"]

    assert calibration["state"] == "done", calibration["message"]
    assert DIAG_B not in calibration["result"]
    assert calibration["skipped"][DIAG_B].startswith("only ") and "record again" in calibration["skipped"][DIAG_B]
    assert calibration["result"][RIGHT]["applied"] is True
    assert links_by_id(payload)[DIAG_B]["threshold"] == pytest.approx(0.6)   # the previous value stays
    assert any(DIAG_B in m and "was quiet" in m for m in payload["status"]["messages"])


def test_a_source_that_ends_before_the_clips_finish_fails_the_job_and_keeps_the_thresholds(serve):
    go = serve.gate()
    server = serve(empty_room(go, duration_s=5), thresholds=None)     # 5 s of data, 3 x 3 s clips need 15 s
    post(server.url, "/api/threshold", {"link": RIGHT, "value": 0.42})
    post(server.url, "/api/calibrate", {"clips": 3, "seconds": 3, "lead_s": 0})
    go.set()

    payload = wait_calibration(server)
    calibration = payload["status"]["calibration"]

    assert calibration["state"] == "failed"
    assert "ended" in calibration["message"]
    assert links_by_id(payload)[RIGHT]["threshold"] == pytest.approx(0.42)   # untouched


def test_second_calibrate_while_one_runs_is_409_and_the_countdown_is_reported(serve):
    server = serve(empty_room(serve.gate()), thresholds=None)
    status, _ = post(server.url, "/api/calibrate", {"lead_s": 60})
    assert status == 202

    status, body = post(server.url, "/api/calibrate", {"lead_s": 60})

    assert status == 409
    assert "already running" in body["error"]
    calibration = get_json(server)["status"]["calibration"]
    assert calibration["state"] == "countdown"
    assert calibration["message"].startswith("Clear the room")
    assert 55 <= calibration["seconds_left"] <= 60


def test_cancel_stops_a_running_calibration_and_a_new_one_can_start(serve):
    server = serve(empty_room(serve.gate()), thresholds=None)
    post(server.url, "/api/calibrate", {"lead_s": 60})

    status, body = post(server.url, "/api/calibrate/cancel", {})

    assert status == 200
    assert body["calibration"]["state"] == "idle"
    assert get_json(server)["status"]["calibration"]["state"] == "idle"
    assert post(server.url, "/api/calibrate/cancel", {})[0] == 409   # nothing running any more
    assert post(server.url, "/api/calibrate", {"lead_s": 60})[0] == 202


def test_calibrate_rejects_bad_options_with_400(serve):
    server = serve(empty_room(serve.gate()), thresholds=None)
    for body in ({"clips": 0}, {"clips": 2.5}, {"clips": 11}, {"seconds": 0}, {"margin": 0},
                 {"margin": "1.2"}, {"lead_s": -1}, {"colour": "red"}):
        status, reply = post(server.url, "/api/calibrate", body)
        assert status == 400, body
        assert "error" in reply
    assert get_json(server)["status"]["calibration"]["state"] == "idle"


# ----------------------------------------------------------------- single-link threshold

def test_threshold_command_sets_one_link_and_the_next_payload_shows_its_state(serve):
    server = serve(empty_room(duration_s=5), thresholds=None)
    assert server.wait_finished(timeout=60)
    before = links_by_id(get_json(server))
    assert before["rx2<-00"]["threshold"] is None
    assert before["rx2<-00"]["state"] != "EMPTY" and before["rx2<-00"]["state"] != "MOTION"

    status, body = post(server.url, "/api/threshold", {"link": "rx2<-00", "value": 0.5})

    assert status == 200 and body == {"link": "rx2<-00", "threshold": 0.5}
    payload = get_json(server)
    row = links_by_id(payload)["rx2<-00"]
    assert row["threshold"] == pytest.approx(0.5)
    assert row["state"] in ("EMPTY", "MOTION", "WARMUP")          # calibrated now: no longer UNCALIBRATED
    assert payload["status"]["calibrated_links"] == ["rx2<-00"]
    assert links_by_id(payload)["rx1<-00"]["threshold"] is None


def test_threshold_command_rejects_bad_input_with_400(serve):
    server = serve(empty_room(duration_s=5), thresholds=None)
    bad = [{"link": "rx9<-99", "value": 1.0}, {"link": "rx2<-00", "value": 0}, {"link": "rx2<-00", "value": -2},
           {"link": "rx2<-00", "value": "high"}, {"link": "rx2<-00", "value": True},
           {"link": "rx2<-00"}, {"link": "rx2<-00", "value": 1.0, "extra": 1}]  # a missing value is a 400; only an explicit null is auto
    for body in bad:
        status, reply = post(server.url, "/api/threshold", body)
        assert status == 400, body
        assert "error" in reply
    assert get_json(server)["status"]["calibrated_links"] == []


def test_commands_reject_non_string_and_non_number_json_values_with_400_not_a_crash(serve):
    server = serve(empty_room(duration_s=5), thresholds=None)
    odd = [None, True, 1.5, [1], {"a": 1}, ["rx2<-00"]]
    for value in odd:
        assert post(server.url, "/api/threshold", {"link": value, "value": 1.0})[0] == 400, value
    for value in [[0.5], {"v": 1}, "1", True]:
        assert post(server.url, "/api/threshold", {"link": "rx2<-00", "value": value})[0] == 400, value
    assert post(server.url, "/api/threshold", {"link": "rx2<-00", "value": None})[0] == 200  # null: back to auto
    for body in [{"seconds": [1]}, {"clips": {"n": 1}}, {"margin": [1.0]}, {"margin": True},
                 {"lead_s": None}, {"seconds": "10"}]:
        assert post(server.url, "/api/calibrate", body)[0] == 400, body
    for body in [{"sensitivity": [1]}, {"decay": {"x": 1}}, {"rise": None}, {"falloff": True}]:
        assert post(server.url, "/api/params", body)[0] == 400, body
    assert get_json(server)["status"]["calibrated_links"] == []


def test_commands_need_a_json_body_and_a_json_object(serve):
    server = serve(empty_room(duration_s=5), thresholds=None)
    assert post(server.url, "/api/threshold", content_type="text/plain",
                raw=b'{"link": "rx2<-00", "value": 1}')[0] == 415
    assert post(server.url, "/api/threshold", raw=b"{not json")[0] == 400
    assert post(server.url, "/api/threshold", raw=b"[1, 2]")[0] == 400
    assert post(server.url, "/api/no-such-command", {})[0] == 404


# ----------------------------------------------------------------- saving and reloading

def test_save_writes_a_plain_thresholds_file_that_load_thresholds_reads(serve, tmp_path):
    target = tmp_path / "links.json"
    server = serve(empty_room(duration_s=5), calibrated_thresholds(), thresholds_path=target)
    expected = calibrated_thresholds()

    status, body = post(server.url, "/api/thresholds/save", {})

    assert status == 200
    assert body["path"] == str(target.resolve())
    assert json.loads(target.read_text()) == pytest.approx(expected)     # plain {label: number}
    assert load_thresholds(target) == pytest.approx(expected)
    with urllib.request.urlopen(server.url + "/api/thresholds", timeout=5) as resp:
        assert json.loads(resp.read())["thresholds"] == pytest.approx(expected)


def test_save_with_nothing_to_save_is_409_and_writes_nothing(serve, tmp_path):
    target = tmp_path / "links.json"
    server = serve(empty_room(duration_s=5), None, thresholds_path=target)

    status, body = post(server.url, "/api/thresholds/save", {})

    assert status == 409
    assert "no thresholds" in body["error"]
    assert not target.exists()


def test_a_saved_threshold_reloads_on_the_next_start(serve, tmp_path):
    target = tmp_path / "links.json"
    first = serve(empty_room(duration_s=5), calibrated_thresholds(), thresholds_path=target)
    post(first.url, "/api/threshold", {"link": "rx1<-01", "value": 0.77})
    assert post(first.url, "/api/thresholds/save", {})[0] == 200

    second = serve(empty_room(duration_s=5), load_thresholds(target))

    assert links_by_id(get_json(second))["rx1<-01"]["threshold"] == pytest.approx(0.77)


# ----------------------------------------------------------------- live heatmap parameters

def test_params_change_the_payload_parameters_without_a_restart(serve):
    server = serve(empty_room(duration_s=5), calibrated_thresholds())
    before = get_json(server)["status"]["parameters"]

    status, body = post(server.url, "/api/params",
                        {"sensitivity": 0.5, "decay": 3.0, "rise": 0.6, "falloff": 0.5})

    assert status == 200
    assert body["parameters"]["sensitivity"] == 0.5
    after = get_json(server)["status"]["parameters"]
    assert after["sensitivity"] == 0.5
    assert after["decay_tau_s"] == 3.0
    assert after["rise_tau_s"] == 0.6
    assert after["falloff_m"] == 0.5
    assert after["cols"] == before["cols"] and after["rows"] == before["rows"]


def test_params_reject_unknown_keys_and_bad_values_with_400(serve):
    server = serve(empty_room(duration_s=5), calibrated_thresholds())
    before = get_json(server)["status"]["parameters"]
    bad = [{"colour": 1}, {"sensitivity": 0}, {"sensitivity": -1}, {"decay": 0}, {"decay": 1000},
           {"falloff": 5}, {"rise": "fast"}, {"rise": True}, {}, {"sensitivity": 0.5, "colour": 1}]
    for body in bad:
        status, reply = post(server.url, "/api/params", body)
        assert status == 400, body
        assert "error" in reply
    assert get_json(server)["status"]["parameters"] == before     # nothing partly applied
    status, reply = post(server.url, "/api/params", {"colour": 1})
    assert "colour" in reply["error"]


# ----------------------------------------------------------------- safety and replay

def test_commands_are_reported_available_on_localhost_and_logged_into_messages(serve):
    server = serve(empty_room(duration_s=5), calibrated_thresholds())
    post(server.url, "/api/threshold", {"link": "rx1<-00", "value": 0.9})

    status = get_json(server)["status"]

    assert status["commands"] == {"available": True, "reason": None}
    assert any("rx1<-00" in m and "0.9" in m for m in status["messages"] if m.startswith("command:"))


def test_the_control_policy_allows_only_loopback_unless_explicitly_allowed():
    assert controls_allowed("127.0.0.1") and controls_allowed("localhost") and controls_allowed("::1")
    assert not controls_allowed("0.0.0.0") and not controls_allowed("192.168.1.20") and not controls_allowed("")
    assert controls_allowed("0.0.0.0", allow_control=True)
    assert is_loopback("127.5.6.7") and not is_loopback("example.org")


def test_replayed_sessions_answer_every_command_with_409(tmp_path):
    from csi.replay import session_dashboard
    from tests.test_evaluate import record_plan

    folder = record_plan(tmp_path / "room", [("empty", 20), ("walking", 10)])
    server = session_dashboard(folder, ROOM, speed=float("inf"), thresholds=calibrated_thresholds())
    server.start()
    try:
        assert server.wait_finished(timeout=120)
        for path, body in (("/api/calibrate", {}), ("/api/calibrate/cancel", {}),
                           ("/api/threshold", {"link": RIGHT, "value": 1.0}),
                           ("/api/thresholds/save", {}), ("/api/params", {"sensitivity": 0.4})):
            status, reply = post(server.url, path, body)
            assert status == 409, path
            assert "replay" in reply["error"]
        payload = get_json(server)
        assert payload["status"]["commands"]["available"] is False
        assert "replay" in payload["status"]["commands"]["reason"]
    finally:
        server.stop()
