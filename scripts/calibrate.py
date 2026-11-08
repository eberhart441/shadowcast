#!/usr/bin/env python
"""Noisy-room thresholds from a running dashboard: sample its live link scores, print the formula's result.

    calibrate.py                          # sample 45 s from http://127.0.0.1:8000, print, change nothing
    calibrate.py --seconds 60             # a longer sample
    calibrate.py --apply                  # then set each link's threshold and save the thresholds file
    calibrate.py --apply --skip-bad-links # apply the links that have a threshold; keep the rest as they are
    calibrate.py --url http://127.0.0.1:8001

Polls GET /api/state about 4 times a second; each poll gives every link one reading: its score, or an empty
reading (None) while the link is silent (NO_SIGNAL) or has no score yet. The threshold is the formula in
docs/agents/hardware.md (csi/noisy_calibration.py).

--apply POSTs /api/threshold for each link, then /api/thresholds/save. It refuses, and changes nothing, if any
link has no threshold, unless --skip-bad-links is given. Keep the dashboard running afterwards: a restart
re-baselines the receivers (docs/agents/hardware.md).

Exit status: 0 done (or a report printed), 1 the dashboard could not be used, 2 --apply refused.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi.noisy_calibration import LinkThreshold, noisy_room_thresholds  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_SECONDS = 45.0
POLL_S = 0.25            # about 4 polls a second
NO_SIGNAL = "NO_SIGNAL"  # the dashboard's state for a link with no packet for 2 s
HTTP_TIMEOUT_S = 5.0


class DashboardError(RuntimeError):
    """The dashboard could not be reached, or answered with an error."""


def _request(url: str, path: str, body: Optional[Mapping] = None) -> dict:
    data, method, headers = None, "GET", {}
    if body is not None:  # commands take a JSON body (the server refuses any other media type)
        data, method = json.dumps(body).encode(), "POST"
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        try:
            message = json.loads(err.read()).get("error", "")
        except ValueError:
            message = ""
        raise DashboardError(f"{method} {path} failed: HTTP {err.code} {message}".rstrip()) from err
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise DashboardError(f"cannot reach the dashboard at {url}: {err}") from err


def sample_links(url: str, seconds: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, list[Optional[float]]]:
    """Poll /api/state for ``seconds``: one reading per link per poll.

    A reading is the link's score, or None (an empty reading) while the link is silent (NO_SIGNAL) or has no
    score yet.
    """
    samples: dict[str, list[Optional[float]]] = {}
    end = clock() + seconds
    while True:
        for row in _request(url, "/api/state")["links"]:
            heard = row.get("state") != NO_SIGNAL
            samples.setdefault(row["id"], []).append(row.get("score") if heard else None)
        if clock() >= end:
            return samples
        sleep(POLL_S)


def plan_apply(results: Mapping[str, LinkThreshold], skip_bad_links: bool) -> tuple[dict[str, float], Optional[str]]:
    """``({link: threshold} to set, None)``, or ``({}, why)`` when --apply must refuse."""
    bad = sorted(link for link, r in results.items() if r.threshold is None)
    if bad and not skip_bad_links:
        return {}, (f"refusing to apply: {len(bad)} link(s) have no threshold ({', '.join(bad)}). "
                    "Sample longer, or pass --skip-bad-links to apply the others.")
    good = {link: r.threshold for link, r in results.items() if r.threshold is not None}
    if not good:
        return {}, "refusing to apply: no link has a threshold"
    return good, None


def apply_thresholds(url: str, thresholds: Mapping[str, float]) -> dict:
    """POST each threshold, then save the dashboard's thresholds file. Returns the save response."""
    for link, value in thresholds.items():
        _request(url, "/api/threshold", {"link": link, "value": value})
    return _request(url, "/api/thresholds/save", {})


def format_report(results: Mapping[str, LinkThreshold]) -> str:
    header = f"{'link':<10} {'samples':>7} {'empty':>5} {'median':>9} {'spread':>9} {'max':>9} {'threshold':>10}"
    lines = [header, "-" * len(header)]
    notes = []
    for link, r in results.items():
        lines.append(f"{link:<10} {r.readings:>7} {r.empty:>5} {_num(r.median):>9} {_num(r.spread):>9} "
                     f"{_num(r.max):>9} {_num(r.threshold):>10}")
        if r.reason:
            notes.append(f"  {link}: no threshold: {r.reason}")
    return "\n".join(lines + notes)


def _num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.4g}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help="dashboard base URL (default %(default)s)")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help="how long to sample the live scores (default %(default)s)")
    parser.add_argument("--apply", action="store_true", help="set the thresholds and save them on the dashboard")
    parser.add_argument("--skip-bad-links", action="store_true",
                        help="with --apply: apply the links that have a threshold, leave the others unchanged")
    args = parser.parse_args(argv)
    if not args.seconds > 0:
        parser.error("--seconds must be positive")

    print(f"sampling {args.url} for {args.seconds:g} s; keep the room as it normally is", file=sys.stderr)
    try:
        samples = sample_links(args.url, args.seconds)
        results = noisy_room_thresholds(samples)
        print(format_report(results))
        if not args.apply:
            return 0
        thresholds, refusal = plan_apply(results, args.skip_bad_links)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
        if args.skip_bad_links and len(thresholds) < len(results):
            print("links without a threshold keep whatever the dashboard has now", file=sys.stderr)
        saved = apply_thresholds(args.url, thresholds)
    except DashboardError as err:
        print(f"calibrate: {err}", file=sys.stderr)
        return 1
    print(f"applied {len(thresholds)} threshold(s); saved {len(saved.get('thresholds', {}))} to {saved.get('path')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
