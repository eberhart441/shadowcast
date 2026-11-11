"""Raw mode over HTTP (ticket #17): a loopback-only command (ADR-0015) that reaches the simulated rig through its
gateway, whose raw CSI the round recording keeps and the replay shows again.

Every assertion is on an HTTP status, the payload, or what a recording holds.
"""
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from csi.raw_mode import RAW_BUDGET_BYTES
from csi.replay import rounds_dashboard
from csi.room import load_room
from csi.round_recording import RoundRecorder, load_rounds
from csi.server import DashboardServer
from csi.simulate import SimulatedRig

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
JSON_TYPE = {"Content-Type": "application/json"}


def post(server, body, headers=None, path="/api/raw-mode"):
    req = urllib.request.Request(server.url + path, data=json.dumps(body).encode(), method="POST",
                                 headers=headers or JSON_TYPE)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def state(server):
    with urllib.request.urlopen(server.url + "/api/state", timeout=30) as resp:
        return json.loads(resp.read())


def wait_for(server, condition, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = state(server)
        if condition(payload):
            return payload
        time.sleep(0.1)
    raise AssertionError(f"timed out; last raw_mode {payload.get('raw_mode')}")


def raw_row(payload, label):
    return {row["id"]: row for row in payload["raw_mode"]["links"]}.get(label, {})


@pytest.fixture
def serve():
    started = []

    def _serve(sources, **kwargs):
        server = DashboardServer(ROOM, sources, port=0, mode="auto", **kwargs)
        server.start()
        started.append(server)
        return server

    yield _serve
    for server in started:
        server.stop()


def everyone_heard(payload):
    return all(node["last_heard_s"] is not None for node in payload["node_health"])


def test_raw_mode_from_the_dashboard_is_recorded_and_replays(serve, tmp_path):
    rig = SimulatedRig(ROOM, None, duration_s=6.0, realtime=True, seed=3)
    path = tmp_path / "raw.rounds"
    with RoundRecorder(path, ROOM, ["sim"], notes="raw mode test") as recorder:
        server = serve(recorder.tee({"sim": rig}), gateways={"sim": rig})
        wait_for(server, everyone_heard)
        status, body = post(server, {"link": "n1<-n5", "on": True})
        assert status == 200, body
        assert (body["sent"], body["via"]) == (3, "sim")
        payload = wait_for(server, lambda p: raw_row(p, "n1<-n5").get("blocks", 0) >= 10)
        assert raw_row(payload, "n1<-n5")["state"] == "on"
        status, _ = post(server, {"link": "n1<-n5", "on": False})
        assert status == 200
        wait_for(server, lambda p: raw_row(p, "n1<-n5").get("state") == "off")
        assert server.wait_finished(timeout=30)
        live = state(server)
    samples = load_rounds(path).raw_csi()
    assert {s.link for s in samples} == {"n1<-n5"}
    assert len(samples) == live["status"]["broadcasts"]["raw_csi"]["blocks"] >= 10
    assert all(len(s.csi.iq) == 128 for s in samples)
    replay = rounds_dashboard(path, speed=math.inf)
    replay.start()
    try:
        assert replay.wait_finished(timeout=60)
        again = state(replay)
        assert post(replay, {"link": "n1<-n5", "on": True})[0] == 409     # a replay takes no commands
    finally:
        replay.stop()
    assert again["status"]["broadcasts"]["raw_csi"] == live["status"]["broadcasts"]["raw_csi"]
    assert raw_row(again, "n1<-n5")["blocks"] == len(samples)


def test_a_request_beyond_the_budget_is_refused_over_http_with_its_reason(serve):
    rig = SimulatedRig(ROOM, None, duration_s=30.0, realtime=True)
    server = serve({"sim": rig}, gateways={"sim": rig})
    wait_for(server, everyone_heard)
    for label in ("n1<-n5", "n2<-n5", "n3<-n5"):
        assert post(server, {"link": label, "on": True})[0] == 200
    status, body = post(server, {"link": "n4<-n5", "on": True})
    assert (status, body["reason"]) == (409, "budget")
    assert str(RAW_BUDGET_BYTES) in body["error"]
    assert len(rig.sent) == 9          # the refused request never reached the gateway
    status, body = post(server, {"link": "n1<-n9", "on": True})
    assert (status, body["reason"]) == (400, "unknown_link")
    assert post(server, {"link": "n1<-n5", "on": "yes"})[0] == 400
    assert post(server, {"link": "n1<-n5", "on": True, "extra": 1})[0] == 400


def test_raw_mode_is_refused_where_commands_are(serve):
    rig = SimulatedRig(ROOM, None, duration_s=30.0, realtime=True)
    open_bind = serve({"sim": rig}, gateways={"sim": rig}, host="0.0.0.0")
    assert post(open_bind, {"link": "n1<-n5", "on": True})[0] == 403          # not loopback, no --allow-control
    local = serve({"sim": rig}, gateways={"sim": rig})
    assert post(local, {"link": "n1<-n5", "on": True}, headers={"Content-Type": "text/plain"})[0] == 415
    assert post(local, {"link": "n1<-n5", "on": True},
                headers={**JSON_TYPE, "Origin": "http://evil.example"})[0] == 403
    assert rig.sent == []


def test_without_a_gateway_that_can_send_raw_mode_is_refused(serve):
    rig = SimulatedRig(ROOM, None, duration_s=30.0, realtime=True)
    server = serve({"sim": rig})                     # a source, but nothing to send through
    wait_for(server, everyone_heard)
    status, body = post(server, {"link": "n1<-n5", "on": True})
    assert (status, body["reason"]) == (409, "no_gateway")


def test_the_cli_sends_raw_mode_through_the_node_gateway_and_simulate_through_the_simulated_rig(monkeypatch, tmp_path):
    import importlib.util
    from csi.broadcast import FRAME_RAW_REQUEST, decode_broadcast
    from csi.gateway_records import encode_heard, encode_status
    from csi.round_auth import tag_ok, write_kit_key
    from csi.simulate import simulate_rounds

    spec = importlib.util.spec_from_file_location("dashboard_cli", Path(__file__).parents[1] / "scripts" / "dashboard.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    key_path = tmp_path / "kit.key"
    key = write_kit_key(key_path)
    t0 = time.time()
    gateway = "02:00:00:00:00:99"
    data = encode_status(gateway, 1_000_000, 1, 1, 6, [0] * 7) + b"".join(
        encode_heard(int((ts - t0) * 1e6), -50, b) for ts, b in simulate_rounds(ROOM, duration_s=1.0, t0=t0,
                                                                                kit_key=key))
    written = []

    class FakeChunks:
        def __init__(self, port, baud=921600, timeout=1.0):
            pass

        def __iter__(self):
            yield time.time(), data
            while True:
                time.sleep(0.05)
                yield time.time(), None

        def write(self, line):
            written.append(line)

    monkeypatch.setattr("csi.gateway_records.SerialChunks", FakeChunks)
    room = Path(__file__).parent / "fixtures" / "room_tr8.json"
    args = cli.build_parser().parse_args(["--node-gateway", "COM7", "--room", str(room), "--http-port", "0",
                                          "--kit-key", str(key_path)])
    cli.check_source(args)
    server = cli.build_server(args)
    server.start()
    try:
        wait_for(server, everyone_heard)
        status, body = post(server, {"link": "n2<-n3", "on": True})
    finally:
        server.stop()
    assert (status, body["via"]) == (200, "gateway"), body
    assert len(written) == 3 and all(line.startswith(b"send ") and line.endswith(b"\n") for line in written)
    frame = bytes.fromhex(written[0][5:-1].decode())
    assert tag_ok(frame, key) and decode_broadcast(frame).frame_type == FRAME_RAW_REQUEST
    assert decode_broadcast(frame).node_id == gateway

    args = cli.build_parser().parse_args(["--simulate", "--room", str(room), "--http-port", "0"])
    cli.check_source(args)
    server = cli.build_server(args)
    server.start()
    try:
        wait_for(server, everyone_heard)
        assert post(server, {"link": "n2<-n3", "on": True})[0] == 200
        wait_for(server, lambda p: raw_row(p, "n2<-n3").get("state") == "on")
    finally:
        server.stop()


class StuckGateway:
    """A transmitter whose port write never returns until released: a wedged USB port."""

    def __init__(self, rig):
        self._rig = rig
        self.gateway_serial = rig.gateway_serial
        self.release = __import__("threading").Event()

    def send_frame(self, frame):
        self.release.wait(30)


def test_a_stuck_gateway_write_neither_blocks_the_state_nor_hangs_the_command(serve, monkeypatch):
    import threading

    import csi.server

    monkeypatch.setattr(csi.server, "SEND_TIMEOUT_S", 0.5)
    rig = SimulatedRig(ROOM, None, duration_s=30.0, realtime=True)
    stuck = StuckGateway(rig)
    server = serve({"sim": rig}, gateways={"sim": stuck})
    try:
        wait_for(server, everyone_heard)
        answer = {}
        thread = threading.Thread(target=lambda: answer.update(r=post(server, {"link": "n1<-n5", "on": True})))
        thread.start()
        time.sleep(0.1)
        started = time.monotonic()
        state(server)                                  # the write holds no lock /api/state needs
        assert time.monotonic() - started < 0.4
        thread.join(10)
        status, body = answer["r"]
        assert (status, body["reason"]) == (503, "timeout")
    finally:
        stuck.release.set()
