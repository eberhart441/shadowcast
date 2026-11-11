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
                sources {name: {heard, accepted, duplicate, queue_overflow}}}, the round broadcasts ingested
                (all 0 in the line pipeline); reasons are csi.rounds.REJECT_REASONS, each always present

Time is host time from the packets. ``snapshot()`` with no argument uses the newest packet time, so the
payload is deterministic for simulated streams; pass ``now`` to advance the decay while packets stop.
"""
from __future__ import annotations

import time
from typing import Mapping, Optional

from .heatmap import Heatmap
from .links import LinkMonitor, NO_SIGNAL, UNCALIBRATED, WARMUP, room_link_ids
from .parser import CSIPacket
from .room import Room
from .rounds import RoundIngest
from .tracker import Tracker

STATES = ("EMPTY", "MOTION", WARMUP, UNCALIBRATED, NO_SIGNAL)
PAYLOAD_DECIMALS = 4


class DashboardState:
    def __init__(self, room: Room, thresholds: Optional[Mapping[str, float]] = None, *, mode: str = "fixed",
                 **heatmap_params) -> None:
        self._room = room
        expected = [link_id for link_id, _, _ in room_link_ids(room)]
        self._monitor = LinkMonitor(thresholds or {}, mode=mode, expected=expected, room=room)
        self._ingest = RoundIngest(room)
        self._heat = Heatmap(room, **heatmap_params)
        self._track = Tracker(room.width_m, room.depth_m, self._heat.centres)
        self._senders = {tx.mac: tx.name for tx in room.senders}
        self._receivers = {rx.name for rx in room.receivers}
        self._last_host: Optional[float] = None

    def push(self, receiver: str, pkt: CSIPacket) -> None:
        self._monitor.push(receiver, pkt)
        self._advance(pkt.host_ts)

    def push_broadcast(self, host_ts: float, data: bytes, source: str = "") -> None:
        """One round broadcast heard at ``host_ts`` by ``source`` (a gateway): its link reports drive their links.
        Bytes that cannot be used, and copies another gateway already delivered, are counted in
        ``status.broadcasts``, never raised."""
        self._monitor.push_reports(host_ts, self._ingest.place(data, host_ts, source))
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
        return {
            "cols": self._heat.cols,
            "rows": self._heat.rows,
            "falloff_m": self._heat.falloff_m,
            "sensitivity": self._heat.sensitivity,
            "agreement": self._heat.agreement,
            "r_floor": self._heat.r_floor,
            "r_full": self._heat.r_full,
            "baseline_window_s": self._heat.baseline_window_s,
            "baseline_percentile": self._heat.baseline_percentile,
            "baseline_warmup_s": self._heat.baseline_warmup_s,
            "rise_tau_s": self._heat.rise_tau,
            "decay_tau_s": self._heat.decay_tau,
            "step_s": self._heat.step,
        }

    def snapshot(self, now: Optional[float] = None) -> dict:
        if now is None:
            now = self._last_host if self._last_host is not None else time.time()
        rows = self._monitor.snapshot(now)
        self._heat.advance(now, rows)
        self._track.update(now, self._heat.target)
        links = [self._link_payload(row) for row in rows]
        return {
            "room": self._room_payload(),
            "nodes": self._nodes(),
            "links": links,
            "heatmap": self._heatmap_payload(),
            "track": self._track_payload(now),
            "status": self._status_payload(now, rows),
        }

    def _link_payload(self, row: Mapping) -> dict:
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
            "baseline": _round(self._heat.baselines.get(row["id"])),
            "evidence": _round(self._heat.evidence.get(row["id"], 0.0)),
            "weight": self._room.link_weights.get(row["id"], 1.0),
            "contributes": row["id"] in self._heat.contributing,
        }

    def _room_payload(self) -> dict:
        out = {"width_m": self._room.width_m, "depth_m": self._room.depth_m}
        zones = self._monitor.room_zones
        if zones is not None:
            out["zones"] = [{"id": z.id, "x0": _round(z.x0), "y0": _round(z.y0), "x1": _round(z.x1), "y1": _round(z.y1)}
                            for z in zones.map.zones]
        return out

    def _nodes(self) -> list[dict]:
        nodes = list(self._room.receivers) + list(self._room.senders) + list(self._room.transceivers)
        return [{"name": n.name, "role": n.role, "chip": n.chip, "x": n.x, "y": n.y} for n in nodes]

    def _heatmap_payload(self) -> dict:
        heat = self._heat
        cells = [round(float(v), PAYLOAD_DECIMALS) for v in heat.values]
        return {
            "cols": heat.cols,
            "rows": heat.rows,
            "extents": {"x_min": 0.0, "y_min": 0.0, "x_max": self._room.width_m, "y_max": self._room.depth_m},
            "cells": cells,
            "max": max(cells) if cells else 0.0,
            "contributing": sorted(heat.contributing),
        }

    def _track_payload(self, now: float) -> Optional[dict]:
        track = self._track.state(now)
        return None if track is None else {k: _round(v) for k, v in track.items()}

    def _status_payload(self, now: float, rows: list[Mapping]) -> dict:
        by_state = {state: 0 for state in STATES}
        for row in rows:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        room = {"state": self._monitor.room_state, "elevated": list(self._monitor.room_elevated),
                "mode": self._monitor.mode}
        if self._monitor.room_zones is not None:
            room["zones"] = self._monitor.room_zones.zone_states
        return {
            "time_s": now,
            "links_by_state": by_state,
            "parameters": self.parameters(),
            "room": room,
            "broadcasts": self._ingest.counts(),
        }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), PAYLOAD_DECIMALS)
