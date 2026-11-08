"""scripts/flash.sh: the printed commands, the refusals and the usage errors.

Dry runs touch nothing. The flash path (no --dry-run) runs against a copy of the repo under tmp_path, with
fake `idf.py` and `python` shims first on PATH, so no real port, board or build is involved. The dashboard
probe goes to an ephemeral local port (a fake dashboard, or a closed port), never to 127.0.0.1:8000.
"""
import os
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "flash.sh"

IDF_SHIM = """#!{exe}
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("idf " + os.getcwd() + " " + " ".join(args) + "\\n")
if "build" in args and os.environ.get("FAKE_NO_FLASH_ARGS") != "1":
    build = args[args.index("-B") + 1]
    os.makedirs(build, exist_ok=True)
    with open(os.path.join(build, "flash_args"), "w") as f:
        f.write("--flash_mode dio\\n0x0 bootloader/bootloader.bin\\n")
"""

PY_SHIM = """#!{exe}
import os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("python " + os.getcwd() + " " + " ".join(sys.argv[1:]) + "\\n")
chip = os.environ.get("FAKE_CHIP", "ESP32-C5")
if "chip_id" in sys.argv and chip:
    print("Connected to " + chip + " on /dev/fake:")
    print("Chip type:           " + chip + " (revision v1.1)")
"""


def closed_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@contextmanager
def fake_dashboard():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"dashboard"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def write_exec(path: Path, text: str) -> None:
    path.write_text(text.format(exe=sys.executable))
    path.chmod(0o755)


@pytest.fixture
def shims(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_exec(bin_dir / "idf.py", IDF_SHIM)
    write_exec(bin_dir / "python", PY_SHIM)
    return bin_dir, tmp_path / "calls.log"


def make_env(shims, tmp_path, **over) -> dict:
    bin_dir, log = shims
    env = {k: v for k, v in os.environ.items() if k not in ("IDF_PATH", "FLASH_DASHBOARD_URL")}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        IDF_PATH=str(tmp_path / "idf"),
        FLASH_DASHBOARD_URL=closed_url(),
        FAKE_LOG=str(log),
        FAKE_CHIP="ESP32-C5",
    )
    env.update(over)
    return env


def flash(args, env, script=SCRIPT) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo(tmp_path):
    """A copy of the scripts and firmware trees, so a real flash run writes only under tmp_path."""
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, r / "scripts" / "flash.sh")
    shutil.copytree(ROOT / "firmware", r / "firmware",
                    ignore=shutil.ignore_patterns("build*", "managed_components"))
    return r


def test_script_parses_under_bash():
    result = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_dry_run_receiver_prints_every_command_and_runs_none(shims, tmp_path):
    env = make_env(shims, tmp_path)
    result = flash(["receiver", "esp32c5", "/dev/cu.fake", "--dry-run"], env)

    assert result.returncode == 0, result.stderr
    src = ROOT / "firmware" / "csi_recv"
    build = src / "build_esp32c5"
    lines = result.stdout.splitlines()
    assert f"+ idf.py -C {src} -B {build} -DSDKCONFIG={build}/sdkconfig set-target esp32c5" in lines
    assert f"+ idf.py -C {src} -B {build} -DSDKCONFIG={build}/sdkconfig build" in lines
    assert "+ python -m esptool --port /dev/cu.fake chip_id" in lines
    assert f"+ cd {build}" in lines
    assert "+ python -m esptool --port /dev/cu.fake write_flash @flash_args" in lines
    assert "SENDER_ID" not in result.stdout
    assert not Path(env["FAKE_LOG"]).exists(), "dry run must not call idf.py or esptool"


def test_dry_run_sender_gets_its_own_build_dir_and_sender_id(shims, tmp_path):
    env = make_env(shims, tmp_path)
    result = flash(["sender", "esp32s3", "/dev/cu.fake", "0x0A", "--dry-run"], env)

    assert result.returncode == 0, result.stderr
    src = ROOT / "firmware" / "csi_send"
    build = src / "build_esp32s3_id0a"
    lines = result.stdout.splitlines()
    assert f"+ idf.py -C {src} -B {build} -DSDKCONFIG={build}/sdkconfig -DSENDER_ID=0x0A set-target esp32s3" in lines
    assert f"+ idf.py -C {src} -B {build} -DSDKCONFIG={build}/sdkconfig -DSENDER_ID=0x0A build" in lines
    assert f"+ cd {build}" in lines


def test_two_chips_from_one_example_get_separate_sdkconfigs(shims, tmp_path):
    env = make_env(shims, tmp_path)
    c5 = flash(["receiver", "esp32c5", "/dev/cu.fake", "--dry-run"], env).stdout
    s3 = flash(["receiver", "esp32s3", "/dev/cu.fake", "--dry-run"], env).stdout
    assert "build_esp32c5/sdkconfig" in c5 and "build_esp32s3/sdkconfig" not in c5
    assert "build_esp32s3/sdkconfig" in s3 and "build_esp32c5/sdkconfig" not in s3


def test_receiver_is_refused_while_dashboard_answers(shims, tmp_path):
    with fake_dashboard() as url:
        env = make_env(shims, tmp_path, FLASH_DASHBOARD_URL=url)
        result = flash(["receiver", "esp32c5", "/dev/cu.fake", "--dry-run"], env)

    assert result.returncode == 1
    assert "refusing to flash a receiver" in result.stderr
    assert "re-baseline" in result.stderr
    assert "set-target" not in result.stdout


