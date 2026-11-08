"""Regularised radio tomographic imaging (RTI) over the heatmap grid (Wilson & Patwari).

Model: the evidence of the links that report now is ``y = W x + n``, where ``x`` is the motion image over the
heatmap cells (flat row-major, as csi.heatmap.cell_centres) and ``W`` (links x cells) says which cells a link
can see. The image is the regularised least-squares (MAP) estimate

    x_hat = Pi y,    Pi = (W^T V W + alpha C^-1)^-1 W^T V  =  C W^T (W C W^T + alpha V^-1)^-1

with an exponential spatial prior ``C_kl = exp(-dist_kl / delta)`` and ``V`` = diag(link confidence). With
every confidence 1 this is exactly the Wilson-Patwari ``Pi = (W^T W + alpha C^-1)^-1 W^T``. The second form
(the push-through identity) is what is computed: it inverts only a links x links matrix and never C, which is
badly conditioned on a fine grid. ``C W^T`` and ``W C W^T`` are computed once per room; ``Pi`` for the set of
links that report now, with their confidences, is a small solve, cached until that set changes (a link drops out
or comes back, or the near-node veto changes a confidence).

Link weights (ellipse model): a cell gets ``A / sqrt(d)`` when ``d_tx + d_rx < d + lambda`` (``d`` the link
length, at least ``min_link_m``; ``A`` the cell area, so ``W x`` approximates an integral and ``alpha`` does not
depend on the grid), else 0. ``lambda`` is the excess path length; RSS RTI uses a few cm (the first Fresnel zone),
but CSI-variance links respond well off their line (multipath: hw5 scored 6.6x quiet with nobody on the line,
and a bistatic fit gives the rig's 1.84-3.33 m links a detectable band of +-1.3-1.9 m). The default 0.3 m is the
excess-path decay length of the variance response (0.29 m in Wilson & Patwari's VRTI fit) and between the
Kaltiokallio fade-level ellipses (0.21 m at fade level 0 dB, 0.84 m in a deep fade). On this rig it is a +-0.55 m
(1.84 m wall link) to +-0.72 m (3.33 m diagonal) band at mid-link, where the response is at least ~1/e of its
on-line value; the farther, weaker tail is left to the spatial prior. In a stdlib study of the 6-link room
(response ``exp(-excess / 0.29 m)`` with and without a flat floor of 0.25, noise 0.1) it gave the lowest median
error (0.15 / 0.25 m) of lambda 0.1-0.6 m. Soft fallback: every link also gets a Gaussian of the cell's distance to
its segment (sigma half a cell diagonal, cut at 3 sigma), so a very short link, or a small ``lambda`` on a coarse
grid, still covers the cells it passes through.

``alpha`` is given relative to the mean per-link signal energy ``mean(diag(W C W^T))``. 0.2 was the best of
0.05-0.5 in the same study, and it keeps a link's confidence meaningful: a link with confidence 0.1 (the
near-node veto) paints about 0.3 of what it paints with confidence 1. Confidence is the room's link weight times
the veto factor; a link with confidence 0 is left out.

Display: the image is clipped at 0 and divided by ``reference``, the brightest cell one link at full evidence
can paint when every link reports (so 1.0 is one fully raised link alone; two crossing links reach more where
they cross), then masked to the cells some reporting link covers. csi.heatmap then applies its soft saturation
and smoothing exactly as for the back-projection.

Room footprint: ``mask`` marks the cells whose centres lie inside the room's outline (csi.room footprint). Only
those cells are unknowns: ``W``, the prior ``C`` and every solve are over them alone, so a cell outside has no
weight and no prior coupling, and nothing can be explained by heat in the notch of an L-shaped room or beyond a
wall. Outside cells are 0 in ``.weights``, in every projection and in the image. Without a mask (or with every
cell inside) every cell is an unknown, as before.

Public API:
    link_weight_row(centres, a, b, excess_m, cell_area, ...)    -> (cells,) ellipse weights of one link
    exponential_prior(centres, delta_m)                         -> (cells, cells)
    RTIProjector(centres, segments, cell_area, ..., mask=None)
        .projection(labels, confidences)   Pi for those links, (cells, len(labels)), cached; 0 rows outside the mask
        .image(evidence, confidence)       display field (cells,) >= 0, 1.0 = one fully raised link alone
        .informative(label)                the link's weights reach the grid (inside the mask)
        .weights                           W, (links, cells), rows in .labels order; 0 columns outside the mask
        .unknowns                          indices of the cells that are unknowns (all, or those inside the mask)
"""
from __future__ import annotations

