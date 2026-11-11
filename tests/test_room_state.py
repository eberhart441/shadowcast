"""The room state of the transceiver pipeline: zones, and agreement among links that do not share a node.

Simulated round broadcasts (csi/simulate.py) go through the host ingest (RoundIngest) into the LinkMonitor that
DashboardState uses, and the assertions are on its link states and room state. The heatmap is left out: at 870
links it costs more than the rest of the pipeline (ticket #10 replaces it). The payload fields (``room.zones``,
``status.room.zones``) are tested in tests/test_round_pipeline.py. The last test drives RoomState directly, to
state a false-alarm rate over hours. Rooms: the frozen 8-node and 30-node fixtures (ADR-0017).
"""
from pathlib import Path

import numpy as np
import pytest

from csi.adaptive import Reading
from csi.links import LinkMonitor, NodeLinkId, room_link_ids
from csi.room_state import RoomState, ZoneMap
from csi.rounds import RoundIngest
from csi.room import load_room
from csi.simulate import simulate_rounds

FIXTURES = Path(__file__).parent / "fixtures"
TR8 = load_room(FIXTURES / "room_tr8.json")
TR30 = load_room(FIXTURES / "room_tr30.json")
T0 = 1_700_000_000.0
WARM_S = 12.0   # every link's adaptive baseline is warm (10 s and 20 samples) after this


class Frame:
    """What the pipeline shows at one host time: the room state, each zone's state, how many links are elevated and
    (with ``links``) each link's row."""

    def __init__(self, t, monitor, links):
        self.t = t
        self.room = monitor.room_state
        self.zones = monitor.room_zones.zone_states
        self.elevated = len(monitor.room_elevated)
        self.links = monitor.snapshot(T0 + t) if links else None


def run(room, stream, times, links=False):
    """Feed ``stream`` into the host pipeline of ``room``; a Frame at each time (seconds since T0) in ``times``."""
    ingest = RoundIngest(room)
    monitor = LinkMonitor(expected=[link_id for link_id, _, _ in room_link_ids(room)], room=room)
    out, pending = [], sorted(times)
    for host_ts, data in stream:
        while pending and host_ts > T0 + pending[0]:
            out.append(Frame(pending.pop(0), monitor, links))
        monitor.push_reports(host_ts, ingest.place(data))
    return out


def steps(start_s, stop_s, every_s=0.05):
    return [start_s + k * every_s for k in range(int(round((stop_s - start_s) / every_s)))]


def touching(frame, node):
    return [row for row in frame.links if node in (row["receiver"], row["transmitter"])]


def test_one_noisy_node_raising_all_its_links_does_not_raise_the_room_state():
    stream = simulate_rounds(TR30, duration_s=WARM_S + 5.0, noisy={"n07": (WARM_S, WARM_S + 5.0)}, seed=1, t0=T0)
    frames = run(TR30, stream, steps(WARM_S + 1.0, WARM_S + 5.0, every_s=0.25), links=True)
    assert len(frames) == 16
    for frame in frames:
        own = touching(frame, "n07")
        assert len(own) == 58
        assert sum(row["state"] == "MOTION" for row in own) >= 50          # the node really is noisy ...
        others = [row for row in frame.links if row not in own]
        assert sum(row["state"] == "MOTION" for row in others) <= 2
        assert frame.room == "EMPTY"                                        # ... and the room is not raised by it


