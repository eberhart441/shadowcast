"""Regularised RTI (csi/rti.py) and its plumbing in the heatmap: estimator choice and robust link evidence.

The geometry tests use the frozen four-link rig (tests/conftest.py) and the five-node fixture (rx3 added at the
top middle, six links). Score timelines follow tests/test_heatmap_graded.py: 11 s of quiet so every baseline is
warm, then the scenario.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from csi.heatmap import Heatmap, QuietBaseline, cell_centres, link_evidence, robust_link_evidence, robust_z
from csi.rti import RTIProjector, exponential_prior, link_weight_row
from csi.room import load_room, point_segment_distance

ROOT = Path(__file__).resolve().parents[1]
ROOM = load_room()                                                                     # four links
ROOM5 = load_room(Path(__file__).resolve().parent / "fixtures" / "room_5node.json")   # six links
COLS, ROWS = 28, 18
W_M, D_M = 2.77, 1.84
AREA = (W_M / COLS) * (D_M / ROWS)
CROSSING = (1.385, 0.92)      # where the two diagonals cross
DIAG_A = "rx2<-00"            # (0, 0) -> (2.77, 1.84)
DIAG_B = "rx1<-01"            # (2.77, 0) -> (0, 1.84), room weight 0.3
QUIET = 44                    # 11 s of 0.25 s steps


def row(label, score, state="EMPTY"):
    return {"id": label, "state": state, "score": score, "threshold": 10.0, "age_s": 0.0}


def replay_links(heat, series, start=0.0, step=0.25):
    length = min(len(s) for s in series.values())
    for k in range(length):
        heat.advance(start + step * k, [row(label, s[k]) for label, s in series.items()])


def hottest(heat, room):
    centres = cell_centres(heat.cols, heat.rows, room.width_m, room.depth_m)
    return centres[int(np.argmax(heat.values))]


def nearest_node(point, room):
    return min(math.hypot(point[0] - n.x, point[1] - n.y) for n in room.receivers + room.senders)


def segments(room):
    return {f"{rx.name}<-{tx.mac[-2:]}": ((rx.x, rx.y), (tx.x, tx.y)) for rx, tx in room.links()}


def excess(point, a, b):
    return (math.hypot(point[0] - a[0], point[1] - a[1]) + math.hypot(point[0] - b[0], point[1] - b[1])
            - math.hypot(b[0] - a[0], b[1] - a[1]))


# ---------------------------------------------------------------- link weights


def test_cells_on_a_link_get_weight_and_cells_far_from_it_get_none():
    centres = cell_centres(COLS, ROWS, W_M, D_M)
    a, b = (0.0, 0.0), (W_M, D_M)
    w = link_weight_row(centres, a, b, 0.3, AREA, soft_sigma_m=0.07)
    d = math.hypot(W_M, D_M)
    on_line = point_segment_distance(centres, a, b) < 0.05
    assert on_line.sum() >= 20
    assert np.allclose(w[on_line], AREA / math.sqrt(d))           # the ellipse weight is cell area / sqrt(d)
    gap = np.array([excess(c, a, b) for c in centres])
    assert np.all(w[gap > 0.3] == 0.0)                              # outside the ellipse: nothing
    assert np.all(w[point_segment_distance(centres, a, b) > 0.8] == 0.0)
    # wider than an RSS Fresnel zone: mid-link cells 0.6 m off the line still count (CSI's wider footprint)
    mid_off = (np.abs(point_segment_distance(centres, a, b) - 0.6) < 0.05) & (np.abs(centres[:, 0] - 1.385) < 0.3)
    assert mid_off.any() and np.all(w[mid_off] > 0)


def test_soft_fallback_lets_a_very_short_link_cover_the_cells_it_passes():
    centres = cell_centres(COLS, ROWS, W_M, D_M)
    a, b = (1.30, 0.90), (1.32, 0.90)       # 2 cm apart, between cell centres
    hard = link_weight_row(centres, a, b, 0.001, AREA, soft_sigma_m=0)
    assert not hard.any()                   # the ellipse alone misses every cell centre
    soft = link_weight_row(centres, a, b, 0.001, AREA, soft_sigma_m=0.07)
    assert soft.any()
    nearest = int(np.argmin(point_segment_distance(centres, a, b)))
    assert int(np.argmax(soft)) == nearest
    assert soft.max() <= AREA / math.sqrt(0.5) + 1e-12    # the length floor caps a short link's weight
    assert np.all(soft[point_segment_distance(centres, a, b) > 0.25] == 0.0)


# ---------------------------------------------------------------- the projection


def test_projection_has_one_column_per_link_and_w_pi_is_symmetric():
    proj = Heatmap(ROOM, estimator="rti").projector()
    labels = sorted(proj.labels)
    pi = proj.projection(labels, [1.0] * len(labels))
    assert pi.shape == (COLS * ROWS, len(labels))
    w = proj.weights[[proj.labels.index(label) for label in labels]]
    resolution = w @ pi                     # = K (K + alpha I)^-1 with K = W C W^T: symmetric, eigenvalues in [0, 1)
    assert np.allclose(resolution, resolution.T, atol=1e-10)
    eig = np.linalg.eigvalsh((resolution + resolution.T) / 2)
    assert eig.min() > -1e-9 and eig.max() < 1.0


def test_mirror_image_links_have_mirror_image_projections():
    """The rig is symmetric about x = 1.385: rx2 <-> rx1 and tx1 <-> tx0, so DIAG_A <-> DIAG_B, left <-> right wall."""
    proj = Heatmap(ROOM, estimator="rti").projector()
    labels = sorted(proj.labels)
    pi = proj.projection(labels, [1.0] * len(labels))
    column = {label: pi[:, i].reshape(ROWS, COLS) for i, label in enumerate(labels)}
    assert np.allclose(column[DIAG_A][:, ::-1], column[DIAG_B], atol=1e-9)
    assert np.allclose(column["rx2<-01"][:, ::-1], column["rx1<-00"], atol=1e-9)


@pytest.mark.parametrize("confidence", [(1.0, 1.0, 1.0, 1.0), (1.0, 0.3, 0.5, 1.0)])
def test_projection_equals_the_wilson_patwari_form(confidence):
    """Pi = C W^T (W C W^T + alpha V^-1)^-1 is (W^T V W + alpha C^-1)^-1 W^T V, checked on a coarse grid."""
    cols, rows = 7, 5
    centres = cell_centres(cols, rows, W_M, D_M)
    area = (W_M / cols) * (D_M / rows)
    segs = segments(ROOM)
    proj = RTIProjector(centres, segs, area, excess_m=0.3, delta_m=1.0, alpha=0.2)
    labels = list(segs)
    w = proj.weights
    prior = exponential_prior(centres, 1.0)
    alpha_abs = 0.2 * float(np.mean(np.diag(w @ prior @ w.T)))
    v = np.diag(confidence)
    direct = np.linalg.solve(w.T @ v @ w + alpha_abs * np.linalg.inv(prior), w.T @ v)
    assert np.allclose(proj.projection(labels, list(confidence)), direct, atol=1e-8)


def test_projections_are_cached_per_link_set_and_recomputed_when_it_changes():
    proj = Heatmap(ROOM5, estimator="rti").projector()
    labels = sorted(proj.labels)
    first = proj.projection(labels, [1.0] * 6)
    assert proj.projection(labels, [1.0] * 6) is first                      # same set: cached
    vetoed = proj.projection(labels, [0.1, 1.0, 1.0, 1.0, 1.0, 1.0])        # a confidence changed: recomputed
    fewer = proj.projection(labels[1:], [1.0] * 5)                          # a link dropped out: recomputed
    assert vetoed is not first and fewer.shape == (COLS * ROWS, 5)
    with pytest.raises(ValueError):
        proj.projection(labels, [0.0] + [1.0] * 5)


# ---------------------------------------------------------------- images


def test_rti_puts_a_person_crossing_two_links_at_the_crossing():
    heat = Heatmap(ROOM, estimator="rti")
    series = {label: [1.0] * (QUIET + 12) for label in sorted(heat.known_labels)}
    series[DIAG_A] = [1.0] * QUIET + [3.0] * 12
    series[DIAG_B] = [1.0] * QUIET + [2.0] * 12
    replay_links(heat, series)
    assert heat.estimator_used == "rti"
    hot = hottest(heat, ROOM)
    assert math.hypot(hot[0] - CROSSING[0], hot[1] - CROSSING[1]) < 0.2
    assert heat.values.max() > 0.4


def test_auto_rti_locates_a_synthetic_person_in_the_six_link_room():
    """Each link whose ellipse holds the person rises to 3x quiet (the model's own forward model). The veto is
    off (busy_link_weight 1): beside a sender all three of its links rise, which is the veto's case, not this one."""
    segs = segments(ROOM5)
    errors = []
    for x in (0.4, 0.8, 1.2, 1.6, 2.0):
        for y in (0.35, 0.73, 1.11, 1.49):
            raised = {label for label, (a, b) in segs.items() if excess((x, y), a, b) < 0.3}
            if len(raised) < 2:
                continue
            heat = Heatmap(ROOM5, busy_link_weight=1.0)
            replay_links(heat, {label: [1.0] * QUIET + [3.0 if label in raised else 1.0] * 12 for label in segs})
            assert heat.estimator_used == "rti"
            hot = hottest(heat, ROOM5)
            errors.append(math.hypot(hot[0] - x, hot[1] - y))
    assert len(errors) >= 15
    assert max(errors) < 0.5
    assert float(np.median(errors)) < 0.35


@pytest.mark.parametrize("room, estimator", [(ROOM, "rti"), (ROOM5, "auto")])
def test_a_rise_on_every_link_at_once_is_not_drawn_at_a_node(room, estimator):
    heat = Heatmap(room, estimator=estimator)
    replay_links(heat, {label: [1.0] * QUIET + [1.8] * 12 for label in sorted(heat.known_labels)})
    assert heat.estimator_used == "rti"
    assert heat.values.max() > 0.1
    assert nearest_node(hottest(heat, room), room) > 0.5


def test_independent_link_noise_does_not_put_false_peaks_at_the_nodes():
    heat = Heatmap(ROOM5, estimator="rti")
    proj = heat.projector()
    centres = cell_centres(COLS, ROWS, W_M, D_M)
    near = np.array([nearest_node(c, ROOM5) <= 0.5 for c in centres])
    confidence = {label: ROOM5.link_weights.get(label, 1.0) for label in proj.labels}
    rng = np.random.default_rng(5)
    peaks = at_nodes = 0
    for _ in range(300):
        evidence = dict(zip(proj.labels, np.clip(rng.standard_normal(len(proj.labels)), 0.0, 1.0)))
        image = proj.image(evidence, confidence)
        if image.max() <= 0:
            continue
        peaks += 1
        at_nodes += bool(near[int(np.argmax(image))])
    assert peaks > 250
    assert at_nodes / peaks < min(0.4, near.mean() + 0.1)    # no more than the nodes' share of the area


def test_rti_is_cold_without_raised_links():
    heat = Heatmap(ROOM5, estimator="rti")
    rng = np.random.default_rng(3)
    replay_links(heat, {label: list(np.clip(1.0 + 0.03 * rng.standard_normal(80), 0.5, None))
                        for label in sorted(heat.known_labels)})
    assert heat.values.max() < 0.02
    assert heat.contributing == []


# ---------------------------------------------------------------- estimator choice


def quiet_series(room, seconds=11.0, step=0.25):
    return {f"{rx.name}<-{tx.mac[-2:]}": [1.0] * int(seconds / step) for rx, tx in room.links()}


def test_auto_uses_rti_only_while_six_links_that_reach_the_grid_report():
    four = Heatmap(ROOM)
    replay_links(four, quiet_series(ROOM))
    assert four.estimator == "auto" and four.estimator_used == "backprojection"

    six = Heatmap(ROOM5)
    replay_links(six, quiet_series(ROOM5, seconds=5.0))
    assert six.estimator_used == "backprojection"        # still warming up: no link reports yet
    replay_links(six, quiet_series(ROOM5, seconds=6.0), start=5.0)
    assert six.estimator_used == "rti"

    one_silent = Heatmap(ROOM5)
    labels = sorted(segments(ROOM5))
    for k in range(QUIET):
        one_silent.advance(0.25 * k, [row(label, 1.0, state="NO_SIGNAL" if label == labels[0] else "EMPTY")
                                      for label in labels])
    assert one_silent.estimator_used == "backprojection"   # five reporting links: back-projection


def test_a_forced_estimator_is_used_whatever_the_link_count():
    rti_four = Heatmap(ROOM, estimator="rti")
    replay_links(rti_four, quiet_series(ROOM))
    assert rti_four.estimator_used == "rti"
    bp_six = Heatmap(ROOM5, estimator="backprojection")
    replay_links(bp_six, quiet_series(ROOM5))
    assert bp_six.estimator_used == "backprojection"
    with pytest.raises(ValueError):
        Heatmap(ROOM, estimator="tomography")
    with pytest.raises(ValueError):
        Heatmap(ROOM, evidence="zscore")


# ---------------------------------------------------------------- robust evidence


def test_robust_z_standardises_the_log_score_with_a_floored_mad():
    assert robust_z(math.exp(0.5), 0.0, 0.1) == pytest.approx(0.5 / (1.4826 * 0.1))
    assert robust_z(math.exp(0.1), 0.0, 0.0) == pytest.approx(0.1 / (1.4826 * 0.05))   # MAD floor 0.05
    assert robust_z(None, 0.0, 0.1) is None and robust_z(2.0, None, None) is None and robust_z(0.0, 0.0, 0.1) is None


def test_robust_evidence_is_zero_to_z_floor_full_at_z_full_and_linear_between():
    sigma = 1.4826 * 0.2

    def at(z):
        return robust_link_evidence(math.exp(z * sigma), 0.0, 0.2)

    assert at(0.0) == 0.0 and at(1.9) == 0.0 and at(2.0) == pytest.approx(0.0, abs=1e-9)
    assert at(4.0) == pytest.approx(0.5)
    assert at(6.0) == pytest.approx(1.0) and at(12.0) == 1.0
    assert robust_link_evidence(math.exp(4 * sigma), 0.0, 0.2, z_floor=2.0, z_full=8.0) == pytest.approx(1 / 3)
    assert robust_link_evidence(None, 0.0, 0.2) == 0.0 and robust_link_evidence(2.0, None, None) == 0.0


def test_a_weak_quiet_link_counts_as_much_as_a_strong_noisy_one():
    """A through-wall link that rises 1.6x over a tight quiet level is as sure as one that rises 40x over a
    noisy level; the ratio rule gives the weak link a third of the evidence."""
    weak = robust_link_evidence(1.6, 0.0, 0.05)
    strong = robust_link_evidence(40.0, 0.0, 0.4)
    assert weak == 1.0 and strong == 1.0
    assert link_evidence(1.6, 1.0) < 0.4 and link_evidence(40.0, 1.0) == 1.0


def test_quiet_baseline_keeps_a_log_median_and_mad():
    baseline = QuietBaseline(window_s=60.0, warmup_s=10.0)
    logs = [0.0, 0.1, -0.1, 0.2, -0.2, 0.05, -0.05, 0.3, -0.3]
    for k in range(48):                                   # 12 s at 0.25 s
        baseline.push(0.25 * k, math.exp(logs[k % len(logs)]))
        if 0.25 * k < 10.0:
            assert baseline.log_stats() is None           # warm-up, like value()
    median, mad = baseline.log_stats()
    pushed = np.array([logs[k % len(logs)] for k in range(48)])
    assert median == pytest.approx(float(np.median(pushed)))
    assert mad == pytest.approx(float(np.median(np.abs(pushed - np.median(pushed)))))
    baseline.push(12.0, 0.0)                              # a zero score is skipped, not log(0)
    assert np.isfinite(baseline.log_stats()[0])


def test_rti_reports_robust_evidence_and_the_backprojection_keeps_the_ratio_rule():
    rng = np.random.default_rng(7)
    jitter = list(np.exp(0.2 * rng.standard_normal(QUIET)))
    six = Heatmap(ROOM5)
    replay_links(six, {label: jitter + [2.0] for label in segments(ROOM5)})
    assert six.estimator_used == "rti"
    median_ln, mad_ln = six._baselines[DIAG_A].log_stats()
    assert six.evidence[DIAG_A] == pytest.approx(robust_link_evidence(2.0, median_ln, mad_ln))

    four = Heatmap(ROOM)
    replay_links(four, {label: jitter + [2.0] for label in segments(ROOM)})
    assert four.estimator_used == "backprojection"
    assert four.evidence[DIAG_A] == pytest.approx(link_evidence(2.0, four.baselines[DIAG_A]))

    forced = Heatmap(ROOM, evidence="robust_z")
    replay_links(forced, {label: jitter + [2.0] for label in segments(ROOM)})
    median_ln, mad_ln = forced._baselines[DIAG_A].log_stats()
    assert forced.estimator_used == "backprojection"
    assert forced.evidence[DIAG_A] == pytest.approx(robust_link_evidence(2.0, median_ln, mad_ln))


# ---------------------------------------------------------------- the dashboard option


def load_cli():
    sys.path.insert(0, str(ROOT / "scripts"))
    import dashboard as cli  # noqa: E402  (scripts/dashboard.py, as in tests/test_dashboard_cli.py)
    return cli


def test_dashboard_estimator_option_parses_and_applies_to_live_sources_only():
    cli = load_cli()
    parser = cli.build_parser()
    assert parser.parse_args(["--simulate-room"]).estimator is None          # the server then runs auto
    args = parser.parse_args(["--port", "rx1=/dev/cu.a", "--estimator", "rti"])
    assert args.estimator == "rti"
    cli.check_source(args)
    with pytest.raises(SystemExit):
        parser.parse_args(["--simulate-room", "--estimator", "tomography"])
    with pytest.raises(SystemExit):
        cli.check_source(parser.parse_args(["--replay", "run.jsonl", "--estimator", "rti"]))
    with pytest.raises(SystemExit):
        cli.check_source(parser.parse_args(["--replay-session", "data/hw5", "--estimator", "backprojection"]))


@pytest.mark.parametrize("argv, expected", [([], "auto"), (["--estimator", "backprojection"], "backprojection")])
def test_dashboard_estimator_option_reaches_the_heatmap_and_the_log_header(argv, expected):
    cli = load_cli()
    args = cli.build_parser().parse_args(["--port", "rx1=/dev/cu.never-opened", "--http-port", "0", *argv])
    cli.check_source(args)
    server = cli.build_server(args)        # built, not started: no port is opened
    try:
        assert server._state._heat.estimator == expected
        assert server._log_header["heatmap"] == {"estimator": expected}
    finally:
        server.stop()
