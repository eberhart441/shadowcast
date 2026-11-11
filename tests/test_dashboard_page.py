"""The dashboard page (web/index.html): served by the server, works offline, wired to the state and event endpoints.

The page's drawing and behaviour are checked by hand (see the ticket); these tests read the page source and
one response from a running server.
"""
import json
import re
import shutil
import subprocess
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
    with urllib.request.urlopen(server.url + "/", timeout=30) as resp:
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


def test_the_link_detail_has_a_raw_mode_switch_that_posts_to_the_raw_mode_command():
    html = PAGE.read_text(encoding="utf-8")
    assert '"/api/raw-mode"' in html
    assert 'id="raw-toggle"' in html and 'id="raw-links"' in html and 'id="raw-error"' in html
    assert "raw_mode" in html and "budget_bytes" in html     # shows the per-round byte budget in use


# ---------------------------------------------------------------- the map draws nodes outside the room

STAR = Path(__file__).resolve().parent / "fixtures" / "room_star.json"
GEOMETRY_BEGIN = "// ---- geometry: pure (tested with node) begin"
GEOMETRY_END = "// ---- geometry: pure (tested with node) end"
HARNESS = """
%(geometry)s
var input = JSON.parse(require("fs").readFileSync(0, "utf8"));
var room = input.room, nodes = input.nodes, bounds = mapBounds(room, nodes);
console.log(JSON.stringify({
  bounds: bounds,
  nodes: nodes.map(function (n) {
    return { name: n.name, outside: isOutsideRoom(n, room), leader: leaderEnd(n, room), side: labelSide(n, bounds) };
  })
}));
"""


def run_geometry(tmp_path, room, nodes):
    """Evaluate the page's pure map geometry under node, on the given room and node positions."""
    html = PAGE.read_text(encoding="utf-8")
    start = html.index(GEOMETRY_BEGIN)
    end = html.index(GEOMETRY_END, start)
    script = tmp_path / "geometry_harness.js"
    script.write_text(HARNESS % {"geometry": html[start:end]}, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], input=json.dumps({"room": room, "nodes": nodes}),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page's geometry")


def star_payload():
    room = load_room(STAR)
    sources = simulate_room(None, room=room, duration_s=1.0, seed=1)
    server = DashboardServer(room, sources, {}, port=0)
    server.start()
    try:
        assert server.wait_finished(timeout=60)
        with urllib.request.urlopen(server.url + "/api/state", timeout=30) as resp:
            return json.loads(resp.read())
    finally:
        server.stop()


DOM_STUB = r"""
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const script = input.html.split("<script>")[1].split("</script>")[0];
const mapLog = [];
function recorder(log) {
  const target = {};
  return new Proxy(target, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === "measureText") return (s) => ({ width: String(s).length * 7 });
      if (k === "createImageData") return (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) });
      return (...args) => { if (log) log.push([k, ...args.map((a) => (typeof a === "object" && a !== null ? "obj" : a))]); };
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}
const mapCtx = recorder(mapLog), heatCtx = recorder(null);
function element(id, tag) {
  const t = { clientWidth: 600, clientHeight: 40, width: 0, height: 0, firstChild: null, style: {}, hidden: false,
    className: "", textContent: "", classList: { toggle() {} } };
  if (tag === "canvas" || id === "map") t.getContext = () => (id === "map" ? mapCtx : heatCtx);
  return new Proxy(t, {
    get(obj, k) { return k in obj ? obj[k] : () => undefined; },
    set(obj, k, v) { obj[k] = v; return true; },
  });
}
const elements = {};
const document = {
  getElementById(id) { return (elements[id] = elements[id] || element(id, "div")); },
  createElement(tag) { return element("tmp", tag); },
  createTextNode(text) { return { text }; },
  activeElement: null,
};
const window = { devicePixelRatio: 1, addEventListener() {}, requestAnimationFrame() {} };
const noop = () => 0;
function FakeEventSource() { this.close = noop; }
const errors = [];
try {
  new Function("window", "document", "fetch", "EventSource", "setTimeout", "clearTimeout", script)(
    window, document, () => Promise.resolve({ json: () => Promise.resolve(input.payload) }),
    FakeEventSource, noop, noop);
} catch (err) { errors.push(String(err)); }
(async () => {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  console.log(JSON.stringify({ calls: mapLog, errors }));
})();
"""


