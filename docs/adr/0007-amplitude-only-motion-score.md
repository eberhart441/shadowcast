# 0007. Motion score from CSI amplitude only, with the breathing band removed

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** `2cc5131` (parser: amplitude from I/Q), `8565d3b` (motion score, issue #3), `2a8c064` ("Ignore breathing band in motion score")
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0005](0005-vendor-the-firmware.md), [0008](0008-motion-only-by-default.md), [0009](0009-fixed-thresholds-by-default.md)

## Context

Each CSI_DATA line carries one signed 8-bit I/Q pair per subcarrier. Each link needs one number that rises with gross movement and stays flat for an empty room and for a person standing still and breathing; breathing has its own, separate path ([0008](0008-motion-only-by-default.md)). The brief was plain signal processing: machine-learning classifiers were out of scope (issue #1).

## Decision

- **Feature: amplitude only.** Per subcarrier, `hypot(I, Q)`; phase is discarded when the line is parsed. Null subcarriers are dropped (on the ESP32-C5, 3 of 117).
- **Score: high-passed amplitude variance.** Per subcarrier, three cascaded high-pass stages (subtract an exponential running mean, time constant 0.16 s, in device time) remove components below about 1 Hz. The score is the variance of the residual over the last 1 s of device time, averaged across subcarriers. MOTION when the score exceeds the link's threshold.
- **Receivers lock their gain** ([0005](0005-vendor-the-firmware.md)), because amplitude moves with gain.

## Alternatives considered

- **Phase, or the full complex CSI.** Not used. No phase experiment was run in this repository and nothing was measured either way. Amplitude needs no per-packet phase correction (general background, not tested here) and does not depend on the I/Q order the firmware prints (`csi/parser.py`).
- **Raw amplitude variance, without the high-pass** (the first version, issue #3). Tried and rejected: breathing lifted a still person's score above a threshold calibrated on an empty room, so still people read MOTION.
- **A learned classifier.** Out of scope in issue #1 ("plain signal processing for now"). It stays on the roadmap as a comparison against this detector on held-out sessions, which first needs a labelled recording of the four-link rig.

## Consequences

- Amplitude depends on receiver gain, so a receiver reset re-baselines its links and stales their thresholds; `csi/adaptive.py` notes scores then move by 3 to 5 times.
- Each link has its own score scale, hence per-link thresholds ([0009](0009-fixed-thresholds-by-default.md)) and per-link heatmap baselines ([0011](0011-heatmap-evidence-from-quiet-baseline.md)).
- By design the motion score cannot see a still person. Stillness is the experimental breathing path's job, and that path is off by default ([0008](0008-motion-only-by-default.md)).
- Windows run on device time: drops do not stretch a window, and a receiver reboot restarts it (`csi/clock.py`).

## Evidence

- **Before and after the high-pass** (`2a8c064`):
  - Simulator: still-person p99 0.131 against empty p99 0.131, where it had been 0.336 against 0.165.
  - hw3: walking median 4.5 against empty p99 3.2, a 1.41× separation where it had been 1.26×; walking p95 10.2, down from 41.7; the recalibrated threshold went from 18.6 to 4.8.
- **Attenuation of the high-pass** (design figure in `csi/detect.py`): about 21 dB at 0.5 Hz and 32 dB at 0.3 Hz.
- **Current code on hw5:** walking read as MOTION in 100% of 2621 windows; the empty validation recording read EMPTY in 94% at margin 1.25 (README "Results and known limits").
- **Not measured:** whether phase would separate motion from empty better.
