#!/usr/bin/env python
"""Evaluate the live detectors on recorded sessions; print a confusion matrix per label.

    python scripts/evaluate.py data/room1
    python scripts/evaluate.py data/room1 data/room2
    python scripts/evaluate.py --breathing data/room1      # also score still_* via breathing (experimental)

Each session's first empty recording calibrates the motion threshold; every other
recording is scored. Splits are by recording, never by random window. By default
only motion is scored (EMPTY/MOTION); the still_* rows are shown as "breathing disabled".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csi.common import add_margin_arg  # noqa: E402
from csi.evaluate import EvaluationError, evaluate_sessions, format_report  # noqa: E402


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+", type=Path, help="session folders written by scripts/record.py")
    ap.add_argument("--breathing", action="store_true",
                    help="also make breathing (STILL_PRESENT) predictions and score the still_* labels")
    add_margin_arg(ap.add_argument_group("calibration"),
                   "the per-session threshold calibrated on the empty recording")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = evaluate_sessions(args.sessions, breathing=args.breathing, margin=args.margin)
    except EvaluationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(format_report(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
