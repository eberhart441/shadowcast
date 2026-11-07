"""Calibration run inside the dashboard, on its live packet stream. Standard library only.

The dashboard already owns the serial ports, so the job never opens any. The server feeds it every
(receiver, packet) as it is released, and the job:

    countdown   lead_s of wall time, "Clear the room"
    recording   ``clips`` clips of ``seconds`` each, separated by PAUSE_S, cut by packet host time
    processing  calibrate_links_report(clips=...) with the margin, then applies the usable thresholds
    done        at least one link got a threshold; ``result`` and ``skipped`` say what happened
    failed      no link could be calibrated (thresholds unchanged), or the sources ended first

A link whose clip data is more than REJECT_FRACTION movement is applied only if it keeps its previous
threshold: the warning says so, and the link is not switched to the new value.
"""
from __future__ import annotations

import math
from typing import Callable, Mapping, Optional, Sequence

from .detect import DEFAULT_MARGIN, validate_margin
from .links import LinkId, calibrate_links_report

PAUSE_S = 3.0
REJECT_FRACTION = 0.5
MAX_CLIPS = 10
MAX_SECONDS = 120.0
MAX_LEAD_S = 60.0
REQUEST_KEYS = frozenset({"clips", "seconds", "margin", "lead_s"})
ACTIVE_STATES = frozenset({"countdown", "recording", "processing"})


class RequestError(ValueError):
    """A calibrate request that cannot be used; the message names the field."""


def _number(body: Mapping, key: str, default: float, lo: float, hi: float) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RequestError(f"{key} must be a number, got {value!r}")
    if not lo <= value <= hi:
        raise RequestError(f"{key} must be between {lo:g} and {hi:g}, got {value:g}")
    return float(value)


def parse_request(body: Mapping) -> dict:
    """Validated calibrate options: clips, seconds, margin, lead_s (defaults 3, 10, DEFAULT_MARGIN, 5)."""
    unknown = sorted(set(body) - REQUEST_KEYS)
    if unknown:
        raise RequestError(f"unknown calibration option(s): {', '.join(unknown)}; "
                           f"allowed: {', '.join(sorted(REQUEST_KEYS))}")
    clips = _number(body, "clips", 3, 1, MAX_CLIPS)
    if clips != int(clips):
        raise RequestError(f"clips must be a whole number, got {clips:g}")
    if isinstance(body.get("margin"), bool):
        raise RequestError("margin must be a positive number, got True")
    try:
        margin = validate_margin(body.get("margin", DEFAULT_MARGIN))
    except (TypeError, ValueError) as e:
        raise RequestError(str(e)) from e
    return {
        "clips": int(clips),
        "seconds": _number(body, "seconds", 10, 1, MAX_SECONDS),
        "margin": margin,
        "lead_s": _number(body, "lead_s", 5, 0, MAX_LEAD_S),
    }


