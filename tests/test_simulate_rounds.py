"""The simulated transceiver rig: round broadcasts for any number of nodes, driven through the pipeline.

The simulator's stream is (host time, round broadcast bytes), the transceiver input seam. Assertions are on the
decoded bytes (what a gateway would hand the host) and on the DashboardState payload they produce.
"""
import itertools
import json
from pathlib import Path

import numpy as np
import pytest

from csi.broadcast import decode_broadcast
from csi.dashboard import DashboardState
from csi.room import FALLOFF_SIGMA_M, Stand, Walk, load_room, point_segment_distance
from csi.simulate import simulate_rounds

ROOM = load_room(Path(__file__).parent / "fixtures" / "room_tr8.json")
NODES = {n.name: n for n in ROOM.transceivers}
T0 = 1_700_000_000.0
QUIET_S = 15.0   # an empty room first: each link learns its adaptive baseline before anyone walks


def payloads_at(stream, times, state=None):
    """Feed ``stream`` into a fresh auto-mode DashboardState; the payload at each host time in ``times``."""
    state = state or DashboardState(ROOM, mode="auto")
    out, pending = [], list(times)
    for host_ts, data in stream:
        while pending and host_ts > pending[0]:
            out.append(state.snapshot(pending.pop(0)))
        if not pending:
            break
        state.push_broadcast(host_ts, data)
    out.extend(state.snapshot(t) for t in pending)
    return out


def distance_to_link(label, point):
    rx, tx = label.split("<-")
    a, b = NODES[rx], NODES[tx]
    return float(point_segment_distance(np.array([point]), (a.x, a.y), (b.x, b.y))[0])


def test_each_node_broadcasts_once_per_round_in_slot_order_with_its_serial():
    stream = list(itertools.islice(simulate_rounds(ROOM, round_hz=20.0, t0=T0), 8 * 3))
    times = [t for t, _ in stream]
    assert times == sorted(times) and times[0] >= T0 and times[-1] < T0 + 3 / 20.0
    decoded = [decode_broadcast(data) for _, data in stream]
    assert [(b.round, b.slot) for b in decoded] == [(r, s) for r in range(3) for s in range(8)]
    assert [b.node_id for b in decoded[:8]] == [n.serial for n in ROOM.transceivers]
    assert all(b.membership == 0xFF and len(b.reports) == 7 for b in decoded)


def test_the_simulator_runs_any_number_of_nodes(tmp_path):
    nodes = {f"t{k:02d}": {"role": "transceiver", "x": float(k % 6), "y": float(k // 6),
                           "serial": f"02:00:00:00:00:{k:02X}"} for k in range(30)}
    path = tmp_path / "room30.json"
    path.write_text(json.dumps({"room": {"width_m": 6.0, "depth_m": 5.0}, "nodes": nodes}))
    room = load_room(path)
    first_round = [decode_broadcast(data) for _, data in itertools.islice(simulate_rounds(room, t0=T0), 30)]
    assert len({b.node_id for b in first_round}) == 30 and all(len(b.reports) == 29 for b in first_round)
    assert len(first_round[-1].reports) * 4 + 26 == 142


def test_every_link_warms_up_then_reads_empty_in_an_empty_room():
    early, late = payloads_at(simulate_rounds(ROOM, duration_s=14.0, seed=3, t0=T0), [T0 + 0.5, T0 + 13.5])
    assert len(early["links"]) == 56
    assert {row["state"] for row in early["links"]} == {"WARMUP"}
    states = [row["state"] for row in late["links"]]
    assert states.count("EMPTY") >= 54 and "NO_SIGNAL" not in states and "WARMUP" not in states


@pytest.mark.parametrize("seed", [0, 1])
def test_link_states_follow_a_walker(seed):
    walk = Walk(((0.6, 0.6), (3.4, 2.4), (3.4, 0.6), (0.6, 2.4)), speed_mps=0.5, loop=True)
    checks = [QUIET_S + s for s in (2.0, 5.0, 8.0, 11.0)]
    stream = simulate_rounds(ROOM, walk, duration_s=QUIET_S + 12.0, quiet_first_s=QUIET_S, seed=seed, t0=T0)
    payloads = payloads_at(stream, [T0 + t for t in checks])
    for t, payload in zip(checks, payloads):
        where = tuple(walk.position(np.array([t - QUIET_S]))[0])
        near = [row["state"] for row in payload["links"] if distance_to_link(row["id"], where) < 0.5 * FALLOFF_SIGMA_M]
        far = [row["state"] for row in payload["links"] if distance_to_link(row["id"], where) > 4 * FALLOFF_SIGMA_M]
        assert near and far
        assert near.count("MOTION") >= 0.8 * len(near), (t, where, near)
        assert far.count("MOTION") <= 0.1 * len(far), (t, where, far)
        assert payload["status"]["room"]["state"] == "MOTION"


def test_a_dead_node_turns_its_links_no_signal_both_ways():
    stream = simulate_rounds(ROOM, duration_s=16.0, dead_after={"n3": 12.0}, seed=2, t0=T0)
    before, after = payloads_at(stream, [T0 + 11.5, T0 + 15.5])
    rows_before = {row["id"]: row["state"] for row in before["links"]}
    rows_after = {row["id"]: row["state"] for row in after["links"]}
    touching = {label for label in rows_after if "n3" in label.split("<-")}
    assert len(touching) == 14 and "NO_SIGNAL" not in {rows_before[label] for label in touching}
    assert {rows_after[label] for label in touching} == {"NO_SIGNAL"}   # heard counters (n3 is the transmitter) and silence
    assert "NO_SIGNAL" not in {state for label, state in rows_after.items() if label not in touching}


def test_lossy_links_report_a_lower_rate():
    stream = simulate_rounds(ROOM, duration_s=6.0, drop_prob=0.5, seed=4, t0=T0)
    payload, = payloads_at(stream, [T0 + 5.9])
    rates = [row["pkt_rate"] for row in payload["links"]]
    assert np.median(rates) == pytest.approx(10.0, abs=2.5)   # about half the 20 Hz round rate
    assert min(row["drops"] for row in payload["links"]) > 0


def test_the_same_seed_gives_the_same_bytes():
    a = list(itertools.islice(simulate_rounds(ROOM, seed=7, t0=T0), 200))
    b = list(itertools.islice(simulate_rounds(ROOM, seed=7, t0=T0), 200))
    c = list(itertools.islice(simulate_rounds(ROOM, seed=8, t0=T0), 200))
    assert a == b and a != c


def test_two_people_each_raise_the_links_near_them():
    a, b = Stand(1.0, 0.75), Stand(3.0, 2.25)
    stream = simulate_rounds(ROOM, (a, b), duration_s=QUIET_S + 3.0, quiet_first_s=QUIET_S, seed=5, t0=T0)
    payload, = payloads_at(stream, [T0 + QUIET_S + 2.5])
    for person, other in ((a, b), (b, a)):
        near = [row["state"] for row in payload["links"]
                if distance_to_link(row["id"], (person.x, person.y)) < 0.5 * FALLOFF_SIGMA_M
                and distance_to_link(row["id"], (other.x, other.y)) > 4 * FALLOFF_SIGMA_M]
        assert near and near.count("MOTION") >= 0.8 * len(near), (person, near)
