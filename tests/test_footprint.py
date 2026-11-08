"""The room footprint: an outline polygon (room.json "footprint", csi/room.py) that keeps the heat and the track inside
the real room. Rooms are not always rectangles, and the boards usually sit outside the walls.

The L-shaped room is the frozen four-link rig (tests/conftest.py), or the six-link fixture, with its bottom-right
quarter cut away: the notch is x > 1.385, y > 0.92, which holds 126 of the 28 x 18 cell centres (none on its edges).
rx1 (2.77, 0) to tx0 (2.77, 1.84) runs down the right wall, so the lower half of that link lies beside the notch. The
tracker tests use the tracker tests' own 6 x 4 m section with the same cut (notch x > 3, y > 2).
"""
import copy
import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from csi.dashboard import DashboardState
from csi.heatmap import Heatmap, cell_centres
from csi.room import inside_mask, load_room, nearest_point_in_polygon, point_in_polygon, rectangle
from csi.rti import RTIProjector, exponential_prior
from csi.tracker import COAST_S, Tracker
from tests.test_dashboard_page import PAGE, needs_node, render_payloads_with_stub
from tests.test_tracker import T0, frames, person, quiet

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOM = load_room()                                   # four links (the frozen rig)
ROOM5 = load_room(FIXTURES / "room_5node.json")      # six links: RTI under --estimator auto
W_M, D_M = 2.77, 1.84
L_FOOTPRINT = ((0.0, 0.0), (2.77, 0.0), (2.77, 0.92), (1.385, 0.92), (1.385, 1.84), (0.0, 1.84))
L_ROOM = dataclasses.replace(ROOM, footprint=L_FOOTPRINT)
L_ROOM5 = dataclasses.replace(ROOM5, footprint=L_FOOTPRINT)
L_SECTION = ((0.0, 0.0), (6.0, 0.0), (6.0, 2.0), (3.0, 2.0), (3.0, 4.0), (0.0, 4.0))   # the tracker's 6 x 4 m
RIGHT = "rx1<-00"                                    # the right wall: its lower half lies beside the notch
QUIET = 44                                           # 11 s of 0.25 s steps: every baseline is warm


def notch(cols=28, rows=18):
    """Per cell (flat row-major), True when its centre is in the notch of the L-shaped room."""
    centres = cell_centres(cols, rows, W_M, D_M)
    return (centres[:, 0] > 1.385) & (centres[:, 1] > 0.92)


def row(label, score, state="EMPTY"):
    return {"id": label, "state": state, "score": score, "threshold": 10.0, "age_s": 0.0}


def replay(heat, series, step=0.25):
    """Feed every link's score series (one row each per step) at host times 0, step, 2 step, ..."""
    for k in range(min(len(s) for s in series.values())):
        heat.advance(step * k, [row(label, s[k]) for label, s in series.items()])


def write_room(tmp_path, **extra):
    """The frozen rig's room config plus ``extra`` top-level keys, written to a file; returns its path."""
    config = json.loads((FIXTURES / "room_rig4.json").read_text())
    config.update(extra)
    path = tmp_path / "room.json"
    path.write_text(json.dumps(config))
    return path


# ---------------------------------------------------------------- loading the footprint (csi/room.py)


def test_a_room_without_a_footprint_is_the_room_rectangle():
    assert ROOM.footprint == ()
    assert ROOM.outline == rectangle(2.77, 1.84) == ((0.0, 0.0), (2.77, 0.0), (2.77, 1.84), (0.0, 1.84))
    assert ROOM.inside(0.1, 0.1) and ROOM.inside(2.77, 1.84) and not ROOM.inside(2.9, 1.0)


def test_load_room_reads_the_footprint_and_still_ignores_keys_it_does_not_know(tmp_path):
    room = load_room(write_room(tmp_path, footprint=[list(p) for p in L_FOOTPRINT], zones={"hall": [0, 0, 1, 1]},
                                walls=[{"a": [0, 0], "b": [1, 0], "db": 4}], send_hz=20, notes={"any": "thing"}))
    assert room.footprint == L_FOOTPRINT and room.outline == L_FOOTPRINT
    assert room.inside(0.5, 1.5) and room.inside(2.5, 0.5)
    assert not room.inside(2.0, 1.5)                                       # the notch
    assert {n.name for n in room.receivers + room.senders} == {"rx1", "rx2", "tx0", "tx1"}


