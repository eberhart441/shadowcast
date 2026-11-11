"""Per-link monitoring and calibration.

A link is one (receiver, sender MAC) pair. Two senders on two receivers give up to four links, and
each has its own signal scale, so each owns its own MotionScorer, debouncer, LinkHealth, last RSSI
and calibrated threshold. In the transceiver pipeline a link is (receiving node, transmitting node) by full
node identity, and its score comes from the receiving node's link reports instead of from packets.

Public API:
    LinkId                 (receiver, mac) with a short label such as "rx1<-01"
    NodeLinkId             (receiver, transmitter) node names, labelled "n1<-n5": a transceiver link
    room_link_ids(room)    [(link id, receiving Node, transmitting Node)] for every link of the room
    LinkMonitor            push(receiver, packet) -> state; push_reports(host_ts, [(NodeLinkId, LinkReport)]);
                           snapshot(now) -> list of per-link dicts; row(label, now) -> one of them

    calibrate_links        tagged empty-room packets -> {label: threshold}
    calibrate_links_report same, plus {label: reason} for links that were skipped
    save_thresholds / load_thresholds   links.json, a plain {label: threshold} object

States: EMPTY and MOTION (from the detector), plus
    WARMUP        no full motion window yet
    UNCALIBRATED  no threshold for this link (the monitor still scores it)
    NO_SIGNAL     no packet (or link report) for more than NO_SIGNAL_S seconds (reported by snapshot); for a
                  transceiver link also a link report whose heard counter is 0
A transceiver link is WARMUP while its link report carries the warming flag or no score yet.

A transceiver link is always judged against its own adaptive baseline, whatever the monitor's mode: there are
too many of them to calibrate one by one (ADR-0029). Given a room with transceivers, the monitor's room state for
them is ``RoomState`` (csi/room_state.py: zones, and agreement among links that share no node); the hackathon links
keep ``RoomDecision``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Iterable, Mapping, Optional, Sequence, Union

from .adaptive import AdaptiveDetector, Reading, RoomDecision
from .detect import (DEBOUNCE_S, DEFAULT_MARGIN, MIN_CLEAN_FRACTION, MOTION, Calibration, CalibrationError,
                     MotionScorer, _Debouncer, calibrate_clips, classify, validate_margin)
from .broadcast import LinkReport
from .health import LinkHealth, ReportHealth
from .parser import CSIPacket
from .room import Node, Room
from .room_state import POSSIBLY_OCCUPIED, POSSIBLY_OCCUPIED_S, RoomState, ZoneMap

NO_SIGNAL = "NO_SIGNAL"
WARMUP = "WARMUP"
UNCALIBRATED = "UNCALIBRATED"
NO_SIGNAL_S = 2.0
ROOM_STEP_S = 0.05     # link reports update the room decision at most this often (host time): once per 20 Hz round
FIXED = "fixed"        # a link judged against a threshold
AUTO = "auto"          # a link judged against its own adaptive baseline (csi/adaptive.py)


@dataclass(frozen=True)
class LinkId:
    receiver: str
    mac: str
    transmitter: ClassVar[Optional[str]] = None  # a sender is known by its MAC here, not by name

    @property
    def label(self) -> str:
        """Short name, e.g. ``rx1<-01``: the receiver and the last octet of the sender MAC."""
        return f"{self.receiver}<-{self.mac[-2:]}"


@dataclass(frozen=True)
class NodeLinkId:
    """A transceiver link by full node identity: the receiving and the transmitting node's names."""
    receiver: str
    transmitter: str
    mac: ClassVar[str] = ""

    @property
    def label(self) -> str:
        """``n1<-n5``: node n1 receiving node n5's round broadcasts."""
        return f"{self.receiver}<-{self.transmitter}"


AnyLinkId = Union[LinkId, NodeLinkId]


def room_link_ids(room: Room) -> list[tuple[AnyLinkId, Node, Node]]:
    """(link id, receiving node, transmitting node) for every link: receiver x sender, then transceiver pairs."""
    out: list[tuple[AnyLinkId, Node, Node]] = [(LinkId(rx.name, tx.mac), rx, tx) for rx, tx in room.links()]
    out.extend((NodeLinkId(rx.name, tx.name), rx, tx) for rx, tx in room.transceiver_links())
    return out


