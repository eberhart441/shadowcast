"""The dashboard page (web/index.html): served by the server, works offline, wired to the state and event endpoints.

The page's drawing and behaviour are checked by hand (see the ticket); these tests read the page source and
one response from a running server.
"""
import copy
import json
import math
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from csi.dashboard import DashboardState
from csi.detect import DEFAULT_MARGIN
from csi.live_calibration import DEFAULT_CLIPS, DEFAULT_SECONDS
from csi.room import load_room
from csi.server import DashboardServer
from csi.simulate import simulate_room

PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"
# Updated in the impl2 integration: the page now draws the one-person track (csi/tracker.py) over the map, so the
# caption no longer says "not a position track"; it names the estimator that drew the heatmap instead (the
# four-link rig's default under --estimator auto is the back-projection).
CAPTION = "Rough motion-likelihood estimate from 4 links (back-projection)."
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


def test_noisy_room_calibration_is_off_by_default():
    # the quiet-room rules (movement in a clip is refused) are the default; "noisy room" is an explicit choice
    html = PAGE.read_text(encoding="utf-8")
    tag = re.search(r'<input[^>]*id="cal-noisy"[^>]*>', html).group(0)
    assert "checked" not in tag


def test_calibration_form_starts_at_the_calibration_api_defaults():
    # the page always sends its own clips, seconds and margin, so its defaults are the calibration a click runs:
    # the same minute of empty room (3 x 20 s, for the 2 s motion window) that POST /api/calibrate defaults to
    html = PAGE.read_text(encoding="utf-8")

    def value(input_id):
        return float(re.search(rf'<input[^>]*id="{input_id}"[^>]*value="([^"]+)"', html).group(1))

    assert value("cal-clips") == DEFAULT_CLIPS
    assert value("cal-seconds") == DEFAULT_SECONDS
    assert value("cal-margin") == DEFAULT_MARGIN


def test_page_talks_to_the_command_endpoints_and_shows_the_countdown_text():
    html = PAGE.read_text(encoding="utf-8")

    for endpoint in ("/api/calibrate", "/api/calibrate/cancel", "/api/threshold", "/api/thresholds/save",
                     "/api/params"):
        assert f'"{endpoint}"' in html, endpoint
    assert "Clear the room" in html
    assert "Keep the room empty" in html
    assert "commands" in html and "available" in html    # hides or disables the controls with the reason
    assert "unreliable" in html or "warning" in html     # warnings are highlighted


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


# ---------------------------------------------------------------- link health, last motion, near-node veto

FIVE = Path(__file__).resolve().parent / "fixtures" / "room_5node.json"
HEALTH = {"seq_rate": 36.4, "seq_loss": 0.125, "clip_pct": 0.032, "agc_gain": 32, "fft_gain": -6,
          "noise_floor": -92, "since_motion_s": 3.2}
RENDER_STUB = r"""
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const script = input.html.split("<script>")[1].split("</script>")[0];
const mapLog = [];
const texts = [];
function recorder(log) {
  const target = {};
  return new Proxy(target, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === "measureText") return (s) => ({ width: String(s).length * 7 });
      if (k === "createImageData") return (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) });
      return (...args) => {
        if (log) log.push([k, ...args.map((a) => (typeof a === "object" && a !== null ? "obj" : a))]);
      };
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
    set(obj, k, v) { obj[k] = v; if (k === "textContent") texts.push(String(v)); return true; },
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
let stream = null;
function FakeEventSource() { this.close = noop; stream = this; }
const errors = [];
const busyHidden = [];
const trackHidden = [];
try {
  // no /api/state: the page goes straight to the event stream, which this stub drives payload by payload
  new Function("window", "document", "fetch", "EventSource", "setTimeout", "clearTimeout", script)(
    window, document, () => Promise.reject(new Error("no server")), FakeEventSource, noop, noop);
} catch (err) { errors.push(String(err)); }
(async () => {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  for (const payload of input.payloads) {
    try {
      if (!stream) throw new Error("the page opened no event stream");
      stream.onmessage({ data: JSON.stringify(payload) });   // render errors surface here, not in a promise
    } catch (err) { errors.push(String((err && err.stack) || err)); }
    busyHidden.push(elements["busy-note"] ? elements["busy-note"].hidden : null);
    trackHidden.push(elements["track-note"] ? elements["track-note"].hidden : null);
  }
  console.log(JSON.stringify({ calls: mapLog, texts, errors, busyHidden, trackHidden }));
})();
"""