def test_a_closed_ring_is_accepted_and_a_footprint_that_is_the_room_rectangle_is_the_default(tmp_path):
    ring = [list(p) for p in L_FOOTPRINT] + [[0.0, 0.0]]                  # the first point repeated at the end
    assert load_room(write_room(tmp_path, footprint=ring)).footprint == L_FOOTPRINT
    rect = load_room(write_room(tmp_path, footprint=[[0, 1.84], [0, 0], [2.77, 0], [2.77, 1.84]]))
    assert rect.footprint == () and rect.outline == ROOM.outline
    assert dataclasses.replace(ROOM, footprint=rectangle(2.77, 1.84)).footprint == ()


@pytest.mark.parametrize("bad", [[[0, 0], [1, 1]], [[0, 0], [1, 0], [2, 0]], [[0, 0], [1, "a"], [1, 1]], "L", [],
                                 [[0, 0, 0], [1, 0], [1, 1]], [[0, 0], [1, 0], [True, 1]]])
def test_a_footprint_that_is_not_a_polygon_with_an_area_is_refused(tmp_path, bad):
    with pytest.raises(ValueError, match="footprint"):
        load_room(write_room(tmp_path, footprint=bad))


def test_a_footprint_point_that_is_not_finite_is_refused():
    with pytest.raises(ValueError, match="footprint"):
        dataclasses.replace(ROOM, footprint=((0.0, 0.0), (1.0, 0.0), (float("nan"), 1.0)))


def test_point_in_polygon_and_the_nearest_point_inside():
    assert point_in_polygon(0.5, 1.5, L_FOOTPRINT) and not point_in_polygon(2.0, 1.5, L_FOOTPRINT)
    assert point_in_polygon(1.385, 1.2, L_FOOTPRINT)                      # on an edge counts as inside
    assert point_in_polygon(0.5, 1.5, L_FOOTPRINT[::-1]) and not point_in_polygon(2.0, 1.5, L_FOOTPRINT[::-1])
    assert nearest_point_in_polygon(0.5, 0.5, L_FOOTPRINT) == (0.5, 0.5)
    assert nearest_point_in_polygon(2.0, 1.5, L_FOOTPRINT) == pytest.approx((2.0, 0.92))    # nearer the notch top
    assert nearest_point_in_polygon(1.5, 1.7, L_FOOTPRINT) == pytest.approx((1.385, 1.7))   # nearer its side
    assert nearest_point_in_polygon(3.5, -1.0, L_FOOTPRINT) == pytest.approx((2.77, 0.0))   # beyond a corner
    mask = inside_mask(cell_centres(28, 18, W_M, D_M), L_FOOTPRINT)
    assert mask.dtype == bool and np.array_equal(mask, ~notch()) and mask.sum() == 378


# ---------------------------------------------------------------- the heatmap (csi/heatmap.py, csi/rti.py)


def test_the_back_projection_puts_no_heat_in_the_notch_of_an_l_shaped_room():
    series = {RIGHT: [1.0] * QUIET + [4.0] * 8}        # 11 s quiet, then 2 s at 4x quiet: full evidence
    rect, shaped = Heatmap(ROOM), Heatmap(L_ROOM)
    replay(rect, series)
    replay(shaped, series)
    assert rect.estimator_used == shaped.estimator_used == "backprojection"
    cut = notch()
    assert rect.values[cut].max() > 0.3                # a rectangle room paints the whole right wall (about 0.41)
    assert np.all(shaped.values[cut] == 0.0)           # the L-shaped room nothing in its notch
    assert shaped.values[~cut].max() > 0.3             # but the upper half of the wall, inside, still lights up
    assert np.array_equal(shaped.room_mask, ~cut) and rect.room_mask.all()


def test_rti_unknowns_are_only_the_cells_inside_the_footprint():
    proj = Heatmap(L_ROOM5, estimator="rti").projector()
    cut = notch()
    assert np.array_equal(proj.unknowns, np.flatnonzero(~cut))
    assert np.all(proj.weights[:, cut] == 0.0)
    labels = sorted(proj.labels)
    assert all(proj.informative(label) for label in labels)   # every link still reaches cells inside
    pi = proj.projection(labels, [1.0] * len(labels))
    assert pi.shape == (28 * 18, len(labels))
    assert np.all(pi[cut] == 0.0) and np.abs(pi[~cut]).max() > 0
    assert len(Heatmap(ROOM5, estimator="rti").projector().unknowns) == 28 * 18   # a rectangle room: every cell