import math
from typing import Mapping, Optional, Sequence

import numpy as np

from .room import point_segment_distance

EXCESS_M = 0.3          # lambda: ellipse excess path length, metres (see the module doc)
PRIOR_DELTA_M = 1.0     # delta: correlation distance of the exponential spatial prior, metres
ALPHA = 0.2             # Tikhonov strength, relative to the mean per-link signal energy
MIN_LINK_M = 0.5        # link length floor in the 1/sqrt(d) weight, so a very short link does not dominate
MIN_COVERAGE = 0.1      # cells covered by less than this (in confidence-weighted footprints) are blank
CACHE_SIZE = 64         # projections kept (one per link set and confidence pattern)
_TINY = 1e-12


def link_weight_row(centres: np.ndarray, a: Sequence[float], b: Sequence[float], excess_m: float,
                    cell_area: float, *, min_link_m: float = MIN_LINK_M,
                    soft_sigma_m: Optional[float] = None) -> np.ndarray:
    """One link's RTI weights over the cells: ``cell_area / sqrt(d)`` inside its ellipse, else 0.

    The ellipse has the link's ends as foci: ``d_a + d_b < d + excess_m``. ``soft_sigma_m`` adds the soft
    fallback, a Gaussian of the distance to the segment taken where it is larger, so the cells the link passes
    through always get weight.
    """
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    d = float(np.hypot(*(b - a)))
    d_a = np.hypot(centres[:, 0] - a[0], centres[:, 1] - a[1])
    d_b = np.hypot(centres[:, 0] - b[0], centres[:, 1] - b[1])
    shape = (d_a + d_b < d + excess_m).astype(float)
    if soft_sigma_m:
        dist = point_segment_distance(centres, a, b)
        soft = np.where(dist <= 3 * soft_sigma_m, np.exp(-dist ** 2 / (2 * soft_sigma_m ** 2)), 0.0)
        shape = np.maximum(shape, soft)
    return cell_area / math.sqrt(max(d, min_link_m)) * shape


def exponential_prior(centres: np.ndarray, delta_m: float) -> np.ndarray:
    """``C_kl = exp(-dist_kl / delta_m)`` between every pair of cell centres (unit variance)."""
    dx = centres[:, None, 0] - centres[None, :, 0]
    dy = centres[:, None, 1] - centres[None, :, 1]
    return np.exp(-np.hypot(dx, dy) / delta_m)


