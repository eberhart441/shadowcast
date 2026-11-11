# 0021. On-node score: streaming window statistics, and a log-scale score code

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** eberhart441/shadowcast issue #12 (spec #2), branch `ticket/12-c-scorer`
- **Related:** [0001](0001-a-miss-costs-more-than-a-false-alarm.md), [0007](0007-amplitude-only-motion-score.md), [0011](0011-heatmap-evidence-from-quiet-baseline.md)

## Context

In the transceiver rig (spec #2) every node scores each link it hears and puts one byte of score into each link
report. At 30 nodes a node has 29 links. The score has to stay the one in [0007](0007-amplitude-only-motion-score.md)
(three cascaded high-pass stages, variance over a 1 s window of device time, averaged across subcarriers), so what was
learned about scores and evidence still holds. Stored 1 s windows of float amplitudes cost about 5 KB per link at
20 Hz and 64 subcarriers, about 160 KB for 29 links (report 07, section 3.2), which is too much next to the WiFi stack
on an ESP32-C5. Spec #2 asks for streaming statistics instead, and for a score that fits a u8.

## Decision

- **Streaming window, approximate mean.** The score is (mean square of the residuals) minus (squared window mean),
  averaged over subcarriers. The first term needs only one number per packet, so a ring of (device time, sum of
  squared residuals) keeps it exact, along with the packet count and window span that decide whether there is a score.
  The second term needs per-subcarrier sums. They are kept per 0.5 s bucket of device time (3 buckets live). The
  oldest bucket counts with the share of its packets still in the window. That bucket weighting is the only
  approximation. Code: `firmware/core/motion_score.{h,c}`.
- **Ring of 48 packets.** This is exact up to about 45 packets per second per link (40 Hz needs 41). Faster than
  that, the window gets shorter than 1 s, the link still scores, and `overflowed` counts it.
- **Tolerance:** the C score is within 15 % + 1e-4 of the Python score on every packet of the shared vectors.
- **Score code.** One byte. Code 0 means no score. Code c >= 1 stands for `2 ** ((c - 1) / 10 - 10)`: 10 codes per
  octave, from 2^-10 (about 0.001) to 2^15.4 (about 43 000), with saturation at both ends. Encoding rounds log2 to the
  nearest code. Code: `csi/score_code.py` and `motion_score_encode`/`motion_score_decode`.
- **Shared test vectors** generated from `csi.detect.MotionScorer` by `scripts/gen_score_vectors.py` pin both sides:
  `tests/test_score_vectors.py` for Python, `firmware/core/test.sh` for C (CI job `node-core`, gcc on Ubuntu).

## Alternatives considered

- **Store the window** (float or int8 per subcarrier per packet). This is exact, and int8 with a per-packet scale
  costs about as much as the buckets at 20 Hz. It grows with the packet rate, though (twice the size at 40 Hz), and
  spec #2 rules stored windows out. Not built.
- **Drop the mean term** (score = mean square). This needs no per-subcarrier sums, but it always reads high. In a
  synthetic sway test it was off by 1-3 % median and up to 18 %. Rejected.
- **Exponentially weighted mean and variance** (2 floats per subcarrier, as report 07 sketched). Its weighting is
  not the 1 s window, and in the same test it was off by up to 16 % (τ = 0.5 s) or 47 % (τ = 0.3 s). Rejected.
- **More buckets.** With 4 or 8 buckets per window the worst case improved only a little (to 6-11 %), and every
  extra bucket costs 256 B per link. Two per window was kept.
- **Score code scales.** 16 codes per octave halves the rounding error, but it covers only 16 octaves, which is too
  narrow for per-link scales that move 3-5x on a re-baseline. A linear byte cannot cover 0.01 to 1000.
  10 codes per octave rounds to within ±3.5 %. That is well inside the 1.2x dead zone of the heatmap evidence
  ([0011](0011-heatmap-evidence-from-quiet-baseline.md)).

## Consequences

- Memory per link is 2016 B at 64 subcarriers (1728 B at 52), so 29 links take 58.5 KB (50.1 KB). The C test
  asserts the 64 KB budget.
- The window mean is approximate, so the node score is not bit-identical to the Python score. The worst errors are
  at movement onsets and stops, where the window mean changes fastest. In steady quiet or steady movement the score is
  within about 1 %.
- The score code is part of link report format v1. Changing the scale means a new format version.
- `motion_score_get` does the window arithmetic. Firmware should call it once per round per link when it builds
  the link report, not on every packet.
- The C5 has no double-precision FPU. Each packet does one double `exp` (for the filter coefficient) and float
  arithmetic otherwise. CPU cost on the board has not been measured.

## Evidence

Measured on the PC (clang 21, and gcc 15 for RISC-V compiles it warning-free) against the shared vectors: 7 cases,
1243 packets, 1059 scores.

| case | relative error median | p99 | max |
|---|---|---|---|
| all cases | 0.14 % | 3.9 % | 11.2 % |
| empty room, 20 Hz | 0.07 % | 0.24 % | 0.29 % |
| simulator walk (empty, motion, still), 20 Hz | 0.08 % | 6.1 % | 11.2 % |
| strong sway, 40 Hz, 25 % loss | 0.18 % | 4.5 % | 6.1 % |

The score code matched Python's in 1008 of 1059 scores, was 1 code off in 50 and 2 codes off in 1. Nothing has been
measured on a board yet.
