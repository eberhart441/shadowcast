#!/usr/bin/env python
"""One-line live telemetry: link health, motion score vs threshold, state, breathing.

    telemetry.py --port /dev/cu.usbmodem1101 --threshold-file calibration.json
    telemetry.py --port /dev/cu.usbmodem1101 --threshold 12
    telemetry.py --simulate                           # simulator default threshold
    telemetry.py --port ... --threshold-file calibration.json --breathing   # opt-in breathing

A real port needs --threshold-file or --threshold (calibrate with detect_live.py --calibrate first).
Motion-only by default; --breathing also shows bpm (experimental).

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

from csi.common import (SenderWatch, add_source_args, add_threshold_args, link_send_rate, open_source,  # noqa: E402
                        resolve_threshold)
from csi.detect import SIM_THRESHOLD, detect_packets  # noqa: E402
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
    add_threshold_args(ap)
    ap.add_argument("--breathing", action="store_true",
                    help="opt in to breathing detection (STILL_PRESENT, bpm); off by default")
    ap.add_argument("--every", type=float, default=0.5, help="seconds between screen updates")
    args = ap.parse_args()
    threshold = resolve_threshold(args, SIM_THRESHOLD)

    parser, health, last = CSIParser(), LinkHealth(send_rate_hz=link_send_rate(args)), {"rssi": 0}
    watch = SenderWatch(lambda message: print(message, file=sys.stderr, flush=True))
    packets = watch.observe_all(observed(open_source(args, parser), health, last))
    next_print = 0.0
    try:
        for r in detect_packets(packets, threshold, breathing=args.breathing):
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