def draw_page_with_stub(tmp_path, payload):
    """Run the page script on ``payload`` under a small DOM stub; return the calls made on the map canvas."""
    script = tmp_path / "dom_stub.js"
    script.write_text(DOM_STUB, encoding="utf-8")
    html = PAGE.read_text(encoding="utf-8")
    proc = subprocess.run(["node", str(script)], input=json.dumps({"html": html, "payload": payload}),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["errors"] == []
    return result["calls"]


@pytest.mark.slow
@needs_node
def test_page_draws_rx3_above_the_room_outline_and_heat_exactly_over_the_room_rectangle(tmp_path):
    calls = draw_page_with_stub(tmp_path, star_payload())
    outline = next(c for c in calls if c[0] == "strokeRect")          # the room outline is drawn first
    heat = next(c for c in calls if c[0] == "drawImage")
    assert heat[2:] == outline[1:]                                    # the heat covers the room rectangle only
    room_top = outline[2]
    # rx3 is 14 px square with its centre above the top wall: inside the canvas, and not on the room
    markers = [c for c in calls if c[0] == "strokeRect" and c[3] == 14 and c[4] == 14]
    above = [c for c in markers if c[2] + 7 < room_top and c[2] >= 0]
    assert len(above) == 1
    assert any(c[0] == "fillText" and c[1] == "outside room" for c in calls)


def test_page_has_marked_pure_geometry_block_and_the_map_draws_from_it():
    html = PAGE.read_text(encoding="utf-8")
    assert GEOMETRY_BEGIN in html and GEOMETRY_END in html
    assert "mapBounds(room, p.nodes)" in html            # the map frame includes every node
    assert "outside room" in html                         # nodes beyond the rectangle say so on the map
    assert "leaderEnd(" in html                           # and get a dashed leader to the nearest room edge


@pytest.mark.slow
@needs_node
def test_map_bounds_are_the_room_and_every_node_plus_a_0_3_m_margin(tmp_path):
    payload = star_payload()
    b = run_geometry(tmp_path, payload["room"], payload["nodes"])["bounds"]
    assert b["x0"] == pytest.approx(-0.3) and b["x1"] == pytest.approx(2.77 + 0.3)
    assert b["y0"] == pytest.approx(-1.5 - 0.3) and b["y1"] == pytest.approx(1.84 + 0.3)


@pytest.mark.slow
@needs_node
def test_map_bounds_without_an_outside_node_are_just_the_room_plus_margin(tmp_path):
    payload = star_payload()
    inside = [n for n in payload["nodes"] if n["name"] != "rx3"]
    b = run_geometry(tmp_path, payload["room"], inside)["bounds"]
    assert b["x0"] == pytest.approx(-0.3) and b["y0"] == pytest.approx(-0.3)
    assert b["x1"] == pytest.approx(2.77 + 0.3) and b["y1"] == pytest.approx(1.84 + 0.3)


@pytest.mark.slow
@needs_node
def test_rx3_is_outside_the_room_with_a_leader_to_the_top_wall_and_labels_point_into_the_bounds(tmp_path):
    payload = star_payload()
    by_name = {n["name"]: n for n in run_geometry(tmp_path, payload["room"], payload["nodes"])["nodes"]}
    rx3 = by_name["rx3"]
    assert rx3["outside"] is True
    assert rx3["leader"] == {"x": pytest.approx(1.385), "y": pytest.approx(0.0)}
    assert rx3["side"]["dy"] > 0                 # rx3 is at the top of the bounds: its label goes below it
    for name in ("rx1", "rx2", "tx0", "tx1"):
        assert by_name[name]["outside"] is False, name
    assert by_name["tx1"]["side"]["dy"] < 0 and by_name["tx1"]["side"]["dx"] > 0   # bottom-left: up and right
    assert by_name["rx2"]["side"]["dx"] > 0 and by_name["rx2"]["side"]["dy"] > 0   # top-left: right and down
