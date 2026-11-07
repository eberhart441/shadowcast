"""scripts/dashboard.py: the CLI contract. Only --simulate-room is run; no port is ever opened."""
import json
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dashboard as cli  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_defaults_bind_localhost_on_port_8000_and_port_is_the_serial_option():
    args = cli.build_parser().parse_args(["--simulate-room"])
    assert args.host == "127.0.0.1"
    assert args.http_port == 8000
    assert args.port is None


def test_named_repeatable_ports_and_the_host_option_parse():
    args = cli.build_parser().parse_args(["--port", "rx1=/dev/cu.a", "--port", "rx2=/dev/cu.b",
                                          "--host", "0.0.0.0", "--http-port", "8123"])
    assert args.port == ["rx1=/dev/cu.a", "rx2=/dev/cu.b"]
    assert args.host == "0.0.0.0" and args.http_port == 8123


def test_exactly_one_source_is_required():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        cli.check_source(parser.parse_args([]))
    with pytest.raises(SystemExit):
        cli.check_source(parser.parse_args(["--simulate-room", "--port", "rx1=/dev/cu.a"]))


def test_serves_the_simulated_room_over_http_and_stops_cleanly_on_ctrl_c():
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "dashboard.py"), "--simulate-room",
                             "--http-port", str(port)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        payload = _wait_for_hot_room(port, timeout=60)
        assert payload["status"]["receivers"] == {"rx1": "connected", "rx2": "connected"}
        assert payload["status"]["finished"] is False
        assert payload["status"]["uncalibrated_links"] == []  # calibrated from the simulated empty room
        assert max(payload["heatmap"]["cells"]) > 0.05
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            _, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
    assert proc.returncode == 0, err
    assert "Traceback" not in err


def _wait_for_hot_room(port: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as resp:
                payload = json.loads(resp.read())
            if payload["links"] and max(payload["heatmap"]["cells"]) > 0.05:
                return payload
        except OSError as e:
            last_error = e
        time.sleep(0.2)
    raise AssertionError(f"no hot room on port {port}: {last_error}")
