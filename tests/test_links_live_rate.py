"""links_live --calibrate on real ports: the loss estimate uses the sender rate from device timestamps unless --rate
is given (the same as the dashboard's live calibration), and the simulator path keeps its nominal rate."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import links_live  # noqa: E402

from csi.links import calibrate_links_report  # noqa: E402
from csi.parser import parse_stream  # noqa: E402
from csi.simulate import Segment, simulate_stream  # noqa: E402


def args_for(*argv):
    return links_live.build_parser().parse_args(list(argv))


def test_a_real_port_without_rate_estimates_the_rate_from_device_timestamps():
    assert links_live.calibration_rate(args_for("--port", "rx1=/dev/cu.no-such", "--calibrate", "5")) is None


def test_rate_given_on_a_real_port_is_an_override():
    assert links_live.calibration_rate(args_for("--port", "rx1=/dev/cu.no-such", "--calibrate", "5",
                                                "--rate", "25")) == 25.0


def test_the_simulator_keeps_its_nominal_rate():
    assert links_live.calibration_rate(args_for("--simulate-links", "--calibrate", "5")) == 100.0
    assert links_live.calibration_rate(args_for("--simulate-links", "--calibrate", "5", "--rate", "40")) == 40.0


def test_a_25_hz_sender_on_a_real_port_is_not_counted_as_40_percent_loss():
    """Loss from the estimated rate (what a real port uses) reads near zero; a fixed 40 Hz would read 37 %."""
    packets = list(parse_stream(simulate_stream(scenario=[Segment("empty", 20)], rate_hz=25.0, seed=3)))
    tagged = [("rx1", p) for p in packets]
    report = calibrate_links_report(clips=[tagged], send_rate_hz=links_live.calibration_rate(
        args_for("--port", "rx1=/dev/cu.no-such", "--calibrate", "5")))
    (label,) = report.calibrations
    assert report.calibrations[label].loss < 0.05
    assert report.calibrations[label].rate_hz == pytest.approx(25.0, rel=0.05)
