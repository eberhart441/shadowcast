"""The image server the nodes pull from in maintenance mode (csi/ota.py ImageServer), over a real socket."""
import urllib.error
import urllib.request

import pytest

from csi import ota
from csi.round_auth import TEST_KIT_KEY

IMAGE = b"\xe9" + bytes(range(256)) * 40
LINE = ("ota-report 1 serial=02:00:00:00:02:5C issued=1794000000 outcome=committed slot=ota_1 "
        "image=abababababababab version=v0.3-18-g1a2b3c4")


@pytest.fixture
def server():
    s = ota.ImageServer(IMAGE, TEST_KIT_KEY, host="127.0.0.1", port=0)
    s.start()
    yield s
    s.close()


def url(server, path):
    return f"http://127.0.0.1:{server.port}{path}"


def test_a_node_gets_the_image_and_is_logged(server):
    with urllib.request.urlopen(url(server, "/node.bin?serial=02:00:00:00:02:5C"), timeout=5) as r:
        assert r.read() == IMAGE
        assert r.headers["Content-Length"] == str(len(IMAGE))
    assert server.fetches == ["02:00:00:00:02:5C"]


def test_a_report_with_the_kits_tag_is_taken(server):
    body = (LINE + " tag=ccad49fd7ccc466e\n").encode()
    with urllib.request.urlopen(urllib.request.Request(url(server, "/report"), data=body), timeout=5) as r:
        assert r.status == 200
    assert [rep.outcome for rep in server.take_reports()] == ["committed"]
    assert server.take_reports() == []


@pytest.mark.parametrize("body", [LINE + " tag=0000000000000000", "nonsense", ""])
def test_a_report_without_the_kits_tag_is_refused(server, body):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url(server, "/report"), data=body.encode()), timeout=5)
    assert e.value.code == 400
    assert server.take_reports() == []
    assert server.refused_reports == 1


def test_anything_else_is_not_found(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url(server, "/etc/passwd"), timeout=5)
    assert e.value.code == 404
