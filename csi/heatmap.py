"""RF-tomography style heatmap of where motion is likely, from per-link motion scores.

Each link is judged against its own quiet baseline, not its threshold. A walking person lifts a real
link to only about 0.6-0.8 of its threshold, so threshold-relative evidence is almost always zero. The
baseline is the median (50th percentile) of that link's own calibrated score over the last 60 s of
host time. It learns only while the link is EMPTY, so a person who keeps a link in MOTION does not pull
it up and stays visible, and the link reads its quiet level again as soon as the movement stops. A
freeze lasts at most ``baseline_freeze_s`` (90 s): a level held longer is learned as the new normal (a
receiver that re-rolled its gain, a moved node). It needs 10 s of history before the link contributes at
all, so there is no flash at start. The median rather than a low percentile: on the real rig a noisy
link's 20th percentile sits at the bottom of its own empty-room noise, which makes every spike look like
3x evidence.

Per link, the evidence ``e`` is the log ratio of score to baseline, mapped to 0..1: ratios up to
``r_floor`` (1.2) are noise and give 0, and ``r_full`` (3x the baseline) gives 1. A link's footprint
``F`` is a Gaussian of the cell centre's distance to its sender-receiver segment, and ``w`` is its weight
from the room config. Per cell, over the links that can report now (EMPTY or MOTION, with a baseline):

    coverage  C = sum(w * F)
    mean      M = sum(w * e * F) / (C + eps)        eps = coverage_eps (0.25) x the room's mean coverage
    agree     A = max over two links that share no node of sqrt(w1 e1 F1 * w2 e2 F2)
    field       = ((1 - agreement) * M + agreement * A) * mask(C)

``M`` normalises by coverage: a cell is as hot as the links that see it say, however many of them overlap
there. So the nodes, where every footprint of a node meets, no longer collect heat from all of them, and
quiet links that cross a cell pull it down. ``eps`` keeps a cell that a link barely reaches from taking
that link's full evidence, and ``mask`` blanks cells covered by less than ``min_coverage`` of one full link,
rising linearly to full at twice that. ``A`` makes two links that cross at a cell hotter than either alone,
so a crossing stands out and a lone noisy link stays dim. Links that share a node always overlap at it,
wherever the person is, so such a pair never counts as agreement. The field goes through
``1 - exp(-sensitivity * field)``, then the grid is smoothed in time with exponential rise and decay, so an
empty room settles to cold. All time is host time from the packets, never the wall clock.

Near-node veto: when at least 70 % of a node's links (it needs at least 3 that can report) have evidence
``busy_evidence`` or more while at most 30 % of the other links do, the movement is right next to that node
(an operator beside an outside node), not in the room. The node is listed in ``busy_nodes`` until
``busy_hold_s`` after the rule last held, and its links keep only ``busy_link_weight`` of their weight, so
they do not paint a starburst across the map.

Known limit: a person who stands still leaves their links EMPTY, so they are absorbed into the baseline
within about half the window (about 30 s) and fade. The map shows change in the room, not standing presence.

Estimators (``estimator``): "backprojection" is the coverage-normalised field above. "rti" is regularised
radio tomographic imaging (csi/rti.py): ellipse link weights, a Tikhonov/MAP inverse with an exponential spatial
prior, precomputed per set of reporting links. It explains all links at once, so two raised links that cross
put the image at the crossing, quiet links that pass a cell hold it down, and a common rise on every link does
not pile up at the nodes. "auto" (the default) uses RTI while at least ``rti_min_links`` (6) links that reach the
grid can report, else the back-projection: a handful of links cannot localise by inversion. ``estimator_used``
says which one drew the last frame. Both go through the same soft saturation and smoothing. In both a vetoed
link keeps ``busy_link_weight`` of its weight: in the back-projection its footprint is scaled, in RTI its evidence
and its confidence (the inverse of its noise variance in the fit) are.

Robust evidence (``evidence``): "ratio" is the log-ratio rule above. "robust_z" standardises each link against
its own quiet baseline: ``z = (ln s - median ln s) / (1.4826 * MAD ln s)`` over the same quiet-only window, with
the MAD floored at ``mad_floor`` (0.05, about a 5 % ripple, so a flat baseline does not make every flicker
infinite), mapped to 0..1 as ``(z - z_floor) / (z_full - z_floor)``. z_floor 2 is where a quiet link's
heavy-tailed score still exceeds its median by chance a few percent of the time; z_full 6 (the low end of 6-8) lets
a weak through-wall link that rises six of its own noise widths count as fully as a strong link; z = 4, where
round-2 measured about 1 % false alarms per link window, gives 0.5, the near-node veto's level. "auto" (the
default) uses robust_z with RTI and the ratio rule with the back-projection, so the four-link rig is unchanged.

Public API:
    link_evidence(score, baseline, r_floor, r_full) -> float in 0..1
    robust_z(score, median_ln, mad_ln, mad_floor)   -> standardised log score, None without a baseline
    robust_link_evidence(score, median_ln, mad_ln, z_floor, z_full, mad_floor) -> float in 0..1
    QuietBaseline(window_s, percentile, warmup_s)   rolling percentile (and log median/MAD) of a link's quiet score
    cell_centres(cols, rows, width_m, depth_m)      -> (cols*rows, 2) row-major
    segment_falloff(centres, a, b, sigma_m)         -> (cols*rows,) in 0..1
    Heatmap(room, ...)                              per-link footprints, baselines, veto, smoothing
        .advance(now, rows)   step the grid to host time ``now`` from link rows (LinkMonitor snapshot)
        .values               current grid, flat row-major, 0..1
        .contributing         link labels that currently put heat on the grid
        .evidence             {label: 0..1} per link, from the last advance
        .baselines            {label: quiet baseline or None while in warm-up}
        .busy_nodes           names of the nodes the near-node veto holds now
        .estimator            the configured estimator: "auto", "backprojection" or "rti"
        .estimator_used       "backprojection" or "rti": the estimator of the last frame
"""
from __future__ import annotations

