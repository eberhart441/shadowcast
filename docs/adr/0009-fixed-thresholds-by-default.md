# 0009. Fixed per-link thresholds by default; adaptive mode is experimental

- **Status:** Accepted (`--auto-threshold` is Experimental) for the line pipeline; superseded by [0027](0027-adaptive-baseline-only-and-a-room-state-from-zones.md) for the transceiver pipeline
- **Date:** 2026-11-07
- **Recorded in:** `bd1cb61` ("Fixed thresholds are the default; --auto-threshold is experimental"), after `f90cbcd` had briefly made auto mode the default
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0007](0007-amplitude-only-motion-score.md), [0010](0010-two-noisy-room-calibration-methods.md)

## Context

Each link has its own score scale ([0007](0007-amplitude-only-motion-score.md)), so one global threshold cannot serve all links. A fixed per-link threshold goes stale when a receiver re-baselines, and is hard to set in a room that is never empty. An adaptive mode avoids both by learning each link's normal level as it runs, at the cost of reacting more slowly.

## Decision

- Links are judged against fixed per-link thresholds by default: from a `--thresholds` file, a dashboard calibration, or a Set.
- A link without one is UNCALIBRATED. The terminal tools refuse to start on a real port without thresholds rather than guess.
- `--auto-threshold` is experimental. Each link is judged against its own adaptive baseline: the median of its score over 75 s of packet time, with a robust spread; it is elevated once its score stays at or above max(baseline + 5.5 × spread, 2 × baseline) for 0.6 s. A Set on a link makes it fixed again.

## Alternatives considered

- **Auto mode as the default.** Shipped at 15:55 (`f90cbcd`) and reverted 8 minutes later (`bd1cb61`): it detected walking less sensitively. Its enter level is at least twice the baseline and must hold for 0.6 s, and its room decision needs two links to agree.
- **One global default threshold.** Removed earlier the same day: the old default of 2.0 "was on the wrong score scale" (`eb5f155`).

## Consequences

- Calibration is an operator step, and it must come after the last receiver reset; afterwards the dashboard must stay running (`docs/agents/hardware.md`).
- UNCALIBRATED is a visible state, never a silent guess.
- In auto mode a person who stays still for about 25 s is absorbed into the baseline (README "Dashboard options"). Fixed-mode link states do not absorb stillness; the heatmap does, after about 30 s ([0011](0011-heatmap-evidence-from-quiet-baseline.md)).
- The roadmap makes auto mode the default once its walking sensitivity improves.

## Evidence

- **The revert:** `f90cbcd` (15:55, "Auto mode (the default without a thresholds file)") and `bd1cb61` (16:03).
- **Score shift on re-baseline:** 3 to 5 times (`csi/adaptive.py` module docstring). No recording of a re-baseline is committed.
- **Not recorded:** walking-sensitivity figures for auto mode. The code cites a report that is not in the repository (`csi/adaptive.py`, the `EXCLUDE_HIGH` comment), so the reason for the revert rests on observation, not a committed measurement.
