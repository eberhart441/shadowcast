"""The single state payload the dashboard renders from: per-link monitor plus heatmap.

``DashboardState.push(receiver, pkt)`` feeds a LinkMonitor and the heatmap smoother. ``snapshot(now)``
returns one JSON-serialisable dict:

    room      {width_m, depth_m}
    nodes     [{name, role, chip, x, y}]              receivers then senders
    links     [{id, receiver, sender, mac, state, score, threshold, normalised, baseline, evidence,
                pkt_rate, drops, rssi, age_s, weight, contributes}]
                baseline: the link's quiet baseline (None in warm-up); evidence: 0..1 above it.
                sender is the node name, None if the MAC is unknown
    heatmap   {cols, rows, extents, cells, max, contributing}
                cells: flat row-major floats in 0..1 (row 0 is the top edge, y = 0)
    status    {time_s, links_by_state, parameters}

Time is host time from the packets. ``snapshot()`` with no argument uses the newest packet time, so the
payload is deterministic for simulated streams; pass ``now`` to advance the decay while packets stop.
"""
from __future__ import annotations

import time
from typing import Mapping, Optional

from .heatmap import Heatmap
from .links import LinkId, LinkMonitor, NO_SIGNAL, UNCALIBRATED, WARMUP
from .parser import CSIPacket
from .room import Room

STATES = ("EMPTY", "MOTION", WARMUP, UNCALIBRATED, NO_SIGNAL)
PAYLOAD_DECIMALS = 4


class DashboardState:
    def __init__(self, room: Room, thresholds: Optional[Mapping[str, float]] = None, **heatmap_params) -> None:
        self._room = room
        expected = [LinkId(rx.name, tx.mac) for rx, tx in room.links()]
        self._monitor = LinkMonitor(thresholds or {}, expected=expected)
        self._heat = Heatmap(room, **heatmap_params)
        self._senders = {tx.mac: tx.name for tx in room.senders}
        self._receivers = {rx.name for rx in room.receivers}
        self._last_host: Optional[float] = None

    def push(self, receiver: str, pkt: CSIPacket) -> None:
        self._monitor.push(receiver, pkt)
        self._last_host = pkt.host_ts
        if self._heat.due(pkt.host_ts):
            self._heat.advance(pkt.host_ts, self._monitor.snapshot(pkt.host_ts))

    @property
    def thresholds(self) -> dict[str, float]:
        return self._monitor.thresholds

    def set_threshold(self, label: str, threshold: float) -> None:
        self._monitor.set_threshold(label, threshold)

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
        links = [self._link_payload(row) for row in rows]
        return {
            "room": {"width_m": self._room.width_m, "depth_m": self._room.depth_m},
            "nodes": self._nodes(),
            "links": links,
            "heatmap": self._heatmap_payload(),
            "status": self._status_payload(now, rows),
        }

    def _link_payload(self, row: Mapping) -> dict:
        score, threshold = row["score"], row["threshold"]
        normalised = None if score is None or not threshold else score / threshold
        return {
            "id": row["id"],
            "receiver": row["receiver"],
            "sender": self._senders.get(row["mac"]),
            "mac": row["mac"],
            "state": row["state"],
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

    def _nodes(self) -> list[dict]:
        nodes = list(self._room.receivers) + list(self._room.senders)
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

    def _status_payload(self, now: float, rows: list[Mapping]) -> dict:
        by_state = {state: 0 for state in STATES}
        for row in rows:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        return {
            "time_s": now,
            "links_by_state": by_state,
            "parameters": self.parameters(),
        }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), PAYLOAD_DECIMALS)