import math
from collections import deque
from typing import Mapping, Optional, Sequence

import numpy as np

from . import rti
from .links import LinkId
from .room import Room, point_segment_distance

COLS = 28
ROWS = 18
FALLOFF_M = 0.35          # Gaussian width of a link's footprint, metres (1 sigma)
SENSITIVITY = 1.3         # gain before the soft saturation; higher lights up weaker evidence more
AGREEMENT = 0.5           # share of the cell value taken from two links that cross there
COVERAGE_EPS = 0.25       # eps of the coverage normalisation, as a share of the room's mean coverage
MIN_COVERAGE = 0.1        # cells covered by less than this (in full links) are blank; full from twice this
R_FLOOR = 1.2             # score / baseline at or below which a link's evidence is zero (dead zone)
R_FULL = 3.0              # score / baseline at which a link's evidence reaches 1
BASELINE_WINDOW_S = 60.0  # host-time span of the quiet baseline
BASELINE_PERCENTILE = 50.0
BASELINE_WARMUP_S = 10.0  # history needed before a link's baseline is trusted
BASELINE_FREEZE_S = 90.0  # longest a baseline stays frozen while its link is in MOTION; longer is the new normal
RISE_TAU_S = 0.3          # time constant of a cell heating up
DECAY_TAU_S = 0.3         # time constant of a cell cooling down
STEP_S = 0.1              # host-time step between smoothing updates while packets arrive
BUSY_EVIDENCE = 0.5       # near-node veto: a link counts as raised at this evidence (about 1.9x its baseline)
BUSY_SHARE = 0.7          # ... a node is busy when at least this share of its links are raised
QUIET_SHARE = 0.3         # ... while at most this share of the links that do not touch it are
BUSY_MIN_LINKS = 3        # ... and at least this many of its links can report
BUSY_LINK_WEIGHT = 0.1    # a busy node's links keep this share of their weight on the map
BUSY_HOLD_S = 1.0         # a node stays busy this long after the rule last held, so the note does not flicker
CONTRIBUTING_STATES = frozenset({"EMPTY", "MOTION"})
BASELINE_STATES = frozenset({"EMPTY"})   # the baseline learns from quiet only, up to BASELINE_FREEZE_S
LIVE_PARAMS = frozenset({"sensitivity", "rise_tau", "decay_tau", "falloff_m"})  # changeable while running
ESTIMATORS = ("auto", "backprojection", "rti")
EVIDENCE_MODES = ("auto", "ratio", "robust_z")
RTI_MIN_LINKS = 6         # auto: RTI from this many reporting links that reach the grid, else back-projection
Z_FLOOR = 2.0             # robust_z evidence: zero at or below this z (dead zone)
Z_FULL = 6.0              # ... and full (1) at this z
MAD_FLOOR = 0.05          # ... floor of the MAD of ln(score), so a flat quiet baseline does not blow z up
MAD_SCALE = 1.4826        # MAD -> standard deviation for Gaussian data
_TINY = 1e-9


