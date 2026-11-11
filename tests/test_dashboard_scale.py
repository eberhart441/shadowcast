"""The dashboard at transceiver scale: per-node health, coverage, the link matrix, per-link detail on demand.

Round broadcasts come from the simulated rig (csi.simulate.simulate_rounds) for the frozen 8-node and 30-node
transceiver rooms; assertions are on DashboardState's payload and on the server's HTTP interface.
"""
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from csi.dashboard import DashboardState
from csi.room import load_room
from csi.server import DashboardServer

from csi.simulate import simulate_rounds

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TR8 = load_room(FIXTURES / "room_tr8.json")
T0 = 1_700_000_000.0


def run(room, seconds, **sim):
    state = DashboardState(room, mode="auto")
    for host_ts, data in simulate_rounds(room, duration_s=seconds, t0=T0, **sim):
        state.push_broadcast(host_ts, data)
    return state


def health(payload):
    return {node["name"]: node for node in payload["node_health"]}


def test_every_transceiver_has_health_and_a_healthy_rig_reads_coverage_ok():
    payload = run(TR8, 3.0).snapshot()
    nodes = health(payload)
    assert sorted(nodes) == [f"n{k}" for k in range(1, 9)]
    for node in nodes.values():
        assert node["state"] == "ok"
        assert node["heard_by"] == 7 and node["hears"] == 7 and node["peers"] == 7
        assert node["last_heard_s"] < 0.1
        assert node["link_rate_hz"] > 15                     # 20 Hz rounds, nothing lost
        assert node["slot_lateness"] == "unknown"            # the nodes do not report it yet (#14)
    assert payload["coverage"]["state"] == "ok"
    assert payload["coverage"]["missing_nodes"] == []


def test_a_node_that_stops_broadcasting_is_missing_and_coverage_reads_degraded():
    payload = run(TR8, 6.0, dead_after={"n3": 2.0}).snapshot()
    nodes = health(payload)
    assert nodes["n3"]["state"] == "missing"
    assert nodes["n3"]["last_heard_s"] > 3.5
    assert nodes["n3"]["heard_by"] == 0 and nodes["n3"]["hears"] == 0
    assert nodes["n1"]["state"] == "ok" and nodes["n1"]["heard_by"] == 6   # everyone but n3 still hears n1
    coverage = payload["coverage"]
    assert coverage["state"] == "degraded"
    assert coverage["missing_nodes"] == ["n3"]
    assert any("n3" in reason for reason in coverage["reasons"])


def test_aggregate_link_health_counts_silent_links_and_the_link_rate():
    payload = run(TR8, 6.0, dead_after={"n3": 2.0}).snapshot()
    agg = payload["link_health"]
    assert agg["links"] == 56
    assert agg["silent"] == 14                      # n3's 7 links each way
    assert agg["heard"] == 42
    assert 18 <= agg["rate_hz"]["median"] <= 21     # 20 Hz rounds
    assert agg["rate_hz"]["min"] <= agg["rate_hz"]["median"] <= agg["rate_hz"]["max"]


def test_the_link_matrix_gives_every_link_state_in_one_character_by_node_pair():
    payload = run(TR8, 12.0, dead_after={"n3": 2.0}).snapshot()   # past the 10 s warm-up of auto mode
    matrix = payload["link_matrix"]

    names = matrix["nodes"]
    assert names == [f"n{k}" for k in range(1, 9)]
    rows = matrix["states"]                         # row: receiving node, column: transmitting node
    assert len(rows) == 8 and all(len(row) == 8 for row in rows)
    n3 = names.index("n3")
    assert rows[n3] == "NN-NNNNN"                   # n3 sends no reports
    assert {row[n3] for k, row in enumerate(rows) if k != n3} == {"N"}   # and nobody hears it
    assert rows[0] == "-EN" + "E" * 5               # n1 hears everyone else, quiet room
    assert matrix["codes"]["E"] == "EMPTY" and matrix["codes"]["N"] == "NO_SIGNAL"


# ---------------------------------------------------------------- per-link detail on demand (HTTP)

