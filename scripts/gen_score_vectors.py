#!/usr/bin/env python
"""Write the shared motion-score test vectors from the Python scorer.

    gen_score_vectors.py                 # rewrite firmware/core/tests/vectors/motion_score.txt
    gen_score_vectors.py --out other.txt

The node's C scorer (firmware/core/motion_score.c) must reproduce these vectors, and tests/test_score_vectors.py
checks that the Python scorer (csi.detect.MotionScorer) and the score code (csi.score_code) still do. Rerun this
only when the Python score changes on purpose, then run firmware/core/test.sh.

File format, one record per line (fields separated by single spaces, '#' starts a comment line):

    case <name> <n_sub>               start a case: a fresh scorer for one link with n_sub subcarriers
    p <t_us> <score|-> <a_1..a_n>     a packet the scorer accepts: device time (us), amplitudes, and the score
                                      after it ('-' = no score yet)
    d <t_us> <score|-> <a_1..a_n>     a packet the scorer drops (not later than the last accepted one); the score
                                      is the unchanged one
    enc <score> <code>                encode_score(score) == code
    dec <code> <score|->              decode_score(code) == score

Amplitudes are written with two decimals and read back as float32, the type the parser and the node both use, so
both sides see the same input. Scores are written with 17 significant digits.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Iterator

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.detect import MotionScorer  # noqa: E402
from csi.parser import CSIPacket  # noqa: E402
from csi.score_code import MAX_CODE, MIN_LOG2, STEPS_PER_OCTAVE, decode_score, encode_score  # noqa: E402
from csi.simulate import Segment, simulate_packets  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "firmware" / "core" / "tests" / "vectors" / "motion_score.txt"

Packet = tuple[int, np.ndarray]  # (device time us, amplitudes)


def _quantised(amp: np.ndarray) -> np.ndarray:
    """Amplitudes as the file stores them: two decimals, clipped to what int8 I/Q can give (0..181)."""
    return np.array([np.float32(f"{v:.2f}") for v in np.clip(amp, 0.0, 181.0)], dtype=np.float32)


def _times(rng: np.random.Generator, n: int, rate_hz: float, t0_us: int, jitter_us: int = 500) -> np.ndarray:
    return t0_us + (np.arange(n) * 1e6 / rate_hz).astype(np.int64) + rng.integers(0, jitter_us + 1, n)


def _sway(rng: np.random.Generator, t_s: np.ndarray, k: int, strength: float) -> np.ndarray:
    """Body sway and walking: a few 0.5-3 Hz components per subcarrier. These leave a large window mean after the
    high-pass, the hardest part of the score to stream."""
    out = np.zeros((t_s.size, k))
    for f in rng.uniform(0.5, 3.0, 6):
        out += strength * rng.uniform(0, 1, k) * np.sin(2 * np.pi * f * t_s[:, None] + rng.uniform(0, 2 * np.pi, k))
    return out


def empty_20hz(rng: np.random.Generator) -> Iterator[Packet]:
    n, k = 160, 52
    base = rng.uniform(12, 45, k)
    for t, a in zip(_times(rng, n, 20, 1_000_000), base + rng.normal(0, 0.4, (n, k))):
        yield int(t), a


def sim_walk_20hz(rng: np.random.Generator) -> Iterator[Packet]:
    """The project simulator (csi.simulate): empty, walking, then a still person breathing."""
    scenario = (Segment("empty", 3), Segment("motion", 4), Segment("still", 4))
    for pkt, _ in simulate_packets(scenario, rate_hz=20, seed=11):
        yield 2_000_000 + int(pkt.device_ts), pkt.amplitude


def sway_40hz_drops(rng: np.random.Generator) -> Iterator[Packet]:
    """40 Hz with 25 % loss; quiet, then strong sway, then quiet. Device time starts past 2**32 us."""
    n, k = 400, 52
    t = _times(rng, n, 40, 5_000_000_000)
    t_s = (t - t[0]) / 1e6
    env = ((t_s > 2.5) & (t_s < 7.5)).astype(float)[:, None]
    amp = rng.uniform(15, 40, k) + rng.normal(0, 0.5, (n, k)) + env * _sway(rng, t_s, k, 5.0)
    for ti, a, keep in zip(t, amp, rng.random(n) >= 0.25):
        if keep:
            yield int(ti), a


def gap_jitter(rng: np.random.Generator) -> Iterator[Packet]:
    """20 Hz with +-3 ms jitter and a 1.6 s gap: the window empties and has to refill."""
    n, k = 200, 24
    t = 3_000_000 + (np.arange(n) * 50_000).astype(np.int64) + rng.integers(-3000, 3001, n)
    t[100:] += 1_600_000
    t_s = (t - t[0]) / 1e6
    amp = rng.uniform(15, 40, k) + rng.normal(0, 0.5, (n, k)) + _sway(rng, t_s, k, 2.0)
    for ti, a in zip(t, amp):
        yield int(ti), a


def out_of_order(rng: np.random.Generator) -> Iterator[Packet]:
    """Repeated and slightly late timestamps, which the scorer drops."""
    n, k = 120, 8
    t = _times(rng, n, 20, 4_000_000)
    amp = rng.uniform(15, 40, k) + rng.normal(0, 1.0, (n, k)) + _sway(rng, (t - t[0]) / 1e6, k, 3.0)
    for i, (ti, a) in enumerate(zip(t, amp)):
        yield int(ti), a
        if i % 17 == 5:
            yield int(ti), a + 3.0             # the same time again
        if i % 23 == 9:
            yield int(ti) - 30_000, a - 2.0    # older than the last accepted packet


def sparse_5hz(rng: np.random.Generator) -> Iterator[Packet]:
    """5 Hz with 20 % loss: windows hold 4-6 packets, so the 5-packet minimum decides whether there is a score."""
    n, k = 80, 16
    t = _times(rng, n, 5, 6_000_000, jitter_us=20_000)
    amp = rng.uniform(15, 40, k) + rng.normal(0, 0.5, (n, k)) + _sway(rng, (t - t[0]) / 1e6, k, 2.0)
    for ti, a, keep in zip(t, amp, rng.random(n) >= 0.2):
        if keep:
            yield int(ti), a


def full_width_64(rng: np.random.Generator) -> Iterator[Packet]:
    """64 subcarriers (the C module's maximum) at 20 Hz, strong motion that clips at 0 and at 181."""
    n, k = 160, 64
    t = _times(rng, n, 20, 7_000_000)
    t_s = (t - t[0]) / 1e6
    amp = rng.uniform(5, 170, k) + rng.normal(0, 0.5, (n, k)) + _sway(rng, t_s, k, 20.0)
    amp += 6 * np.cumsum(rng.normal(0, 0.3, (n, 1)), axis=0)
    for ti, a in zip(t, amp):
        yield int(ti), a


CASES = (empty_20hz, sim_walk_20hz, sway_40hz_drops, gap_jitter, out_of_order, sparse_5hz, full_width_64)


def _score_text(score) -> str:
    return "-" if score is None else f"{score:.17g}"


def case_lines(name: str, packets: Iterator[Packet]) -> list[str]:
    scorer = MotionScorer()
    lines = []
    for t_us, raw in packets:
        amp = _quantised(raw)
        if not lines:
            lines.append(f"case {name} {amp.size}")
        dropped = scorer.dropped
        score = scorer.push(CSIPacket(host_ts=t_us / 1e6, device_ts=t_us, rssi=-50, channel=6, amplitude=amp))
        kind = "d" if scorer.dropped > dropped else "p"
        lines.append(f"{kind} {t_us} {_score_text(score)} " + " ".join(f"{v:.2f}" for v in amp))
    return lines


def code_lines(rng: np.random.Generator) -> list[str]:
    """Encode pairs: grid points, both ends and beyond, and random scores away from the half-step ties (where
    float32 and float64 rounding could disagree). Decode pairs: every code."""
    scores = [decode_score(c) for c in range(1, MAX_CODE + 1, 7)] + [0.0, 1e-7, 2.0 ** 16, 1e9]
    for log2 in rng.uniform(MIN_LOG2 - 1, MIN_LOG2 + (MAX_CODE - 1) / STEPS_PER_OCTAVE + 1, 400):
        frac = (STEPS_PER_OCTAVE * (log2 - MIN_LOG2) + 0.5) % 1.0
        if 0.01 < frac < 0.99:
            scores.append(2.0 ** log2)
    lines = [f"enc {s:.17g} {encode_score(s)}" for s in scores]
    lines += [f"dec {c} {_score_text(decode_score(c))}" for c in range(MAX_CODE + 1)]
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    lines = ["# Motion score test vectors, generated by scripts/gen_score_vectors.py from csi.detect.MotionScorer",
             "# and csi.score_code. Do not edit by hand; the format is in the script's docstring."]
    for i, case in enumerate(CASES):
        lines += case_lines(case.__name__, case(np.random.default_rng(1000 + i)))
    lines += code_lines(np.random.default_rng(99))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(("\n".join(lines) + "\n").encode("ascii"))
    print(f"wrote {args.out} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