def link_evidence(score: Optional[float], baseline: Optional[float], r_floor: float = R_FLOOR,
                  r_full: float = R_FULL) -> float:
    """Evidence in 0..1 that a link's score is raised above its own quiet baseline.

    The log ratio is zero at ``r_floor`` and one at ``r_full``, clipped. Zero when there is no score or
    no baseline yet.
    """
    if score is None or baseline is None or baseline <= 0 or score <= 0:
        return 0.0
    x = math.log(score / baseline / r_floor) / math.log(r_full / r_floor)
    return min(1.0, max(0.0, x))


def robust_z(score: Optional[float], median_ln: Optional[float], mad_ln: Optional[float],
             mad_floor: float = MAD_FLOOR) -> Optional[float]:
    """``(ln score - median_ln) / (1.4826 * max(mad_ln, mad_floor))``; None without a score or a baseline."""
    if score is None or median_ln is None or mad_ln is None or score <= 0:
        return None
    return (math.log(score) - median_ln) / (MAD_SCALE * max(mad_ln, mad_floor, _TINY))


def robust_link_evidence(score: Optional[float], median_ln: Optional[float], mad_ln: Optional[float],
                         z_floor: float = Z_FLOOR, z_full: float = Z_FULL, mad_floor: float = MAD_FLOOR) -> float:
    """Evidence in 0..1 from the robust z of a link's score against its quiet log median and MAD.

    Zero at or below ``z_floor``, one at ``z_full``, linear between. Zero when there is no score or no
    baseline yet.
    """
    z = robust_z(score, median_ln, mad_ln, mad_floor)
    if z is None:
        return 0.0
    return min(1.0, max(0.0, (z - z_floor) / max(z_full - z_floor, _TINY)))


class QuietBaseline:
    """Percentile of a link's score over the last ``window_s`` of host time, once warmed up."""

    def __init__(self, window_s: float = BASELINE_WINDOW_S, percentile: float = BASELINE_PERCENTILE,
                 warmup_s: float = BASELINE_WARMUP_S) -> None:
        self._window_s, self._percentile, self._warmup_s = window_s, percentile, warmup_s
        self._samples: deque[tuple[float, float]] = deque()

    def push(self, t: float, score: float) -> None:
        self._samples.append((t, score))
        while self._samples and self._samples[0][0] < t - self._window_s:
            self._samples.popleft()

    def shift(self, seconds: float) -> None:
        """Move the kept samples ``seconds`` later, so time the link spent frozen does not age them out."""
        if seconds > 0:
            self._samples = deque((t + seconds, s) for t, s in self._samples)

    def value(self) -> Optional[float]:
        """The baseline, or None until the window holds ``warmup_s`` of history."""
        if not self._warm():
            return None
        return float(np.percentile([s for _, s in self._samples], self._percentile))

    def log_stats(self) -> Optional[tuple[float, float]]:
        """(median, MAD) of ln(score) over the same window, or None in warm-up or without a positive score."""
        if not self._warm():
            return None
        logs = np.log([s for _, s in self._samples if s > 0])
        if not len(logs):
            return None
        median = float(np.median(logs))
        return median, float(np.median(np.abs(logs - median)))

    def _warm(self) -> bool:
        return bool(self._samples) and self._samples[-1][0] - self._samples[0][0] >= self._warmup_s


def cell_centres(cols: int, rows: int, width_m: float, depth_m: float) -> np.ndarray:
    """Centre of each grid cell in metres, origin top-left, flat row-major (row r, column c at r*cols + c)."""
    xs = (np.arange(cols) + 0.5) * width_m / cols
    ys = (np.arange(rows) + 0.5) * depth_m / rows
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel()])


def segment_falloff(centres: np.ndarray, a: Sequence[float], b: Sequence[float], sigma_m: float) -> np.ndarray:
    """Gaussian of each cell's distance to the segment a-b: 1 on the segment, exp(-1/2) at sigma_m."""
    d = point_segment_distance(centres, a, b)
    return np.exp(-d ** 2 / (2 * sigma_m ** 2))


