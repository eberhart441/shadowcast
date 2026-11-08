"""serial_stream against a fake port: chunked reads, heartbeats on silence, a port that opens without resetting
the board, and closing the sources releases the port quickly.

pyserial is replaced by a fake, so no hardware is touched. Like ``serial.Serial()`` the fake is created unopened
and records its settings when opened. It serves scripted chunks: ``in_waiting`` is the size of the next chunk and
``read`` returns it. With no chunk left it sends nothing: ``read`` waits out its timeout and returns b"", as
pyserial does. The fake records close(), which is what releases a real port.
"""
import sys
import threading
import time
import types

import pytest

from csi.multisource import MultiSource
from csi.parser import MAX_LINE_BYTES, CSIParser, parse_stream, serial_stream


class FakePort:
    """A serial.Serial stand-in (see the module docstring). ``FakePort.script`` is what the next port sends."""

    opened: list = []
    script: list = []

    def __init__(self, *args, **kwargs):
        self.created_with = (args, kwargs)
        self.port, self.baudrate, self.timeout = None, 9600, None
        self.dtr = self.rts = True          # pyserial asserts both at open unless they are set otherwise first
        self.settings_at_open = None
        self.closed = False
        self._chunks = list(FakePort.script)
        FakePort.opened.append(self)

    def open(self):
        self.settings_at_open = {"port": self.port, "baudrate": self.baudrate, "timeout": self.timeout,
                                 "dtr": self.dtr, "rts": self.rts}

    def reset_input_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, size=1):
        if not self._chunks:
            time.sleep(self.timeout)
            return b""
        chunk = self._chunks.pop(0)
        if size < len(chunk):
            self._chunks.insert(0, chunk[size:])
        return chunk[:size]

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    FakePort.opened, FakePort.script = [], []
    module = types.ModuleType("serial")
    module.Serial = FakePort
    monkeypatch.setitem(sys.modules, "serial", module)
    return FakePort


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


def lines_from(script: list, n: int) -> list:
    """The first ``n`` items a port sending ``script`` yields (then it is silent: heartbeats)."""
    FakePort.script = script
    stream = serial_stream("/dev/cu.fake", timeout=0.05)
    items = next_items(stream, n, within_s=5.0)
    stream.close()
    return items


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
    assert fake_serial.opened[0].closed is True


def test_the_port_is_created_unopened_and_opens_with_dtr_and_rts_released(fake_serial):
    # DTR/RTS asserted at open can reset an ESP32, and a receiver that resets re-rolls its forced gain
    stream = serial_stream("/dev/cu.fake", baud=921600, timeout=0.05)
    next_items(stream, 1, within_s=5.0)
    stream.close()

    port = fake_serial.opened[0]
    assert port.created_with == ((), {})               # Serial(path, ...) would open at once, before dtr/rts are set
    assert port.settings_at_open == {"port": "/dev/cu.fake", "baudrate": 921600, "timeout": 0.05,
                                     "dtr": False, "rts": False}
    assert port.closed is True


def test_a_line_split_across_chunks_arrives_once_whole_without_its_line_ending(fake_serial):
    items = lines_from([b"CSI_DA", b"TA,1,2\r", b"\nnext li", b"ne\r\n"], 3)

    assert [line for _, line in items] == ["CSI_DATA,1,2", "next line", None]   # then silence: a heartbeat


def test_several_lines_in_one_chunk_keep_their_order_and_share_the_chunk_time(fake_serial):
    items = lines_from([b"a\r\nb\nc\r\n", b"d\n"], 4)

    assert [line for _, line in items] == ["a", "b", "c", "d"]
    assert items[0][0] == items[1][0] == items[2][0] <= items[3][0]


def test_undecodable_bytes_are_replaced_not_raised(fake_serial):
    (_, line), = lines_from([b"ok \xff\xfe\n"], 1)

    assert line == "ok ��"


def test_an_overlong_run_without_a_newline_is_handed_on_not_buffered_forever(fake_serial):
    items = lines_from([b"x" * (MAX_LINE_BYTES + 1), b"CSI_DATA,1\n"], 2)

    assert [len(line) for _, line in items] == [MAX_LINE_BYTES + 1, len("CSI_DATA,1")]


def test_a_c5_line_split_across_chunks_parses_as_if_it_came_whole(fake_serial):
    text = ('CSI_DATA,7,1a:0c:12:00:00:01,-40,11,-97,2,30,6,123456,47,2,234,0,"['
            + ",".join(["5"] * 234) + ']"\r\n').encode()
    items = lines_from([text[:12], text[12:300], text[300:]], 1)

    pkt, = parse_stream(items, CSIParser())
    assert (pkt.seq, pkt.mac, pkt.device_ts, pkt.agc_gain, pkt.fft_gain) == (7, "1a:0c:12:00:00:01", 123456, 30, 2)
    assert len(pkt.amplitude) == 114
