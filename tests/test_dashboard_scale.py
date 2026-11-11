"""The dashboard at transceiver scale: per-node health, coverage, the link matrix, per-link detail on demand.

Round broadcasts come from the simulated rig (csi.simulate.simulate_rounds) for the frozen 8-node and 30-node
transceiver rooms; assertions are on DashboardState's payload and on the server's HTTP interface.
"""
from pathlib import Path

from csi.dashboard import DashboardState
from csi.room import load_room
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
