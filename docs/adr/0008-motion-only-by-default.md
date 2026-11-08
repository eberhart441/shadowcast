# 0008. Motion-only by default; breathing is opt-in

- **Status:** Accepted (the breathing detector itself is Experimental)
- **Date:** 2026-11-07
- **Recorded in:** `eb5f155` ("Make motion-only the default; breathing is opt-in")
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0007](0007-amplitude-only-motion-score.md)

## Context

Issue #1 specified three states, with STILL_PRESENT for a person who is not moving but is breathing. The detector (issue #5, `csi/breathing.py`) band-limits a 15 s window to 0.1 to 0.5 Hz, takes the first principal component of the strongest subcarriers, finds the spectral peak, and declares a rate when the top two components carry at least 85% of the in-band variance. It works on the simulator. On real hardware it fired on empty rooms.

## Decision

- Detection emits EMPTY and MOTION only by default, and breathing is not computed at all.
- `--breathing` turns it on in `scripts/detect_live.py`, `scripts/telemetry.py` and `scripts/evaluate.py`; MOTION still overrides it.
- The dashboard never computes breathing: its per-link monitor classifies without it (`csi/links.py`), so STILL_PRESENT never appears there.
- The evaluator marks the still labels "breathing disabled" instead of scoring them 0%.

## Alternatives considered

- **Breathing on by default**, as issue #5 specified. Rejected: 43% of empty windows read STILL_PRESENT, so the state carries no information.
- **Remove breathing entirely.** Not chosen: kept behind a flag so it can be measured and improved, and so the evaluator can still score the still labels.

## Consequences

- A person who is completely still is not reliably detected. The README says so in "Intended use and limits"; this is the one place where [0001](0001-a-miss-costs-more-than-a-false-alarm.md) accepts more misses, because the alternative was alarms that mean nothing.
- The motion score already strips the breathing band ([0007](0007-amplitude-only-motion-score.md)), and the heatmap absorbs a still person after about 30 s, so nothing on the dashboard represents stillness.
- "Detecting still people" stays on the roadmap.

## Evidence

- **hw5 with `--breathing`:** 1305 of 3065 empty windows (43%) read STILL_PRESENT (README "Breathing"). The commit that made the change quoted "about 44%" (`eb5f155`); the README figure is the exact count.
- **Simulator:** after the high-pass change a simulated still person reads STILL_PRESENT at about 18 breaths per minute (`2a8c064`). Synthetic success, hardware failure.
- **Not measured:** breathing on the four-link rig. No still-state recording of the rig exists (README "Little data").
