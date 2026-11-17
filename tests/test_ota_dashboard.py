"""OTA start as a dashboard command (spec #2: "Commands: raw mode on/off for a link, and OTA start"; ADR-0015, ADR-0031).

POST /api/ota starts a staged rollout through the gateway the dashboard already holds, without stopping it: the
gateway sends the signed maintenance command, the nodes fetch the image from the dashboard host's image server and
report there, and the round broadcasts the dashboard ingests tell the rollout who is back on the round. Assertions
are on HTTP statuses, the payload's status.ota, and the frames the gateway was handed.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from csi import ota
from csi.room import load_room
from csi.round_auth import TEST_KIT_KEY
from csi.server import DashboardServer
from csi.simulate import SimulatedRig

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
SERIAL = {node.name: node.serial.upper() for node in ROOM.transceivers}
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ota"
SIGNED = FIXTURES / "signed.bin"
OTHER = FIXTURES / "signed_by_other_key.bin"
TRUSTED_HEX = json.loads((FIXTURES / "image.json").read_text())["test_key_digest"]
JSON_TYPE = {"Content-Type": "application/json"}


class Gateway(SimulatedRig):
    """The simulated rig behind a node gateway that knows its channel and sends maintenance commands."""
    channel = 6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.maintenance = []

    def send_maintenance(self, frame):
        self.maintenance.append(bytes(frame))


def post(server, body, path="/api/ota", headers=None):
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
    payload = {}
    while time.monotonic() < deadline:
        payload = state(server)
        if condition(payload):
            return payload
        time.sleep(0.1)
    raise AssertionError(f"timed out; last status.ota {payload.get('status', {}).get('ota')}")


@pytest.fixture
def signer(tmp_path):
    path = tmp_path / "ota-signing.digest"
    path.write_text(TRUSTED_HEX + "\n")
    return path


@pytest.fixture
def serve(signer):
    started = []

    def _serve(gateway, **kwargs):
        kwargs.setdefault("ota_signer", signer)
        server = DashboardServer(ROOM, {"gateway": gateway}, port=0, mode="auto", gateways={"gateway": gateway},
                                 **kwargs)
        server.start()
        started.append(server)
        return server

    yield _serve
    for server in started:
        server.stop()


def report(port, serial, issued, outcome="committed"):
    line = f"ota-report 1 serial={serial} issued={issued} outcome={outcome} slot=ota_1 image=- version=test"
    body = f"{line} tag={ota.report_tag(line, TEST_KIT_KEY)}\n".encode()
    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/report", data=body), timeout=5) as r:
        return r.status


def test_ota_start_rolls_out_through_the_dashboards_gateway_while_it_keeps_serving(serve):
    gateway = Gateway(ROOM, None, duration_s=60.0, realtime=True)
    server = serve(gateway)
    wait_for(server, lambda p: p["status"]["broadcasts"]["accepted"] > 16)
    status, body = post(server, {"image": str(SIGNED), "nodes": ["n1", "n2"], "ap_channel": 36, "port": 0,
                                 "bind": "127.0.0.1"})
    assert status == 202, body
    assert body["ota"]["state"] == "running" and body["ota"]["stage"] == 1
    payload = wait_for(server, lambda p: len(gateway.maintenance) >= ota.COPIES)
    command = ota.decode_command(gateway.maintenance[0])
    assert command.targets == (SERIAL["n1"], SERIAL["n2"]) and command.ap_channel == 36
    assert payload["status"]["ota"]["state"] == "running"
    assert post(server, {"image": str(SIGNED)})[0] == 409           # one rollout at a time
    port = payload["status"]["ota"]["image_port"]
    for name in ("n1", "n2"):
        assert report(port, SERIAL[name], command.issued) == 200
    payload = wait_for(server, lambda p: p["status"]["ota"]["state"] == "done")   # reported and heard again
    assert payload["status"]["ota"]["results"] == {SERIAL["n1"]: "committed", SERIAL["n2"]: "committed"}
    assert payload["status"]["broadcasts"]["accepted"] > 16 and payload["status"]["finished"] is False


def test_ota_start_is_refused_with_a_reason(serve, signer, tmp_path):
    gateway = Gateway(ROOM, None, duration_s=60.0, realtime=True)
    server = serve(gateway)
    status, body = post(server, {"image": str(OTHER), "port": 0})
    assert (status, body["reason"]) == (409, "signer")
    assert post(server, {"image": str(tmp_path / "missing.bin"), "port": 0})[0] == 400
    assert post(server, {"image": str(SIGNED), "ap_channel": 4, "port": 0})[1]["reason"] == "channel"   # overlaps 6
    assert post(server, {"image": str(SIGNED), "nodes": ["n9"], "port": 0})[0] == 400
    assert post(server, {"image": str(SIGNED), "extra": 1})[0] == 400
    assert gateway.maintenance == []
    unsigned = serve(Gateway(ROOM, None, duration_s=60.0, realtime=True), ota_signer=tmp_path / "none.digest")
    assert post(unsigned, {"image": str(SIGNED), "port": 0})[1]["reason"] == "no_signer"
    plain = SimulatedRig(ROOM, None, duration_s=60.0, realtime=True)        # a source that cannot send commands
    no_gateway = DashboardServer(ROOM, {"sim": plain}, port=0, mode="auto", ota_signer=signer)
    no_gateway.start()
    try:
        assert post(no_gateway, {"image": str(SIGNED), "port": 0})[1]["reason"] == "no_gateway"
    finally:
        no_gateway.stop()


def test_ota_start_is_a_command_like_the_others(serve):
    gateway = Gateway(ROOM, None, duration_s=60.0, realtime=True)
    open_bind = serve(gateway, host="0.0.0.0")
    assert post(open_bind, {"image": str(SIGNED)})[0] == 403                  # not loopback, no --allow-control
    local = serve(gateway)
    assert post(local, {"image": str(SIGNED)}, headers={"Content-Type": "text/plain"})[0] == 415
    replay = serve(gateway, replay=True)
    assert post(replay, {"image": str(SIGNED)})[0] == 409                     # a replay takes no commands
    assert gateway.maintenance == []


def test_a_node_gateway_sends_a_maintenance_command_as_one_ota_send_line_and_knows_its_channel():
    from csi.gateway_records import GatewaySource, encode_status

    class Port(list):
        written = b""

        def write(self, data):
            Port.written += data

    gateway = GatewaySource(Port([(0.0, encode_status("02:00:00:00:00:80", 5_000_000, 1, 1, 11, [0] * 7))]))
    assert gateway.channel is None
    list(gateway)
    assert gateway.channel == 11
    gateway.send_maintenance(bytes.fromhex("0208"))
    assert Port.written == b"ota send 0208\n"
