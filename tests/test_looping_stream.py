"""The endless simulated room stream (dashboard --simulate-room): continuous across loop boundaries, and an
optional empty lead-in (--sim-quiet-first) that a user can calibrate on.

Driven at the (host_ts, line) stream seam: lines go through the real parser, then DeviceClock and LinkHealth
decide what the dashboard would see. Non-realtime generation keeps the timestamps synthetic and the test fast.
"""
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from csi.clock import RESET, DeviceClock  # noqa: E402
from csi.health import LinkHealth  # noqa: E402
from csi.links import LinkMonitor, WARMUP  # noqa: E402
from csi.parser import parse_stream  # noqa: E402
from csi.room import Walk, load_room  # noqa: E402
from csi.server import default_walk, looping_room_streams, simulated_thresholds, walk_period_s  # noqa: E402

ROOM = load_room()
T0 = 1_700_000_000.0
RIGHT = "rx1<-00"


def packets_until(stream, horizon_s: float):
    """Parsed packets of one receiver's endless stream, up to ``horizon_s`` after T0."""
    lines = itertools.takewhile(lambda item: item[0] < T0 + horizon_s, stream)
    return list(parse_stream(lines))


def test_no_link_sees_a_receiver_reboot_over_two_loop_boundaries():
    walk = default_walk(ROOM, speed_mps=2.0)          # a short loop: two boundaries in a few seconds of data
    horizon = 2.5 * walk_period_s(walk)
    streams = looping_room_streams(ROOM, walk, seed=3, realtime=False)

    for name, stream in streams.items():
        pkts = packets_until(stream, horizon)
        host = [p.host_ts for p in pkts]
        assert host == sorted(host), f"{name}: host time goes backwards"
        for mac in {p.mac for p in pkts}:
            link = [p for p in pkts if p.mac == mac]
            health = LinkHealth(send_rate_hz=100.0)
            clock = DeviceClock()
            events = [clock.advance(p.device_ts, p.host_ts)[1] for p in link]
            for p in link:
                health.observe(p)
            assert RESET not in events, f"{name} {mac}: device clock restarted at a loop boundary"
            assert health.reboots == 0


def test_no_link_goes_to_warmup_after_the_first_full_window_across_loop_boundaries():
    walk = default_walk(ROOM, speed_mps=2.0)
    horizon = 2.5 * walk_period_s(walk)
    monitor = LinkMonitor({label: 1.0 for label in ("rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01")})
    warmed = set()
    for name, stream in looping_room_streams(ROOM, walk, seed=3, realtime=False).items():
        for p in packets_until(stream, horizon):
            state = monitor.push(name, p)
            if state != WARMUP:
                warmed.add((name, p.mac))
            elif (name, p.mac) in warmed:
                pytest.fail(f"{name} {p.mac} fell back to WARMUP at host time {p.host_ts - T0:.2f} s")
    assert len(warmed) == 4


@pytest.mark.slow
def test_quiet_first_seconds_are_empty_so_a_user_can_calibrate_then_the_walk_starts():
    quiet_s = 14.0
    thresholds = simulated_thresholds(ROOM)
    walk = Walk((ROOM.wall_point("left"), ROOM.wall_point("right")), speed_mps=0.5, loop=True)
    monitor = LinkMonitor(thresholds)
    walked = False
    for name, stream in looping_room_streams(ROOM, walk, seed=3, realtime=False, quiet_first_s=quiet_s).items():
        for p in packets_until(stream, quiet_s + 12.0):
            state = monitor.push(name, p)
            if p.host_ts < T0 + quiet_s:
                assert state != "MOTION", f"{name} moved at {p.host_ts - T0:.2f} s during the empty lead-in"
            elif state == "MOTION":
                walked = True
    assert walked, "the walk never registered after the empty lead-in"


def test_sim_quiet_first_option_is_non_negative_and_only_for_the_simulated_room():
    import dashboard  # scripts/dashboard.py

    args = dashboard.build_parser().parse_args(["--simulate-room", "--sim-quiet-first", "60"])
    assert args.sim_quiet_first == 60
    assert dashboard.build_parser().parse_args(["--simulate-room"]).sim_quiet_first == 0
    with pytest.raises(SystemExit):
        dashboard.build_parser().parse_args(["--simulate-room", "--sim-quiet-first", "-5"])
    with pytest.raises(SystemExit):
        dashboard.check_source(dashboard.build_parser().parse_args(
            ["--port", "/dev/cu.no-such", "--sim-quiet-first", "10"]))
