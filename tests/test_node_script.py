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
        f"ok serial={SERIAL} role=transceiver channel=6 index=0 nodes=2 round_hz=40")},
        preamble=b"ESP-ROM:esp32c5\r\n" + encode_status(SERIAL, 1, 1, 0, 6, [0] * 7))
    assert node_cli.run(port, ["show"], timeout_s=1.0) == 0
    assert port.written == b"show\n"
    assert capsys.readouterr().out.strip().splitlines()[-1] == \
        f"ok serial={SERIAL} role=transceiver channel=6 index=0 nodes=2 round_hz=40"


def test_set_sends_one_line_and_exits_0_on_ok(capsys):
    port = FakeNodePort({"set index=1 nodes=2": encode_text("ok role=transceiver channel=6 index=1 nodes=2 "
                                                            "round_hz=40 (saved; restarting)")})
    assert node_cli.run(port, ["set", "index=1", "nodes=2"], timeout_s=1.0) == 0
    assert port.written == b"set index=1 nodes=2\n"
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


def test_key_provisions_the_kit_key_and_checks_the_nodes_check_value(tmp_path, capsys):
    from csi.round_auth import kit_key_check, write_kit_key

    key = write_kit_key(tmp_path / "kit.key")
    line = f"key {key.hex()}"
    port = FakeNodePort({line: encode_text(f"ok key check={kit_key_check(key)} (saved; restarting)")})
    assert node_cli.provision(port, tmp_path / "kit.key", timeout_s=1.0) == 0
    assert port.written == (line + "\n").encode()
    out = capsys.readouterr().out
    assert kit_key_check(key) in out and key.hex() not in out


def test_key_fails_when_the_node_reports_another_check_value(tmp_path, capsys):
    from csi.round_auth import write_kit_key

    key = write_kit_key(tmp_path / "kit.key")
    port = FakeNodePort({f"key {key.hex()}": encode_text("ok key check=00000000 (saved; restarting)")})
    assert node_cli.provision(port, tmp_path / "kit.key", timeout_s=1.0) == 1
    assert "check" in capsys.readouterr().err


def test_key_without_a_file_shows_the_nodes_key_check(capsys):
    port = FakeNodePort({"key": encode_text("ok key check=88a46367")})
    assert node_cli.run(port, ["key"], timeout_s=1.0) == 0
    assert "88a46367" in capsys.readouterr().out


def test_keygen_writes_a_new_kit_key_once(tmp_path, capsys):
    from csi.round_auth import kit_key_check, load_kit_key

    path = tmp_path / "kit.key"
    assert node_cli.main(["keygen", str(path)]) == 0
    assert kit_key_check(load_kit_key(path)) in capsys.readouterr().out
    assert node_cli.main(["keygen", str(path)]) == 1
    assert "exists" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["COM5", "key", "a.key", "b.key"], ["keygen", "a.key", "b.key"]])
def test_key_usage_errors_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        node_cli.main(argv)
    assert e.value.code == 2
