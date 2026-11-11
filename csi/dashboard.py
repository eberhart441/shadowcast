"""The single state payload the dashboard renders from: per-link monitor plus heatmap.

``DashboardState.push(receiver, pkt)`` feeds a LinkMonitor and the heatmap smoother. In the transceiver
pipeline ``push_broadcast(host_ts, data)`` does the same from one round broadcast's link reports (csi/rounds.py).
``snapshot(now)`` returns one JSON-serialisable dict:

    room      {width_m, depth_m, zones}
                zones: [{id, x0, y0, x1, y1}], the room state's zones (csi/room_state.py), "A1" top-left; only
                for a room with transceivers
    nodes     [{name, role, chip, x, y}]              receivers, senders, then transceivers
    links     [{id, receiver, sender, mac, state, mode, elevated, level, score, threshold, normalised, baseline,
                evidence, pkt_rate, drops, rssi, age_s, weight, contributes}]
                baseline: the link's quiet baseline (None in warm-up); evidence: 0..1 above it.
                mode: "fixed" (judged against threshold) or "auto" (its own adaptive baseline, csi/adaptive.py).
                elevated: True when the link is in MOTION (auto: elevated above its adaptive enter level).
                level: score / threshold (auto: / the adaptive enter level); >= 1 at or above it.
                sender is the node name, None if the MAC is unknown; for a transceiver link (id "n1<-n5")
                receiver and sender are the receiving and transmitting node, and mac is ""
    heatmap   {cols, rows, extents, cells, max, contributing}
                cells: flat row-major floats in 0..1 (row 0 is the top edge, y = 0)
    track     {x, y, vx, vy, confidence} or None: the motion track, a Kalman-smoothed dot following the
                heatmap's hot blob (csi/tracker.py). Where change is, like the heatmap; not a person count.
    status    {time_s, links_by_state, parameters, room}
                room: {state, elevated (labels), mode, zones}: room-level MOTION. With transceivers, from zones
                    and links that share no node (csi/room_state.py); zones: {zone id: MOTION, EMPTY or WARMUP}. The
                    hackathon links: from link agreement (csi/adaptive.py), and no zones key
                broadcasts: {heard, accepted, rejected {reason: n}, unknown_nodes {serial: n}, unplaced_reports,
                join_frames {request, grant}, sources {name: {heard, accepted, duplicate, queue_overflow}}}, the
                round broadcasts ingested
                (all 0 in the line pipeline); reasons are csi.rounds.REJECT_REASONS, each always present
    node_health [{name, state, heard_by, hears, peers, last_heard_s, link_rate_hz, slot_lateness}] per transceiver
                state "ok" or "missing" (no round broadcast reached the host for > 2 s); slot_lateness: median
                us into its slot that its round broadcasts start, as the others report it, or "unknown"
    coverage    {state "ok" | "degraded", reasons [text], missing_nodes, unknown_nodes, silent_links,
                unexplained_silent_links}
    link_health {links, heard, silent, rate_hz {min, median, max}}: every link at once
    link_matrix {nodes [names], states [one string per receiving node], codes {char: state}}: states[r][t] is the
                link from nodes[t] to nodes[r]: E M W U N for the five states, "-" on the diagonal
    (csi/coverage.py computes the last four.)

Time is host time from the packets. ``snapshot()`` with no argument uses the newest packet time, so the
payload is deterministic for simulated streams; pass ``now`` to advance the decay while packets stop.
``snapshot(now, links=False)`` leaves ``links`` out (frames of a large room, ADR-0024), and
``link_detail(label)`` gives one link's row. ``snapshot`` is ``render(capture(now))``: ``capture`` is the only
part that touches live state (the server runs it under its lock), ``render`` reads only the capture.

"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import numpy as np

from .coverage import SlotLateness, coverage, link_health, link_matrix, node_health
from .heatmap import RTI, Heatmap
from .links import LinkMonitor, NO_SIGNAL, UNCALIBRATED, WARMUP, room_link_ids
from .parser import CSIPacket
from .room import Room
from .round_auth import TEST_KIT_KEY
from .rounds import RoundIngest
from .tracker import Tracker

STATES = ("EMPTY", "MOTION", WARMUP, UNCALIBRATED, NO_SIGNAL)
PAYLOAD_DECIMALS = 4


class DashboardState:
    def __init__(self, room: Room, thresholds: Optional[Mapping[str, float]] = None, *, mode: str = "fixed",
                 kit_key: bytes = TEST_KIT_KEY, **heatmap_params) -> None:
        self._room = room
        expected = [link_id for link_id, _, _ in room_link_ids(room)]
        self._monitor = LinkMonitor(thresholds or {}, mode=mode, expected=expected, room=room)
        self._ingest = RoundIngest(room, kit_key)
        self._heat = Heatmap(room, **heatmap_params)
        self._track = Tracker(room.width_m, room.depth_m, self._heat.centres)
        self._senders = {tx.mac: tx.name for tx in room.senders}
        self._receivers = {rx.name for rx in room.receivers}
        self._last_host: Optional[float] = None
        self._heard_at: dict[str, float] = {}   # transceiver name -> host time its last round broadcast arrived
        self._lateness = SlotLateness()

    def push(self, receiver: str, pkt: CSIPacket) -> None:
        self._monitor.push(receiver, pkt)
        self._advance(pkt.host_ts)

    def push_broadcast(self, host_ts: float, data: bytes, source: str = "") -> None:
        """One round broadcast heard at ``host_ts`` by ``source`` (a gateway): its link reports drive their links.
        Bytes that cannot be used, and copies another gateway already delivered, are counted in
        ``status.broadcasts``, never raised."""
        placed = self._ingest.place(data, host_ts, source)
        self._monitor.push_reports(host_ts, placed)
        self._lateness.observe(host_ts, placed)
        if self._ingest.last_node is not None:
            self._heard_at[self._ingest.last_node] = max(host_ts, self._heard_at.get(self._ingest.last_node, host_ts))
        self._advance(host_ts)

    def count_dropped(self, source: str, n: int) -> None:
        """``n`` round broadcasts from ``source`` were dropped before ingest (a full queue): counted, not used."""
        self._ingest.count_dropped(source, n)

    def _advance(self, host_ts: float) -> None:
        self._last_host = host_ts
        if self._heat.due(host_ts):
            self._heat.advance(host_ts, self._monitor.snapshot(host_ts))
            self._track.update(host_ts, self._heat.target)  # unsmoothed: no double smoothing

    @property
    def thresholds(self) -> dict[str, float]:
        return self._monitor.thresholds

    def set_threshold(self, label: str, threshold: Optional[float]) -> None:
        """A number makes the link fixed; None returns it to auto (in auto mode)."""
        self._monitor.set_threshold(label, threshold)

    @property
    def mode(self) -> str:
        return self._monitor.mode

    def set_params(self, **kwargs) -> None:
        """Live heatmap tunables (see Heatmap.set_params); the next payload reports them."""
        self._heat.set_params(**kwargs)

    def parameters(self) -> dict:
        """Heatmap parameters. ``imaging`` is "rti" (radio tomography, a room with transceivers: ``excess_path_m``,
        ``corr_m`` and ``noise_ratio`` apply, ``falloff_m`` and ``agreement`` are None) or "footprint"."""
        rti = self._heat.imaging == RTI
        return {
            "imaging": self._heat.imaging,
            "cols": self._heat.cols,
            "rows": self._heat.rows,
            "falloff_m": None if rti else self._heat.falloff_m,
            "sensitivity": self._heat.sensitivity,
            "agreement": None if rti else self._heat.agreement,
            "excess_path_m": self._heat.excess_path_m if rti else None,
            "corr_m": self._heat.corr_m if rti else None,
            "noise_ratio": self._heat.noise_ratio if rti else None,
            "r_floor": self._heat.r_floor,
            "r_full": self._heat.r_full,
            "baseline_window_s": self._heat.baseline_window_s,
            "baseline_percentile": self._heat.baseline_percentile,
            "baseline_warmup_s": self._heat.baseline_warmup_s,
            "rise_tau_s": self._heat.rise_tau,
            "decay_tau_s": self._heat.decay_tau,
            "step_s": self._heat.step,
        }

    def snapshot(self, now: Optional[float] = None, *, links: bool = True) -> dict:
        """The payload at host time ``now``: ``render(capture(now))``. ``links=False`` leaves out the per-link rows."""
        return self.render(self.capture(now), links=links)

    def capture(self, now: Optional[float] = None) -> "Capture":
        """Advance the heatmap and track to ``now`` and take what a payload needs, in one cheap step.

        This is the only part of a payload that touches live state, so it is the only part that must run under the
        server lock. ``render`` turns the capture into the payload, without the lock.
        """
        if now is None:
            now = self._last_host if self._last_host is not None else time.time()
        rows = self._monitor.snapshot(now)
        heat = self._heat
        heat.advance(now, rows)
        self._track.update(now, heat.target)
        # every value below is a new object per advance or per call, never changed in place afterwards
        return Capture(now=now, rows=rows, heat_values=heat.values, contributing=tuple(heat.contributing),
                       evidence=heat.evidence, baselines=heat.baselines, track=self._track.state(now),
                       room_state=self._monitor.room_state, room_elevated=tuple(self._monitor.room_elevated),
                       mode=self._monitor.mode, broadcasts=self._ingest.counts(), parameters=self.parameters(),
                       heard_at=dict(self._heard_at), lateness=self._lateness.by_node(now),
                       room_zones=None if self._monitor.room_zones is None else self._monitor.room_zones.zone_states)

    def render(self, cap: "Capture", *, links: bool = True) -> dict:
        """The payload from a capture. Reads nothing that the pipeline changes, so it is safe off the lock."""
        rows = cap.rows
        health = node_health(self._room.transceivers, rows, cap.heard_at, cap.now, cap.lateness)
        payload = {
            "room": self._room_payload(),
            "nodes": self._nodes(),
            "links": [self._link_payload(row, cap) for row in rows] if links else None,
            "heatmap": self._heatmap_payload(cap),
            "track": None if cap.track is None else {k: _round(v) for k, v in cap.track.items()},
            "status": self._status_payload(cap),
            "node_health": [{**node, "last_heard_s": _round(node["last_heard_s"]),
                             "link_rate_hz": _round(node["link_rate_hz"])} for node in health],
            "coverage": coverage(health, rows, cap.broadcasts["unknown_nodes"]),
            "link_health": self._link_health_payload(rows),
            "link_matrix": link_matrix(self._room.transceivers, rows),
        }
        if not links:
            del payload["links"]
        return payload

    def link_detail(self, label: str, now: Optional[float] = None) -> Optional[dict]:
        """One link's payload row (as in ``links``) at host time ``now``, or None for an unknown label. It reads the
        heatmap's baseline and evidence from its last advance and changes nothing."""
        if now is None:
            now = self._last_host if self._last_host is not None else time.time()
        row = self._monitor.row(label, now)
        if row is None:
            return None
        heat = self._heat
        cap = Capture(now=now, rows=[row], heat_values=heat.values, contributing=tuple(heat.contributing),
                      evidence=heat.evidence, baselines=heat.baselines, track=None, room_state="", room_elevated=(),
                      mode=self._monitor.mode, broadcasts={}, parameters={}, heard_at={}, lateness={})
        return self._link_payload(row, cap)

    def _link_payload(self, row: Mapping, cap: "Capture") -> dict:
        score, threshold = row["score"], row["threshold"]
        normalised = None if score is None or not threshold else score / threshold
        return {
            "id": row["id"],
            "receiver": row["receiver"],
            "sender": row.get("transmitter") or self._senders.get(row["mac"]),
            "mac": row["mac"],
            "state": row["state"],
            "mode": row["mode"],
            "elevated": row["elevated"],
            "level": _round(row["level"]),
            "score": _round(score),
            "threshold": _round(threshold),
            "normalised": _round(normalised),
            "pkt_rate": _round(row["pkt_rate"]),
            "drops": row["drops"],
            "rssi": row["rssi"],
            "age_s": _round(row["age_s"]),
            "baseline": _round(cap.baselines.get(row["id"])),
            "evidence": _round(cap.evidence.get(row["id"], 0.0)),
            "weight": self._room.link_weights.get(row["id"], 1.0),
            "contributes": row["id"] in cap.contributing,
        }

    def _room_payload(self) -> dict:
        """Room size, and the zones of a transceiver room (fixed per layout, so safe to read off the lock)."""
        out = {"width_m": self._room.width_m, "depth_m": self._room.depth_m}
        zones = self._monitor.room_zones
        if zones is not None:
            out["zones"] = [{"id": z.id, "x0": _round(z.x0), "y0": _round(z.y0), "x1": _round(z.x1), "y1": _round(z.y1)}
                            for z in zones.map.zones]
        return out

    def _nodes(self) -> list[dict]:
        nodes = list(self._room.receivers) + list(self._room.senders) + list(self._room.transceivers)
        return [{"name": n.name, "role": n.role, "chip": n.chip, "x": n.x, "y": n.y} for n in nodes]

    def _heatmap_payload(self, cap: "Capture") -> dict:
        heat = self._heat
        cells = np.round(np.asarray(cap.heat_values, dtype=float), PAYLOAD_DECIMALS).tolist()
        return {
            "cols": heat.cols,
            "rows": heat.rows,
            "extents": {"x_min": 0.0, "y_min": 0.0, "x_max": self._room.width_m, "y_max": self._room.depth_m},
            "cells": cells,
            "max": max(cells) if cells else 0.0,
            "contributing": sorted(cap.contributing),
        }

    @staticmethod
    def _link_health_payload(rows: list[Mapping]) -> dict:
        agg = link_health(rows)
        return {**agg, "rate_hz": {k: _round(v) for k, v in agg["rate_hz"].items()}}

    def _status_payload(self, cap: "Capture") -> dict:
        by_state = {state: 0 for state in STATES}
        for row in cap.rows:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        room = {"state": cap.room_state, "elevated": list(cap.room_elevated), "mode": cap.mode}
        if cap.room_zones is not None:
            room["zones"] = dict(cap.room_zones)
        return {
            "time_s": cap.now,
            "links_by_state": by_state,
            "parameters": cap.parameters,
            "room": room,
            "broadcasts": cap.broadcasts,
        }


@dataclass(frozen=True)
class Capture:
    """What one payload needs, taken under the lock by ``DashboardState.capture``; ``render`` reads only this."""
    now: float
    rows: list                        # LinkMonitor.snapshot rows (new dicts)
    heat_values: Any                  # the smoothed grid (an array the heatmap replaces, never edits)
    contributing: tuple
    evidence: Mapping[str, float]
    baselines: Mapping[str, Optional[float]]
    track: Optional[dict]
    room_state: str
    room_elevated: tuple
    mode: str
    broadcasts: dict
    parameters: dict
    heard_at: dict
    lateness: dict
    room_zones: Optional[dict] = None   # {zone id: state} of a transceiver room (a new dict per capture), else None


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), PAYLOAD_DECIMALS)