class CalibrationJob:
    """One calibration run. Not thread-safe: the server calls it under its lock.

    ``expected`` lists every link of the room, so a link with no packets is reported as skipped.
    ``current()`` returns the thresholds in effect now; ``apply(label, value)`` sets one on the live
    monitor; ``now_mono`` arguments are wall-clock seconds (time.monotonic) and drive the countdown only.
    """

    def __init__(self, request: Mapping, expected: Sequence[LinkId], *, now_mono: float,
                 current: Callable[[], Mapping[str, float]], apply: Callable[[str, float], None]) -> None:
        self.request = dict(request)
        self._expected = list(expected)
        self._current, self._apply = current, apply
        self.state = "countdown"
        self.message = ""
        self.result: dict[str, dict] = {}
        self.skipped: dict[str, str] = {}
        self.warnings_kept: list[str] = []
        self._countdown_end = now_mono + self.request["lead_s"]
        self._t0: Optional[float] = None           # host time of the recording start
        self._period = self.request["seconds"] + PAUSE_S
        self._total = self.request["clips"] * self.request["seconds"] + (self.request["clips"] - 1) * PAUSE_S
        self._clips: list[list[tuple[str, object]]] = [[] for _ in range(self.request["clips"])]

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    # ------------------------------------------------------------------ feeding

    def observe(self, receiver: str, pkt, now_mono: float) -> None:
        """One packet, in host-time order. Advances the phases, then keeps the packet if it is in a clip."""
        self.tick(pkt.host_ts, now_mono)
        if self.state != "recording":
            return
        rel = pkt.host_ts - self._t0
        if rel < 0:  # stamped before the recording started: not part of any clip
            return
        k, offset = divmod(rel, self._period)
        if k < self.request["clips"] and offset < self.request["seconds"]:  # else: the pause after a clip
            self._clips[int(k)].append((receiver, pkt))

    def tick(self, host_now: Optional[float], now_mono: float) -> None:
        """Advance the phases to the current host time (None: no packet time yet) and wall clock."""
        if not self.active or self.state == "processing":
            return
        if self.state == "countdown":
            if now_mono < self._countdown_end or host_now is None:
                return
            self.state, self._t0 = "recording", host_now
        if host_now is not None and host_now >= self._t0 + self._total:
            self._finish()

    def abort(self, reason: str) -> None:
        """Stop a run that cannot finish (the sources ended). Thresholds are left as they were."""
        if self.state in ("countdown", "recording"):
            self._fail(reason)

    def cancel(self) -> bool:
        if not self.active or self.state == "processing":
            return False
        self.state = "idle"
        self.message = "Calibration cancelled: thresholds unchanged."
        return True

    # ------------------------------------------------------------------ reporting

    def status(self, host_now: Optional[float], now_mono: float) -> dict:
        """The ``calibration`` block of the payload."""
        clip, seconds_left, message = None, 0, self.message
        if self.state == "countdown":
            seconds_left = math.ceil(max(0.0, self._countdown_end - now_mono))
            message = f"Clear the room: recording starts in {seconds_left} s"
        elif self.state == "recording" and host_now is not None:
            rel = max(0.0, host_now - self._t0)
            k, offset = divmod(rel, self._period)
            k = int(min(k, self.request["clips"] - 1))
            clip = k + 1
            if offset < self.request["seconds"]:
                seconds_left = math.ceil(self.request["seconds"] - offset)
                message = f"Keep the room empty (clip {clip}/{self.request['clips']}, {seconds_left} s)"
            else:
                seconds_left = math.ceil(self._period - offset)
                message = f"Keep the room empty: clip {clip + 1}/{self.request['clips']} starts in {seconds_left} s"
        elif self.state == "recording":
            message = "Keep the room empty: waiting for packets"
        elif self.state == "processing":
            message = "Processing the clips"
        return {
            "state": self.state,
            "message": message,
            "clip": clip,
            "clips": self.request["clips"],
            "seconds_left": seconds_left,
            "result": self.result,
            "skipped": self.skipped,
        }

    # ------------------------------------------------------------------ finishing

    def _finish(self) -> None:
        self.state = "processing"
        try:
            report = calibrate_links_report(clips=self._clips, expected=self._expected,
                                            margin=self.request["margin"])
        except Exception as e:  # any failure keeps the old thresholds and says why
            self._fail(f"calibration failed: {e}")
            return
        previous = dict(self._current())
        applied: dict[str, float] = {}
        self.result, self.skipped, self.warnings_kept = {}, dict(report.skipped), []
        for label, cal in report.calibrations.items():
            total = cal.clean_s + cal.rejected_s
            entry = {"threshold": cal.threshold, "clean_s": cal.clean_s, "rejected_s": cal.rejected_s,
                     "pkt_rate_hz": cal.pkt_rate_hz, "loss": cal.loss, "margin": cal.margin, "applied": False}
            if total > 0 and cal.rejected_s > REJECT_FRACTION * total:
                entry["warning"] = (f"threshold may be unreliable: only {cal.clean_s:.1f} s of {total:.1f} s kept")
                if label in previous:
                    note = f"{label} keeps its previous threshold {previous[label]:.4g}"
                else:
                    note = f"{label} stays uncalibrated"
                self.warnings_kept.append(f"{label}: {entry['warning']}; {note}")
            else:
                applied[label] = cal.threshold
                entry["applied"] = True
            self.result[label] = entry
        for label, value in applied.items():
            self._apply(label, value)
        if applied:
            self.state = "done"
            parts = [f"{len(applied)} link(s) calibrated"]
            if self.skipped:
                parts.append(f"{len(self.skipped)} skipped")
            if self.warnings_kept:
                parts.append(f"{len(self.warnings_kept)} with a warning kept their previous threshold")
            self.message = "; ".join(parts) + "."
        else:  # nothing usable: keep the per-link reasons and the warnings visible
            self.state = "failed"
            self.message = "no link could be calibrated; thresholds unchanged"

    def _fail(self, reason: str) -> None:
        self.state = "failed"
        self.message = reason
        self.result = {}
        self.skipped = {}