class Heatmap:
    """Per-link footprints for the room's links, per-link baselines, the near-node veto, plus the smoothed grid."""

    def __init__(self, room: Room, *, cols: int = COLS, rows: int = ROWS, falloff_m: float = FALLOFF_M,
                 sensitivity: float = SENSITIVITY, agreement: float = AGREEMENT,
                 coverage_eps: float = COVERAGE_EPS, min_coverage: float = MIN_COVERAGE,
                 r_floor: float = R_FLOOR, r_full: float = R_FULL,
                 baseline_window_s: float = BASELINE_WINDOW_S,
                 baseline_percentile: float = BASELINE_PERCENTILE,
                 baseline_warmup_s: float = BASELINE_WARMUP_S,
                 baseline_freeze_s: float = BASELINE_FREEZE_S,
                 rise_tau_s: float = RISE_TAU_S, decay_tau_s: float = DECAY_TAU_S,
                 step_s: float = STEP_S, busy_evidence: float = BUSY_EVIDENCE,
                 busy_link_weight: float = BUSY_LINK_WEIGHT, busy_hold_s: float = BUSY_HOLD_S,
                 estimator: str = "auto", evidence: str = "auto", rti_min_links: int = RTI_MIN_LINKS,
                 z_floor: float = Z_FLOOR, z_full: float = Z_FULL, mad_floor: float = MAD_FLOOR,
                 rti_excess_m: float = rti.EXCESS_M, rti_delta_m: float = rti.PRIOR_DELTA_M,
                 rti_alpha: float = rti.ALPHA) -> None:
        if estimator not in ESTIMATORS:
            raise ValueError(f"unknown estimator {estimator!r}; expected one of {', '.join(ESTIMATORS)}")
        if evidence not in EVIDENCE_MODES:
            raise ValueError(f"unknown evidence {evidence!r}; expected one of {', '.join(EVIDENCE_MODES)}")
        if not z_full > z_floor:
            raise ValueError(f"z_full ({z_full!r}) must be above z_floor ({z_floor!r})")
        self.estimator, self.evidence_mode, self.rti_min_links = estimator, evidence, rti_min_links
        self.z_floor, self.z_full, self.mad_floor = z_floor, z_full, mad_floor
        self.rti_excess_m, self.rti_delta_m, self.rti_alpha = rti_excess_m, rti_delta_m, rti_alpha
        self.estimator_used = "rti" if estimator == "rti" else "backprojection"
        self._rti: Optional[rti.RTIProjector] = None   # built on first use
        self.cols, self.rows = cols, rows
        self.falloff_m, self.sensitivity, self.agreement = falloff_m, sensitivity, agreement
        self.coverage_eps, self.min_coverage = coverage_eps, min_coverage
        self.r_floor, self.r_full = r_floor, r_full
        self.baseline_window_s, self.baseline_percentile = baseline_window_s, baseline_percentile
        self.baseline_warmup_s, self.baseline_freeze_s = baseline_warmup_s, baseline_freeze_s
        self.rise_tau, self.decay_tau, self.step = rise_tau_s, decay_tau_s, step_s
        self.busy_evidence, self.busy_link_weight, self.busy_hold_s = busy_evidence, busy_link_weight, busy_hold_s
        self._weights: Mapping[str, float] = dict(room.link_weights)
        self._room = room
        # link label -> the names of its two nodes (pairs that share one never agree; the veto groups by node)
        self._ends: dict[str, frozenset[str]] = {
            LinkId(rx.name, tx.mac).label: frozenset((rx.name, tx.name)) for rx, tx in room.links()}
        self._nodes = sorted(set().union(*self._ends.values())) if self._ends else []
        self._centres = cell_centres(cols, rows, room.width_m, room.depth_m)
        self._falloff: dict[str, np.ndarray] = {}
        self._weighted: dict[str, np.ndarray] = {}
        self._build_falloff(falloff_m)
        self._baselines: dict[str, QuietBaseline] = {
            label: QuietBaseline(baseline_window_s, baseline_percentile, baseline_warmup_s)
            for label in self._falloff}
        self._fed_until: dict[str, float] = {}
        self._frozen_since: dict[str, float] = {}   # link label -> packet time its baseline froze (MOTION)
        self._busy_until: dict[str, float] = {}     # node name -> host time its near-node veto ends
        self.values = np.zeros(cols * rows)
        self._last_t: Optional[float] = None
        self.contributing: list[str] = []
        self.evidence: dict[str, float] = {}
        self.baselines: dict[str, Optional[float]] = {}
        self.busy_nodes: list[str] = []

    def _build_falloff(self, falloff_m: float) -> None:
        """Each link's footprint and weighted footprint, and the room's coverage that sets eps."""
        for rx, tx in self._room.links():
            label = LinkId(rx.name, tx.mac).label
            self._falloff[label] = segment_falloff(self._centres, (rx.x, rx.y), (tx.x, tx.y), falloff_m)
            self._weighted[label] = self._weights.get(label, 1.0) * self._falloff[label]
        self._coverage = sum(self._weighted.values(), np.zeros(len(self._centres)))
        self._eps = self.coverage_eps * float(np.mean(self._coverage)) if len(self._coverage) else 0.0

    def set_params(self, **kwargs) -> None:
        """Change live tunables: sensitivity, rise_tau, decay_tau, falloff_m (attribute names).

        Only these names are accepted; anything else raises ValueError and nothing is changed. All values
        must be positive finite numbers. A new falloff_m rebuilds the per-link footprints.
        """
        unknown = sorted(set(kwargs) - LIVE_PARAMS)
        if unknown:
            raise ValueError(f"not a live heatmap parameter: {', '.join(unknown)}; "
                             f"allowed: {', '.join(sorted(LIVE_PARAMS))}")
        for name, value in kwargs.items():
            if not (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                    and value > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        for name, value in kwargs.items():
            setattr(self, name, float(value))
        if "falloff_m" in kwargs:
            self._build_falloff(self.falloff_m)

    @property
    def known_labels(self) -> set[str]:
        return set(self._falloff)

    def due(self, now: float) -> bool:
        return self._last_t is None or now - self._last_t >= self.step

    def advance(self, now: float, rows: Sequence[Mapping]) -> None:
        """Move the grid to host time ``now`` towards what ``rows`` (link snapshot at ``now``) implies."""
        self.contributing = []
        self.evidence = {}
        self.baselines = {}
        scores: dict[str, float] = {}   # score of the links that can report now
        for row in rows:
            label = row["id"]
            if label not in self._falloff:
                continue
            baseline = self._update_baseline(label, now, row)
            self.baselines[label] = baseline
            if row["state"] not in CONTRIBUTING_STATES or row["score"] is None or baseline is None:
                continue
            scores[label] = row["score"]
        self.estimator_used = self._choose_estimator(scores)
        robust = self.evidence_mode == "robust_z" or (self.evidence_mode == "auto" and self.estimator_used == "rti")
        live: dict[str, float] = {}   # evidence of the links that can report now
        for label, score in scores.items():
            if robust:
                stats = self._baselines[label].log_stats()
                median_ln, mad_ln = stats if stats is not None else (None, None)
                evidence = robust_link_evidence(score, median_ln, mad_ln, self.z_floor, self.z_full, self.mad_floor)
            else:
                evidence = link_evidence(score, self.baselines[label], self.r_floor, self.r_full)
            self.evidence[label] = evidence
            live[label] = evidence
            if evidence > 0:
                self.contributing.append(label)
        self.busy_nodes = self._update_busy(now, live)
        field = self._rti_field(live) if self.estimator_used == "rti" else self._combine(live)
        target = 1.0 - np.exp(-self.sensitivity * field)
        if self._last_t is None or now > self._last_t:
            dt = 0.0 if self._last_t is None else now - self._last_t
            self._smooth(target, dt)
            self._last_t = now
        else:
            self._smooth(target, 0.0)

    def _combine(self, live: Mapping[str, float]) -> np.ndarray:
        """The field from the live links' evidence, before the soft saturation (see the module doc).

        Coverage counts only the links that can report now, so a silent or uncalibrated link neither
        dilutes nor vetoes the others. A busy node's links count with ``busy_link_weight`` of their weight.
        """
        cells = len(self._centres)
        busy = set(self.busy_nodes)
        coverage, weighted_evidence = np.zeros(cells), np.zeros(cells)
        parts = []
        for label, evidence in live.items():
            footprint = self._weighted[label]
            if self._ends[label] & busy:
                footprint = self.busy_link_weight * footprint
            coverage += footprint
            if evidence > 0:
                part = evidence * footprint
                weighted_evidence += part
                parts.append((label, part))
        if not parts:
            return np.zeros(cells)
        field = (1.0 - self.agreement) * weighted_evidence / (coverage + max(self._eps, _TINY))
        if self.agreement > 0 and len(parts) >= 2:
            field = field + self.agreement * self._crossing_agreement(parts)
        if self.min_coverage > 0:
            field = field * np.clip(coverage / self.min_coverage - 1.0, 0.0, 1.0)
        return field

    def projector(self) -> rti.RTIProjector:
        """The RTI weights and projections for the room's links over this grid (built on first use)."""
        if self._rti is None:
            segments = {LinkId(rx.name, tx.mac).label: ((rx.x, rx.y), (tx.x, tx.y)) for rx, tx in self._room.links()}
            cell_area = (self._room.width_m / self.cols) * (self._room.depth_m / self.rows)
            self._rti = rti.RTIProjector(self._centres, segments, cell_area, excess_m=self.rti_excess_m,
                                         delta_m=self.rti_delta_m, alpha=self.rti_alpha,
                                         min_coverage=self.min_coverage)
        return self._rti

    def _choose_estimator(self, scores: Mapping[str, float]) -> str:
        """The configured estimator; for "auto", RTI while ``rti_min_links`` links that reach the grid report."""
        if self.estimator != "auto":
            return self.estimator
        if len(scores) < self.rti_min_links:   # cheap test first: never builds the projector for a small rig
            return "backprojection"
        projector = self.projector()
        informative = sum(1 for label in scores
                          if projector.informative(label) and self._weights.get(label, 1.0) > 0)
        return "rti" if informative >= self.rti_min_links else "backprojection"

    def _rti_field(self, live: Mapping[str, float]) -> np.ndarray:
        """The RTI image of the live links' evidence (see csi/rti.py), before the soft saturation.

        A link's confidence is its room weight; a busy node's links keep ``busy_link_weight`` of both their
        confidence and their evidence, as the back-projection scales their whole footprint.
        """
        busy = set(self.busy_nodes)
        evidence: dict[str, float] = {}
        confidence: dict[str, float] = {}
        for label, value in live.items():
            scale = self.busy_link_weight if self._ends[label] & busy else 1.0
            evidence[label] = value * scale
            confidence[label] = self._weights.get(label, 1.0) * scale
        return self.projector().image(evidence, confidence)

    def _crossing_agreement(self, parts: Sequence[tuple[str, np.ndarray]]) -> np.ndarray:
        """Per cell, the largest geometric mean of two raised links' parts, over pairs that share no node.

        Quadratic in the number of raised links, which stays small.
        """
        best = np.zeros(len(self._centres))
        for i, (label_a, part_a) in enumerate(parts):
            for label_b, part_b in parts[i + 1:]:
                if not self._ends[label_a] & self._ends[label_b]:
                    np.maximum(best, np.sqrt(part_a * part_b), out=best)
        return best

    def _update_busy(self, now: float, live: Mapping[str, float]) -> list[str]:
        """Near-node veto (see the module doc): the nodes whose links rise together while the others stay quiet."""
        raised = {label for label, evidence in live.items() if evidence >= self.busy_evidence}
        if raised:
            for node in self._nodes:
                mine = [label for label in live if node in self._ends[label]]
                others = [label for label in live if node not in self._ends[label]]
                if len(mine) < BUSY_MIN_LINKS or not others:
                    continue
                if (sum(label in raised for label in mine) / len(mine) >= BUSY_SHARE
                        and sum(label in raised for label in others) / len(others) <= QUIET_SHARE):
                    self._busy_until[node] = now + self.busy_hold_s
        return sorted(node for node, until in self._busy_until.items() if now <= until)

    def _update_baseline(self, label: str, now: float, row: Mapping) -> Optional[float]:
        """Feed the link's quiet baseline with its newest calibrated score, once per packet time.

        Only EMPTY scores are learned. In MOTION the baseline freezes, for at most ``baseline_freeze_s``; a
        freeze that ends in time does not age the quiet samples out, and a longer one learns the new level.
        """
        score = row["score"]
        if row["state"] not in CONTRIBUTING_STATES or score is None:
            return self._baselines[label].value()
        sample_t = now - (row.get("age_s") or 0.0)
        if sample_t <= self._fed_until.get(label, -math.inf):
            return self._baselines[label].value()
        baseline = self._baselines[label]
        if row["state"] in BASELINE_STATES:
            since = self._frozen_since.pop(label, None)
            if since is not None and sample_t - since < self.baseline_freeze_s:
                baseline.shift(sample_t - since)
        elif sample_t - self._frozen_since.setdefault(label, sample_t) < self.baseline_freeze_s:
            return baseline.value()
        baseline.push(sample_t, score)
        self._fed_until[label] = sample_t
        return baseline.value()

    def _smooth(self, target: np.ndarray, dt: float) -> None:
        if dt <= 0:
            return
        tau = np.where(target > self.values, self.rise_tau, self.decay_tau)
        self.values = self.values + (target - self.values) * (1.0 - np.exp(-dt / tau))
        self.values = np.clip(self.values, 0.0, 1.0)
