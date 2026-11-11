"""scripts/node.py: set and read a node's runtime settings over its USB port, and watch its status.

The port is faked: it answers each command line the way the node does, with gateway records
(docs/gateway-record.md), after boot text and between log lines. No port is opened.
"""
import sys
from pathlib import Path

import pytest

from csi.gateway_records import encode_heard, encode_status, encode_text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import node as node_cli  # noqa: E402

SERIAL = "10:BD:A3:CC:85:BC"


class FakeNodePort:
    """Answers command lines with scripted records; read() returns what is waiting, then b"" (a timeout)."""

    def __init__(self, answers: dict, preamble: bytes = b""):
        self.answers = answers
        self.written = b""
        self.pending = bytearray(preamble)

    def write(self, data: bytes) -> int:
        self.written += data
        for line in data.decode().splitlines():
            self.pending += self.answers.get(line.strip(), b"")
        return len(data)

    def read(self, size: int = 1) -> bytes:
        out, self.pending = bytes(self.pending[:max(size, 1)]), self.pending[max(size, 1):]
        return out

    @property
    def in_waiting(self) -> int:
        return len(self.pending)


def test_show_prints_the_nodes_reply_and_skips_logs_and_boot_text(capsys):
    port = FakeNodePort({"show": encode_text("I (812) node: started") + encode_text(
        f"ok serial={SERIAL} role=transceiver channel=6 round_hz=40 guard_us=250")},
        preamble=b"ESP-ROM:esp32c5\r\n" + encode_status(SERIAL, 1, 1, 0, 6, [0] * 7))
    assert node_cli.run(port, ["show"], timeout_s=1.0) == 0
    assert port.written == b"show\n"
    assert capsys.readouterr().out.strip().splitlines()[-1] == \
        f"ok serial={SERIAL} role=transceiver channel=6 round_hz=40 guard_us=250"


def test_set_sends_one_line_and_exits_0_on_ok(capsys):
    port = FakeNodePort({"set guard_us=300 round_hz=20": encode_text("ok role=transceiver channel=6 round_hz=20 "
                                                                     "guard_us=300 (saved; restarting)")})
    assert node_cli.run(port, ["set", "guard_us=300", "round_hz=20"], timeout_s=1.0) == 0
    assert port.written == b"set guard_us=300 round_hz=20\n"
    assert "saved; restarting" in capsys.readouterr().out


def test_a_refused_command_exits_1_with_the_nodes_reason(capsys):
    port = FakeNodePort({"set channel=14": encode_text("error channel must be a 2.4 GHz channel, 1 to 13")})
    assert node_cli.run(port, ["set", "channel=14"], timeout_s=1.0) == 1
    assert "channel must be" in capsys.readouterr().out


def test_no_reply_exits_1_and_says_why(capsys):
    port = FakeNodePort({})
    assert node_cli.run(port, ["show"], timeout_s=0.2) == 1
    assert "no reply" in capsys.readouterr().err


def test_status_prints_the_counters_by_name_and_text_lines(capsys):
    status = encode_status(SERIAL, 5_000_000, 1, 0, 6, [120, 0, 3, 121, 0, 2, 20])
    port = FakeNodePort({}, preamble=status + encode_text("W (900) node: something") + encode_heard(1, -40, b"x"))
    assert node_cli.run(port, ["status"], timeout_s=0.3) == 0
    out = capsys.readouterr().out
    assert SERIAL in out and "transceiver" in out and "channel 6" in out
    assert "heard 120" in out and "sent 121" in out and "rejected 2" in out and "csi_skipped 20" in out
    assert "W (900) node: something" in out
    assert port.written == b""                          # status only listens


@pytest.mark.parametrize("argv", [[], ["COM5"], ["COM5", "flash"], ["COM5", "set"]])
def test_usage_errors_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        node_cli.main(argv)
    assert e.value.code == 2
