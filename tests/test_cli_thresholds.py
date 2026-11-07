"""Threshold handling at the CLI boundary: real ports need an explicit threshold, the simulator has a default.

Port tests use a device path that does not exist and rely on the error being raised before any port is
opened, so no hardware is touched.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from csi.common import resolve_threshold
from csi.detect import SIM_THRESHOLD, load_threshold
from csi.simulate import Segment, simulate_stream

ROOT = Path(__file__).resolve().parents[1]
FAKE_PORT = "/dev/cu.no-such-csi-port"


def run(script: str, *args: str, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def ns(**kw) -> argparse.Namespace:
    base = dict(simulate=False, port=None, threshold=None, threshold_file=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_resolve_threshold_uses_simulator_default_only_under_simulate():
    assert resolve_threshold(ns(simulate=True), SIM_THRESHOLD) == SIM_THRESHOLD
    assert resolve_threshold(ns(simulate=True, threshold=4.0), SIM_THRESHOLD) == 4.0


def test_resolve_threshold_on_a_port_requires_an_explicit_value():
    with pytest.raises(SystemExit, match="--threshold"):
        resolve_threshold(ns(port=FAKE_PORT), SIM_THRESHOLD)


def test_resolve_threshold_reads_threshold_file(tmp_path):
    from csi.detect import Calibration
    path = tmp_path / "calib.json"
    Calibration(threshold=11.5, rate_hz=100.0, duration_s=20.0, loss=0.0).save(path)
    assert resolve_threshold(ns(port=FAKE_PORT, threshold_file=path), SIM_THRESHOLD) == 11.5


@pytest.mark.parametrize("script", ["detect_live.py", "telemetry.py", "live.py"])
def test_real_port_without_threshold_fails_clearly_before_opening_it(script):
    r = run(script, "--port", FAKE_PORT)
    assert r.returncode != 0
    assert "--threshold" in r.stderr and "--threshold-file" in r.stderr
    assert "calibrat" in r.stderr.lower()


def test_detect_live_simulate_runs_without_threshold_and_uses_simulator_default():
    r = run("detect_live.py", "--simulate", "--duration", "3")
    assert r.returncode == 0, r.stderr
    assert f"threshold {SIM_THRESHOLD:.3f}" in r.stdout


def test_calibrate_then_live_round_trip_on_simulator(tmp_path):
    out = tmp_path / "calib.json"
    cal = run("detect_live.py", "--simulate", "--calibrate", "15", "--out", str(out))
    assert cal.returncode == 0, cal.stderr
    threshold = load_threshold(out)
    assert threshold > 0
    live = run("detect_live.py", "--simulate", "--threshold-file", str(out), "--duration", "2")
    assert live.returncode == 0, live.stderr
    assert f"threshold {threshold:.3f}" in live.stdout


def test_simulated_empty_and_motion_separate_at_the_simulator_default():
    from csi.detect import EMPTY, MOTION, detect
    empty = [r.state for r in detect(simulate_stream(scenario=[Segment("empty", 20)], seed=3), SIM_THRESHOLD)]
    motion = [r.state for r in detect(simulate_stream(scenario=[Segment("motion", 10)], seed=3), SIM_THRESHOLD)]
    assert set(empty) == {EMPTY}
    assert motion[-1] == MOTION
