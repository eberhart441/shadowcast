# 0018. Evaluate by recording, never by random window

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** issue #1 ("Evaluation"), `d4d3994` ("Add offline evaluation with per-label confusion matrices (#7)")
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0007](0007-amplitude-only-motion-score.md), [0008](0008-motion-only-by-default.md)

## Context

The team needed "an honest accuracy number that isn't inflated by leakage between training and test data" (issue #1). The detector emits a decision per packet, each over its trailing 1 s window, so consecutive decisions share almost all their data. A random split by window would put near-copies of every test window into the calibration data.

## Decision

- For each session, the first `empty` recording by start time calibrates the threshold. Every other recording is replayed through the same detector the live tools use (`detect_packets`), and each decision is compared with the label of its file.
- Splits are by recording or session, never by random window (`csi/evaluate.py`).
- Results are per-label confusion matrices. Without `--breathing`, the still labels are marked "breathing disabled" rather than scored 0%.

## Alternatives considered

- **Random window splits.** Rejected in issue #1 ("No random window splitting anywhere"): they leak.

## Consequences

- A session with only one empty recording scores no empty windows, because that recording did the calibrating (hw3).
- The numbers are few and honest: two single-link sessions, and nothing for the four-link rig until it has a labelled recording.
- Quoted figures are re-run on the current code, not copied: the data branch's README reports 95% EMPTY for hw5, and the main README quotes the re-run's 94% at the default margin.

## Evidence

- **hw5** (C5 receiver, S3 sender, about 2 m, gain locked): walking read as MOTION in 100% of 2621 windows; the empty validation recording read EMPTY in 94% at margin 1.25 and 95% at 1.5 (README "Results and known limits").
- **hw3** (same boards, gain not yet locked): walking 80% MOTION; no empty windows scored.
- **Not available:** any accuracy figure for the four-link rig. No labelled recording of it is committed.
