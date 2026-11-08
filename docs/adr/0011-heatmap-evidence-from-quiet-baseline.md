# 0011. Heatmap evidence from each link's quiet baseline, not its threshold

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `7a48835` ("Grade heatmap evidence against each link's own quiet baseline"), `c920847` (sensitivity 1.3, decay 0.3 s)
- **Related:** [0007](0007-amplitude-only-motion-score.md), [0009](0009-fixed-thresholds-by-default.md), [0014](0014-room-model-as-config.md)

## Context

The heatmap spreads each link's evidence of motion along the line between its sender and receiver, a simple radio-tomography estimate. Issue #9 specified the evidence as each link's score normalised by its own threshold. On the real rig that produced almost no heat: a walking person lifts a link to well below its threshold.

## Decision

- Each link is graded against its own **quiet baseline**: the median of its score over the last 60 s of host time, used once 10 s of history exist.
- **Evidence** is log(score / baseline) mapped to 0..1: a ratio of 1.2 or less gives 0, a ratio of 3 gives 1.
- Evidence × link weight is spread around the sender-receiver segment with a Gaussian falloff (0.35 m). Per cell, links combine as (1 − 0.5) × sum + 0.5 × √(strongest × second strongest), so links that agree are hotter than one link alone. The result is soft-saturated (1 − e^(−1.3x)) and smoothed with 0.3 s rise and decay on a 28 × 18 grid.
- Only EMPTY and MOTION links contribute. The threshold is shown on the page but does not decide heat.

## Alternatives considered

- **Threshold-normalised evidence** (issue #9's specification). Built and rejected: almost always zero on real links.
- **A low-percentile baseline** (the 20th percentile). Rejected: on a noisy link it sits at the bottom of the link's own empty-room noise, so every spike looks like full evidence (`csi/heatmap.py` module docstring).

## Consequences

- The map and the link states can disagree: a link below its threshold (EMPTY) can still put heat on the map. That is intended; the map shows change, the states show threshold crossings.
- A person who stays still for more than about half the baseline window (about 30 s) is absorbed and fades. The page caption and the README say the map shows change, not presence.
- No heat for the first 10 s of a link's history, so there is no flash at start.
- Link weights scale a link's evidence. The only one set (`rx1<-01`: 0.3) predates `rx1` becoming an ESP32-C5 and has not been re-measured.
- It is meaningful for one moving person at zone level; it cannot count or separate people.

## Evidence

- **Why threshold-relative failed:** a walker lifts a real link to only about 0.6 to 0.8 of its threshold (`csi/heatmap.py`; `7a48835` gives 0.65 to 0.8).
- **Observed on the rig:** a continuous walk reliably lights the left-wall and C5-diagonal links and the heatmap; the two `rx1` links respond more weakly (README "Results and known limits").
- **Not measured:** localisation accuracy. No labelled recording of the four-link rig with known positions exists.
