"""Per-link monitoring and calibration.

A link is one (receiver, sender MAC) pair. Two senders on two receivers give up to four links, and
each has its own signal scale, so each owns its own MotionScorer, debouncer, LinkHealth, last RSSI
and calibrated threshold.

Public API:
    LinkId                 (receiver, mac) with a short label such as "rx1<-01"
    LinkMonitor            push(receiver, packet) -> state; snapshot(now) -> list of per-link dicts
    calibrate_links        tagged empty-room packets -> {label: threshold}
    calibrate_links_report same, plus {label: reason} for links that were skipped
    save_thresholds / load_thresholds   links.json: {label: threshold}, optionally with the calibration's record
    read_thresholds        links.json -> ThresholdFile (thresholds plus scorer, packet rates and time, if recorded)
    threshold_file_notes / rate_notes   warnings when a file does not fit the live scorer or the live packet rates

Every link scores with ``LIVE_SCORER`` (csi/detect.py), the configuration the calibration paths use too.

States: EMPTY and MOTION (from the detector), plus
    WARMUP        no full motion window yet
    UNCALIBRATED  no threshold for this link (the monitor still scores it)
    NO_SIGNAL     no packet for more than NO_SIGNAL_S seconds (reported by snapshot)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from .adaptive import AdaptiveDetector, Reading, RoomDecision
from .detect import (DEBOUNCE_S, DEFAULT_MARGIN, EMPTY, LIVE_SCORER, MIN_CLEAN_FRACTION, MOTION, Calibration,
                     CalibrationError, MotionScorer, ScorerConfig, _Debouncer, calibrate_clips, classify,
                     scorer_note, validate_margin)
from .health import LinkHealth
from .parser import CSIPacket

NO_SIGNAL = "NO_SIGNAL"
WARMUP = "WARMUP"
UNCALIBRATED = "UNCALIBRATED"
NO_SIGNAL_S = 2.0
FIXED = "fixed"        # a link judged against a threshold
AUTO = "auto"          # a link judged against its own adaptive baseline (csi/adaptive.py)
THRESHOLDS_FORMAT = "csi-link-thresholds"  # links.json with a calibration record (a plain file has no format key)
THRESHOLDS_VERSION = 2
RATE_TOLERANCE = 0.3   # a live packet rate this far (relative) from the calibration rate gets a warning


@dataclass(frozen=True)
class LinkId:
    receiver: str
    mac: str

    @property
    def label(self) -> str:
        """Short name, e.g. ``rx1<-01``: the receiver and the last octet of the sender MAC."""
        return f"{self.receiver}<-{self.mac[-2:]}"


class _Link:
    """Everything one link needs: scorer, debouncer, health, last RSSI, and either a fixed threshold or an
    adaptive detector (``mode``: "fixed" or "auto"; see csi/adaptive.py). ``auto_allowed`` is the monitor's
    mode: a link set back to no threshold returns to auto only when the monitor is in auto mode."""

    def __init__(self, link_id: LinkId, threshold: Optional[float], debounce_s: float,
                 send_rate_hz: Optional[float], auto: bool = False) -> None:
        self.id, self.threshold = link_id, threshold
        self.auto_allowed = auto
        self.mode = FIXED
        self.detector: Optional[AdaptiveDetector] = None
        self.scorer = MotionScorer.from_config(LIVE_SCORER)  # the configuration every calibration path uses
        self.debounce = _Debouncer(debounce_s)
        self.health = LinkHealth(send_rate_hz=send_rate_hz)
        self.score: Optional[float] = None
        self.rssi: Optional[int] = None
        self.last_host: Optional[float] = None
        self.reading: Optional[Reading] = None  # the link's latest reading, for the room decision
        self.level: Optional[float] = None
        self.elevated = False
        if threshold is None and auto:
            self._enter_auto()
            self.state = WARMUP
        else:
            self.state = UNCALIBRATED if threshold is None else WARMUP

    def _enter_auto(self) -> None:
        self.mode, self.threshold, self.detector = AUTO, None, AdaptiveDetector()

    def set_threshold(self, threshold: Optional[float]) -> None:
        """Change the threshold at runtime. A number makes the link fixed; None returns it to auto when the
        monitor is in auto mode (else to UNCALIBRATED). The state follows at once, from the last score."""
        if threshold is None and self.auto_allowed:
            self._enter_auto()
            self.state = WARMUP
            return
        was_auto = self.mode == AUTO
        self.mode, self.detector = FIXED, None
        self.threshold = threshold
        if threshold is None:
            self.state = UNCALIBRATED
        elif was_auto or self.state == UNCALIBRATED:
            self.state = WARMUP if self.score is None else classify(self.score, None, threshold)

    def push(self, pkt: CSIPacket) -> str:
        self.health.observe(pkt)
        self.rssi, self.last_host = pkt.rssi, pkt.host_ts
        self.score = self.scorer.push(pkt)
        if self.mode == AUTO:
            self.reading = self.detector.push(pkt.host_ts, self.score)
            self.state = self.reading.state
            self.level = self.reading.level
            self.elevated = self.reading.elevated
            return self.state
        if self.threshold is None:
            self.state = UNCALIBRATED
        elif self.score is None:
            self.state = WARMUP
        else:
            self.state = self.debounce.update(classify(self.score, None, self.threshold), pkt.host_ts)
        self.level = None if self.score is None or not self.threshold else self.score / self.threshold
        self.elevated = self.state == MOTION
        self.reading = Reading(pkt.host_ts, self.state, self.score, elevated=self.elevated,
                               warm=self.state not in (WARMUP, UNCALIBRATED))
        return self.state

    @property
    def shown_threshold(self) -> Optional[float]:
        """The threshold this link is judged against: fixed, or the adaptive enter level in auto mode."""
        if self.mode == AUTO:
            return self.reading.enter if self.reading is not None else None
        return self.threshold


class LinkMonitor:
    """Demultiplexes tagged packets into per-link detectors, and the links into one room decision.

    ``mode="fixed"`` (default): each link is judged against its threshold from ``thresholds``.
    ``mode="auto"``: each link is judged by its own AdaptiveDetector (csi/adaptive.py); ``thresholds`` is
    not used for state, and a link given a number by ``set_threshold`` becomes fixed.
    ``expected`` lists links to show from the start, so a link that never transmits appears as NO_SIGNAL
    instead of being absent. The room decision (``room_state``) is updated on every packet, at its time.
    """

    def __init__(self, thresholds: Optional[Mapping[str, float]] = None, *, mode: str = FIXED,
                 expected: Iterable[LinkId] = (), debounce_s: float = DEBOUNCE_S,
                 send_rate_hz: Optional[float] = None, no_signal_s: float = NO_SIGNAL_S) -> None:
        if mode not in (FIXED, AUTO):
            raise ValueError(f"mode must be 'fixed' or 'auto', got {mode!r}")
        self._mode = mode
        self._thresholds = {} if mode == AUTO else dict(thresholds or {})
        self._debounce_s, self._send_rate_hz, self._no_signal_s = debounce_s, send_rate_hz, no_signal_s
        self._links: dict[LinkId, _Link] = {}
        self._room = RoomDecision()
        self.room_state = WARMUP
        self.room_elevated: list[str] = []
        for link_id in expected:
            self._get(link_id)

    @property
    def mode(self) -> str:
        return self._mode

    def _get(self, link_id: LinkId) -> _Link:
        if link_id not in self._links:
            self._links[link_id] = _Link(link_id, self._thresholds.get(link_id.label),
                                         self._debounce_s, self._send_rate_hz, auto=self._mode == AUTO)
        return self._links[link_id]

    @property
    def thresholds(self) -> dict[str, float]:
        """Current fixed threshold of every link that has one, by label (auto links have none)."""
        return {lid.label: link.threshold for lid, link in self._links.items() if link.mode == FIXED and link.threshold is not None}

    def set_threshold(self, label: str, threshold: Optional[float]) -> None:
        """Set one link's threshold (by label) on the live monitor, for every link with that label.

        A number makes those links fixed. None returns them to auto (in auto mode) or to UNCALIBRATED.
        """
        if threshold is None:
            self._thresholds.pop(label, None)
        else:
            self._thresholds[label] = threshold
        for link_id, link in self._links.items():
            if link_id.label == label:
                link.set_threshold(threshold)

    def push(self, receiver: str, pkt: CSIPacket) -> str:
        """Feed one packet from ``receiver``. Returns that link's state after the packet."""
        state = self._get(LinkId(receiver, pkt.mac)).push(pkt)
        self._update_room(pkt.host_ts)
        return state

    def _update_room(self, now: float) -> None:
        """Room decision at packet time ``now``, from the links heard within the NO_SIGNAL window."""
        readings = {lid.label: link.reading for lid, link in self._links.items()
                    if link.reading is not None and link.last_host is not None
                    and now - link.last_host <= self._no_signal_s}
        self.room_state = self._room.update(now, readings)
        self.room_elevated = list(self._room.elevated)

    def snapshot(self, now: Optional[float] = None) -> list[dict]:
        """One dict per link, sorted by label. ``now`` is host time (default: the wall clock).

        ``threshold`` is the fixed threshold, or in auto mode the adaptive enter level (None while warming up).
        ``level`` is score relative to that threshold (>= 1 at or above it); ``elevated`` is True in MOTION.
        """
        now = time.time() if now is None else now
        rows = []
        for link_id in sorted(self._links, key=lambda lid: lid.label):
            link = self._links[link_id]
            age = None if link.last_host is None else max(0.0, now - link.last_host)
            silent = age is None or age > self._no_signal_s
            threshold = link.shown_threshold
            if link.mode == AUTO:
                level = link.level
            else:
                level = None if link.score is None or not threshold else link.score / threshold
            rows.append({
                "id": link_id.label,
                "receiver": link_id.receiver,
                "mac": link_id.mac,
                "state": NO_SIGNAL if silent else link.state,
                "mode": link.mode,
                "score": link.score,
                "threshold": threshold,
                "level": None if silent else level,
                "elevated": (not silent) and link.elevated,
                "pkt_rate": 0.0 if silent else link.health.packet_rate(),
                "drops": link.health.drops,
                "rssi": link.rssi,
                "age_s": age,
                "packets": link.health.packets,
            })
        return rows


