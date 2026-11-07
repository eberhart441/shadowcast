"""A silent serial port: the reader keeps checking its stop flag, and closing the sources releases the port quickly.

pyserial is replaced by a fake that never returns data (readline waits out its timeout and returns b""), so no
hardware is touched. The fake records close(), which is what releases a real port.
"""
import sys
import threading
import time
import types

import pytest

from csi.multisource import MultiSource
from csi.parser import CSIParser, parse_stream, serial_stream


class SilentPort:
    """A serial.Serial stand-in that sends nothing; readline blocks for its timeout, as pyserial does."""

    opened: list = []

    def __init__(self, port, baud, timeout):
        self.timeout = timeout
        self.closed = False
        SilentPort.opened.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def reset_input_buffer(self):
        pass

    def readline(self):
        time.sleep(self.timeout)
        return b""

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    SilentPort.opened = []
    module = types.ModuleType("serial")
    module.Serial = SilentPort
    monkeypatch.setitem(sys.modules, "serial", module)
    return SilentPort


def next_items(stream, n: int, within_s: float) -> list:
    """The next ``n`` items of ``stream``, failing (not hanging) if they do not arrive within ``within_s``."""
    out, done = [], threading.Event()

    def pull():
        for _ in range(n):
            out.append(next(stream))
        done.set()

    threading.Thread(target=pull, daemon=True).start()
    if not done.wait(within_s):
        pytest.fail(f"only {len(out)} of {n} items within {within_s} s: a silent port yields nothing")
    return out


@pytest.mark.slow
def test_a_silent_port_yields_heartbeats_about_once_a_second(fake_serial):
    stream = serial_stream("/dev/cu.fake", timeout=1.0)
    start = time.time()
    beats = next_items(stream, 2, within_s=5.0)
    elapsed = time.time() - start
    stream.close()

    assert all(line is None for _, line in beats)      # heartbeat: no data, but the reader is alive
    assert elapsed < 3.0
    assert fake_serial.opened[0].closed is True        # closing the generator releases the port


@pytest.mark.slow
def test_closing_sources_of_a_silent_port_stops_the_reader_and_closes_the_port_quickly(fake_serial):
    ms = MultiSource({"rx1": serial_stream("/dev/cu.fake", timeout=1.0)})
    ms.start()
    time.sleep(0.3)
    start = time.time()

    ms.close()

    assert time.time() - start < 2.5                   # not the full join timeout
    assert fake_serial.opened[0].closed is True
    assert ms.finished()


def test_heartbeats_never_reach_the_parser_as_lines(fake_serial):
    parser = CSIParser()
    stream = serial_stream("/dev/cu.fake", timeout=0.05)
    beats = next_items(stream, 3, within_s=5.0)
    stream.close()

    assert all(line is None for _, line in beats)
    assert list(parse_stream(beats, parser)) == []
    assert parser.stats.lines == 0 and parser.stats.ignored == 0
