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


def test_controls_container_holds_the_calibrate_panel_threshold_editors_and_tuning_sliders():
    html = PAGE.read_text(encoding="utf-8")
    start = html.index('id="controls"')
    end = html.index("</section>", start)
    controls = html[start:end]

    for needle in ('id="cal-clips"', 'id="cal-seconds"', 'id="cal-margin"', 'id="cal-start"', 'id="cal-cancel"',
                   'id="thr-save"', 'id="tune-sensitivity"', 'id="tune-decay"', "Calibrate", "Save thresholds",
                   "sensitivity", "decay"):
        assert needle in controls, needle
    # the rest of the page is intact: the status bar and the links card are still there
    assert html.index('id="calib"') < start and html.index('id="links"') < start


def test_page_talks_to_the_command_endpoints_and_shows_the_countdown_text():
    html = PAGE.read_text(encoding="utf-8")

    for endpoint in ("/api/calibrate", "/api/calibrate/cancel", "/api/threshold", "/api/thresholds/save",
                     "/api/params"):
        assert f'"{endpoint}"' in html, endpoint
    assert "Clear the room" in html
    assert "Keep the room empty" in html
    assert "commands" in html and "available" in html    # hides or disables the controls with the reason
    assert "unreliable" in html or "warning" in html     # warnings are highlighted
