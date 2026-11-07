"""record.py keyboard reader: the terminal must be back in its original mode when stop() returns."""
import os
import pty
import sys
import termios
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import record  # noqa: E402


def settings(fd):
    """termios attributes minus PENDIN, which the kernel sets transiently on a mode change."""
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~termios.PENDIN
    return attrs


@pytest.fixture
def tty(monkeypatch):
    master, slave = pty.openpty()
    f = os.fdopen(slave, "r", closefd=False)  # isatty() is True, fileno() is the pty
    monkeypatch.setattr(sys, "stdin", f)
    try:
        yield slave
    finally:
        f.close()
        os.close(master)
        os.close(slave)


def test_stop_restores_the_terminal_before_returning(tty):
    before = settings(tty)
    poll, stop = record.keyboard_poll()
    assert not termios.tcgetattr(tty)[3] & termios.ICANON  # cbreak while recording
    stop()
    assert settings(tty) == before


def test_stop_restores_even_if_called_right_away_and_is_idempotent(tty):
    before = settings(tty)
    _, stop = record.keyboard_poll()
    stop()
    stop()
    assert settings(tty) == before


def test_no_terminal_means_no_changes_and_a_working_poll(monkeypatch):
    class NotATty:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", NotATty())
    poll, stop = record.keyboard_poll()
    assert poll() is None
    stop()