def test_the_masked_fit_is_the_wilson_patwari_estimate_over_the_cells_inside_alone():
    """Pi = (W^T W + alpha C^-1)^-1 W^T with W and C over the inside cells only: no weight, no prior coupling."""
    cols, rows = 8, 6                                  # no cell centre on the notch's edges
    centres = cell_centres(cols, rows, W_M, D_M)
    inside = inside_mask(centres, L_FOOTPRINT)
    assert inside.sum() == 36
    segs = {f"{rx.name}<-{tx.mac[-2:]}": ((rx.x, rx.y), (tx.x, tx.y)) for rx, tx in ROOM5.links()}
    proj = RTIProjector(centres, segs, (W_M / cols) * (D_M / rows), excess_m=0.3, delta_m=1.0, alpha=0.2,
                        mask=inside)
    labels = list(segs)
    pi = proj.projection(labels, [1.0] * len(labels))
    w = proj.weights[:, inside]
    prior = exponential_prior(centres[inside], 1.0)
    alpha_abs = 0.2 * float(np.mean(np.diag(w @ prior @ w.T)))
    direct = np.linalg.solve(w.T @ w + alpha_abs * np.linalg.inv(prior), w.T)
    assert np.allclose(pi[inside], direct, atol=1e-8)
    assert np.all(pi[~inside] == 0.0)
    with pytest.raises(ValueError):
        RTIProjector(centres, segs, 0.1, mask=inside[:-1])    # one entry per cell


def test_rti_puts_no_heat_in_the_notch_and_images_the_raised_links_inside_instead():
    """A person in the notch at (2.2, 1.4), beside tx0, raises the two links whose ellipses hold them (rx2<-00 and
    rx3<-00). A rectangle room draws the heat there (peak about 0.78); the L-shaped room cannot, so its heat goes to
    the cells of those links inside it (peak about 0.8)."""
    raised = {"rx2<-00", "rx3<-00"}
    series = {f"{rx.name}<-{tx.mac[-2:]}": [1.0] * QUIET + [1.0] * 12 for rx, tx in ROOM5.links()}
    for label in raised:
        series[label] = [1.0] * QUIET + [3.0] * 12
    rect, shaped = Heatmap(ROOM5, busy_link_weight=1.0), Heatmap(L_ROOM5, busy_link_weight=1.0)
    replay(rect, series)
    replay(shaped, series)
    assert rect.estimator_used == shaped.estimator_used == "rti"
    cut = notch()
    assert rect.values[cut].max() > 0.3
    assert np.all(shaped.values[cut] == 0.0)
    assert shaped.values.max() > 0.3


@pytest.mark.parametrize("room, estimator", [(ROOM, "backprojection"), (ROOM5, "rti")])
def test_a_footprint_that_holds_every_cell_centre_changes_nothing(room, estimator):
    five = ((0.0, 0.0), (1.385, 0.0), (2.77, 0.0), (2.77, 1.84), (0.0, 1.84))   # the rectangle with an extra corner
    plain = Heatmap(room, estimator=estimator)
    fenced = Heatmap(dataclasses.replace(room, footprint=five), estimator=estimator)
    assert fenced.room_mask.all()
    series = {label: [1.0] * QUIET + [3.0 if label in ("rx2<-00", "rx1<-01") else 1.0] * 8
              for label in sorted(plain.known_labels)}
    replay(plain, series)
    replay(fenced, series)
    assert plain.estimator_used == fenced.estimator_used == estimator
    assert plain.values.max() > 0.1
    assert np.array_equal(plain.values, fenced.values)


def test_a_footprint_that_holds_no_cell_centre_is_refused():
    sliver = dataclasses.replace(ROOM, footprint=((0.0, 0.0), (0.03, 0.0), (0.0, 0.03)))   # between cell centres
    with pytest.raises(ValueError, match="footprint"):
        Heatmap(sliver)


# ---------------------------------------------------------------- the track (csi/tracker.py)


def test_a_walker_in_the_notch_is_kept_on_its_edge_and_so_is_the_last_seen_marker():
    def down(t):
        return 4.5, min(3.5, 0.3 + 0.8 * t)            # down into the notch (x > 3, y > 2) and on to y = 3.5

    fenced = Tracker(footprint=L_SECTION)
    shown = [p for p in (fenced.update(T0 + t, person(*down(t))) for t in frames(0, 6.0)) if p]
    assert shown and shown[-1]["active"] and shown[-1]["last_seen_s"] == 0
    assert all(point_in_polygon(p["x"], p["y"], L_SECTION, tol=1e-3) for p in shown)
    assert all(point_in_polygon(x, y, L_SECTION, tol=1e-3) for _, x, y in shown[-1]["trail"])
    assert (shown[-1]["x"], shown[-1]["y"]) == (pytest.approx(4.5, abs=0.05), pytest.approx(2.0, abs=0.02))
    marker = fenced.update(T0 + 5.8 + COAST_S + 0.2, quiet())
    assert marker["active"] is False
    assert point_in_polygon(marker["x"], marker["y"], L_SECTION, tol=1e-3)
    assert marker["y"] == pytest.approx(2.0, abs=0.02)

    free = Tracker()                                   # without the footprint the same walk ends in the notch
    for t in frames(0, 6.0):
        last = free.update(T0 + t, person(*down(t)))
    assert last["y"] > 3.0