@dataclass(frozen=True)
class LinkCalibration:
    calibrations: dict[str, Calibration]   # calibrated links: label -> full calibration report
    skipped: dict[str, str]                # link label -> why it was refused
    margin: float = DEFAULT_MARGIN         # the margin every link was calibrated with
    scorer: ScorerConfig = LIVE_SCORER     # the motion-scorer configuration every link was calibrated with

    @property
    def thresholds(self) -> dict[str, float]:
        return {label: c.threshold for label, c in self.calibrations.items()}

    @property
    def clean_s(self) -> dict[str, float]:
        return {label: c.clean_s for label, c in self.calibrations.items()}

    @property
    def rejected_s(self) -> dict[str, float]:
        return {label: c.rejected_s for label, c in self.calibrations.items()}

    @property
    def pkt_rate_hz(self) -> dict[str, float]:
        """Received packet rate per calibrated link during its calibration clips."""
        return {label: c.pkt_rate_hz for label, c in self.calibrations.items()}

    def record(self) -> dict:
        """What a thresholds file keeps about this calibration (see ``save_thresholds``)."""
        return {"scorer": self.scorer.as_dict(),
                "pkt_rate_hz": {label: round(rate, 2) for label, rate in sorted(self.pkt_rate_hz.items())},
                "margin": self.margin,
                "noisy": any(c.noisy for c in self.calibrations.values())}