class Visits:
    """A person who appears at each point in turn, walks ``walk_s`` from it towards the room centre, then is gone for
    ``gap_s``; away, the person is far outside the room, so no link responds. Time 0 is the first arrival."""

    def __init__(self, room, points, *, walk_s=1.5, gap_s=3.0, speed_mps=0.4):
        self.points, self.walk_s, self.period, self.speed = points, walk_s, walk_s + gap_s, speed_mps
        self.centre = np.array([room.width_m / 2, room.depth_m / 2])

    def arrivals(self):
        return [k * self.period for k in range(len(self.points))]

    def position(self, t):
        out = np.full((len(t), 2), -100.0)
        for i, ti in enumerate(np.asarray(t, dtype=float)):
            k, into = int(ti // self.period), ti % self.period
            if 0 <= k < len(self.points) and into < self.walk_s:
                start = np.array(self.points[k])
                heading = (self.centre - start) / max(float(np.linalg.norm(self.centre - start)), 1e-9)
                out[i] = start + heading * self.speed * into
        return out


def grid(room, inset=0.3, nx=5, ny=3):
    """Points spread over the whole room, the outer ones ``inset`` from the walls (corners included)."""
    return [(inset + i * (room.width_m - 2 * inset) / (nx - 1), inset + j * (room.depth_m - 2 * inset) / (ny - 1))
            for j in range(ny) for i in range(nx)]


def onsets(room, points, seed):
    """Per point: (seconds from the person's arrival to the first room MOTION, room states in the 0.3 s before). Before
    an arrival the room is EMPTY, or POSSIBLY_OCCUPIED while it still holds the zones of the previous visit."""
    person = Visits(room, points)
    stream = simulate_rounds(room, person, duration_s=WARM_S + len(points) * person.period, quiet_first_s=WARM_S,
                             seed=seed, t0=T0)
    times = [t for arrive in person.arrivals() for t in steps(WARM_S + arrive - 0.3, WARM_S + arrive + person.walk_s)]
    frames = run(room, stream, times)
    out = []
    for arrive in person.arrivals():
        at = WARM_S + arrive
        before = {f.room for f in frames if at - 0.3 <= f.t < at}
        motion = [f.t - at for f in frames if at <= f.t <= at + person.walk_s and f.room == "MOTION"]
        out.append((motion[0] if motion else None, before))
    return out


def test_a_walker_anywhere_in_the_8_node_room_raises_motion_within_one_second_except_at_a_corner_node():
    """15 points over the room, 0.3 m from the walls at the edge. At the four corner points the person is within
    0.5 m of a corner node and no other node's link passes within 0.75 m: only that node's links see them, which is
    what a fault of that node looks like, so MOTION waits until they walk into another node's link (ADR-0029)."""
    points = grid(TR8)
    corners = {(0.3, 0.3), (3.7, 0.3), (0.3, 2.7), (3.7, 2.7)}
    for point, (delay, before) in zip(points, onsets(TR8, points, seed=2)):
        assert before and before <= {"EMPTY", "POSSIBLY_OCCUPIED"}, point
        limit = 1.25 if tuple(round(c, 2) for c in point) in corners else 1.0
        assert delay is not None and delay <= limit, (point, delay)


@pytest.mark.slow
def test_a_walker_anywhere_in_the_30_node_room_raises_motion_within_one_second():
    """24 points over the 8 x 6 m room (nodes 0.93 m apart round the walls), the outer ones 0.3 m from the walls,
    corners included: every one within 1 s (measured 0.7 s at all 24)."""
    points = grid(TR30, nx=6, ny=4)
    for point, (delay, before) in zip(points, onsets(TR30, points, seed=3)):
        assert before and before <= {"EMPTY", "POSSIBLY_OCCUPIED"}, point
        assert delay is not None and delay <= 1.0, (point, delay)


@pytest.mark.slow
def test_a_30_node_empty_room_with_glitching_links_stays_empty():
    """False-alarm rate: 0 false MOTION in 180 s of an empty 30-node room (870 links, 20 rounds/s), with every link
    glitching on its own about once every 2 hours (a 1.5 s score burst, x6: about 80 glitches in the run). Over the
    same run the hackathon rule (two links elevated at once) would read MOTION; see the last assertion."""
    seconds = 180.0
    stream = simulate_rounds(TR30, duration_s=WARM_S + seconds, glitch_rate_hz=1 / 7200, seed=0, t0=T0)
    frames = run(TR30, stream, steps(WARM_S, WARM_S + seconds, every_s=0.1))
    assert len(frames) == 1800
    assert {f.room for f in frames} == {"EMPTY"}
    assert sum(f.elevated >= 2 for f in frames) > 0       # the noise is real: two links were elevated together


def test_chance_agreement_among_870_independently_glitching_links_is_rare():
    """The false-alarm rate at the room state's own input, over hours instead of minutes.

    Each of the 870 links of the 30-node room glitches on its own about once an hour and is then elevated for 0.4 s
    (a 1 s score burst less the detector's 0.6 s sustain): about 3400 glitches in 4 simulated hours. The hackathon
    rule (any two links elevated at once) reads MOTION about 75 times an hour on this; the room state about 1.5 times
    an hour (8 in this run; seeds 1 to 3 give 4, 5 and 6). Stated bound: more than 50 an hour against less than 3."""
    hours, per_link_hz, elevated_s = 4.0, 1 / 3600, 0.4
    rng = np.random.default_rng(11)
    links = [(NodeLinkId(rx.name, tx.name).label, rx, tx) for rx, tx in TR30.transceiver_links()]
    n = int(rng.poisson(per_link_hz * hours * 3600 * len(links)))
    starts, which = rng.uniform(0, hours * 3600, n), rng.integers(0, len(links), n)
    events = sorted([(t, int(k), +1) for t, k in zip(starts, which)]
                    + [(t + elevated_s, int(k), -1) for t, k in zip(starts, which)])
    room_state = RoomState(ZoneMap(TR30, links), no_signal_s=float("inf"))   # every link stays heard
    quiet, high = Reading(0.0, "EMPTY", 1.0, warm=True), Reading(0.0, "MOTION", 3.0, elevated=True, warm=True)
    for label, _, _ in links:
        room_state.observe(label, 0.0, quiet)
    assert room_state.update(0.0) == "EMPTY"
    glitching, elevated = [0] * len(links), 0
    new_alarms = old_alarms = 0
    was_new = was_old = False
    for t, k, step in events:
        elevated += (glitching[k] == 0) if step > 0 else -(glitching[k] == 1)
        glitching[k] += step
        room_state.observe(links[k][0], t, high if glitching[k] else quiet)
        new = room_state.update(t) == "MOTION"
        new_alarms += new and not was_new
        old_alarms += elevated >= 2 and not was_old
        was_new, was_old = new, elevated >= 2
    assert n > 3000
    assert old_alarms / hours > 50
    assert new_alarms / hours < 3.0


# ---------------------------------------------------------------- possibly occupied (#9)

QUIET = Reading(0.0, "EMPTY", 1.0, warm=True)
HIGH = Reading(0.0, "MOTION", 3.0, elevated=True, warm=True)


def held_room(room, **kwargs):
    """A RoomState of ``room`` with every link heard and quiet at t = 0, and the labels of its links."""
    links = [(NodeLinkId(rx.name, tx.name).label, rx, tx) for rx, tx in room.transceiver_links()]
    state = RoomState(ZoneMap(room, links), no_signal_s=float("inf"), **kwargs)
    for label, _, _ in links:
        state.observe(label, 0.0, QUIET)
    state.update(0.0)
    return state, links


def covering(links, point, within=0.3):
    """Labels of the links passing within ``within`` of ``point``: the links a person there would raise."""
    from csi.room import point_segment_distance
    return [label for label, rx, tx in links
            if point_segment_distance(np.array([point]), (rx.x, rx.y), (tx.x, tx.y))[0] <= within]


def drive(state, labels, reading, start_s, stop_s, every_s=0.05):
    """Observe ``reading`` on ``labels`` and update, every ``every_s`` from ``start_s`` up to ``stop_s``;
    {time: (room state, zone states)} at each update."""
    out = {}
    for t in steps(start_s, stop_s, every_s):
        for label in labels:
            state.observe(label, t, reading)
        out[round(t, 2)] = (state.update(t), state.zone_states)
    return out


def test_a_zone_that_stops_moving_reads_possibly_occupied_for_the_hold_then_empty():
    """Motion in B2 (the 8-node room's zone around (1.5, 1.5)) for 2 s, then nothing: MOTION for the 1 s hold, then
    POSSIBLY_OCCUPIED for the 60 s possibly-occupied hold, then EMPTY. Zones never in MOTION stay EMPTY."""
    state, links = held_room(TR8)
    person = covering(links, (1.5, 1.5))
    moving = drive(state, person, HIGH, 1.0, 3.0)
    assert moving[2.0][0] == "MOTION" and moving[2.0][1]["B2"] == "MOTION"
    seen = {zone for _, zones in moving.values() for zone, s in zones.items() if s == "MOTION"}
    after = drive(state, person, QUIET, 3.0, 66.0, every_s=0.1)
    assert after[3.5][0] == "MOTION"                                   # the 1 s motion hold
    for t in (4.5, 30.0, 63.5):
        room, zones = after[t]
        assert room == "POSSIBLY_OCCUPIED", t
        assert {z for z, s in zones.items() if s == "POSSIBLY_OCCUPIED"} == seen, t
        assert {s for z, s in zones.items() if z not in seen} == {"EMPTY"}, t
    assert after[64.5][0] == "EMPTY"
    assert set(after[64.5][1].values()) == {"EMPTY"}


def test_renewed_motion_during_the_hold_returns_the_zone_to_motion_and_restarts_the_hold():
    state, links = held_room(TR8)
    person = covering(links, (1.5, 1.5))
    drive(state, person, HIGH, 1.0, 3.0)
    held = drive(state, person, QUIET, 3.0, 20.0, every_s=0.1)
    assert held[19.9][1]["B2"] == "POSSIBLY_OCCUPIED"
    again = drive(state, person, HIGH, 20.0, 22.0)
    assert again[21.0][0] == "MOTION" and again[21.0][1]["B2"] == "MOTION"
    after = drive(state, person, QUIET, 22.0, 84.0, every_s=0.1)
    assert after[82.5][1]["B2"] == "POSSIBLY_OCCUPIED"     # 60 s from the renewed motion, not from the first
    assert after[83.5][1]["B2"] == "EMPTY" and after[83.5][0] == "EMPTY"


def test_the_possibly_occupied_hold_is_a_setting_and_zero_turns_it_off():
    for hold_s, held_at, empty_at in ((5.0, 8.5, 9.5), (0.0, None, 4.5)):
        state, links = held_room(TR8, possibly_occupied_s=hold_s)
        person = covering(links, (1.5, 1.5))
        drive(state, person, HIGH, 1.0, 3.0)
        after = drive(state, person, QUIET, 3.0, 12.0, every_s=0.1)
        if held_at is not None:
            assert after[held_at][0] == "POSSIBLY_OCCUPIED", hold_s
        assert after[empty_at][0] == "EMPTY", hold_s
        assert "POSSIBLY_OCCUPIED" not in after[empty_at][1].values(), hold_s
    with pytest.raises(ValueError):
        held_room(TR8, possibly_occupied_s=-1.0)