def test_a_coasting_estimate_stops_at_the_edge_of_the_notch():
    def down(t):
        return 4.5, 0.5 + 0.6 * t                      # heading for the notch at 0.6 m/s

    fenced, free = Tracker(footprint=L_SECTION), Tracker()
    for t in frames(0, 2.6):                           # measured down to y = 1.94, just short of the notch
        fenced.update(T0 + t, person(*down(t)))
        free.update(T0 + t, person(*down(t)))
    unseen = frames(2.6, 5.0)                          # then 2.4 s unseen: a mature track coasts on
    fenced_ys = [fenced.update(T0 + t, quiet())["y"] for t in unseen]
    free_ys = [free.update(T0 + t, quiet())["y"] for t in unseen]
    assert max(free_ys) > 2.2                          # without the footprint it coasts into the notch
    assert 1.95 <= max(fenced_ys) <= 2.0               # with it, it stops at the notch's edge


def test_a_tracker_footprint_must_be_a_polygon():
    with pytest.raises(ValueError, match="footprint"):
        Tracker(footprint=[(0.0, 0.0), (1.0, 1.0)])
    assert Tracker().footprint is None


# ---------------------------------------------------------------- the payload (csi/dashboard.py) and the page


def test_the_payload_carries_the_footprint_and_the_tracker_gets_it():
    state = DashboardState(L_ROOM)
    payload = state.snapshot(now=T0)
    json.dumps(payload, allow_nan=False)
    assert payload["room"] == {"width_m": 2.77, "depth_m": 1.84, "footprint": [list(p) for p in L_FOOTPRINT]}
    assert state._tracker.footprint == L_FOOTPRINT
    cut = notch()
    assert all(v == 0.0 for v, out in zip(payload["heatmap"]["cells"], cut) if out)


def test_a_rectangle_room_payload_and_tracker_are_as_before():
    for room in (ROOM, dataclasses.replace(ROOM, footprint=rectangle(2.77, 1.84))):
        state = DashboardState(room)
        assert state.snapshot(now=T0)["room"] == {"width_m": 2.77, "depth_m": 1.84}
        assert state._tracker.footprint is None        # extents plus margin only, as before


def test_page_clips_the_heat_to_the_footprint_and_draws_its_outline():
    html = PAGE.read_text(encoding="utf-8")
    assert "footprintPoints(room)" in html
    assert "ctx.clip()" in html and 'ctx.fill("evenodd")' in html
    assert "drawHeat(p.heatmap, g)" in html


@pytest.mark.slow
@needs_node
def test_page_draws_the_footprint_and_still_renders_rooms_and_logs_without_one(tmp_path):
    shaped = DashboardState(L_ROOM).snapshot(now=T0)
    old = copy.deepcopy(shaped)
    del old["room"]["footprint"]                       # a rectangle room, or a log written before footprints
    broken = copy.deepcopy(shaped)
    broken["room"]["footprint"] = [[0, 0], [1, "a"]]   # malformed: ignored, not an error
    calls = {}
    for name, payload in (("shaped", shaped), ("old", old), ("broken", broken)):
        result = render_payloads_with_stub(tmp_path, [payload])
        assert result["errors"] == [], name
        calls[name] = result["calls"]
    names = [c[0] for c in calls["shaped"]]
    assert "clip" in names and names.index("clip") < names.index("drawImage")   # the heat is clipped to it
    assert ["fill", "evenodd"] in calls["shaped"]                               # outside it is shaded
    outline = next(c for c in calls["shaped"] if c[0] == "strokeRect")
    heat = next(c for c in calls["shaped"] if c[0] == "drawImage")
    assert heat[2:] == outline[1:]                     # the heat image still spans the room rectangle
    for name in ("old", "broken"):
        assert not any(c[0] == "clip" for c in calls[name]), name
        assert ["fill", "evenodd"] not in calls[name], name
