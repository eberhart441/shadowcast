"""--rate means the sender's nominal rate: simulator default 100 Hz, real ports estimate it unless given."""
import argparse

from csi.common import SIM_RATE_HZ, add_source_args, add_threshold_args, link_send_rate, simulated_rate_hz
from csi.detect import SIM_THRESHOLD


def parse(*argv):
    ap = argparse.ArgumentParser()
    add_source_args(ap)
    add_threshold_args(ap)
    return ap.parse_args(list(argv))


def test_simulator_defaults_to_100hz_for_both_uses():
    args = parse("--simulate")
    assert simulated_rate_hz(args) == SIM_RATE_HZ == 100.0
    assert link_send_rate(args) == 100.0


def test_real_port_estimates_the_rate_unless_one_is_given():
    assert link_send_rate(parse("--port", "/dev/x")) is None
    assert link_send_rate(parse("--port", "/dev/x", "--rate", "50")) == 50.0


def test_explicit_rate_drives_the_simulator_too():
    assert simulated_rate_hz(parse("--simulate", "--rate", "50")) == 50.0


def test_threshold_help_states_both_scales_and_says_to_calibrate():
    ap = argparse.ArgumentParser()
    add_threshold_args(ap)
    text = ap.format_help()
    assert "calibrate" in text
    assert f"{SIM_THRESHOLD:g}" in text
    assert "empty" in text and "walking" in text