def calibrate_links_report(tagged: Optional[Iterable[tuple[str, CSIPacket]]] = None,
                           expected: Iterable[LinkId] = (), send_rate_hz: Optional[float] = None,
                           *, clips: Optional[Sequence[Iterable[tuple[str, CSIPacket]]]] = None,
                           margin: float = DEFAULT_MARGIN,
                           min_clean_fraction: float = MIN_CLEAN_FRACTION, noisy: bool = False,
                           scorer: ScorerConfig = LIVE_SCORER) -> LinkCalibration:
    """Calibrate each link from its own empty-room packets (see ``calibrate_clips``).

    ``tagged`` is one clip; ``clips`` is several clips, each scored separately and pooled per link. A
    link with too few packets, too sparse, too lossy or too contaminated is skipped and the reason is
    kept. ``expected`` links with no packets at all are skipped as "no packets". ``margin`` applies to
    every link; a non-positive margin raises ValueError before any link is looked at. ``scorer`` is the live
    configuration unless a caller deliberately compares another one.
    """
    margin = validate_margin(margin)
    if clips is None:
        clips = [tagged if tagged is not None else []]
    expected = list(expected)
    per_link: dict[LinkId, list[list[CSIPacket]]] = {link_id: [] for link_id in expected}
    for clip in clips:
        groups: dict[LinkId, list[CSIPacket]] = {}
        for receiver, pkt in clip:
            groups.setdefault(LinkId(receiver, pkt.mac), []).append(pkt)
        for link_id, pkts in groups.items():
            per_link.setdefault(link_id, []).append(pkts)
    calibrations: dict[str, Calibration] = {}
    skipped: dict[str, str] = {}
    for link_id in sorted(per_link, key=lambda lid: lid.label):
        label, link_clips = link_id.label, per_link[link_id]
        if not link_clips:
            skipped[label] = "no packets"
            continue
        try:
            calibrations[label] = calibrate_clips(link_clips, send_rate_hz, margin, min_clean_fraction, noisy=noisy,
                                                  scorer=scorer)
        except CalibrationError as e:
            skipped[label] = str(e)
    return LinkCalibration(calibrations, skipped, margin, scorer)