class _Link:
    """Everything one link needs: scorer, debouncer, health, last RSSI, and either a fixed threshold or an
    adaptive detector (``mode``: "fixed" or "auto"; see csi/adaptive.py). ``auto_allowed`` is the monitor's
    mode: a link set back to no threshold returns to auto only when the monitor is in auto mode."""

    def __init__(self, link_id: AnyLinkId, threshold: Optional[float], debounce_s: float,
                 send_rate_hz: Optional[float], auto: bool = False) -> None:
        self.adaptive_only = isinstance(link_id, NodeLinkId)
        if self.adaptive_only:
            threshold, auto = None, True
        self.id, self.threshold = link_id, threshold
        self.label = link_id.label   # computed once: the room decision reads it for every link many times a second
        self.auto_allowed = auto
        self.mode = FIXED
        self.detector: Optional[AdaptiveDetector] = None
        self.scorer = MotionScorer()
        self.debounce = _Debouncer(debounce_s)
        self.health = ReportHealth() if isinstance(link_id, NodeLinkId) else LinkHealth(send_rate_hz=send_rate_hz)
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
        monitor is in auto mode (else to UNCALIBRATED). The state follows at once, from the last score.
        A transceiver link takes no threshold (ValueError): it is judged by its adaptive baseline only."""
        if self.adaptive_only:
            if threshold is not None:
                raise ValueError(f"{self.id.label} is a transceiver link: it is judged against its own adaptive "
                                 "baseline only and takes no threshold")
            return
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
        return self._judge(pkt.host_ts)

    def push_report(self, host_ts: float, report: LinkReport) -> str:
        """One link report from the receiving node: its score is judged as a packet's score would be.

        A heard counter of 0 is NO_SIGNAL; the warming flag, or no score yet, is WARMUP. Neither feeds the
        detector, and neither counts as a warm link in the room decision."""
        self.health.observe_report(host_ts, report)
        self.last_host = host_ts
        if report.rssi is not None:
            self.rssi = report.rssi
        self.score = report.score
        if report.heard == 0 or report.warming or self.score is None:
            self.state = NO_SIGNAL if report.heard == 0 else WARMUP
            self.level, self.elevated = None, False
            self.reading = None if report.heard == 0 else Reading(host_ts, WARMUP, self.score)
            return self.state
        return self._judge(host_ts)

    def _judge(self, t: float) -> str:
        """State from the latest score at time ``t``: the adaptive detector in auto mode, else the threshold."""
        if self.mode == AUTO:
            self.reading = self.detector.push(t, self.score)
            self.state = self.reading.state
            self.level = self.reading.level
            self.elevated = self.reading.elevated
            return self.state
        if self.threshold is None:
            self.state = UNCALIBRATED
        elif self.score is None:
            self.state = WARMUP
        else:
            self.state = self.debounce.update(classify(self.score, None, self.threshold), t)
        self.level = None if self.score is None or not self.threshold else self.score / self.threshold
        self.elevated = self.state == MOTION
        self.reading = Reading(t, self.state, self.score, elevated=self.elevated,
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
    instead of being absent. The room decision (``room_state``) is updated on every packet, at its time; from link
    reports at most once per ``room_step_s`` of host time (a round), and brought up to date by ``snapshot``.
    ``possibly_occupied_s`` is the transceiver room state's possibly-occupied hold (csi/room_state.py).
    """

    def __init__(self, thresholds: Optional[Mapping[str, float]] = None, *, mode: str = FIXED,
                 expected: Iterable[LinkId] = (), debounce_s: float = DEBOUNCE_S,
                 send_rate_hz: Optional[float] = None, no_signal_s: float = NO_SIGNAL_S,
                 room: Optional[Room] = None, possibly_occupied_s: float = POSSIBLY_OCCUPIED_S) -> None:
        if mode not in (FIXED, AUTO):
            raise ValueError(f"mode must be 'fixed' or 'auto', got {mode!r}")
        self._mode = mode
        self._thresholds = {} if mode == AUTO else dict(thresholds or {})
        self._debounce_s, self._send_rate_hz, self._no_signal_s = debounce_s, send_rate_hz, no_signal_s
        self._links: dict[AnyLinkId, _Link] = {}
        self._by_label: dict[str, AnyLinkId] = {}   # first link with each label, for row()
        self._room = RoomDecision()
        self._room_t: Optional[float] = None        # host time of the last room decision
        self._room_due: Optional[float] = None      # host time of reports not in the room decision yet
        self.room_step_s = ROOM_STEP_S
        self._room_state = WARMUP                   # the hackathon links' decision (RoomDecision)
        self._room_elevated: list[str] = []
        self._newest: Optional[float] = None        # newest host time pushed: "now" when none is given (ADR-0013)
        self._zones: Optional[RoomState] = None     # the transceiver links' room state (csi/room_state.py)
        if room is not None and room.transceivers:
            self._zones = RoomState(ZoneMap(room, [(NodeLinkId(rx.name, tx.name).label, rx, tx)
                                                   for rx, tx in room.transceiver_links()]), no_signal_s=no_signal_s,
                                    possibly_occupied_s=possibly_occupied_s)
        for link_id in expected:
            self._get(link_id)

    @property
    def mode(self) -> str:
        return self._mode

    def _get(self, link_id: AnyLinkId) -> _Link:
        if link_id not in self._links:
            self._links[link_id] = _Link(link_id, self._thresholds.get(link_id.label),
                                         self._debounce_s, self._send_rate_hz, auto=self._mode == AUTO)
            self._by_label.setdefault(link_id.label, link_id)
        return self._links[link_id]

    @property
    def thresholds(self) -> dict[str, float]:
        """Current fixed threshold of every link that has one, by label (auto links have none)."""
        return {lid.label: link.threshold for lid, link in self._links.items() if link.mode == FIXED and link.threshold is not None}

    def set_threshold(self, label: str, threshold: Optional[float]) -> None:
        """Set one link's threshold (by label) on the live monitor, for every link with that label.

        A number makes those links fixed. None returns them to auto (in auto mode) or to UNCALIBRATED.
        A transceiver link takes no threshold: a number for one raises ValueError and changes nothing.
        """
        if threshold is not None and any(link.adaptive_only for lid, link in self._links.items() if lid.label == label):
            raise ValueError(f"{label} is a transceiver link: it is judged against its own adaptive baseline only "
                             "and takes no threshold")
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
        self._saw(pkt.host_ts)
        self._update_room(pkt.host_ts)
        return state

    def _saw(self, host_ts: float) -> None:
        if self._newest is None or host_ts > self._newest:
            self._newest = host_ts

    @property
    def newest_host_ts(self) -> Optional[float]:
        """The newest host time pushed so far (packet or link report), None before any."""
        return self._newest

    def _now(self, now: Optional[float]) -> float:
        """``now``, else the newest host time pushed (0.0 before any: every link is silent then anyway). Never the
        wall clock: a replay or simulation runs on its own host times (ADR-0013)."""
        if now is not None:
            return now
        return 0.0 if self._newest is None else self._newest

    def push_reports(self, host_ts: float, reports: Iterable[tuple[NodeLinkId, LinkReport]]) -> None:
        """Feed the link reports of one round broadcast, heard at ``host_ts``; the room decision follows once."""
        self._saw(host_ts)
        if self._zones is not None:
            seen = []
            for link_id, report in reports:
                link = self._get(link_id)
                link.push_report(host_ts, report)
                seen.append((link.label, link.reading))
            self._zones.observe_many(host_ts, seen)
            self._zones.update(host_ts)   # incremental; it re-judges its points at most once a round itself
            return
        for link_id, report in reports:
            self._get(link_id).push_report(host_ts, report)
        # one decision per round, not per round broadcast: each looks at every link (870 at 30 nodes)
        if self._room_t is None or not 0 <= host_ts - self._room_t < self.room_step_s:
            self._update_room(host_ts)
        else:
            self._room_due = host_ts

    def _update_room(self, now: float) -> None:
        """Room decision at packet time ``now``, from the links heard within the NO_SIGNAL window.

        With a transceiver room, the transceiver links are left to its RoomState and combined in ``room_state``."""
        self._room_t, self._room_due = now, None
        zoned = self._zones is not None
        readings = {link.label: link.reading for link in self._links.values()
                    if link.reading is not None and link.last_host is not None
                    and now - link.last_host <= self._no_signal_s and not (zoned and link.adaptive_only)}
        self._room_state = self._room.update(now, readings)
        self._room_elevated = list(self._room.elevated)

    def _settle_room(self) -> None:
        if self._room_due is not None:
            self._update_room(self._room_due)

    @property
    def room_state(self) -> str:
        """Room-level state, up to date with every report pushed so far: MOTION if the hackathon links' decision or
        the transceiver room state says so, else POSSIBLY_OCCUPIED if the transceiver room state holds a zone after
        motion (csi/room_state.py), else EMPTY if either is warm, else WARMUP."""
        self._settle_room()
        states = [self._room_state] if self._zones is None else [self._room_state, self._zones.state]
        for state in (MOTION, POSSIBLY_OCCUPIED, "EMPTY"):
            if state in states:
                return state
        return WARMUP

    @property
    def room_elevated(self) -> list[str]:
        """Labels of the links elevated in the latest room decision (both kinds of link)."""
        self._settle_room()
        if self._zones is None:
            return self._room_elevated
        return sorted(set(self._room_elevated) | set(self._zones.elevated))

    @property
    def room_zones(self) -> Optional[RoomState]:
        """The transceiver room state (zones, csi/room_state.py), or None for a room without transceivers."""
        return self._zones

    def snapshot(self, now: Optional[float] = None) -> list[dict]:
        """One dict per link, sorted by label. ``now`` is host time (default: the newest host time pushed).

        ``threshold`` is the fixed threshold, or in auto mode the adaptive enter level (None while warming up).
        ``level`` is score relative to that threshold (>= 1 at or above it); ``elevated`` is True in MOTION.
        """
        now = self._now(now)
        self._settle_room()
        return [self._row(
link_id, self._links[link_id], now)

                for link_id in sorted(self._links, key=lambda lid: lid.label)]

    def row(self, label: str, now: Optional[float] = None) -> Optional[dict]:
        """The snapshot row of the link labelled ``label`` (None if there is none): one link, not all of them."""
        link_id = self._by_label.get(label)
        if link_id is None:
            return None
        return self._row(link_id, self._links[link_id], self._now(now))

    def _row(self, link_id: AnyLinkId, link: _Link, now: float) -> dict:
        age = None if link.last_host is None else max(0.0, now - link.last_host)
        silent = age is None or age > self._no_signal_s
        threshold = link.shown_threshold
        if link.mode == AUTO:
            level = link.level
        else:
            level = None if link.score is None or not threshold else link.score / threshold
        return {
            "id": link_id.label,
            "receiver": link_id.receiver,
            "mac": link_id.mac,
            "transmitter": link_id.transmitter,
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
        }



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
