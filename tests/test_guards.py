"""Guards that keep the test suite independent of the live rig config (config/room.json).

conftest.py pins CSI_ROOM to tests/fixtures/room_rig4.json before any test imports csi. These tests fail if that
pin is lost, or if a test file refers to the live config again.
"""
from pathlib import Path

import csi.room

ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIG = (ROOT / "config" / "room.json").resolve()
# The one test module that deliberately loads the real config, and this file (it names the path to check for).
EXEMPT = {"test_config_driven_nodes.py", "test_guards.py"}


def test_default_room_path_is_not_the_live_rig_config():
    assert Path(csi.room.DEFAULT_ROOM_PATH).resolve() != LIVE_CONFIG


def test_no_test_file_refers_to_the_live_rig_config():
    needle = "config/room.json"
    offenders = []
    for path in sorted((ROOT / "tests").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.name in EXEMPT:
            continue
        if needle in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"these files refer to {needle}; use tests/fixtures/room_rig4.json instead: {offenders}"


def test_check_script_reports_usage_and_rejects_unknown_flags():
    # scripts/check.sh runs under macOS bash 3.2 (set -u), where an empty array expansion once crashed it.
    import subprocess

    script = ROOT / "scripts" / "check.sh"
    help_run = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert help_run.returncode == 0 and "check.sh" in help_run.stdout
    bad_run = subprocess.run(["bash", str(script), "--nope"], capture_output=True, text=True)
    assert bad_run.returncode == 2 and "usage" in bad_run.stderr
