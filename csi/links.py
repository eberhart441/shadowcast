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

from .detect import (DEBOUNCE_S, DEFAULT_MARGIN, Calibration, CalibrationError, MotionScorer, _Debouncer,
                     calibrate_clips, classify, validate_margin)
from .health import LinkHealth
from .parser import CSIPacket

NO_SIGNAL = "NO_SIGNAL"
WARMUP = "WARMUP"
UNCALIBRATED = "UNCALIBRATED"
NO_SIGNAL_S = 2.0


@dataclass(frozen=True)
class LinkId:
    receiver: str
    mac: str

    @property
    def label(self) -> str:
        """Short name, e.g. ``rx1<-01``: the receiver and the last octet of the sender MAC."""
        return f"{self.receiver}<-{self.mac[-2:]}"


class _Link:
    """Everything one link needs: scorer, debouncer, health, last RSSI and threshold."""

    def __init__(self, link_id: LinkId, threshold: Optional[float], debounce_s: float,
                 send_rate_hz: Optional[float]) -> None:
        self.id, self.threshold = link_id, threshold
        self.scorer = MotionScorer()
        self.debounce = _Debouncer(debounce_s)
        self.health = LinkHealth(send_rate_hz=send_rate_hz)
        self.score: Optional[float] = None
        self.rssi: Optional[int] = None
        self.last_host: Optional[float] = None
        self.state = UNCALIBRATED if threshold is None else WARMUP

    def push(self, pkt: CSIPacket) -> str:
        self.health.observe(pkt)
        self.rssi, self.last_host = pkt.rssi, pkt.host_ts
        self.score = self.scorer.push(pkt)
        if self.threshold is None:
            self.state = UNCALIBRATED
        elif self.score is None:
            self.state = WARMUP
        else:
            self.state = self.debounce.update(classify(self.score, None, self.threshold), pkt.host_ts)
        return self.state


class LinkMonitor:
    """Demultiplexes tagged packets into per-link detectors.

    ``thresholds`` maps link label to threshold. ``expected`` lists links to show from the start, so a
    link that never transmits appears as NO_SIGNAL instead of being absent.
    """

    def __init__(self, thresholds: Optional[Mapping[str, float]] = None, *,
                 expected: Iterable[LinkId] = (), debounce_s: float = DEBOUNCE_S,
                 send_rate_hz: Optional[float] = None, no_signal_s: float = NO_SIGNAL_S) -> None:
        self._thresholds = dict(thresholds or {})
        self._debounce_s, self._send_rate_hz, self._no_signal_s = debounce_s, send_rate_hz, no_signal_s
        self._links: dict[LinkId, _Link] = {}
        for link_id in expected:
            self._get(link_id)

    def _get(self, link_id: LinkId) -> _Link:
        if link_id not in self._links:
            self._links[link_id] = _Link(link_id, self._thresholds.get(link_id.label),
                                         self._debounce_s, self._send_rate_hz)
        return self._links[link_id]

    def push(self, receiver: str, pkt: CSIPacket) -> str:
        """Feed one packet from ``receiver``. Returns that link's state after the packet."""
        return self._get(LinkId(receiver, pkt.mac)).push(pkt)

    def snapshot(self, now: Optional[float] = None) -> list[dict]:
        """One dict per link, sorted by label. ``now`` is host time (default: the wall clock)."""
        now = time.time() if now is None else now
        rows = []
        for link_id in sorted(self._links, key=lambda lid: lid.label):
            link = self._links[link_id]
            age = None if link.last_host is None else max(0.0, now - link.last_host)
            silent = age is None or age > self._no_signal_s
            rows.append({
                "id": link_id.label,
                "receiver": link_id.receiver,
                "mac": link_id.mac,
                "state": NO_SIGNAL if silent else link.state,
                "score": link.score,
                "threshold": link.threshold,
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
                           margin: float = DEFAULT_MARGIN) -> LinkCalibration:
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
            calibrations[label] = calibrate_clips(link_clips, send_rate_hz, margin)
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
