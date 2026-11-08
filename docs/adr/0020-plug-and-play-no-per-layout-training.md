# 0020. Plug and play: no per-layout training

- **Status:** Accepted
- **Date:** 2026-11-08
- **Recorded in:** `ac4f0f5` (reverts `90d87f9`)
- **Related:** [0011](0011-heatmap-evidence-from-quiet-baseline.md), [0014](0014-room-model-as-config.md)

## Context

The rig gets moved, and its boards restart, lose power and get replaced: on 2026-11-08 alone a sender moved and two boards were replaced. Setting it up in a new room should take the room geometry and a calibration, nothing more.

## Decision

Locating motion uses only the room geometry in `config/room.json` ([0014](0014-room-model-as-config.md)) and the live link scores: the heatmap ([0011](0011-heatmap-evidence-from-quiet-baseline.md)) and the Kalman motion track that follows its hot blob (`csi/tracker.py`). Nothing is trained per layout: no walk-through, no reference recording, no per-room accuracy map. Moving, restarting or replacing a board needs at most a recalibration.

## Alternatives considered

- **A person locator trained by walking to marked points** (`90d87f9`): an empty-room reference plus an accuracy map from a guided walk-through. Built, run live, and reverted (`ac4f0f5`).

## Consequences

- The dot shows where change is, drawn towards the link lines and their crossings; it is not a calibrated position.
- The accuracy-run tooling (`scripts/accuracy_run.py`, `scripts/accuracy_eval.py`) stays, for measuring, not for training.

## Evidence

Live, the trained locator's empty-room reference went stale whenever boards moved, restarted or lost power, and the team judged it worse than the Kalman track. On a replay of its own walk-through it was about 0.8 m off in the 2.77 x 1.84 m room. Its live error was not measured.
