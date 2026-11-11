# 0023. The transceiver heatmap is regularised radio tomography over every link

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** issue #10 (eberhart441/shadowcast), spec #2 ("Detection, room state, heatmap, track")
- **Extends:** [0011](0011-heatmap-evidence-from-quiet-baseline.md) (its evidence is the input, unchanged)
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0014](0014-room-model-as-config.md), [0017](0017-tests-pinned-to-frozen-fixture.md), [0020](0020-plug-and-play-no-per-layout-training.md)

## Context

The hackathon heatmap spreads each link's evidence over a Gaussian band around its line and combines the bands
per cell as half the sum plus half the geometric mean of the two strongest. At four links that was enough. At
30 nodes (870 links) it is not: every link that sees a little change paints its whole line, the strongest two
links per cell decide everything, and extra links add nothing. On the simulated walker in the 30-node test room
its hottest cell was a median 1.1 m from the walker, and the map was saturated along most lines (heat 1.0).
The spec asks for a heatmap that sharpens as links are added and keeps ADR-0011's evidence.

## Decision

- In a room with transceivers, the heatmap is an image x over the 28 x 18 grid estimated from the vector y of
  every link's evidence (ADR-0011, times its link weight) with one matrix: x = Pi y.
- Weight model: a link's weight for a cell is 1/sqrt(link length) when the path from one node via the cell to
  the other is at most `EXCESS_PATH_M` = 0.2 m longer than the direct path (an ellipse with the nodes as foci),
  else 0, with cells within half a cell of the line always included. This is the normalised elliptical model of
  Wilson and Patwari's variance-based radio tomography (VRTI, eq. 14).
- Inversion: the MAP / Tikhonov solution with a Gaussian prior whose correlation falls off as
  exp(-distance / `CORR_M`), `CORR_M` = 0.5 m, and a noise-to-prior variance ratio `NOISE_RATIO` = 10:
  Pi = Cx W' (W Cx W' + r I)^-1, the same estimator as RTI eq. 13 written so that only an M x M system is
  solved and Cx is never inverted. Negative image values are clipped to 0.
- Pi depends only on the room geometry, the grid and these three parameters. It is computed when the heatmap
  is built and cached on exactly that geometry, so a changed `config/room.json` gives a new matrix on the next
  start, with nothing measured, walked or trained (ADR-0020).
- The image is scaled so that 1 is one person whose every covering link is at full evidence (the median over
  covered cells of the image of that pattern), then goes through the same soft saturation and time smoothing as
  before. The Kalman track reads it unchanged (`csi/tracker.py`).
- The top-2 agreement rule and the Gaussian footprint stay for rooms with only receivers and senders (the
  hackathon line pipeline and its recordings); `falloff` is refused on a transceiver room.

## Alternatives considered

- **Keep the footprint and agreement rule at 870 links.** Measured on the simulator and rejected: median error
  1.1 m at 30 nodes against 0.2 m at 8, and a saturated map.
- **Tikhonov with difference operators** (VRTI eq. 21). Equivalent in spirit; the exponential prior was chosen
  because its one length scale is easy to state and the papers' parameter tables use it.
- **Thin ellipses as in the papers** (RTI 0.02 m excess path on 0.6 m pixels). On our 0.14 m cells the 30-node
  image broke into speckle around the person and its peak sat 0.45 m off; 0.2 m gave 0.12 m in the same run.
- **A trained or calibrated weight model** (measured per room, or a walk-through): ruled out by ADR-0020.

## Consequences

- More links now sharpen the map: on the simulated walker the hottest cell's worst error over the walk falls
  from about 0.7 m at 8 nodes to about 0.3 m at 30 (tests/test_heatmap_rti.py). Two people far apart show as two
  hot spots at 30 nodes; at 8 nodes the two ghost crossings of their links are almost as hot as they are.
- The parameters were chosen on the simulator, whose link response (a Gaussian of 0.25 m around the line) is
  itself a guess. They must be checked on the 8-node labelled walk (#20) before any accuracy is claimed.
- The heat level differs from the footprint map: one walker gives about 0.6 at 8 nodes and 0.35 at 30, so the
  tracker's `MIN_HEAT` 0.1 still applies but the page's colour scale reads cooler at 30 nodes.
- Cost: building Pi for 870 links and 504 cells takes about 0.7 s once per layout; applying it is about 0.1 ms
  per heatmap step. Per-link baselines dominate a step (about 6 ms at 870 links); the quiet baseline keeps its
  window sorted for that.
- Still only change, not presence (ADR-0011); still decision support (ADR-0001).

## Evidence

- Sources: J. Wilson and N. Patwari, "Through-Wall Motion Tracking Using Variance-Based Radio Tomography
  Networks", arXiv:0909.5417 (2009), https://arxiv.org/abs/0909.5417 (eq. 14 weights, eq. 19-21 Tikhonov,
  section IV Kalman on the image maximum; 34 nodes, about 3 ft average tracking error through walls);
  J. Wilson and N. Patwari, "Radio Tomographic Imaging with Wireless Networks", University of Utah technical
  report (2008), https://span.ece.utah.edu/uploads/RTI.pdf (eq. 12-15, Table II).
- Simulated, frozen 4 x 3 m rooms with 8 and 30 nodes (`tests/fixtures/room_tr8.json`, `room_tr30.json`):
  `tests/test_heatmap_rti.py`. Not measured: any error on a real rig.
