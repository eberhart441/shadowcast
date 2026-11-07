"""Shared CLI plumbing: every script takes --simulate or --port."""
from __future__ import annotations

import argparse
from typing import Iterator

from .parser import CSIPacket, CSIParser, parse_stream, serial_stream
from .simulate import simulate_stream


def add_source_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("packet source")
    g.add_argument("--simulate", action="store_true", help="use fake CSI, no hardware")
    g.add_argument("--port", help="serial port of the csi_recv node")
    g.add_argument("--baud", type=int, default=921600)
    g.add_argument("--rate", type=float, default=100.0, help="send rate in Hz (simulator)")
    g.add_argument("--seed", type=int, default=0, help="simulator seed")


def open_source(args: argparse.Namespace, parser: CSIParser) -> Iterator[CSIPacket]:
    """Packets from the simulator (real-time, looping) or the serial port."""
    if args.simulate:
        stream = simulate_stream(rate_hz=args.rate, seed=args.seed, realtime=True, loop=True)
    elif args.port:
        stream = serial_stream(args.port, args.baud)
    else:
        raise SystemExit("error: pass --port <serial port> or --simulate")
    return parse_stream(stream, parser)