def render_payloads_with_stub(tmp_path, payloads):
    """Run the page script under a DOM stub and feed it ``payloads`` as events; texts set and map calls made."""
    script = tmp_path / "render_stub.js"
    script.write_text(RENDER_STUB, encoding="utf-8")
    html = PAGE.read_text(encoding="utf-8")
    proc = subprocess.run(["node", str(script)], input=json.dumps({"html": html, "payloads": payloads}),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def health_payloads():
    """A five-node payload with link health, last motion and a busy node, and the same payload as an old log
    (or an older server) would have it: none of those keys."""
    new = DashboardState(load_room(FIVE)).snapshot(now=1_700_000_000.0)
    new["heatmap"]["busy_nodes"] = ["tx0"]
    new["links"][0].update(HEALTH)
    new["status"]["room"]["since_motion_s"] = 12.4
    old = copy.deepcopy(new)
    del old["heatmap"]["busy_nodes"], old["status"]["room"]["since_motion_s"]
    for row in old["links"]:
        for key in HEALTH:                    # the six health keys and since_motion_s
            row.pop(key, None)
    return new, old


@pytest.mark.slow
@needs_node
def test_page_shows_link_health_last_motion_and_activity_near_a_node_and_still_renders_old_payloads(tmp_path):
    new, old = health_payloads()
    result = render_payloads_with_stub(tmp_path, [new, old])
    assert result["errors"] == []
    texts = result["texts"]
    for shown in ("12.5 %", "36.4", "3.2 %", "32 / -6", "-92", "3 s ago", "12 s ago"):
        assert shown in texts, shown                                  # loss, sent/s, clipped, gains, noise, motion
    assert any(t.startswith("activity near tx0") for t in texts)      # the note under the map
    assert any(c[0] == "fillText" and c[1] == "activity near tx0" for c in result["calls"])   # and on the map
    assert result["busyHidden"] == [False, True]                      # the old payload has no busy node


# ---------------------------------------------------------------- the person track (payload.track, csi/tracker.py)

TRACK_BEGIN = "// ---- track: pure (tested with node) begin"
TRACK_END = "// ---- track: pure (tested with node) end"
NOW = 1_700_000_000.0
ACTIVE_TRACK = {"active": True, "x": 1.2, "y": 0.8, "sx": 0.3, "sy": 0.2, "rho": 0.4, "vx": 0.5, "vy": -0.1,
                "age_s": 8.0, "last_seen_s": 0.0,
                "trail": [[NOW - 2.0, 0.6, 0.9], [NOW - 1.0, 0.9, 0.85], [NOW, 1.2, 0.8]]}


def test_page_draws_the_track_over_the_heatmap_with_a_toggle_and_tolerates_no_track():
    html = PAGE.read_text(encoding="utf-8")
    assert TRACK_BEGIN in html and TRACK_END in html
    assert "usableTrack(p.track)" in html                 # a missing or null track draws nothing
    assert re.search(r'<input[^>]*id="track-show"[^>]*checked', html)   # shown by default, can be hidden
    assert "drawHeat(p.heatmap, g)" in html               # the heatmap stays
    assert "last seen" in html and "ctx.ellipse(" in html


@pytest.mark.slow
@needs_node
def test_sigma_ellipse_axes_and_rotation_follow_the_covariance(tmp_path):
    html = PAGE.read_text(encoding="utf-8")
    block = html[html.index(TRACK_BEGIN):html.index(TRACK_END)]
    script = tmp_path / "ellipse_harness.js"
    script.write_text(block + """
var cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(cases.map(function (c) { return sigmaEllipse(c[0], c[1], c[2]); })));
""", encoding="utf-8")
    cases = [[0.3, 0.1, 0.0], [0.1, 0.3, 0.0], [0.2, 0.2, 0.9], [0.2, 0.2, -0.9]]
    proc = subprocess.run(["node", str(script)], input=json.dumps(cases), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    wide, tall, down_right, up_right = json.loads(proc.stdout)
    assert (wide["rx"], wide["ry"], wide["angle"]) == (pytest.approx(0.3), pytest.approx(0.1), pytest.approx(0.0))
    assert tall["rx"] == pytest.approx(0.3) and tall["angle"] == pytest.approx(math.pi / 2)
    # y runs down on the map as in the room: positive correlation leans the ellipse down to the right
    assert down_right["angle"] == pytest.approx(math.pi / 4) and down_right["rx"] > down_right["ry"]
    assert up_right["angle"] == pytest.approx(3 * math.pi / 4)


@pytest.mark.slow
@needs_node
def test_page_draws_a_track_a_coasting_track_and_a_last_seen_marker_and_still_renders_without_one(tmp_path):
    base = DashboardState(load_room(FIVE)).snapshot(now=NOW)
    assert "track" in base and base["track"] is None      # no track until one is confirmed
    active = {**copy.deepcopy(base), "track": dict(ACTIVE_TRACK)}
    coasting = {**copy.deepcopy(base), "track": {**ACTIVE_TRACK, "last_seen_s": 1.5}}
    lost = {**copy.deepcopy(base), "track": {**ACTIVE_TRACK, "active": False, "vx": 0.0, "vy": 0.0,
                                             "last_seen_s": 12.4}}
    old = copy.deepcopy(base)
    del old["track"]                                       # a log written before the tracker
    result = render_payloads_with_stub(tmp_path, [active, coasting, lost, old, copy.deepcopy(base)])
    assert result["errors"] == []
    calls, texts = result["calls"], result["texts"]
    assert sum(c[0] == "ellipse" for c in calls) == 3      # one 1-sigma ellipse per drawn track
    assert any(c[0] == "fillText" and c[1] == "last seen 12 s ago" for c in calls)
    assert any(t.startswith("Person track at (1.20, 0.80) m") for t in texts)
    assert any(t.startswith("Person track coasting near (1.20, 0.80) m") for t in texts)
    assert any(t.startswith("Last seen 12 s ago at (1.20, 0.80) m") for t in texts)
    assert result["trackHidden"] == [False, False, False, True, True]


# ---------------------------------------------------------------- the estimator in the caption (heatmap.estimator)

def test_page_caption_names_the_heatmap_estimator_from_the_payload():
    html = PAGE.read_text(encoding="utf-8")
    assert "els.caption.textContent = mapCaption(p)" in html     # the caption follows each payload
    assert "p.heatmap.estimator" in html
    assert '"RTI"' in html and '"back-projection"' in html


@pytest.mark.slow
@needs_node
def test_page_caption_says_which_estimator_drew_the_map_and_names_none_for_an_old_log(tmp_path):
    base = DashboardState(load_room(FIVE)).snapshot(now=NOW)
    assert base["heatmap"]["estimator"] == "backprojection"   # six links but none reporting yet: auto draws BP
    rti = copy.deepcopy(base)
    rti["heatmap"]["estimator"] = "rti"
    old = copy.deepcopy(base)
    del old["heatmap"]["estimator"]                           # a log written before the RTI estimator
    result = render_payloads_with_stub(tmp_path, [base, rti, old])
    assert result["errors"] == []
    n = len(base["links"])
    captions = [t for t in result["texts"] if t.startswith("Rough motion-likelihood estimate")]
    assert captions == [f"Rough motion-likelihood estimate from {n} links (back-projection).",
                        f"Rough motion-likelihood estimate from {n} links (RTI).",
                        f"Rough motion-likelihood estimate from {n} links."]
