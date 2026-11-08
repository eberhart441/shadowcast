"""scripts/calibrate.py against the repo's own dashboard server on an ephemeral port, fed by the realtime simulator.

The seam is the HTTP interface plus the saved thresholds file (read back with load_thresholds). The realtime
simulator stamps packets with the wall clock, so the server is live while the script polls it.
"""
import importlib.util
import json
import urllib.request
from pathlib import Path

import pytest

from csi.links import load_thresholds
from csi.noisy_calibration import LinkThreshold
from csi.room import load_room
from csi.server import DashboardServer
from csi.simulate import simulate_room

ROOT = Path(__file__).resolve().parents[1]
ROOM = load_room()
LABELS = {"rx1<-00", "rx1<-01", "rx2<-00", "rx2<-01"}
SECONDS = "15"  # about 60 polls at 4 Hz: enough for the 40-score minimum with margin


def load_script():
    spec = importlib.util.spec_from_file_location("calibrate_script", ROOT / "scripts" / "calibrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calibrate = load_script()


def result(threshold, reason=None):
    return LinkThreshold(readings=50, empty=0, median=10.0, spread=1.0, max=20.0, threshold=threshold, reason=reason)


def test_plan_apply_refuses_when_a_link_has_no_threshold_and_changes_nothing():
    thresholds, refusal = calibrate.plan_apply({"rx1<-00": result(50.0), "rx2<-00": result(None, "few")}, False)

    assert thresholds == {}
    assert "rx2<-00" in refusal


def test_plan_apply_with_skip_bad_links_applies_only_the_links_that_have_a_threshold():
    thresholds, refusal = calibrate.plan_apply({"rx1<-00": result(50.0), "rx2<-00": result(None, "few")}, True)

    assert refusal is None
    assert thresholds == {"rx1<-00": 50.0}


def test_plan_apply_refuses_when_no_link_has_a_threshold_even_with_skip():
    thresholds, refusal = calibrate.plan_apply({"rx2<-00": result(None, "few")}, True)

    assert thresholds == {}
    assert refusal


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """A dashboard on an ephemeral port, its thresholds file in a temp directory, fed by a live simulated room."""
    folder = tmp_path_factory.mktemp("calibrate")
    streams = simulate_room(None, room=ROOM, duration_s=120, seed=4, realtime=True)
    server = DashboardServer(ROOM, streams, None, port=0, thresholds_path=folder / "links_thresholds.json")
    server.start()
    try:
        yield server, folder / "links_thresholds.json"
    finally:
        server.stop()


def get_thresholds(server) -> dict[str, float]:
    with urllib.request.urlopen(server.url + "/api/thresholds", timeout=5) as resp:
        return json.loads(resp.read())["thresholds"]


def report_rows(out: str) -> dict[str, list[str]]:
    rows = {}
    for line in out.splitlines():
        fields = line.split()
        if fields and fields[0] in LABELS:
            rows[fields[0]] = fields
    return rows


@pytest.mark.slow
def test_report_samples_every_link_and_changes_no_threshold(live_server, capsys):
    server, _ = live_server

    code = calibrate.main(["--url", server.url, "--seconds", SECONDS])

    out = capsys.readouterr().out
    assert code == 0
    rows = report_rows(out)
    assert set(rows) == LABELS
    for label, fields in rows.items():
        samples, empty = int(fields[1]), int(fields[2])
        assert samples >= 40, (label, fields)
        assert empty < samples  # most polls of the live simulated room have a score
    assert get_thresholds(server) == {}  # no --apply: the dashboard's thresholds are untouched
    assert not live_server[1].exists()  # and nothing was saved


@pytest.mark.slow
def test_apply_sets_each_threshold_on_the_dashboard_and_saves_the_file(live_server, capsys):
    server, path = live_server

    code = calibrate.main(["--url", server.url, "--seconds", SECONDS, "--apply"])

    out = capsys.readouterr().out
    assert code == 0, out
    printed = {label: float(fields[-1]) for label, fields in report_rows(out).items()}
    assert set(printed) == LABELS
    saved = load_thresholds(path)
    assert saved == pytest.approx(printed, rel=1e-3)  # the table shows 4 significant figures
    assert get_thresholds(server) == pytest.approx(printed, rel=1e-3)  # the live dashboard was set too