def finished_server(room, seconds, **sim):
    stream = simulate_rounds(room, duration_s=seconds, t0=T0, **sim)
    server = DashboardServer(room, {"gateway": stream}, mode="auto", port=0)
    server.start()
    assert server.wait_finished(timeout=120)
    return server


def get(server, path):
    try:
        with urllib.request.urlopen(server.url + path, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_any_link_s_detail_is_served_on_demand():
    server = finished_server(TR8, 3.0, dead_after={"n3": 1.0})
    try:
        status, body = get(server, "/api/link?id=" + quote("n8<-n4"))
        assert status == 200
        link = body["link"]
        assert (link["id"], link["receiver"], link["sender"]) == ("n8<-n4", "n8", "n4")
        assert link["state"] == "WARMUP" and link["pkt_rate"] > 15 and link["rssi"] is not None
        assert {"score", "threshold", "level", "drops", "age_s", "baseline", "evidence", "mode"} <= set(link)
        status, body = get(server, "/api/link?id=" + quote("n3<-n1"))
        assert status == 200 and body["link"]["state"] == "NO_SIGNAL"
        assert body["time_s"] == pytest.approx(T0 + 3.0, abs=0.1)
    finally:
        server.stop()


def test_link_detail_refuses_an_unknown_or_missing_link_id():
    server = finished_server(TR8, 0.5)
    try:
        status, body = get(server, "/api/link?id=" + quote("n9<-n1"))
        assert status == 404 and "n9<-n1" in body["error"]
        status, body = get(server, "/api/link")
        assert status == 400 and "id" in body["error"]
    finally:
        server.stop()


# ---------------------------------------------------------------- frames at 30 nodes

TR30 = load_room(FIXTURES / "room_tr30.json")


def read_events(server):
    """Every payload of a finished run's /api/events, with the size of its JSON text."""
    events = []
    with urllib.request.urlopen(server.url + "/api/events", timeout=60) as response:
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line.startswith("data: "):
                events.append((json.loads(line[len("data: "):]), len(line)))
                if events[-1][0]["status"]["finished"]:
                    break
    return events


def test_frames_of_a_30_node_rig_carry_node_health_and_the_matrix_but_not_870_link_rows():
    server = finished_server(TR30, 1.5)
    try:
        events = read_events(server)
        status, state = get(server, "/api/state")
    finally:
        server.stop()
    assert len(events) >= 5
    for payload, size in events:
        assert "links" not in payload                        # per-link rows are fetched on demand
        assert len(payload["node_health"]) == 30
        assert len(payload["link_matrix"]["states"]) == 30
        assert payload["link_health"]["links"] == 870
        assert size < 40_000
    assert events[-1][0]["status"]["links_in_frames"] is False
    assert len(state["links"]) == 870                       # one GET of /api/state still has every row


def test_frames_of_an_8_node_rig_still_carry_every_link_row():
    server = finished_server(TR8, 0.6)
    try:
        events = read_events(server)
    finally:
        server.stop()
    assert all(len(payload["links"]) == 56 for payload, _ in events)
    assert events[-1][0]["status"]["links_in_frames"] is True


# ---------------------------------------------------------------- CPU budget at 30 nodes

PIPELINE_BUDGET = 1.0       # CPU seconds per second of traffic, steady state: the whole pipeline fits in one core
CAPTURE_BUDGET_S = 0.025    # per frame, under the server lock (heatmap and track advance, link rows)
FRAME_BUDGET_S = 0.040      # per frame, capture + render + JSON: at most 20 % of a core at 5 Hz
FRAME_BYTES_MAX = 40_000


@pytest.mark.slow
def test_a_30_node_rig_keeps_5_hz_frames_within_the_cpu_budget():
    """30 s of a simulated 30-node rig (870 links, 600 round broadcasts/s) with a walker after 15 s, through the
    same calls the server makes. The budget is measured over the last 10 s, once every link and heatmap baseline
    is warm. Laptop (Core Ultra 7): about 0.5 CPU-s/s, 6 ms and 16 KB per frame (twice that with the machine
    loaded). The rest of the pipeline's CPU is mostly the link detectors' statistics and the heatmap baselines."""

    from csi.server import default_walk

    seconds = 30.0
    stream = list(simulate_rounds(TR30, default_walk(TR30), duration_s=seconds, quiet_first_s=15.0, seed=3, t0=T0))
    state = DashboardState(TR30, mode="auto")
    steady_from = T0 + 20.0
    cpu = capture_s = frame_s = 0.0
    frames, sizes, next_frame = 0, [], T0 + 0.2
    for host_ts, data in stream:
        start = time.process_time()
        state.push_broadcast(host_ts, data)
        if host_ts >= next_frame:
            next_frame = host_ts + 0.2
            began = time.process_time()
            cap = state.capture(host_ts)
            captured = time.process_time()
            text = json.dumps(state.render(cap, links=False))
            done = time.process_time()
            if host_ts >= steady_from:
                frames += 1
                capture_s += captured - began
                frame_s += done - began
                sizes.append(len(text))
        if host_ts >= steady_from:
            cpu += time.process_time() - start
    steady_s = stream[-1][0] - steady_from
    assert 49 <= frames <= 51                                  # 5 Hz of packet time
    assert cpu / steady_s < PIPELINE_BUDGET
    assert capture_s / frames < CAPTURE_BUDGET_S
    assert frame_s / frames < FRAME_BUDGET_S
    assert max(sizes) < FRAME_BYTES_MAX


# ---------------------------------------------------------------- the page at 30 nodes (run under node)

PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"
MATRIX_BEGIN = "// ---- matrix: pure (tested with node) begin"
MATRIX_END = "// ---- matrix: pure (tested with node) end"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page script")

PAGE_HARNESS = r"""
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const html = input.html;
const script = html.split("<script>")[1].split("</script>")[0];
const pure = html.slice(html.indexOf(input.matrixBegin), html.indexOf(input.matrixEnd));
const created = {};
const elements = {};
const fetches = [];
function ctx() {
  return new Proxy({}, { get(t, k) {
    if (k in t) return t[k];
    if (k === "measureText") return (s) => ({ width: String(s).length * 7 });
    if (k === "createImageData") return (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) });
    return () => undefined;
  }, set(t, k, v) { t[k] = v; return true; } });
}
function element(id, tag) {
  const t = { id, tag, children: [], listeners: {}, clientWidth: 600, clientHeight: 40, width: 0, height: 0,
    firstChild: null, style: {}, hidden: false, className: "", textContent: "", disabled: false, value: "",
    classList: { toggle() {} } };
  t.appendChild = (c) => { t.children.push(c); t.firstChild = t.children[0]; return c; };
  t.removeChild = (c) => { t.children.splice(t.children.indexOf(c), 1); t.firstChild = t.children[0] || null; return c; };
  t.addEventListener = (k, f) => { t.listeners[k] = f; };
  t.setAttribute = (k, v) => { t["attr:" + k] = v; };
  t.getAttribute = (k) => t["attr:" + k];
  const c = ctx();
  t.getContext = () => c;
  return new Proxy(t, { get(o, k) { return k in o ? o[k] : () => undefined; }, set(o, k, v) { o[k] = v; return true; } });
}
const document = {
  getElementById(id) { return (elements[id] = elements[id] || element(id, "div")); },
  createElement(tag) { created[tag] = (created[tag] || 0) + 1; return element(null, tag); },
  createTextNode(text) { return { textContent: text }; },
  activeElement: null,
};
const window = { devicePixelRatio: 1, addEventListener() {}, requestAnimationFrame() {} };
let source = null;
function FakeEventSource(url) { source = this; this.close = () => {}; }
function fetch(url) {
  fetches.push(url);
  const body = url.startsWith("/api/link") ? input.detail : input.state;
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}
function text(node) {
  return (node.textContent || "") + node.children.map(text).join(" ");
}
const errors = [];
const tick = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
(async () => {
  try {
    new Function("window", "document", "fetch", "EventSource", "setTimeout", "clearTimeout", script)(
      window, document, fetch, FakeEventSource, () => 0, () => 0);
    await tick();
    source.onopen && source.onopen();
    source.onmessage({ data: JSON.stringify(input.frame) });
    await tick();
    const before = Object.assign({}, created);
    source.onmessage({ data: JSON.stringify(input.frame) });
    await tick();
    const perFrame = {};
    Object.keys(created).forEach((k) => { if (created[k] !== before[k]) perFrame[k] = created[k] - (before[k] || 0); });
    const centre = new Function(pure + "; return matrixCellCentre(matrixLayout(" + input.nodes + ", 600), " +
      input.cell[0] + ", " + input.cell[1] + ");")();
    elements["matrix"].listeners.click({ offsetX: centre.x, offsetY: centre.y });
    await tick();
    source.onmessage({ data: JSON.stringify(input.frame) });
    await tick();
    console.log(JSON.stringify({
      errors, perFrame, fetches,
      coverageHidden: elements["coverage"].hidden, coverage: text(elements["coverage-reasons"]),
      nodeRows: elements["node-rows"].children.length, nodes: text(elements["node-rows"]),
      linksHidden: elements["links-card"].hidden, linkCards: elements["links"].children.length,
      detail: text(elements["link-detail"]),
    }));
  } catch (err) { console.log(JSON.stringify({ errors: [String(err && err.stack || err)] })); }
})();
"""


def run_page(tmp_path, *, state, frame, detail, nodes, cell):
    script = tmp_path / "page_harness.js"
    script.write_text(PAGE_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60, encoding="utf-8",
                          input=json.dumps({"html": PAGE.read_text(encoding="utf-8"), "state": state, "frame": frame,
                                            "detail": detail, "nodes": nodes, "cell": cell,
                                            "matrixBegin": MATRIX_BEGIN, "matrixEnd": MATRIX_END}))
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["errors"] == []
    return result


@needs_node
def test_the_page_at_30_nodes_shows_node_health_and_coverage_degraded_and_fetches_link_detail(tmp_path):
    server = finished_server(TR30, 3.0, dead_after={"n05": 0.5})
    try:
        frame = read_events(server)[-1][0]
        _, state = get(server, "/api/state")
        _, detail = get(server, "/api/link?id=" + quote("n02<-n07"))
    finally:
        server.stop()
    assert "links" not in frame
    for payload in (frame, state):        # stand-ins for a live run: a finished one stops the page's event stream
        payload["status"]["finished"] = False
    result = run_page(
tmp_path, state=state, frame=frame, detail=detail, nodes=30, cell=[1, 6])
    assert result["coverageHidden"] is False                              # COVERAGE DEGRADED is up

    assert "missing" in result["coverage"] and "n05" in result["coverage"]       # the banner says why
    assert result["nodeRows"] == 30 and "missing" in result["nodes"] and "unknown" in result["nodes"]
    assert result["linksHidden"] is True and result["linkCards"] == 0     # no link cards at all
    assert "article" not in result["perFrame"]                            # a frame builds no cards
    assert sum(result["perFrame"].values()) < 30                          # and hardly any DOM at all
    assert "/api/link?id=" + quote("n02<-n07", safe="") in result["fetches"]
    assert "n02<-n07" in result["detail"] and "n02 from n07" in result["detail"]


@needs_node
def test_matrix_cells_map_back_to_their_node_pair(tmp_path):
    html = PAGE.read_text(encoding="utf-8")
    pure = html[html.index(MATRIX_BEGIN):html.index(MATRIX_END)]
    script = tmp_path / "matrix.js"
    script.write_text(pure + """
var out = [];
var layout = matrixLayout(30, 600);
[[0, 1], [1, 6], [29, 28], [12, 12]].forEach(function (rt) {
  var c = matrixCellCentre(layout, rt[0], rt[1]);
  out.push(matrixCellAt(c.x, c.y, layout));
});
out.push(matrixCellAt(-5, 3, layout), matrixCellAt(layout.left + layout.cell * 30 + 2, layout.top + 1, layout));
console.log(JSON.stringify(out));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    cells = json.loads(proc.stdout)
    assert cells[:4] == [{"r": 0, "t": 1}, {"r": 1, "t": 6}, {"r": 29, "t": 28}, {"r": 12, "t": 12}]
    assert cells[4:] == [None, None]


def test_the_page_has_a_coverage_degraded_banner_and_fetches_link_detail_on_demand():
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="coverage"' in html and "COVERAGE DEGRADED" in html
    assert '"/api/link?id="' in html
    assert "Slot lateness" in html