def test_force_overrides_the_dashboard_refusal(shims, tmp_path):
    with fake_dashboard() as url:
        env = make_env(shims, tmp_path, FLASH_DASHBOARD_URL=url)
        result = flash(["receiver", "esp32c5", "/dev/cu.fake", "--dry-run", "--force"], env)

    assert result.returncode == 0, result.stderr
    assert "continuing because of --force" in result.stderr
    assert "set-target esp32c5" in result.stdout


def test_sender_is_not_refused_by_a_running_dashboard(shims, tmp_path):
    with fake_dashboard() as url:
        env = make_env(shims, tmp_path, FLASH_DASHBOARD_URL=url)
        result = flash(["sender", "esp32c5", "/dev/cu.fake", "0x01", "--dry-run"], env)

    assert result.returncode == 0, result.stderr


USAGE_CASES = [
    ([], "need <role> <chip> <port>"),
    (["receiver", "esp32c5"], "need <role> <chip> <port>"),
    (["lidar", "esp32c5", "/dev/cu.fake"], "role must be receiver or sender"),
    (["receiver", "esp32h2", "/dev/cu.fake"], "chip must be esp32c5 or esp32s3"),
    (["receiver", "esp32c5", ""], "port is empty"),
    (["sender", "esp32c5", "/dev/cu.fake"], "a sender needs a sender_id"),
    (["sender", "esp32c5", "/dev/cu.fake", "1"], "sender_id must look like 0x01"),
    (["sender", "esp32c5", "/dev/cu.fake", "0x1"], "sender_id must look like 0x01"),
    (["sender", "esp32c5", "/dev/cu.fake", "0xZZ"], "sender_id must look like 0x01"),
    (["receiver", "esp32c5", "/dev/cu.fake", "0x01"], "takes no sender_id"),
    (["receiver", "esp32c5", "/dev/cu.fake", "extra", "more"], "too many arguments"),
    (["receiver", "esp32c5", "/dev/cu.fake", "--bogus"], "unknown option: --bogus"),
]


@pytest.mark.parametrize("args, message", USAGE_CASES)
def test_usage_errors_exit_2(shims, tmp_path, args, message):
    result = flash([*args, "--dry-run"], make_env(shims, tmp_path))

    assert result.returncode == 2, result.stdout
    assert message in result.stderr
    assert "usage: scripts/flash.sh" in result.stderr


def test_help_exits_0(shims, tmp_path):
    result = flash(["--help"], make_env(shims, tmp_path))

    assert result.returncode == 0
    assert "usage: scripts/flash.sh" in result.stdout


def test_missing_idf_path_is_reported(shims, tmp_path):
    env = make_env(shims, tmp_path)
    del env["IDF_PATH"]
    result = flash(["receiver", "esp32c5", "/dev/cu.fake", "--dry-run"], env)

    assert result.returncode == 1
    assert "IDF_PATH is not set" in result.stderr


def test_missing_idf_py_is_reported(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "IDF_PATH": str(tmp_path), "FLASH_DASHBOARD_URL": closed_url()}
    result = flash(["receiver", "esp32c5", "/dev/cu.fake", "--dry-run"], env)

    assert result.returncode == 1
    assert "idf.py is not on PATH" in result.stderr


def test_flash_refuses_when_port_holds_another_chip(repo, shims, tmp_path):
    env = make_env(shims, tmp_path, FAKE_CHIP="ESP32-S3")
    result = flash(["receiver", "esp32c5", "/dev/cu.fake"], env, script=repo / "scripts" / "flash.sh")

    assert result.returncode == 1
    assert "holds a ESP32-S3, not ESP32-C5" in result.stderr
    calls = Path(env["FAKE_LOG"]).read_text()
    assert "idf " in calls and "build" in calls, "the build runs before the port check"
    assert "write_flash @flash_args" not in calls, "nothing may be flashed on a mismatch"


def test_flash_refuses_when_chip_cannot_be_read(repo, shims, tmp_path):
    env = make_env(shims, tmp_path, FAKE_CHIP="")
    result = flash(["sender", "esp32c5", "/dev/cu.fake", "0x01"], env, script=repo / "scripts" / "flash.sh")

    assert result.returncode == 1
    assert "could not read the chip type" in result.stderr
    assert "write_flash @flash_args" not in Path(env["FAKE_LOG"]).read_text()


def test_flash_stops_when_the_build_left_no_flash_args(repo, shims, tmp_path):
    env = make_env(shims, tmp_path, FAKE_NO_FLASH_ARGS="1")
    result = flash(["sender", "esp32c5", "/dev/cu.fake", "0x01"], env, script=repo / "scripts" / "flash.sh")

    assert result.returncode == 1
    assert "no flash_args" in result.stderr
    assert "chip_id" not in Path(env["FAKE_LOG"]).read_text()


def test_flash_runs_write_flash_from_inside_the_build_dir(repo, shims, tmp_path):
    env = make_env(shims, tmp_path, FAKE_CHIP="ESP32-C5")
    result = flash(["sender", "esp32c5", "/dev/cu.fake", "0x01"], env, script=repo / "scripts" / "flash.sh")

    assert result.returncode == 0, result.stderr
    build = (repo / "firmware" / "csi_send" / "build_esp32c5_id01").resolve()
    calls = Path(env["FAKE_LOG"]).read_text().splitlines()
    flash_calls = [c for c in calls if c.startswith("python ") and "write_flash" in c]
    assert flash_calls == [f"python {build} -m esptool --port /dev/cu.fake write_flash @flash_args"]
    assert any("-DSENDER_ID=0x01" in c and "build" in c for c in calls)
