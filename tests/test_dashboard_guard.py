"""Command guard over HTTP: media type, Host and Origin checks on POST commands (cross-site pages, DNS rebinding).

Every assertion is on an HTTP status, error text or the payload. GET endpoints stay open.
"""
import json
import urllib.error
import urllib.request

import pytest

from csi.links import calibrate_links
from csi.room import load_room
from csi.server import DashboardServer
from csi.simulate import simulate_room_links

ROOM = load_room()
RIGHT = "rx1<-00"
JSON_TYPE = {"Content-Type": "application/json"}


@pytest.fixture(scope="module")
def thresholds():
    return calibrate_links(list(simulate_room_links(room=ROOM, person=None, duration_s=25, seed=10)))


@pytest.fixture
def serve():
    started = []

    def _serve(**kwargs):
        server = DashboardServer(ROOM, {}, None, port=0, **kwargs)
        server.start()
        started.append(server)
        return server

    yield _serve
    for server in started:
        server.stop()


def post(url, path, *, headers, body=None, raw=None):
    data = raw if raw is not None else json.dumps({"link": RIGHT, "value": 1.0} if body is None else body).encode()
    req = urllib.request.Request(url + path, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def state(server):
    with urllib.request.urlopen(server.url + "/api/state", timeout=30) as resp:
        return json.loads(resp.read())


def test_content_type_must_equal_application_json_not_merely_contain_it(serve):
    server = serve()
    for ctype in ["text/plain;application/json", "text/plain; x=application/json", "application/jsonp", "text/plain"]:
        status, _ = post(server.url, "/api/threshold", headers={"Content-Type": ctype})
        assert status in (403, 415), ctype
    assert state(server)["status"]["calibrated_links"] == []


def test_content_type_parameters_after_the_media_type_are_accepted(serve):
    server = serve()
    status, _ = post(server.url, "/api/threshold", headers={"Content-Type": "application/json; charset=utf-8"})
    assert status == 200


def test_a_foreign_host_name_is_refused_with_403_even_when_it_reaches_this_server(serve):
    """DNS rebinding: the browser sends the attacker's name in Host while the request reaches this server."""
    server = serve()
    status, body = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Host": f"evil.example:{server.address[1]}"})
    assert status == 403
    assert "Host" in body["error"]
    assert state(server)["status"]["calibrated_links"] == []


def test_host_without_the_bound_port_is_refused(serve):
    server = serve()
    status, _ = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Host": "localhost"})
    assert status == 403
    status, _ = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Host": "localhost:1"})
    assert status == 403


def test_loopback_host_names_with_the_bound_port_are_accepted(serve):
    server = serve()
    port = server.address[1]
    for host in [f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"]:
        status, _ = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Host": host})
        assert status == 200, host


def test_origin_must_match_an_allowed_origin_when_present(serve):
    server = serve()
    port = server.address[1]
    for origin in ["http://evil.example", "https://localhost:%d" % port, "null", "http://localhost:1"]:
        status, body = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Origin": origin})
        assert status == 403, origin
        assert "Origin" in body["error"]
    for origin in [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]:
        status, _ = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Origin": origin})
        assert status == 200, origin


def test_network_control_trusts_the_host_option_and_ip_literals_but_not_names(serve):
    server = serve(host="0.0.0.0", allow_control=True)
    port = server.address[1]
    for host in [f"0.0.0.0:{port}", f"192.168.1.20:{port}"]:
        status, _ = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Host": host})
        assert status == 200, host
    status, _ = post(server.url, "/api/threshold", headers={**JSON_TYPE, "Host": f"evil.example:{port}"})
    assert status == 403


def test_get_requests_stay_open_whatever_the_host_or_origin(serve):
    server = serve()
    req = urllib.request.Request(server.url + "/api/state",
                                 headers={"Host": "evil.example:1", "Origin": "http://evil.example"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert resp.status == 200
