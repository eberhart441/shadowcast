# 0010. Two noisy-room calibration methods, no winner declared

- **Status:** Experimental
- **Date:** 2026-11-07
- **Recorded in:** `f90cbcd` (noisy-room mode, the Calibrate button's default), `f3d1294` (noisy-room formula, `scripts/calibrate.py`), `5d44928` (hardware.md: unrecorded run settings removed)
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0002](0002-esp32-c5-receivers-only.md), [0009](0009-fixed-thresholds-by-default.md)

## Context

In real use the room is never empty. The quiet-room rules (`csi/detect.py`, `calibrate_clips`) drop every 1 s chunk that looks like movement and refuse a link with under 8 s or under half its clip time clean, so in a busy room links stay UNCALIBRATED. Two answers were built the same day, and they disagree.

## Decision

Ship both methods, on the running dashboard's HTTP API, and declare neither the better one until it is checked against labelled motion.

1. **Noisy-room mode**, the Calibrate button's default (`POST /api/calibrate` with `noisy: true`): a 5 s countdown, 3 clips of 10 s, then threshold = margin × p97 over every window. Movement never causes a refusal; the result warns how many seconds looked like movement. (`csi/detect.py`, `csi/live_calibration.py`)
2. **Noisy-room formula** (`scripts/calibrate.py`, `csi/noisy_calibration.py`): poll `GET /api/state` about 4 times a second for 45 s, then threshold = max(median + 6 × 1.4826 × lower-half spread, 3 × median) per link. It refuses a link with under half its readings usable or under 40 usable scores, and changes nothing without `--apply`.

The quiet-room rules stay available (`noisy: false`, and always in the terminal tools).

## Alternatives considered

- **Quiet-room rules only.** Rejected as the dashboard default: in a busy room they leave links without thresholds, which leaves them unable to report motion ([0001](0001-a-miss-costs-more-than-a-false-alarm.md)).
- **Declare one method the winner now.** Deferred: the only head-to-head is one link on one run (Evidence).

## Consequences

- Two methods can give a link very different thresholds; the operator chooses, and neither replaces watching each link's score during a walk.
- The formula needs no countdown, so it can sample a room that cannot be cleared, and it reports whether each link had enough usable readings. The button records fixed clips after a countdown and reports skipped links with reasons.
- Each method is described as having worked on the real rig in its own source (the p97 comment in `csi/detect.py`; README "Setting thresholds from live data"). Neither claim was checked against labelled motion.
- **What would settle it:** a labelled recording of the four-link rig with empty and walking segments, scored per link under both thresholds.

## Evidence

- **The one comparison** (`docs/agents/hardware.md`, "Evidence, 2026-11-07"): on link `rx2<-00` the Calibrate button gave 50.2 and the formula 137.0.
- **Its limits:** one link, one run. The button's settings for that run (clips, margin, and whether `noisy` was on) were not recorded, so 50.2 may come from noisy-room mode or from the quiet-room rules. An earlier draft of hardware.md stated settings that had been inferred, not recorded; `5d44928` removed them. `rx2` was an ESP32-S3 receiver at the time (between `33dbc43` and `7cc6184`), so the comparison was made on hardware no longer in the rig ([0002](0002-esp32-c5-receivers-only.md)).
- **Not measured:** which threshold catches more walking at what false-alarm cost.
