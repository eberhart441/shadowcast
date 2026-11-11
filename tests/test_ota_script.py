"""scripts/ota.py: refuses bad images, names the maintenance network, and drives a rollout through the gateway.

The rollout loop runs against a fake gateway port (it hears round broadcasts as gateway records and takes the
"ota send" lines) and a fake image server (it hands over reports), on a fake clock.
"""
import json
import sys
from pathlib import Path

from csi import ota
from csi.broadcast import FORMAT_VERSION, LinkReport, RoundBroadcast, encode_broadcast
from csi.gateway_records import encode_heard, encode_text
from csi.round_auth import TEST_KIT_KEY, sign_broadcast, write_kit_key

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ota as ota_script  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ota"
INFO = json.loads((FIXTURES / "image.json").read_text())
A, B = "02:00:00:00:01:5C", "02:00:00:00:02:5C"


def signer_file(tmp_path, digest=INFO["test_key_digest"]):
    path = tmp_path / "ota-signing.digest"
    path.write_bytes(bytes.fromhex(digest))
    return path


def test_check_takes_an_image_signed_by_the_kits_key(tmp_path, capsys):
    assert ota_script.main(["check", str(FIXTURES / "signed.bin"), "--signer", str(signer_file(tmp_path))]) == 0
    assert INFO["signed_sha256"] in capsys.readouterr().out


def test_check_refuses_an_unsigned_image_and_one_from_another_key(tmp_path, capsys):
    unsigned = tmp_path / "unsigned.bin"
    unsigned.write_bytes((FIXTURES / "signed.bin").read_bytes()[:INFO["unsigned_length"]])
    assert ota_script.main(["check", str(unsigned), "--signer", str(signer_file(tmp_path))]) == 1
    assert "refused (unsigned)" in capsys.readouterr().err
    assert ota_script.main(["check", str(FIXTURES / "signed_by_other_key.bin"),
                            "--signer", str(signer_file(tmp_path))]) == 1
    assert "refused (signer)" in capsys.readouterr().err


def test_a_signer_digest_may_be_written_as_hex(tmp_path):
    path = tmp_path / "digest.txt"
    path.write_text(INFO["test_key_digest"] + "\n")
    assert ota_script.load_signer(path).hex() == INFO["test_key_digest"]


def test_network_names_the_access_point_of_the_kit(tmp_path, capsys):
    key_file = tmp_path / "kit.key"
    key = write_kit_key(key_file)
    assert ota_script.main(["network", "--kit-key", str(key_file)]) == 0
    ssid, passphrase = ota.maintenance_network(key)
    out = capsys.readouterr().out
    assert f"SSID {ssid}" in out and f"passphrase {passphrase}" in out


def test_a_rollout_over_the_sensing_channel_is_refused_before_anything_is_opened(capsys):
    assert ota_script.main(["checkin", "--gateway", "NO-SUCH-PORT", "--ap-channel", "7", "--channel", "6"]) == 2
    assert "overlaps" in capsys.readouterr().err


def round_broadcast(serial, round_no, key=TEST_KIT_KEY):
    rows = (LinkReport(1, 100, -50, 15, 0),)
    b = RoundBroadcast(FORMAT_VERSION, 1, round_no, 0, 3, 1, 0, 0b11, serial, bytes(8), rows)
    return sign_broadcast(encode_broadcast(b), key)


def test_only_authenticated_round_broadcasts_count_as_heard():
    from csi.gateway_records import RecordReader

    good = encode_heard(1, -40, round_broadcast(A, 5))
    forged = encode_heard(2, -40, round_broadcast(B, 5, key=bytes(16)))
    noise = encode_heard(3, -40, b"\x02\x08 not a round broadcast")
    records = RecordReader().feed(good + forged + noise + encode_text("ok"))
    assert ota_script.heard_serials(records, TEST_KIT_KEY) == [A]


class FakeGateway:
    """A gateway port: the "ota send" lines written to it, and records to read once the clock passes their time."""

    def __init__(self, clock):
        self.clock = clock
        self.lines = []
        self.pending = []   # (time, bytes)

    def write(self, data):
        self.lines.append(data.decode())

    @property
    def in_waiting(self):
        return sum(len(b) for t, b in self.pending if t <= self.clock.t)

    def read(self, n):
        ready = b"".join(b for t, b in self.pending if t <= self.clock.t)
        self.pending = [(t, b) for t, b in self.pending if t > self.clock.t]
        return ready


class FakeServer:
    def __init__(self):
        self.reports = []   # (time, Report)
        self.clock = None

    def take_reports(self):
        ready = [r for t, r in self.reports if t <= self.clock.t]
        self.reports = [(t, r) for t, r in self.reports if t > self.clock.t]
        return ready


class Clock:
    t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_drive_sends_the_command_through_the_gateway_and_waits_for_report_and_rejoin():
    clock, server = Clock(), FakeServer()
    server.clock = clock
    gateway = FakeGateway(clock)
    image = ota.ImageInfo(4096, bytes(32), bytes(32), 23, "v2")
    rollout = ota.Rollout([[A, B]], TEST_KIT_KEY, image=image, port=8070, ap_channel=36, sensing_channel=6,
                          send=lambda f: gateway.write(ota_script.send_line(f)), wall=lambda: 1794000000,
                          log=lambda m: None)
    gateway.pending.append((0.5, encode_text("ok ota sent 84 bytes (command 1794000000)")))
    for serial, t in ((A, 40.0), (B, 50.0)):
        server.reports.append((t, ota.Report(serial, 1794000000, "committed", "ota_1", "-", "v2")))
        gateway.pending.append((t + 15.0, encode_heard(1, -40, round_broadcast(serial, 9))))
    assert ota_script.drive(rollout, server, gateway, TEST_KIT_KEY, clock=clock, sleep=clock.sleep) == "done"
    assert len(gateway.lines) == 3 and all(line.startswith("ota send 0208") for line in gateway.lines)
    sent = ota.decode_command(bytes.fromhex(gateway.lines[0].split()[2]))
    assert sent.targets == (A, B)
    assert 65.0 <= clock.t < 66.0


def test_unchecked_names_the_exact_bytes_so_only_the_node_can_refuse_them(capsys):
    data = (FIXTURES / "signed_by_other_key.bin").read_bytes()
    image = ota_script.unchecked_image(data)
    assert image.size == len(data) and image.sha256 == __import__("hashlib").sha256(data).digest()
    assert "rig check only" in capsys.readouterr().err
