"""Several receivers read concurrently into one queue of (receiver, packet). Fake sources only: no ports."""
import time

import pytest

from csi.multisource import MultiSource, parse_port_specs
from csi.simulate import SENDER_MACS, Segment, simulate_link_streams


def drain_until_finished(ms: MultiSource, timeout: float = 20.0) -> list:
    out, deadline = [], time.time() + timeout
    while time.time() < deadline:
        out.extend(ms.drain(timeout=0.05))
        if ms.finished():
            out.extend(ms.drain())
            return out
    raise AssertionError(f"sources did not finish: {ms.status()}")


def failing(lines):
    """Yields some lines, then raises like an unplugged port would."""
    for item in lines:
        yield item
    raise OSError("device disconnected")


class EndlessPort:
    """A fake port that never ends; records close() so shutdown can be checked."""

    def __init__(self):
        self.closed = False

    def __iter__(self):
        i = 0
        while True:
            time.sleep(0.005)
            yield 1_700_000_000.0 + i * 0.01, "type,rssi"
            i += 1

    def close(self):
        self.closed = True


def test_port_specs_named_and_unnamed():
    assert parse_port_specs(["rx1=/dev/cu.a", "rx2=/dev/cu.b"]) == [("rx1", "/dev/cu.a"), ("rx2", "/dev/cu.b")]
    assert parse_port_specs(["/dev/cu.a", "/dev/cu.b"]) == [("rx1", "/dev/cu.a"), ("rx2", "/dev/cu.b")]


def test_unnamed_port_skips_names_already_taken():
    assert parse_port_specs(["rx1=/dev/cu.a", "/dev/cu.b"]) == [("rx1", "/dev/cu.a"), ("rx2", "/dev/cu.b")]


def test_duplicate_receiver_names_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        parse_port_specs(["rx1=/dev/cu.a", "rx1=/dev/cu.b"])


def test_packets_from_all_sources_arrive_tagged_with_their_receiver_and_parsed_mac():
    streams = simulate_link_streams(senders=SENDER_MACS, receivers=("rx1", "rx2"),
                                    scenario=[Segment("empty", 2)], seed=1)
    ms = MultiSource(streams)
    ms.start()
    got = drain_until_finished(ms)
    assert {r for r, _ in got} == {"rx1", "rx2"}
    assert {(r, p.mac) for r, p in got} == {(r, m) for r in ("rx1", "rx2") for m in SENDER_MACS}
    assert ms.status() == {"rx1": "done", "rx2": "done"}
    assert ms.parser_stats("rx1").parsed == len([1 for r, _ in got if r == "rx1"])


def test_failing_source_is_reported_and_does_not_stop_the_others():
    good = simulate_link_streams(senders=SENDER_MACS, receivers=("rx1",),
                                 scenario=[Segment("empty", 2)], seed=2)["rx1"]
    bad = failing(list(simulate_link_streams(senders=SENDER_MACS, receivers=("rx2",),
                                             scenario=[Segment("empty", 1)], seed=3)["rx2"])[:50])
    ms = MultiSource({"rx1": good, "rx2": bad})
    ms.start()
    got = drain_until_finished(ms)
    assert sum(1 for r, _ in got if r == "rx1") > 300
    assert any(r == "rx2" for r, _ in got)
    assert ms.status()["rx2"].startswith("error") and "disconnected" in ms.status()["rx2"]
    assert ms.status()["rx1"] == "done"
    assert "disconnected" in ms.errors()["rx2"]


def test_close_stops_every_reader_and_closes_every_port():
    ports = {"rx1": EndlessPort(), "rx2": EndlessPort()}
    ms = MultiSource(ports)
    ms.start()
    time.sleep(0.2)
    assert ms.parser_stats("rx1").lines > 0 and ms.parser_stats("rx2").lines > 0  # they really are reading
    ms.close()
    assert not ms.alive()
    assert all(p.closed for p in ports.values())
    assert ms.status() == {"rx1": "stopped", "rx2": "stopped"}
