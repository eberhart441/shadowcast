"""The shared motion-score test vectors still describe the Python scorer and the score code.

The node's C scorer is tested against the same file (firmware/core/tests). If this test fails, the Python score has
changed: regenerate the vectors with scripts/gen_score_vectors.py on purpose, and the C tests then show whether the
node still matches.
"""
from pathlib import Path

import numpy as np
import pytest

from csi.detect import MotionScorer
from csi.parser import CSIPacket
from csi.score_code import decode_score, encode_score

VECTORS = Path(__file__).resolve().parents[1] / "firmware" / "core" / "tests" / "vectors" / "motion_score.txt"


def read_vectors(path: Path):
    """(cases, encode pairs, decode pairs). A case is (name, n_sub, [(kind, t_us, expected, amplitudes)])."""
    cases, encodes, decodes = [], [], []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        kind, *fields = line.split()
        if kind == "case":
            cases.append((fields[0], int(fields[1]), []))
        elif kind in ("p", "d"):
            expected = None if fields[1] == "-" else float(fields[1])
            amp = np.array([np.float32(v) for v in fields[2:]], dtype=np.float32)
            assert amp.size == cases[-1][1]
            cases[-1][2].append((kind, int(fields[0]), expected, amp))
        elif kind == "enc":
            encodes.append((float(fields[0]), int(fields[1])))
        elif kind == "dec":
            decodes.append((int(fields[0]), None if fields[1] == "-" else float(fields[1])))
        else:
            raise ValueError(f"unknown vector line {line[:20]!r}")
    return cases, encodes, decodes


CASES, ENCODES, DECODES = read_vectors(VECTORS)


def test_vectors_cover_the_scenarios_the_node_must_match():
    names = {name for name, _, _ in CASES}
    assert {"empty_20hz", "sim_walk_20hz", "sway_40hz_drops", "gap_jitter", "out_of_order", "sparse_5hz",
            "full_width_64"} <= names
    assert len(ENCODES) >= 100 and [code for code, _ in DECODES] == list(range(256))


@pytest.mark.parametrize("name, n_sub, packets", CASES, ids=[c[0] for c in CASES])
def test_python_scorer_reproduces_the_vectors(name, n_sub, packets):
    scorer = MotionScorer()
    for kind, t_us, expected, amp in packets:
        dropped = scorer.dropped
        score = scorer.push(CSIPacket(host_ts=t_us / 1e6, device_ts=t_us, rssi=-50, channel=6, amplitude=amp))
        assert (scorer.dropped > dropped) == (kind == "d")
        if expected is None:
            assert score is None
        else:
            assert score == pytest.approx(expected, rel=1e-9, abs=1e-12)


def test_python_score_code_reproduces_the_vectors():
    for score, code in ENCODES:
        assert encode_score(score) == code
    for code, score in DECODES:
        assert (decode_score(code) is None) if code == 0 else decode_score(code) == pytest.approx(score, rel=1e-12)
