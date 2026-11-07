#!/usr/bin/env python
"""Live room state: EMPTY, MOTION or STILL_PRESENT (breathing), debounced.

    detect_live.py --simulate --duration 65                  # demo cycle, no hardware
    detect_live.py --port /dev/ttyUSB0 --threshold-file calibration.json
    detect_live.py --port /dev/ttyUSB0 --calibrate 60 --out calibration.json   # room empty, receiver reset first!
    detect_live.py --port /dev/ttyUSB0 --threshold 12 --breathing              # opt-in breathing (experimental)

Motion-only by default: states are EMPTY and MOTION. Real ports need an explicit
--threshold or --threshold-file (calibrate first). The simulator uses a default.
--breathing turns on the breathing detector (STILL_PRESENT); it is off by default
because it false-fires on empty rooms on real hardware. Breathing needs a full
15 s window; until then the output says "not enough data".
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.common import add_threshold_args, resolve_threshold, simulated_rate_hz  # noqa: E402
from csi.detect import MOTION, SIM_THRESHOLD, STILL_PRESENT, CalibrationError, Reading, calibrate, detect  # noqa: E402
from csi.parser import CSIParser, Stream, serial_stream  # noqa: E402
from csi.simulate import Segment, simulate_stream  # noqa: E402

SIM_DEMO_SCENARIO = (Segment("empty", 20), Segment("motion", 10), Segment("still", 30))  # 60 s cycle
STILL_REPRINT_S = 5.0  # while STILL_PRESENT, refresh the bpm line this often (host time)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_argument_group("packet source")
    g.add_argument("--simulate", action="store_true", help="use fake CSI, no hardware")
    g.add_argument("--port", help="serial port of the csi_recv node")
    g.add_argument("--baud", type=int, default=921600)
    g.add_argument("--rate", type=float, default=None,
                   help="sender's nominal send rate in Hz (simulator; default 100)")
    g.add_argument("--seed", type=int, default=0, help="simulator seed")
    add_threshold_args(ap)
    c = ap.add_argument_group("calibration")
    c.add_argument("--calibrate", type=float, metavar="SECONDS", help="learn threshold from an empty room")
    c.add_argument("--out", type=Path, default=Path("calibration.json"), help="where to save it")
    ap.add_argument("--breathing", action="store_true",
                    help="opt in to breathing detection (STILL_PRESENT); off by default")
    ap.add_argument("--debounce", type=float, default=0.5, help="seconds a state must persist")
    ap.add_argument("--duration", type=float, help="stop after this many seconds (default: run until Ctrl+C)")
    return ap


def open_stream(args: argparse.Namespace, calibrating: bool = False) -> Stream:
    if args.simulate:
        scenario = (Segment("empty", args.calibrate),) if calibrating else SIM_DEMO_SCENARIO
        return simulate_stream(scenario=scenario, rate_hz=simulated_rate_hz(args), seed=args.seed,
                               realtime=not calibrating, loop=not calibrating)
    if args.port:
        return serial_stream(args.port, args.baud)
    raise SystemExit("error: pass --port <serial port> or --simulate")


def until(stream: Stream, seconds: float | None) -> Iterator[tuple[float, str]]:
    """Cut a stream after ``seconds`` of host time; closes the source on exit."""
    start = None
    try:
        for host_ts, line in stream:
            if start is None and line.startswith("CSI_DATA"):
                start = host_ts
            if seconds is not None and start is not None and host_ts - start > seconds:
                return
            yield host_ts, line
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()


def run_calibration(args: argparse.Namespace) -> int:
    parser = CSIParser()
    print(f"calibrating for {args.calibrate:.0f}s: keep the room empty...", flush=True)
    try:
        cal = calibrate(until(open_stream(args, calibrating=True), args.calibrate), parser)
    except CalibrationError as e:
        print(f"calibration failed: {e}", file=sys.stderr)
        return 1
    cal.save(args.out)
    print(f"clean {cal.clean_s:.1f} s, rejected {cal.rejected_s:.1f} s as movement "
          f"({cal.pkt_rate_hz:.1f} pkt/s, loss {cal.loss:.1%})")
    print(f"threshold {cal.threshold:.3f} (rate {cal.rate_hz:.0f} Hz, loss {cal.loss:.1%}) -> {args.out}")
    return 0


def describe(r: Reading, breathing: bool) -> str:
    """Human-readable detail for the state line."""
    if r.state == MOTION or not breathing:
        return f"score {r.score:.2f}"
    if r.state == STILL_PRESENT and r.bpm is not None:
        return f"{r.bpm:.1f} breaths/min, confidence {r.confidence:.2f}"
    if r.state == STILL_PRESENT:
        return "breathing held, re-checking"  # debounce hold while breathing drops out
    if not r.enough_data:
        return f"score {r.score:.2f}, breathing: not enough data"
    return f"score {r.score:.2f}, no breathing (confidence {r.confidence:.2f})"


def run_live(args: argparse.Namespace) -> int:
    threshold = resolve_threshold(args, SIM_THRESHOLD)
    parser = CSIParser()
    mode = "motion + breathing" if args.breathing else "motion-only"
    print(f"threshold {threshold:.3f} ({mode}); Ctrl+C to stop", flush=True)
    state, printed_at = None, 0.0
    try:
        readings = detect(until(open_stream(args), args.duration), threshold, parser, args.debounce,
                          breathing=args.breathing)
        for r in readings:
            due = r.state == STILL_PRESENT and r.host_ts - printed_at >= STILL_REPRINT_S
            if r.state != state or due:
                state, printed_at = r.state, r.host_ts
                print(f"{time.strftime('%H:%M:%S', time.localtime(r.host_ts))}  {state:<13} "
                      f"{describe(r, args.breathing)}", flush=True)
    except KeyboardInterrupt:
        pass
    s = parser.stats
    print(f"packets {s.parsed}, malformed {s.malformed}, layout-skipped {s.layout_skipped}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return run_calibration(args) if args.calibrate else run_live(args)


if __name__ == "__main__":
    sys.exit(main())
