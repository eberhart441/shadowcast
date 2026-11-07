"""Dashboard HTTP server, driven by simulated per-receiver (host_ts, line) streams.

The only seam is the HTTP interface: every assertion is on a response (or on the bound address the
server reports). Thresholds come from a simulated empty room, as in tests/test_dashboard_state.py.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from functools import lru_cache

import numpy as np
import pytest

from csi.links import calibrate_links
from csi.parser import DEFAULT_HEADER
from csi.room import Walk, load_room, stand_near_wall
from csi.server import DashboardServer, default_walk, looping_room_streams
from csi.simulate import simulate_room, simulate_room_links

ROOM = load_room()
LEFT = "rx2<-01"
RIGHT = "rx1<-00"
LABELS = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01"}
FRAME_S = 0.25


@lru_cache(maxsize=None)
def calibrated_thresholds():
    """Thresholds from a simulated empty room: the calibration a deployment would be given."""
    return calibrate_links(list(simulate_room_links(room=ROOM, person=None, duration_s=25, seed=10)))


def room_streams(person=None, duration_s=10.0, seed=12, **kwargs):
    return simulate_room(person, room=ROOM, duration_s=duration_s, seed=seed, **kwargs)


def get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.headers, resp.read()


def get_json(server: DashboardServer, path: str = "/api/state") -> dict:
    _, _, body = get(server.url + path)
    return json.loads(body)


def links_by_id(payload: dict) -> dict:
    return {row["id"]: row for row in payload["links"]}


def grid(payload: dict) -> np.ndarray:
    heat = payload["heatmap"]
    return np.array(heat["cells"]).reshape(heat["rows"], heat["cols"])


def read_events(server: DashboardServer, count: int | None = None, timeout: float = 10.0) -> list[dict]:
    """Payloads from /api/events: the first ``count`` events, or all of them up to the final event."""
    events = []
    with urllib.request.urlopen(server.url + "/api/events", timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode().rstrip("\n")
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: "):])
            events.append(payload)
            if payload["status"]["finished"] or (count is not None and len(events) >= count):
                break
    return events


def heat_centre_x(payload: dict) -> float | None:
    """Horizontal centre of mass of the heat in metres, or None when the room is cold."""
    heat = payload["heatmap"]
    cells = np.array(heat["cells"])
    if cells.sum() <= 0:
        return None
    xs = (np.arange(heat["cols"]) + 0.5) * payload["room"]["width_m"] / heat["cols"]
    return float((grid(payload).sum(axis=0) * xs).sum() / cells.sum())


@pytest.fixture
def serve():
    """Start servers on ephemeral ports; all are stopped at teardown."""
    started = []

    def _serve(sources, thresholds=None, **kwargs):
        server = DashboardServer(ROOM, sources, thresholds, port=0, **kwargs)
        server.start()
        started.append(server)
        return server

    yield _serve
    for server in started:
        server.stop()


def test_api_state_returns_per_link_states_and_a_hot_heatmap_for_a_person_near_the_right_wall(serve):
    server = serve(room_streams(stand_near_wall(ROOM, "right")), calibrated_thresholds())
    assert server.wait_finished(timeout=30)

    payload = get_json(server)

    assert {row["id"] for row in payload["links"]} == LABELS
    assert links_by_id(payload)[RIGHT]["state"] == "MOTION"
    g = grid(payload)
    half = g.shape[1] // 2
    assert g[:, half:].max() > 0.3
    assert g[:, half:].max() > 2 * g[:, :half].max()
    assert payload["status"]["finished"] is True
    assert payload["status"]["receivers"] == {"rx1": "finished", "rx2": "finished"}


def test_event_stream_emits_intermediate_events_that_follow_a_person_walking_left_to_right(serve):
    walk = Walk((ROOM.wall_point("left"), ROOM.wall_point("right")), speed_mps=0.5)
    server = serve(room_streams(walk, duration_s=6.0), calibrated_thresholds(), frame_interval_s=0.5)
    assert server.wait_finished(timeout=30)

    events = read_events(server)  # attached after the run: replays the retained frames, paced

    assert len(events) >= 5                       # emitted per batch, not only at the end
    assert events[0]["status"]["finished"] is False
    assert events[-1]["status"]["finished"] is True
    centres = [c for c in map(heat_centre_x, events) if c is not None]
    assert len(centres) >= 3
    assert centres[-1] > ROOM.width_m / 2          # ends near the right wall
    assert centres[-1] - centres[0] > 0.5          # the heat has moved right, early to late


def failing_after(lines, n: int):
    """Yields ``n`` lines, then raises like an unplugged port would."""
    for i, item in enumerate(lines):
        if i >= n:
            raise OSError("device disconnected")
        yield item


def test_failing_receiver_is_reported_while_the_other_keeps_feeding(serve):
    good = room_streams(stand_near_wall(ROOM, "right"), duration_s=10)["rx1"]
    bad = failing_after(room_streams(stand_near_wall(ROOM, "right"), duration_s=10)["rx2"], 200)
    server = serve({"rx1": good, "rx2": bad}, calibrated_thresholds())
    assert server.wait_finished(timeout=30)

    payload = get_json(server)

    assert payload["status"]["receivers"] == {"rx1": "finished", "rx2": "failed"}
    assert "disconnected" in payload["status"]["errors"]["rx2"]
    rx1_rows = [row for row in payload["links"] if row["receiver"] == "rx1"]
    assert rx1_rows and all(row["pkt_rate"] > 0 for row in rx1_rows)
    assert links_by_id(payload)[RIGHT]["state"] == "MOTION"
    assert RIGHT in payload["heatmap"]["contributing"]


def test_server_binds_to_localhost_by_default(serve):
    server = serve(room_streams(duration_s=1), calibrated_thresholds())
    assert server.address[0] == "127.0.0.1"
    assert server.url.startswith("http://127.0.0.1:")


def test_host_option_is_honoured_and_the_server_still_answers_on_loopback(serve):
    server = serve(room_streams(duration_s=1), calibrated_thresholds(), host="0.0.0.0")
    assert server.address[0] == "0.0.0.0"
    assert server.wait_finished(timeout=30)
    assert get_json(server)["status"]["finished"] is True


def test_source_without_thresholds_shows_every_link_uncalibrated_with_a_message(serve):
    server = serve(room_streams(stand_near_wall(ROOM, "right"), duration_s=6), thresholds=None)
    assert server.wait_finished(timeout=30)

    payload = get_json(server)

    assert {row["state"] for row in payload["links"]} == {"UNCALIBRATED"}
    assert all(row["contributes"] is False for row in payload["links"])
    assert payload["heatmap"]["max"] < 0.05
    assert payload["status"]["calibrated_links"] == []
    assert payload["status"]["uncalibrated_links"] == sorted(LABELS)
    assert any("UNCALIBRATED" in message for message in payload["status"]["messages"])


def test_root_serves_the_static_page_as_html(serve):
    server = serve(room_streams(duration_s=1), calibrated_thresholds())
    status, headers, body = get(server.url + "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"/api/state" in body


def test_unknown_path_is_404(serve):
    server = serve(room_streams(duration_s=1), calibrated_thresholds())
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server.url + "/no-such-page")
    assert err.value.code == 404


class QuietPort:
    """A fake port that stays open and sends no packets (only the header line), and records close()."""

    def __init__(self):
        self.closed = False

    def __iter__(self):
        yield time.time(), DEFAULT_HEADER
        while True:
            time.sleep(0.01)
            yield time.time(), DEFAULT_HEADER

    def close(self):
        self.closed = True


def live_room_streams():
    """Endless real-time streams of a person walking round the room."""
    return looping_room_streams(ROOM, default_walk(ROOM), seed=3)


def test_live_sources_are_reported_as_connected_and_not_finished(serve):
    server = serve(live_room_streams(), calibrated_thresholds())
    time.sleep(1.0)

    payload = get_json(server)

    assert payload["status"]["finished"] is False
    assert payload["status"]["receivers"] == {"rx1": "connected", "rx2": "connected"}
    assert links_by_id(payload)[RIGHT]["pkt_rate"] > 0


def test_client_disconnecting_mid_stream_leaves_no_thread_and_no_traceback(serve, capfd):
    server = serve(live_room_streams(), calibrated_thresholds())
    time.sleep(0.5)
    baseline = threading.active_count()

    assert len(read_events(server, count=1)) == 1  # each stream is dropped after reading
    events = read_events(server, count=3)
    assert len(events) == 3
    deadline = time.time() + 5.0
    while threading.active_count() > baseline and time.time() < deadline:
        time.sleep(0.05)

    assert threading.active_count() <= baseline
    assert "Traceback" not in capfd.readouterr().err
    assert get_json(server)["status"]["finished"] is False  # the server itself carries on


def test_stop_closes_the_sources_and_the_server_refuses_new_connections(serve):
    port = QuietPort()
    server = serve({"rx1": port}, calibrated_thresholds())
    time.sleep(0.2)
    url = server.url
    server.stop()

    assert port.closed
    with pytest.raises(OSError):
        urllib.request.urlopen(url + "/api/state", timeout=2)
