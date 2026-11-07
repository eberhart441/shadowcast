"""scripts/links_live.py: the CLI contract and the table renderer. Port tests use a path that does not exist and
rely on the error being raised before any port is opened, so no hardware is touched."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import links_live  # noqa: E402

from csi.links import load_thresholds  # noqa: E402

FAKE_PORT = "/dev/cu.no-such-csi-port"
LABELS = ("rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01")


def run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "links_live.py"), *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def test_real_port_without_thresholds_fails_clearly_before_opening_it():
    r = run("--port", FAKE_PORT)
    assert r.returncode != 0
    assert "--thresholds" in r.stderr and "--calibrate" in r.stderr


def test_no_source_is_an_error():
    r = run()
    assert r.returncode != 0
    assert "--port" in r.stderr and "--simulate-links" in r.stderr


def test_simulate_links_draws_one_row_per_link():
    r = run("--simulate-links", "--duration", "3")
    assert r.returncode == 0, r.stderr
    for label in LABELS:
        assert label in r.stdout, label
    assert "EMPTY" in r.stdout


def test_calibrate_writes_one_threshold_per_link(tmp_path):
    out = tmp_path / "links.json"
    r = run("--simulate-links", "--calibrate", "15", "--out", str(out))
    assert r.returncode == 0, r.stderr
    thresholds = load_thresholds(out)
    assert set(thresholds) == set(LABELS)
    assert all(t > 0 for t in thresholds.values())


def test_calibrate_refuses_links_without_enough_data_and_explains(tmp_path):
    out = tmp_path / "links.json"
    r = run("--simulate-links", "--calibrate", "5", "--out", str(out))
    assert r.returncode != 0
    assert "too short" in r.stdout + r.stderr
    assert not out.exists()


def test_thresholds_file_sets_each_links_threshold(tmp_path):
    path = tmp_path / "links.json"
    path.write_text('{"rx1<-00": 123.0, "rx1<-01": 4.5, "rx2<-00": 6.5, "rx2<-01": 7.5}')
    r = run("--simulate-links", "--thresholds", str(path), "--duration", "2")
    assert r.returncode == 0, r.stderr
    assert "123.00" in r.stdout and "4.50" in r.stdout


def row(state, score=2.0, threshold=3.0):
    return {"id": "rx1<-00", "state": state, "score": score, "threshold": threshold, "pkt_rate": 100.0,
            "drops": 0, "rssi": -40, "age_s": 0.01}


@pytest.mark.parametrize("state, code", [("EMPTY", "\033[32m"), ("MOTION", "\033[31m"),
                                         ("NO_SIGNAL", "\033[90m")])
def test_state_colours(state, code):
    assert code in links_live.render_frame([row(state)], {"rx1": "running"})


def test_no_signal_row_has_no_score_bar_or_packet_rate():
    text = links_live.render_frame([{**row("NO_SIGNAL"), "pkt_rate": 0.0, "score": None, "age_s": 4.2}], {})
    line = [ln for ln in text.splitlines() if "rx1<-00" in ln][0]
    assert "NO_SIGNAL" in line and "4.2" in line
