#!/usr/bin/env python
"""One-line live telemetry: link health, motion score vs threshold, state, breathing.

    telemetry.py --port /dev/cu.usbmodem1101 --threshold-file data/hw3/calib.json
    telemetry.py --simulate --threshold 2.0

Needs the serial port to itself: don't run it while record.py or another reader
is using the same port. For recordings in progress, tail the recorder log instead.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.common import add_source_args, open_source  # noqa: E402
from csi.detect import detect_packets, load_threshold  # noqa: E402
from csi.health import LinkHealth  # noqa: E402
from csi.parser import CSIPacket, CSIParser  # noqa: E402

COLORS = {"EMPTY": "\033[32m", "MOTION": "\033[31m", "STILL_PRESENT": "\033[36m"}
RESET = "\033[0m"
BAR_W = 30


def bar(score: float, threshold: float) -> str:
    """Bar scaled so the threshold sits at 2/3 width; '|' marks it."""
    full = max(threshold * 1.5, 1e-9)
    n = min(BAR_W, int(BAR_W * score / full))
    mark = int(BAR_W * threshold / full)
    cells = ["#" if i < n else "." for i in range(BAR_W)]
    cells[min(mark, BAR_W - 1)] = "|"
    return "".join(cells)


def observed(packets: Iterator[CSIPacket], health: LinkHealth, last: dict) -> Iterator[CSIPacket]:
    for p in packets:
        health.observe(p)
        last["rssi"] = p.rssi
        yield p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(ap)
    ap.add_argument("--threshold-file")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--every", type=float, default=0.5, help="seconds between screen updates")
    args = ap.parse_args()
    if args.threshold_file:
        threshold = load_threshold(args.threshold_file)
    elif args.threshold is not None:
        threshold = args.threshold
    else:
        raise SystemExit("error: pass --threshold-file or --threshold")

    parser, health, last = CSIParser(), LinkHealth(send_rate_hz=args.rate), {"rssi": 0}
    packets = observed(open_source(args, parser), health, last)
    next_print = 0.0
    try:
        for r in detect_packets(packets, threshold):
            if time.time() < next_print:
                continue
            next_print = time.time() + args.every
            extra = f" {r.bpm:4.1f} bpm ({r.confidence:.2f})" if r.bpm is not None else ""
            line = (f"{COLORS.get(r.state, '')}{r.state:<13}{RESET} score {r.score:6.1f} "
                    f"[{bar(r.score, threshold)}] thr {threshold:.1f} | "
                    f"{health.packet_rate():5.1f} pkt/s drops {health.drops} rssi {last['rssi']}{extra}")
            print("\r" + line + "\033[K", end="", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"\nparsed {parser.stats.parsed} malformed {parser.stats.malformed}")


if __name__ == "__main__":
    main()
