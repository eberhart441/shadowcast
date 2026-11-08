"""Replay status over HTTP: a replayed log advertises no commands, and a link with no packets in a replayed session
is reported as having no data (not as UNCALIBRATED)."""
import json
import math
import urllib.error
import urllib.request

import pytest

from csi.replay import ReplayServer, load_log, session_dashboard
from csi.room import load_room
from csi.server import DashboardServer
from tests.test_dashboard_server import calibrated_thresholds, quiet_then_streams, RIGHT
from tests.test_evaluate import record_plan

ROOM = load_room()


@pytest.fixture
def started():
    servers = []

    def _start(server):
        server.start()
        servers.append(server)
        return server

    yield _start
    for s in servers:
        s.stop()


def get_json(server, path="/api/state"):
    with urllib.request.urlopen(server.url + path, timeout=30) as resp:
        return json.loads(resp.read())


def post_status(server, path, body):
    req = urllib.request.Request(server.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


@pytest.fixture(scope="module")
def live_log(tmp_path_factory):
    """A live run (localhost, so commands are available) logged to a file."""
    path = tmp_path_factory.mktemp("log") / "live.jsonl"
    server = DashboardServer(ROOM, quiet_then_streams(None, duration_s=2), calibrated_thresholds(), port=0, log=path)
    server.start()
    try:
        assert server.wait_finished(timeout=60)
    finally:
        server.stop()
    return path


def test_the_live_log_advertised_commands_while_it_was_live(live_log):
    last = json.loads(live_log.read_text().splitlines()[-1])
    assert last["status"]["commands"]["available"] is True


def test_a_replayed_log_advertises_no_commands_and_answers_them_with_409(live_log, started):
    server = started(ReplayServer(load_log(live_log), speed=math.inf, port=0))
    assert server.wait_finished(timeout=60)

    commands = get_json(server)["status"]["commands"]

    assert commands["available"] is False
    assert "replay" in commands["reason"]
    assert post_status(server, "/api/threshold", {"link": RIGHT, "value": 1.0}) == 409


def test_a_link_with_no_packets_in_a_replayed_session_is_reported_as_no_data_not_uncalibrated(tmp_path, started):
    folder = record_plan(tmp_path / "room", [("empty", 20), ("walking", 10)])  # one sender link only
    server = started(session_dashboard(folder, ROOM, speed=math.inf, margin=1.5, port=0))
    assert server.wait_finished(timeout=120)

    messages = " ".join(get_json(server)["status"]["messages"])

    assert "have no data in this recording" in messages
    assert "UNCALIBRATED" not in messages
