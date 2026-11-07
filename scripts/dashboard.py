#!/usr/bin/env python
"""Dashboard server: the multi-link pipeline plus a small HTTP interface (JSON state, SSE events, a page).

    dashboard.py --simulate-room                                   # a simulated person walks the room, no hardware
    dashboard.py --simulate-room --http-port 8000 --host 0.0.0.0   # reachable from the network (opt-in)
    dashboard.py --port rx1=/dev/cu.usbmodem2101 --port rx2=/dev/cu.usbmodem101 --thresholds data/links_cal1_m1.json
    dashboard.py --simulate-room --log run.jsonl                   # also append the state stream to a file
    dashboard.py --replay run.jsonl --speed 8                      # serve a logged run (1 = original pace, max = fastest)
    dashboard.py --replay-session data/hw5 --speed 8 [--margin X | --thresholds links.json]

Endpoints: GET /  (page), GET /api/state  (one payload), GET /api/events  (SSE, ~5 Hz).
Binds to 127.0.0.1 unless --host says otherwise. Real ports without --thresholds still run: their links
show UNCALIBRATED with a message in status. Ctrl+C stops the server and releases every port.
A replay needs no serial port; when it has finished, the final state stays up (status.finished is true).
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.common import margin_arg  # noqa: E402
from csi.detect import DEFAULT_MARGIN  # noqa: E402
from csi.links import load_thresholds  # noqa: E402
from csi.multisource import parse_port_specs  # noqa: E402
from csi.parser import Stream, serial_stream  # noqa: E402
from csi.replay import ReplayError, ReplayServer, load_log, session_dashboard  # noqa: E402
from csi.room import DEFAULT_ROOM_PATH, Room, load_room  # noqa: E402
from csi.server import DEFAULT_HOST, DEFAULT_PORT, DashboardServer, FrameServer, default_walk, \
    looping_room_streams, simulated_thresholds  # noqa: E402

TICK_S = 0.5


def speed_arg(text: str) -> float:
    """--speed: a positive factor (1 is the original pace), or 'max' for as fast as possible."""
    if text == "max":
        return math.inf
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not value > 0 or math.isinf(value):
        raise argparse.ArgumentTypeError(f"speed must be a positive number or 'max', got {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("packet source")
    g.add_argument("--simulate-room", action="store_true",
                   help="a simulated person walks a loop through the room in real time (no hardware)")
    g.add_argument("--port", action="append", metavar="[NAME=]PATH",
                   help="serial port of a csi_recv node; repeat for each receiver. Unnamed ports get rx1, rx2...")
    g.add_argument("--baud", type=int, default=921600)
    g.add_argument("--seed", type=int, default=0, help="simulator seed")
    r = ap.add_argument_group("replay (no hardware)")
    r.add_argument("--replay", type=Path, metavar="run.jsonl",
                   help="serve a run written with --log: the same states, at the logged pace")
    r.add_argument("--replay-session", type=Path, metavar="DIR",
                   help="feed a recorded session (scripts/record.py) through the live pipeline, paced by its "
                        "recorded host times; with one-link recordings the heatmap has one link")
    r.add_argument("--speed", type=speed_arg, default=None, metavar="1|N|max",
                   help="replay speed: 1 is the original pace, N is N times faster, max as fast as possible "
                        "(default 1)")
    r.add_argument("--margin", type=margin_arg, default=None, metavar="X",
                   help=f"with --replay-session and no --thresholds: calibration margin, threshold = X x p99 of "
                        f"the session's empty recording (default {DEFAULT_MARGIN:g})")
    ap.add_argument("--log", type=Path, metavar="run.jsonl",
                    help="append the state stream to this file: a header line (room, thresholds, start time) and "
                         "then one payload per line, the same as /api/events")
    ap.add_argument("--room", type=Path, default=DEFAULT_ROOM_PATH, help="room configuration (default config/room.json)")
    ap.add_argument("--thresholds", metavar="links.json",
                    help="per-link thresholds from links_live.py --calibrate. With --simulate-room the default is "
                         "a calibration on a simulated empty room; with --replay-session the default is one "
                         "calibrated on the session's first empty recording.")
    s = ap.add_argument_group("server")
    s.add_argument("--http-port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT})")
    s.add_argument("--host", default=DEFAULT_HOST,
                   help=f"address to listen on (default {DEFAULT_HOST}, localhost only; 0.0.0.0 for the network)")
    return ap


def check_source(args: argparse.Namespace) -> None:
    chosen = [flag for flag, on in (("--simulate-room", args.simulate_room), ("--port", bool(args.port)),
                                    ("--replay", args.replay is not None),
                                    ("--replay-session", args.replay_session is not None)) if on]
    if len(chosen) > 1:
        raise SystemExit(f"error: use one source, not {' and '.join(chosen)}")
    if not chosen:
        raise SystemExit("error: pass a source: --simulate-room, --port [NAME=]PATH (repeat for each receiver), "
                         "--replay run.jsonl or --replay-session DIR")
    replaying = args.replay is not None or args.replay_session is not None
    if args.speed is not None and not replaying:
        raise SystemExit("error: --speed applies to --replay and --replay-session only")
    if args.margin is not None and args.replay_session is None:
        raise SystemExit("error: --margin applies to --replay-session only")
    if args.replay is not None and args.log is not None:
        raise SystemExit("error: --log records a live run; it cannot be combined with --replay")
    if args.replay is not None and args.thresholds is not None:
        raise SystemExit("error: --thresholds does not apply to --replay: a log already holds its states")


def load_link_thresholds(path: Optional[Path]) -> Optional[Mapping[str, float]]:
    if path is None:
        return None
    try:
        return load_thresholds(path)
    except (OSError, ValueError) as e:
        raise SystemExit(f"error: cannot read thresholds {path}: {e}")


def build_sources(args: argparse.Namespace, room: Room) -> tuple[dict[str, Stream], Optional[Mapping[str, float]]]:
    """Named (host_ts, line) streams and the per-link thresholds for them. Serial ports open on first read."""
    if args.simulate_room:
        thresholds = load_link_thresholds(args.thresholds)
        if thresholds is None:
            print("calibrating the simulated empty room...", flush=True)
            thresholds = simulated_thresholds(room)
        return looping_room_streams(room, default_walk(room), seed=args.seed), thresholds
    sources = {name: serial_stream(path, args.baud) for name, path in parse_port_specs(args.port)}
    return sources, load_link_thresholds(args.thresholds)


def build_server(args: argparse.Namespace) -> FrameServer:
    """The server for the chosen source, not yet started. Raises ReplayError for unusable replay inputs."""
    speed = 1.0 if args.speed is None else args.speed
    if args.replay is not None:
        return ReplayServer(load_log(args.replay), speed=speed, host=args.host, port=args.http_port)
    room = load_room(args.room)
    if args.replay_session is not None:
        thresholds = load_link_thresholds(args.thresholds)
        source = f"loaded from {args.thresholds}" if thresholds is not None else None
        return session_dashboard(args.replay_session, room, speed=speed,
                                 margin=DEFAULT_MARGIN if args.margin is None else args.margin,
                                 thresholds=thresholds, thresholds_source=source,
                                 host=args.host, port=args.http_port, log=args.log)
    sources, thresholds = build_sources(args, room)
    source = "simulate-room" if args.simulate_room else "ports " + ", ".join(n for n, _ in parse_port_specs(args.port))
    return DashboardServer(room, sources, thresholds, host=args.host, port=args.http_port, log=args.log,
                           log_meta={"source": source, "thresholds_source":
                                     f"loaded from {args.thresholds}" if args.thresholds else
                                     ("simulated empty room" if args.simulate_room else "none")})


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    check_source(args)
    try:
        server = build_server(args)
        server.start()
    except (ReplayError, OSError) as e:
        raise SystemExit(f"error: {e}")
    print(f"dashboard at {server.url}  (listening on {server.address[0]})  Ctrl+C to stop", flush=True)
    if args.port and args.thresholds is None:
        print("no thresholds: every link shows UNCALIBRATED until calibrated (see links_live.py --calibrate)",
              flush=True)
    announced = False
    try:
        while True:
            if not announced and server.wait_finished(timeout=0):
                print("replay finished: serving the final state (Ctrl+C to stop)", flush=True)
                announced = True
            time.sleep(TICK_S)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
