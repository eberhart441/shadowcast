"""Shared CLI plumbing: every script takes --simulate or --port."""
from __future__ import annotations

import argparse
from typing import Iterator, Optional

from .detect import SIM_THRESHOLD, load_threshold
from .parser import CSIPacket, CSIParser, parse_stream, serial_stream
from .simulate import simulate_stream

SIM_RATE_HZ = 100.0  # the simulator's send rate, and the sender rate it implies under --simulate


def add_source_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("packet source")
    g.add_argument("--simulate", action="store_true", help="use fake CSI, no hardware")
    g.add_argument("--port", help="serial port of the csi_recv node")
    g.add_argument("--baud", type=int, default=921600)
    g.add_argument("--rate", type=float, default=None,
                   help="the sender's nominal send rate in Hz, used for drop estimation and by the "
                        f"simulator (default {SIM_RATE_HZ:g} with --simulate). On a real port leave it "
                        "out: the rate is estimated from device timestamps, so a non-100 Hz sender is "
                        "counted correctly.")
    g.add_argument("--seed", type=int, default=0, help="simulator seed")


def add_threshold_args(ap: argparse.ArgumentParser) -> None:
    t = ap.add_argument_group("threshold")
    t.add_argument("--threshold-file", help="calibration JSON from detect_live.py --calibrate")
    t.add_argument(
        "--threshold", type=float,
        help="motion threshold on the detector's score scale. Hardware (calibrated, receiver and sender "
             "about 2 m apart): empty scores ~3-9, walking ~25-70, so calibrate on the actual room with "
             "detect_live.py --calibrate instead of typing a value. Simulator scale (the --simulate "
             f"default is {SIM_THRESHOLD:g}): simulated empty ~0.2, motion ~3-9.")


def simulated_rate_hz(args: argparse.Namespace) -> float:
    """Send rate for the simulator: --rate if given, else 100 Hz."""
    return SIM_RATE_HZ if args.rate is None else args.rate


def link_send_rate(args: argparse.Namespace) -> Optional[float]:
    """Nominal sender rate for drop estimation.

    --rate if given; 100 Hz under --simulate; otherwise None, so the rate is estimated from
    device timestamps (a real sender's rate is not assumed).
    """
    if args.rate is not None:
        return args.rate
    return SIM_RATE_HZ if args.simulate else None


def resolve_threshold(args: argparse.Namespace, sim_default: float) -> float:
    """Motion threshold for a run. Real ports must say what they want; the simulator has a default.

    Real-hardware scores are on a different scale from the simulator's, so a port with no explicit
    threshold is an error rather than a silent guess.
    """
    if args.threshold is not None:
        return args.threshold
    if args.threshold_file:
        return load_threshold(args.threshold_file)
    if args.simulate:
        return sim_default
    raise SystemExit("error: a real port needs an explicit threshold: pass --threshold-file <calibration.json> "
                     "or --threshold <score>. Calibrate on an empty room first with: "
                     "scripts/detect_live.py --port <PORT> --calibrate 60 --out calibration.json")


def open_source(args: argparse.Namespace, parser: CSIParser) -> Iterator[CSIPacket]:
    """Packets from the simulator (real-time, looping) or the serial port."""
    if args.simulate:
        stream = simulate_stream(rate_hz=simulated_rate_hz(args), seed=args.seed, realtime=True, loop=True)
    elif args.port:
        stream = serial_stream(args.port, args.baud)
    else:
        raise SystemExit("error: pass --port <serial port> or --simulate")
    return parse_stream(stream, parser)
