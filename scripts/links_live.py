#!/usr/bin/env python
"""Live per-link table: one row per (receiver, sender) link, state, score vs threshold, rate, drops, RSSI.

    links_live.py --simulate-links                             # 2 senders x 2 receivers, no hardware
    links_live.py --simulate-links --calibrate 15 --out links.json
    links_live.py --port rx1=/dev/cu.usbmodem2101 --port rx2=/dev/cu.usbserial-0001 --thresholds links.json
    links_live.py --port rx1=/dev/cu.usbmodem2101 --calibrate 60 --out links.json   # empty room, reset receivers first

Each link is calibrated and judged on its own, since each has its own signal scale. Real ports need
--thresholds (from --calibrate) and refuse to start without it. --simulate-links uses the simulator
default for every link unless --thresholds is given. Links without enough calibration data get no
threshold and are reported, not guessed.

States: EMPTY (green), MOTION (red), NO_SIGNAL (grey: no packet for 2 s), WARMUP (no full window yet),
UNCALIBRATED (no threshold for that link). Ctrl+C stops and releases every port.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.common import SIM_RATE_HZ, simulated_rate_hz  # noqa: E402
from csi.detect import SIM_THRESHOLD  # noqa: E402
from csi.links import (NO_SIGNAL, UNCALIBRATED, WARMUP, LinkId, LinkMonitor, calibrate_links_report,  # noqa: E402
                       load_thresholds, save_thresholds)
from csi.multisource import MultiSource, parse_port_specs  # noqa: E402
from csi.parser import CSIPacket, Stream, serial_stream  # noqa: E402
from csi.simulate import DEFAULT_RECEIVERS, SENDER_MACS, Segment, simulate_link_streams  # noqa: E402

SIM_DEMO_SCENARIO = (Segment("empty", 20), Segment("motion", 10), Segment("still", 30))  # loops
CLEAR = "\033[H\033[2J"
RESET = "\033[0m"
COLOURS = {"EMPTY": "\033[32m", "MOTION": "\033[31m", NO_SIGNAL: "\033[90m",
           WARMUP: "\033[33m", UNCALIBRATED: "\033[33m"}
RED = "\033[31m"
BAR_W = 24


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_argument_group("packet source")
    g.add_argument("--simulate-links", action="store_true",
                   help="2 simulated senders x 2 simulated receivers (rx1, rx2)")
    g.add_argument("--port", action="append", metavar="[NAME=]PATH",
                   help="serial port of a csi_recv node; repeat for each receiver. Unnamed ports get rx1, rx2...")
    g.add_argument("--baud", type=int, default=921600)
    g.add_argument("--rate", type=float, default=None, help="simulator send rate in Hz (default 100)")
    g.add_argument("--seed", type=int, default=0, help="simulator seed")
    t = ap.add_argument_group("thresholds")
    t.add_argument("--thresholds", metavar="links.json", help="per-link thresholds from --calibrate")
    c = ap.add_argument_group("calibration")
    c.add_argument("--calibrate", type=float, metavar="SECONDS",
                   help="record an empty room for SECONDS, write per-link thresholds to --out, then exit")
    c.add_argument("--out", type=Path, default=Path("links.json"), help="where --calibrate writes the file")
    ap.add_argument("--duration", type=float, help="stop after this many seconds (default: until Ctrl+C)")
    ap.add_argument("--every", type=float, default=0.5, help="seconds between redraws (default 0.5 = 2 Hz)")
    return ap


# ------------------------------------------------------------------ sources and thresholds

def check_source(args: argparse.Namespace) -> None:
    if args.simulate_links and args.port:
        raise SystemExit("error: use --simulate-links or --port, not both")
    if not args.simulate_links and not args.port:
        raise SystemExit("error: pass --port [NAME=]PATH (repeat for each receiver) or --simulate-links")


def simulated_links() -> list[LinkId]:
    return [LinkId(r, m) for r in DEFAULT_RECEIVERS for m in SENDER_MACS]


def open_sources(args: argparse.Namespace, calibrating: bool) -> dict[str, Stream]:
    """Line streams per receiver. Serial streams open their port only when first read (on a reader thread)."""
    if args.simulate_links:
        scenario = (Segment("empty", args.calibrate),) if calibrating else SIM_DEMO_SCENARIO
        return simulate_link_streams(SENDER_MACS, DEFAULT_RECEIVERS, scenario, rate_hz=simulated_rate_hz(args),
                                     seed=args.seed, realtime=not calibrating, loop=not calibrating)
    return {name: serial_stream(path, args.baud) for name, path in parse_port_specs(args.port)}


def resolve_thresholds(args: argparse.Namespace) -> tuple[dict[str, float], str]:
    """(thresholds by link label, description for the header). Runs before any port is opened."""
    if args.thresholds:
        try:
            return load_thresholds(args.thresholds), f"--thresholds {args.thresholds}"
        except (OSError, ValueError) as e:
            raise SystemExit(f"error: cannot read thresholds {args.thresholds}: {e}")
    if args.simulate_links:
        return {lid.label: SIM_THRESHOLD for lid in simulated_links()}, "simulator default"
    raise SystemExit("error: real ports need per-link thresholds: pass --thresholds links.json, or calibrate "
                     "first on an empty room with: links_live.py --port <PORT> --calibrate 60 --out links.json")


# ------------------------------------------------------------------ run loops

def collect(ms: MultiSource, seconds: Optional[float]) -> list[tuple[str, CSIPacket]]:
    """Read every source until it ends or ``seconds`` of wall time pass. Closes the sources."""
    ms.start()
    tagged: list[tuple[str, CSIPacket]] = []
    start = time.time()
    try:
        while True:
            tagged.extend(ms.drain(timeout=0.1))
            if ms.finished():
                break
            if seconds is not None and time.time() - start >= seconds:
                break
    except KeyboardInterrupt:
        print("\ninterrupted: calibrating on what was recorded", flush=True)
    finally:
        tagged.extend(ms.drain())
        ms.close()
    return tagged


def run_calibration(args: argparse.Namespace) -> int:
    expected = simulated_links() if args.simulate_links else []
    ms = MultiSource(open_sources(args, calibrating=True))
    print(f"calibrating for {args.calibrate:.0f}s: keep the room empty...", flush=True)
    report = calibrate_links_report(collect(ms, args.calibrate), expected=expected)
    for label, threshold in sorted(report.thresholds.items()):
        print(f"  {label}  threshold {threshold:.2f}")
    for label, why in sorted(report.skipped.items()):
        print(f"  {label}  NOT CALIBRATED: {why}")
    if not report.thresholds:
        print("calibration failed: no link has enough data, nothing written", file=sys.stderr)
        return 1
    save_thresholds(args.out, report.thresholds)
    print(f"wrote {len(report.thresholds)} link threshold(s) -> {args.out}")
    if report.skipped:
        print(f"{len(report.skipped)} link(s) have no threshold and will show UNCALIBRATED; "
              "record a longer empty room to calibrate them.", file=sys.stderr)
        return 1
    return 0


def run_live(args: argparse.Namespace) -> int:
    expected = simulated_links() if args.simulate_links else []
    thresholds, source_desc = resolve_thresholds(args)
    send_rate = (args.rate or SIM_RATE_HZ) if args.simulate_links else args.rate
    monitor = LinkMonitor(thresholds, expected=expected, send_rate_hz=send_rate)
    ms = MultiSource(open_sources(args, calibrating=False))
    ms.start()
    header = f"links_live  thresholds: {source_desc}  Ctrl+C to stop"
    start, next_draw, total = time.time(), 0.0, 0
    try:
        while True:
            for name, pkt in ms.drain(timeout=0.1):
                monitor.push(name, pkt)
                total += 1
            now = time.time()
            if now >= next_draw:
                sys.stdout.write(CLEAR + render_frame(monitor.snapshot(now), ms.status(), header))
                sys.stdout.flush()
                next_draw = now + args.every
            if args.duration and now - start >= args.duration:
                break
            if ms.finished():
                break
    except KeyboardInterrupt:
        pass
    finally:
        ms.close()
    print(f"\nstopped after {time.time() - start:.1f}s, {total} packets")
    return 0


# ------------------------------------------------------------------ rendering

def score_bar(score: Optional[float], threshold: Optional[float]) -> str:
    """Bar scaled so the threshold sits at 2/3 width; '|' marks the threshold."""
    if score is None or threshold is None:
        return " " * BAR_W
    full = max(threshold * 1.5, 1e-9)
    n = min(BAR_W, int(BAR_W * score / full))
    mark = min(BAR_W - 1, int(BAR_W * threshold / full))
    cells = ["#" if i < n else "." for i in range(BAR_W)]
    cells[mark] = "|"
    return "".join(cells)


def _fmt(value: Optional[float], spec: str) -> str:
    return "-" if value is None else format(value, spec)


def render_frame(rows: Iterable[Mapping], status: Mapping[str, str], header: str = "") -> str:
    """The table as text (no screen clearing; the caller prints CLEAR first)."""
    lines = [header, ""] if header else []
    lines.append(f"{'LINK':<9} {'STATE':<12} {'SCORE':>8} {'THRESH':>8}  {'BAR':<{BAR_W}}  "
                 f"{'PKT/S':>6} {'DROPS':>6} {'RSSI':>5} {'AGE':>6}")
    for r in rows:
        silent = r["state"] == NO_SIGNAL
        colour = COLOURS.get(r["state"], "")
        score = None if silent else r["score"]
        bar = "" if silent else score_bar(score, r["threshold"])
        rssi = "-" if r["rssi"] is None else str(r["rssi"])
        age = "-" if r["age_s"] is None else f"{r['age_s']:.1f}s"
        lines.append(f"{r['id']:<9} {colour}{r['state']:<12}{RESET} {_fmt(score, '8.2f')} "
                     f"{_fmt(r['threshold'], '8.2f')}  {bar:<{BAR_W}}  {r['pkt_rate']:6.1f} "
                     f"{r['drops']:6d} {rssi:>5} {age:>6}")
    for name, state in sorted(status.items()):
        if state.startswith("error"):
            lines.append(f"{RED}{name}: {state}{RESET}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = build_parser().parse_args()
    check_source(args)
    if args.calibrate is not None:
        return run_calibration(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