class RTIProjector:
    """Ellipse weights, prior products and cached projections for one room's links over one grid.

    ``segments`` maps a link label to its two end points (metres, the grid's frame; ends may be outside it).
    """

    def __init__(self, centres: np.ndarray, segments: Mapping[str, tuple[Sequence[float], Sequence[float]]],
                 cell_area: float, *, excess_m: float = EXCESS_M, delta_m: float = PRIOR_DELTA_M,
                 alpha: float = ALPHA, min_link_m: float = MIN_LINK_M, min_coverage: float = MIN_COVERAGE,
                 soft_sigma_m: Optional[float] = None, mask: Optional[Sequence[bool]] = None) -> None:
        for name, value in (("excess_m", excess_m), ("delta_m", delta_m), ("alpha", alpha),
                            ("cell_area", cell_area)):
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        self.centres = np.asarray(centres, dtype=float)
        self.labels = list(segments)
        self.excess_m, self.delta_m, self.alpha, self.min_coverage = excess_m, delta_m, alpha, min_coverage
        if soft_sigma_m is None:
            soft_sigma_m = 0.5 * math.sqrt(2 * cell_area)   # half the diagonal of a square cell of that area
        cells = len(self.centres)
        if mask is None:
            self.unknowns = np.arange(cells)
        else:
            inside = np.asarray(mask, dtype=bool)
            if inside.shape != (cells,):
                raise ValueError(f"mask needs one entry per cell ({cells}), got shape {inside.shape}")
            self.unknowns = np.flatnonzero(inside)
        # the room footprint leaves cells out: the fit runs over the unknowns only (no weight, no prior coupling)
        self._masked = len(self.unknowns) < cells
        inner = self.centres[self.unknowns] if self._masked else self.centres
        rows = [link_weight_row(inner, a, b, excess_m, cell_area, min_link_m=min_link_m,
                                soft_sigma_m=soft_sigma_m) for a, b in segments.values()]
        inner_weights = np.vstack(rows) if rows else np.zeros((0, len(inner)))
        if self._masked:
            self.weights = np.zeros((len(rows), cells))
            self.weights[:, self.unknowns] = inner_weights
        else:
            self.weights = inner_weights
        self._index = {label: i for i, label in enumerate(self.labels)}
        peak = self.weights.max(axis=1, initial=0.0)
        self._footprint = self.weights / np.where(peak > 0, peak, 1.0)[:, None]   # each link's shape, max 1
        prior = exponential_prior(inner, delta_m)
        self._cw = prior @ inner_weights.T           # C W^T, (unknowns, links)
        self._wcw = inner_weights @ self._cw         # W C W^T, (links, links), symmetric
        energy = np.diag(self._wcw)
        self._energy = energy
        positive = energy[energy > _TINY]
        self._alpha_abs = alpha * (float(np.mean(positive)) if len(positive) else 1.0)
        self._cache: dict[tuple, np.ndarray] = {}
        informative = [label for label in self.labels if self.informative(label)]
        self.reference = 1.0
        if informative:
            full = self.projection(informative, [1.0] * len(informative))
            brightest = float(full.max(initial=0.0))
            self.reference = brightest if brightest > _TINY else 1.0

    def informative(self, label: str) -> bool:
        """True when the link's weights reach at least one cell of the grid (inside the mask, with one)."""
        i = self._index.get(label)
        return i is not None and self._energy[i] > _TINY

    def projection(self, labels: Sequence[str], confidences: Sequence[float]) -> np.ndarray:
        """``Pi`` (cells x links) for these links with these confidences (all > 0), cached per pattern.

        Rows of cells outside the mask are 0: those cells are not unknowns.
        """
        key = (tuple(labels), tuple(round(float(v), 9) for v in confidences))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if any(not (v > 0) for v in confidences):
            raise ValueError("link confidences must be positive; leave a link out instead")
        idx = [self._index[label] for label in labels]
        system = self._wcw[np.ix_(idx, idx)] + np.diag(self._alpha_abs / np.asarray(confidences, dtype=float))
        pi = np.linalg.solve(system, self._cw[:, idx].T).T
        if self._masked:
            full = np.zeros((len(self.centres), len(idx)))
            full[self.unknowns] = pi
            pi = full
        if len(self._cache) >= CACHE_SIZE:
            self._cache.clear()
        self._cache[key] = pi
        return pi

    def image(self, evidence: Mapping[str, float], confidence: Mapping[str, float]) -> np.ndarray:
        """The display field from the reporting links' evidence (0..1) and confidences (> 0 to count).

        Clipped at 0, divided by ``reference`` and masked to the cells the counted links cover.
        """
        labels = sorted(label for label in evidence
                        if label in self._index and confidence.get(label, 0.0) > 0)
        cells = len(self.centres)
        if not labels:
            return np.zeros(cells)
        conf = [float(confidence[label]) for label in labels]
        y = np.array([float(evidence[label]) for label in labels])
        if not np.any(y > 0):
            return np.zeros(cells)
        field = np.clip(self.projection(labels, conf) @ y, 0.0, None) / self.reference
        if self.min_coverage > 0:
            idx = [self._index[label] for label in labels]
            coverage = np.asarray(conf) @ self._footprint[idx]
            field = field * np.clip(coverage / self.min_coverage - 1.0, 0.0, 1.0)
        return field
