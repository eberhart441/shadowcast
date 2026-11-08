# 0001. A missed person costs more than a false alarm

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `60067fa` (README "Intended use and limits"), `d836c52` (`docs/agents/project-context.md`)
- **Related:** [0008](0008-motion-only-by-default.md), [0009](0009-fixed-thresholds-by-default.md), [0010](0010-two-noisy-room-calibration-methods.md)

## Context

The system is meant for firefighters and special-operations teams deciding whether people are in an area (`docs/agents/project-context.md`, "What this is for"). For them the two errors are not symmetric: a missed person can cost a life, a false alarm costs a second look. Most of the tuning in this system (the calibration margin and percentile, fixed against adaptive detection, the debounce) trades one error for the other, so the direction has to be settled once instead of argued again in every change.

## Decision

- When a trade-off sets fewer false alarms against fewer misses, choose fewer misses, and state in the change which way it went and why. Do not default to quieter output.
- The output is decision support. The README says so in its "Intended use and limits": the system senses movement, it is not a life-detection device, and it is never the only check that a room is empty.
- Limits that cause misses are stated where users read them, not left in the code: a still person fades after about 30 s, breathing is unreliable, and the map is for one moving person at zone level.

## Alternatives considered

- **Tune for the fewest false alarms**, the natural target for a demo that should look clean. Rejected: it optimises the cheap error.
- **A single accuracy figure as the tuning target.** Not adopted: evaluation reports a confusion matrix per label (`csi/evaluate.py`), so each error rate stays visible on its own.

## Consequences

Where the rule shows in the code and defaults:

- **The debounce is asymmetric.** MOTION takes effect on the first window above threshold; leaving MOTION needs the lower state to hold for 0.5 s (`csi/detect.py`, `_Debouncer`). The behaviour came from issue #1 ("MOTION overrides"); this ADR is the reason it stays.
- **The default margin, 1.25, sits where walking detection stayed at 100%** while empty-room false alarms fell (Evidence).
- **Fixed thresholds stay the default** over the adaptive mode, which detects walking less sensitively ([0009](0009-fixed-thresholds-by-default.md)).
- **The dashboard's Calibrate button defaults to noisy-room mode**, which never refuses a link for movement, so a link in a room that is never empty gets a threshold instead of staying UNCALIBRATED ([0010](0010-two-noisy-room-calibration-methods.md)).
- **One stated exception:** breathing (STILL_PRESENT) is off by default even though that leaves a still person undetected, because at a 43% false-fire rate on empty windows it carries no information ([0008](0008-motion-only-by-default.md)). The miss risk moves into the product wording instead.
- **Open tension, not yet resolved.** `status.room` (`csi/adaptive.py`, `RoomDecision`) needs two links elevated together, or one strongly elevated link, which only auto mode can produce. It is computed in fixed mode too. The default page shows per-link states only, but the optional new UI (`web/ui-overhaul/index.html`) puts `status.room` in its headline, so on the current server one link in MOTION reads CLEAR there. That contradicts this ADR.

## Evidence

- **Margin sweep, hw5 single-link session** (README "Calibration margin"; commit `fce0345`). Share of empty windows read as MOTION: 14% at margin 1.0, 11% at 1.1, 6% at 1.25, 5% at 1.5. Walking read as MOTION 100% at every margin.
- **Why 1.25 rather than 1.5** (commit `fce0345`): at 1.25, held-out simulated empty clips and a simulated still, breathing person both read 0% MOTION, against 39% and 68% at margin 1.0. Going to 1.5 bought one more point of quiet on hw5, and the README notes that higher margins may miss weak motion. No recording measures weak motion, so that cost is unquantified.
- **Never measured:** the miss rate on the four-link rig. No labelled recording of the rig exists (README "Results and known limits"), so no figure says how often a person in the room goes undetected.
