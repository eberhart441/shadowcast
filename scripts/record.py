#!/usr/bin/env python
"""Record labeled CSI into a session folder.

    python scripts/record.py --simulate --session demo --label empty
    python scripts/record.py --port /dev/cu.usbserial-0001 --session room1 --label walking

Keys while running: e=empty  w=walking  s=still_standing  l=still_sitting_or_lying  q=stop.
Each label switch closes the file and starts a new one after a countdown.
"""
from __future__ import annotations

import argparse
import contextlib
import queue
import select
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csi.common import add_source_args  # noqa: E402
from csi.parser import serial_stream  # noqa: E402
from csi.recording import LABELS, record_session  # noqa: E402
from csi.simulate import simulate_stream  # noqa: E402

KEYS = {"e": "empty", "w": "walking", "s": "still_standing", "l": "still_sitting_or_lying"}
QUIT_KEY = "q"


def label_arg(value: str) -> str:
    if value not in LABELS:
        raise argparse.ArgumentTypeError(f"invalid label {value!r}; choose one of: {', '.join(LABELS)}")
    return value


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(ap)
    ap.add_argument("--session", required=True, help="session folder name")
    ap.add_argument("--data-dir", default="data", help="parent directory for sessions")
    ap.add_argument("--label", type=label_arg, default="empty", help="initial label")
    ap.add_argument("--countdown", type=float, default=5.0, help="seconds before each recording")
    ap.add_argument("--notes", default="", help="node placement notes for the metadata")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    return ap.parse_args(argv)


def keyboard_poll() -> tuple[Callable[[], Optional[str]], Callable[[], None]]:
    """Return (poll_label, stop). Non-blocking single-key reads in a daemon thread."""
    pending: queue.SimpleQueue = queue.SimpleQueue()
    done = threading.Event()

    def reader() -> None:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not done.is_set():
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    pending.put(sys.stdin.read(1).lower())
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    if sys.stdin.isatty():
        threading.Thread(target=reader, daemon=True).start()

    def poll() -> Optional[str]:
        with contextlib.suppress(queue.Empty):
            key = pending.get_nowait()
            if key == QUIT_KEY:
                raise KeyboardInterrupt
            return KEYS.get(key)
        return None

    return poll, done.set


def make_stream(args: argparse.Namespace) -> Iterator[tuple[float, str]]:
    if args.simulate:
        return simulate_stream(rate_hz=args.rate, seed=args.seed, realtime=True, loop=True)
    if args.port:
        return serial_stream(args.port, args.baud)
    raise SystemExit("error: pass --port <serial port> or --simulate")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    stream = make_stream(args)
    poll, stop = keyboard_poll()
    print(f"recording to {Path(args.data_dir) / args.session}; keys: "
          + ", ".join(f"{k}={v}" for k, v in KEYS.items()) + f", {QUIT_KEY}=stop")
    try:
        report = record_session(
            stream, Path(args.data_dir) / args.session, args.label,
            send_rate_hz=args.rate, countdown_s=args.countdown, placement_notes=args.notes,
            duration_s=args.duration, poll_label=poll,
            on_countdown=lambda n: print(f"starting in {n}...", flush=True),
        )
    finally:
        stop()
        close = getattr(stream, "close", None)
        if close:
            close()  # releases the serial port
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
