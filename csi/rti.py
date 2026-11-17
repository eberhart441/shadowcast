"""Regularised radio tomography: image every link's evidence over the room with one precomputed matrix.

Each link is a straight line between its two nodes. Motion at a point changes the link when the point lies in
an ellipse with the nodes as foci, the points whose path via the point is at most ``excess_m`` longer than the
direct path. A link's weight for a cell is 1/sqrt(link length) inside that ellipse and 0 outside: Wilson and
Patwari's normalised elliptical model (VRTI eq. 14: of the length normalisations they tried, dividing by the
root gave the best images). The links' evidence y (one number per link, ADR-0011) is modelled as y = W x + noise, where
x is the motion image over the cells, and x is estimated by the MAP / Tikhonov solution with a Gaussian prior
whose covariance falls off exponentially with the distance between cells (RTI eq. 12-15):

    x = Pi y,   Pi = (W'W + r Cx^-1)^-1 W' = Cx W' (W Cx W' + r I)^-1,   [Cx]kl = exp(-d_kl / corr_m)

with r the noise-to-prior variance ratio. The second form (the matrix inversion lemma) solves an M x M system
(M links) and never inverts Cx, which is ill-conditioned for a smooth prior. Pi depends only on the room's
geometry, so it is computed once per layout and cached on that geometry: a changed ``config/room.json`` gives a
new key and a new matrix. Nothing is trained or calibrated per layout (ADR-0020).

The image is scaled so that 1 is what one person would give whose every covering link is at full evidence:
``reference`` is the median over covered cells of the image of that ideal pattern at that cell.

Sources: J. Wilson and N. Patwari, "Through-Wall Motion Tracking Using Variance-Based Radio Tomography
Networks", arXiv:0909.5417 (2009), https://arxiv.org/abs/0909.5417 (VRTI: eq. 14 weights, eq. 19-21 Tikhonov,
section IV Kalman tracking on the image's maximum); J. Wilson and N. Patwari, "Radio Tomographic Imaging with
Wireless Networks", University of Utah tech report (2008), https://span.ece.utah.edu/uploads/RTI.pdf (eq. 12-15:
MAP projection with an exponential spatial prior).

Public API:
    ellipse_weights(centres, ends, excess_m, min_cover_m) -> W (links, cells)
    projection(W, centres, corr_m, noise_ratio)            -> Pi (cells, links)
    RtiImager(centres, ends, excess_m, corr_m, noise_ratio)  cached per geometry; see ``imager``
        .image(y)        y (links,) -> (cells,) image, 1 = one person at full evidence, may be negative
        .weights, .projection, .reference
    imager(centres, ends, ...) -> RtiImager, the cached one for this geometry and these parameters
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

# Chosen on the simulated rig (tests/test_heatmap_rti.py, 8 and 30 nodes in 4 x 3 m, 0.14 x 0.17 m cells), not on
# a real one: re-check them on the 8-node labelled walk (#20). Excess path 0.2 m is wider than the papers' (RTI
# 0.02 m on 0.6 m pixels): a person's body reaches a link from a few tenths of a metre off its line, and with
# thinner ellipses the 30-node image broke up into speckle around the person (peak error 0.45 m, not 0.12 m).
EXCESS_PATH_M = 0.2   # ellipse: path via the cell at most this much longer than the direct path
CORR_M = 1.0          # prior correlation distance between cells (image smoothness)
NOISE_RATIO = 30.0    # measurement noise variance / prior pixel variance (more = smoother, weaker image)


def ellipse_weights(centres: np.ndarray, ends: np.ndarray, excess_m: float = EXCESS_PATH_M,
                    min_cover_m: float = 0.0) -> np.ndarray:
    """(links, cells) weights: 1/sqrt(length) for cells inside a link's ellipse, else 0.

    ``ends`` is (links, 2, 2): each link's two node positions. A cell within ``min_cover_m`` of the segment also
    counts as inside, so a link shorter than a cell still covers the cells it runs through.
    """
    centres = np.asarray(centres, dtype=float)
    ends = np.asarray(ends, dtype=float)
    a, b = ends[:, 0, :], ends[:, 1, :]
    length = np.linalg.norm(b - a, axis=1)
    via = (np.linalg.norm(centres[None, :, :] - a[:, None, :], axis=2)
           + np.linalg.norm(centres[None, :, :] - b[:, None, :], axis=2))
    inside = via <= length[:, None] + excess_m
    if min_cover_m > 0:
        inside |= _segment_distance(centres, a, b) <= min_cover_m
    return inside / np.sqrt(np.maximum(length, 1e-6))[:, None]


def projection(weights: np.ndarray, centres: np.ndarray, corr_m: float = CORR_M,
               noise_ratio: float = NOISE_RATIO) -> np.ndarray:
    """(cells, links) Pi = Cx W' (W Cx W' + r I)^-1, the regularised inverse of ``weights``."""
    centres = np.asarray(centres, dtype=float)
    d = np.linalg.norm(centres[:, None, :] - centres[None, :, :], axis=2)
    cx = np.exp(-d / corr_m)
    cxwt = cx @ weights.T                                    # (cells, links)
    gram = weights @ cxwt + noise_ratio * np.eye(len(weights))
    return np.linalg.solve(gram, cxwt.T).T                   # gram is symmetric


class RtiImager:
    """The weights, projection and scale for one layout. Build with ``imager`` to share it across states."""

    def __init__(self, centres: np.ndarray, ends: np.ndarray, excess_m: float = EXCESS_PATH_M,
                 corr_m: float = CORR_M, noise_ratio: float = NOISE_RATIO) -> None:
        centres = np.asarray(centres, dtype=float)
        cell_m = _cell_size(centres)
        self.weights = ellipse_weights(centres, ends, excess_m, min_cover_m=cell_m / 2)
        self.projection = projection(self.weights, centres, corr_m, noise_ratio) if len(ends) else \
            np.zeros((len(centres), 0))
        covered = self.weights > 0                               # (links, cells)
        ideal = (self.projection * covered.T).sum(axis=1)        # image at cell c of "every link over c at 1"
        on = covered.any(axis=0)
        self.reference = float(np.median(ideal[on])) if on.any() else 1.0
        if not self.reference > 0:
            self.reference = 1.0

    def image(self, y: np.ndarray) -> np.ndarray:
        """The motion image of per-link evidence ``y`` (link order as ``ends``), 1 = one person at full evidence."""
        return (self.projection @ np.asarray(y, dtype=float)) / self.reference


def imager(centres: np.ndarray, ends: np.ndarray, excess_m: float = EXCESS_PATH_M, corr_m: float = CORR_M,
           noise_ratio: float = NOISE_RATIO) -> RtiImager:
    """The RtiImager for this geometry, built once and cached (a few layouts at most per process)."""
    key_c = tuple(map(tuple, np.round(np.asarray(centres, dtype=float), 9)))
    key_e = tuple(tuple(map(tuple, e)) for e in np.round(np.asarray(ends, dtype=float), 9))
    return _cached(key_c, key_e, float(excess_m), float(corr_m), float(noise_ratio))


@lru_cache(maxsize=8)
def _cached(centres: tuple, ends: tuple, excess_m: float, corr_m: float, noise_ratio: float) -> RtiImager:
    return RtiImager(np.array(centres), np.array(ends).reshape(-1, 2, 2), excess_m, corr_m, noise_ratio)


def _segment_distance(points: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(links, points) distance from each point to each segment a-b."""
    ab = b - a                                                      # (L, 2)
    ap = points[None, :, :] - a[:, None, :]                         # (L, P, 2)
    denom = np.maximum((ab * ab).sum(axis=1), 1e-12)[:, None]
    u = np.clip((ap * ab[:, None, :]).sum(axis=2) / denom, 0.0, 1.0)
    return np.linalg.norm(ap - u[:, :, None] * ab[:, None, :], axis=2)


def _cell_size(centres: np.ndarray) -> float:
    """The grid's larger cell side, from the spacing of distinct cell centres."""
    sides = []
    for axis in (0, 1):
        v = np.unique(np.round(centres[:, axis], 9))
        if len(v) > 1:
            sides.append(float(np.min(np.diff(v))))
    return max(sides) if sides else 0.0
