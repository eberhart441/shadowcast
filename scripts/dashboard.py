#!/usr/bin/env python
"""Dashboard server: the multi-link pipeline plus a small HTTP interface (JSON state, SSE events, a page).

    dashboard.py --simulate-room                                   # a simulated person walks the room, no hardware
    dashboard.py --simulate-room --http-port 8000 --host 0.0.0.0   # reachable from the network (opt-in)
    dashboard.py --port rx1=/dev/cu.usbmodem2101 --port rx2=/dev/cu.usbmodem101 --thresholds data/links_cal1_m1.json

Endpoints: GET /  (page), GET /api/state  (one payload), GET /api/events  (SSE, ~5 Hz).
Binds to 127.0.0.1 unless --host says otherwise. Real ports without --thresholds still run: their links
show UNCALIBRATED with a message in status. Ctrl+C stops the server and releases every port.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.links import load_thresholds  # noqa: E402
from csi.multisource import parse_port_specs  # noqa: E402
from csi.parser import Stream, serial_stream  # noqa: E402
from csi.room import DEFAULT_ROOM_PATH, Room, load_room  # noqa: E402
from csi.server import DEFAULT_HOST, DEFAULT_PORT, DashboardServer, default_walk, looping_room_streams, \
    simulated_thresholds  # noqa: E402

TICK_S = 0.5


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("packet source")
    g.add_argument("--simulate-room", action="store_true",
                   help="a simulated person walks a loop through the room in real time (no hardware)")
    g.add_argument("--port", action="append", metavar="[NAME=]PATH",
                   help="serial port of a csi_recv node; repeat for each receiver. Unnamed ports get rx1, rx2...")
    g.add_argument("--baud", type=int, default=921600)
    g.add_argument("--seed", type=int, default=0, help="simulator seed")
    ap.add_argument("--room", type=Path, default=DEFAULT_ROOM_PATH, help="room configuration (default config/room.json)")
    ap.add_argument("--thresholds", metavar="links.json",
                    help="per-link thresholds from links_live.py --calibrate. With --simulate-room the default is "
                         "a calibration on a simulated empty room.")
    s = ap.add_argument_group("server")
    s.add_argument("--http-port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT})")
    s.add_argument("--host", default=DEFAULT_HOST,
                   help=f"address to listen on (default {DEFAULT_HOST}, localhost only; 0.0.0.0 for the network)")
    return ap


def check_source(args: argparse.Namespace) -> None:
    if args.simulate_room and args.port:
        raise SystemExit("error: use --simulate-room or --port, not both")
    if not args.simulate_room and not args.port:
        raise SystemExit("error: pass --simulate-room or --port [NAME=]PATH (repeat for each receiver)")


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


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    check_source(args)
    room = load_room(args.room)
    sources, thresholds = build_sources(args, room)
    server = DashboardServer(room, sources, thresholds, host=args.host, port=args.http_port)
    server.start()
    print(f"dashboard at {server.url}  (listening on {server.address[0]})  Ctrl+C to stop", flush=True)
    if thresholds is None:
        print("no thresholds: every link shows UNCALIBRATED until calibrated (see links_live.py --calibrate)",
              flush=True)
    try:
        while True:
            time.sleep(TICK_S)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