def calibrate_links(tagged: Iterable[tuple[str, CSIPacket]]) -> dict[str, float]:
    """``{link label: threshold}`` for the links that could be calibrated."""
    return calibrate_links_report(tagged).thresholds


@dataclass(frozen=True)
class ThresholdFile:
    """A links.json as read back. A plain file (no calibration record) has only ``thresholds``."""
    thresholds: dict[str, float]
    scorer: Optional[ScorerConfig] = None                        # the scorer it was calibrated with, if recorded
    pkt_rate_hz: dict[str, float] = field(default_factory=dict)  # each link's packet rate during calibration
    saved_at: Optional[str] = None                               # ISO 8601 time of the save, if recorded
    margin: Optional[float] = None
    noisy: Optional[bool] = None


def save_thresholds(path: str | Path, thresholds: Mapping[str, float], record: Optional[Mapping] = None) -> None:
    """Write links.json. Without ``record``: the plain ``{label: threshold}`` object (what the dashboard's Save
    writes). With one (``LinkCalibration.record()``): ``{"format", "version", "thresholds": {...}, "scorer",
    "pkt_rate_hz", "margin", "noisy", "saved_at"}``, so a later run can tell whether the thresholds still fit.
    ``load_thresholds`` and ``read_thresholds`` read both shapes."""
    values = {k: float(v) for k, v in sorted(thresholds.items())}
    if record is None:
        obj: dict = values
    else:
        obj = {"format": THRESHOLDS_FORMAT, "version": THRESHOLDS_VERSION, "thresholds": values}
        obj.update((k, v) for k, v in record.items() if k not in obj)
        obj.setdefault("saved_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    Path(path).write_text(json.dumps(obj, indent=2) + "\n")


def read_thresholds(path: str | Path) -> ThresholdFile:
    """Read links.json in either shape. Raises OSError, or ValueError for a file that is not link thresholds."""
    obj = json.loads(Path(path).read_text())
    if not isinstance(obj, dict):
        raise ValueError(f"{path} is not a JSON object of link thresholds")
    try:
        if not isinstance(obj.get("thresholds"), dict):  # plain {label: threshold}, as before the record existed
            return ThresholdFile({k: float(v) for k, v in obj.items()})
        scorer = obj.get("scorer")
        margin, noisy = obj.get("margin"), obj.get("noisy")
        return ThresholdFile(
            thresholds={k: float(v) for k, v in obj["thresholds"].items()},
            scorer=None if scorer is None else ScorerConfig.from_dict(scorer),
            pkt_rate_hz={k: float(v) for k, v in (obj.get("pkt_rate_hz") or {}).items()},
            saved_at=obj.get("saved_at"),
            margin=None if margin is None else float(margin),
            noisy=None if noisy is None else bool(noisy))
    except (TypeError, AttributeError) as e:
        raise ValueError(f"{path} is not a link thresholds file: {e}") from e


def load_thresholds(path: str | Path) -> dict[str, float]:
    """``{label: threshold}`` from links.json (either shape)."""
    return read_thresholds(path).thresholds


def threshold_file_notes(file: ThresholdFile, source: str = "the thresholds file",
                         live: ScorerConfig = LIVE_SCORER) -> list[str]:
    """Warnings for a thresholds file that may not fit the live scorer (none when it records the live one)."""
    note = scorer_note(file.scorer, source, live)
    return [] if note is None else [note]


def rate_notes(calibration_rates: Mapping[str, float], rows: Iterable[Mapping],
               tolerance: float = RATE_TOLERANCE) -> list[str]:
    """One warning per link whose live packet rate is more than ``tolerance`` away from its calibration rate.

    ``rows`` are ``LinkMonitor.snapshot`` rows. Only links in EMPTY or MOTION are compared (not warming up, silent
    or uncalibrated), and only those with a recorded rate. The score scale hardly moves with the rate any more
    (trapezoidal high-pass), but a large change still means another sender setup or interference."""
    notes = []
    for row in rows:
        rate = calibration_rates.get(row["id"])
        if not rate or row.get("state") not in (EMPTY, MOTION):
            continue
        live = float(row.get("pkt_rate") or 0.0)
        if abs(live - rate) > tolerance * rate:
            notes.append(f"{row['id']}: {live:.0f} pkt/s now against {rate:.0f} pkt/s at calibration "
                         f"(more than {tolerance:.0%} apart): check the senders, or recalibrate")
    return notes
