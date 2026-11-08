"""Per-link monitoring and calibration.

A link is one (receiver, sender MAC) pair. Two senders on two receivers give up to four links, and
each has its own signal scale, so each owns its own MotionScorer, debouncer, LinkHealth, last RSSI
and calibrated threshold.

Public API:
    LinkId                 (receiver, mac) with a short label such as "rx1<-01"
    LinkMonitor            push(receiver, packet) -> state; snapshot(now) -> list of per-link dicts
    calibrate_links        tagged empty-room packets -> {label: threshold}
    calibrate_links_report same, plus {label: reason} for links that were skipped
    save_thresholds / load_thresholds   links.json, a plain {label: threshold} object

States: EMPTY and MOTION (from the detector), plus
    WARMUP        no full motion window yet
    UNCALIBRATED  no threshold for this link (the monitor still scores it)
    NO_SIGNAL     no packet for more than NO_SIGNAL_S seconds (reported by snapshot)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from .adaptive import AdaptiveDetector, Reading, RoomDecision
from .detect import (DEBOUNCE_S, DEFAULT_MARGIN, MIN_CLEAN_FRACTION, MOTION, Calibration, CalibrationError,
                     MotionScorer, _Debouncer, calibrate_clips, classify, validate_margin)
from .health import LinkHealth
from .parser import CSIPacket

NO_SIGNAL = "NO_SIGNAL"
WARMUP = "WARMUP"
UNCALIBRATED = "UNCALIBRATED"
NO_SIGNAL_S = 2.0
FIXED = "fixed"        # a link judged against a threshold
AUTO = "auto"          # a link judged against its own adaptive baseline (csi/adaptive.py)


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
        self.scorer = MotionScorer()
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

        Link health (csi/health.py), each None when unknown: ``seq_loss`` (0..1, from the sender's packet counter:
        the loss to trust; ``drops`` assumes a send rate), ``seq_rate`` (packets/s the sender really sends) and
        ``clip_pct`` (0..1 of raw I/Q values at the int8 rails), all three None while the link is silent; and the
        newest packet's ``agc_gain``, ``fft_gain`` and ``noise_floor`` as the receiver printed them.
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
                "seq_loss": None if silent else link.health.seq_loss(),
                "seq_rate": None if silent else link.health.seq_rate(),
                "clip_pct": None if silent else link.health.clip_fraction(),
                "agc_gain": link.health.agc_gain,
                "fft_gain": link.health.fft_gain,
                "noise_floor": link.health.noise_floor,
            })
        return rows


@dataclass(frozen=True)
class LinkCalibration:
    calibrations: dict[str, Calibration]   # calibrated links: label -> full calibration report
    skipped: dict[str, str]                # link label -> why it was refused
    margin: float = DEFAULT_MARGIN         # the margin every link was calibrated with

    @property
    def thresholds(self) -> dict[str, float]:
        return {label: c.threshold for label, c in self.calibrations.items()}

    @property
    def clean_s(self) -> dict[str, float]:
        return {label: c.clean_s for label, c in self.calibrations.items()}

    @property
    def rejected_s(self) -> dict[str, float]:
        return {label: c.rejected_s for label, c in self.calibrations.items()}


def calibrate_links_report(tagged: Optional[Iterable[tuple[str, CSIPacket]]] = None,
                           expected: Iterable[LinkId] = (), send_rate_hz: Optional[float] = None,
                           *, clips: Optional[Sequence[Iterable[tuple[str, CSIPacket]]]] = None,
                           margin: float = DEFAULT_MARGIN,
                           min_clean_fraction: float = MIN_CLEAN_FRACTION, noisy: bool = False) -> LinkCalibration:
    """Calibrate each link from its own empty-room packets (see ``calibrate_clips``).

    ``tagged`` is one clip; ``clips`` is several clips, each scored separately and pooled per link. A
    link with too few packets, too sparse, too lossy or too contaminated is skipped and the reason is
    kept. ``expected`` links with no packets at all are skipped as "no packets". ``margin`` applies to
    every link; a non-positive margin raises ValueError before any link is looked at.
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
            calibrations[label] = calibrate_clips(link_clips, send_rate_hz, margin, min_clean_fraction, noisy=noisy)
        except CalibrationError as e:
            skipped[label] = str(e)
    return LinkCalibration(calibrations, skipped, margin)


def calibrate_links(tagged: Iterable[tuple[str, CSIPacket]]) -> dict[str, float]:
    """``{link label: threshold}`` for the links that could be calibrated."""
    return calibrate_links_report(tagged).thresholds


def save_thresholds(path: str | Path, thresholds: Mapping[str, float]) -> None:
    Path(path).write_text(json.dumps({k: float(v) for k, v in sorted(thresholds.items())}, indent=2) + "\n")


def load_thresholds(path: str | Path) -> dict[str, float]:
    return {k: float(v) for k, v in json.loads(Path(path).read_text()).items()}
