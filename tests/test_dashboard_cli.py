"""scripts/dashboard.py: the CLI contract. Only --simulate-room is run; no port is ever opened."""
import json
import math
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_evaluate import record_plan

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


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def test_speed_is_a_positive_factor_or_max():
    assert parse("--replay", "run.jsonl", "--speed", "8").speed == 8.0
    assert parse("--replay", "run.jsonl", "--speed", "max").speed == math.inf
    with pytest.raises(SystemExit):
        parse("--replay", "run.jsonl", "--speed", "0")
    with pytest.raises(SystemExit):
        parse("--replay", "run.jsonl", "--speed", "fast")


def test_replay_session_log_and_margin_options_parse():
    args = parse("--replay-session", "data/hw5", "--margin", "1.5", "--log", "run.jsonl")
    assert args.replay_session == Path("data/hw5") and args.margin == 1.5
    assert args.log == Path("run.jsonl") and args.replay is None


def test_replay_sources_are_exclusive_and_their_options_fit_them():
    cli.check_source(parse("--replay", "run.jsonl", "--speed", "max"))
    cli.check_source(parse("--replay-session", "data/hw5", "--speed", "8"))
    with pytest.raises(SystemExit):
        cli.check_source(parse("--replay", "run.jsonl", "--simulate-room"))
    with pytest.raises(SystemExit):
        cli.check_source(parse("--replay", "run.jsonl", "--replay-session", "data/hw5"))
    with pytest.raises(SystemExit):
        cli.check_source(parse("--replay", "run.jsonl", "--log", "again.jsonl"))
    with pytest.raises(SystemExit):
        cli.check_source(parse("--replay", "run.jsonl", "--thresholds", "links.json"))
    with pytest.raises(SystemExit):
        cli.check_source(parse("--simulate-room", "--speed", "8"))
    with pytest.raises(SystemExit):
        cli.check_source(parse("--simulate-room", "--margin", "1.5"))


def run_cli(*argv: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "dashboard.py"), *argv], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


def test_replay_of_a_missing_log_exits_with_a_clear_error(tmp_path):
    result = run_cli("--replay", str(tmp_path / "missing.jsonl"))
    assert result.returncode != 0
    assert "cannot read replay log" in result.stderr
    assert "Traceback" not in result.stderr


def test_replay_of_a_corrupt_log_names_the_bad_line(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"format": "csi-dashboard-log", "version": 1}\n{"links": [\n')
    result = run_cli("--replay", str(bad))
    assert result.returncode != 0
    assert "line 2" in result.stderr
    assert "Traceback" not in result.stderr


def test_replay_session_serves_the_recorded_walk_and_logs_it_with_its_thresholds_source(tmp_path):
    folder = record_plan(tmp_path / "room", [("empty", 20), ("walking", 10)])
    log, port = tmp_path / "session.jsonl", free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "dashboard.py"), "--replay-session", str(folder),
                             "--speed", "max", "--margin", "1.5", "--log", str(log), "--http-port", str(port)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        payload = _wait_for_finished(port, timeout=120)
        assert payload["status"]["receivers"] == {"rx1": "finished"}
        assert any("single link" in m for m in payload["status"]["messages"])
        lines = log.read_text().splitlines()
        header = json.loads(lines[0])
        assert header["thresholds_source"].startswith("calibrated on empty_")
        motion = [json.loads(line)["links"] for line in lines[1:]]
        assert any(row["state"] == "MOTION" and row["id"] == "rx1<-00" for links in motion for row in links)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            _, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
    assert proc.returncode == 0, err
    assert "Traceback" not in err


def _wait_for_finished(port: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as resp:
                payload = json.loads(resp.read())
            if payload["status"]["finished"]:
                return payload
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"replay on port {port} did not finish")


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
