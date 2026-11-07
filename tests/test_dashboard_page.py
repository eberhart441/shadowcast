"""The dashboard page (web/index.html): served by the server, works offline, wired to the state and event endpoints.

The page's drawing and behaviour are checked by hand (see the ticket); these tests read the page source and
one response from a running server.
"""
import re
import urllib.request
from pathlib import Path

import pytest

from csi.room import load_room
from csi.server import DashboardServer
from csi.simulate import simulate_room

PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"
CAPTION = "Rough motion-likelihood estimate from 4 links, not a position track."
ROOM = load_room()


@pytest.fixture
def server():
    sources = simulate_room(None, room=ROOM, duration_s=1.0, seed=1)
    srv = DashboardServer(ROOM, sources, None, port=0)
    srv.start()
    yield srv
    srv.stop()


def test_root_serves_the_page_with_the_caption_pause_and_controls_container(server):
    with urllib.request.urlopen(server.url + "/", timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        body = resp.read().decode("utf-8")

    assert CAPTION in body
    assert "/api/state" in body
    assert "/api/events" in body
    assert 'id="pause"' in body
    assert 'id="controls"' in body
    assert "Controls" in body


def test_page_loads_nothing_from_the_network():
    html = PAGE.read_text(encoding="utf-8")

    assert not re.search(r"https?://", html, re.I)               # no URLs at all, not even in comments
    assert not re.search(r"<link\b", html, re.I)                  # no stylesheets or fonts from elsewhere
    assert not re.search(r"<(script|img|iframe|source)\b[^>]*\bsrc\s*=", html, re.I)
    assert not re.search(r"@import|url\(\s*[\"']?\s*//", html, re.I)


def test_page_closes_the_event_stream_when_the_run_is_finished():
    html = PAGE.read_text(encoding="utf-8")

    # each test of status.finished closes the EventSource before anything can reconnect it
    hits = [m.start() for m in re.finditer(r"status\.finished", html)]
    assert hits
    assert any("source.close()" in html[i:i + 300] for i in hits)
    assert "run finished" in html
