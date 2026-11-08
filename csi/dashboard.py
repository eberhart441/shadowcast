"""The single state payload the dashboard renders from: per-link monitor plus heatmap.

``DashboardState.push(receiver, pkt)`` feeds a LinkMonitor and the heatmap smoother. ``snapshot(now)``
returns one JSON-serialisable dict:

    room      {width_m, depth_m}
    nodes     [{name, role, chip, x, y}]              receivers then senders
    links     [{id, receiver, sender, mac, state, mode, elevated, level, score, threshold, normalised, baseline,
                evidence, pkt_rate, drops, rssi, age_s, weight, contributes, since_motion_s,
                seq_rate, seq_loss, clip_pct, agc_gain, fft_gain, noise_floor}]
                baseline: the link's quiet baseline (None in warm-up); evidence: 0..1 above it.
                mode: "fixed" (judged against threshold) or "auto" (its own adaptive baseline, csi/adaptive.py).
                elevated: True when the link is in MOTION (auto: elevated above its adaptive enter level).
                level: score / threshold (auto: / the adaptive enter level); >= 1 at or above it.
                sender is the node name, None if the MAC is unknown
                since_motion_s: seconds since the link was last in MOTION (0 while it is), None if never
                seq_rate .. noise_floor: link health from the monitor rows (csi/links.py), passed through:
                packets/s the sender sent and the share lost (from its sequence counter), the share of clipped
                samples (0..1), the receiver's gains and noise floor (ints). None when the monitor has none.
    heatmap   {cols, rows, extents, cells, max, contributing, busy_nodes}
                cells: flat row-major floats in 0..1 (row 0 is the top edge, y = 0)
                busy_nodes: nodes with movement right next to them (near-node veto, csi/heatmap.py); their
                links are down-weighted on the map
    status    {time_s, links_by_state, parameters, room}
                room: {state, elevated (labels), mode, since_motion_s}: room-level MOTION from link agreement
                (csi/adaptive.py); since_motion_s: seconds since any of the room's links was last in MOTION

Time is host time from the packets. ``snapshot()`` with no argument uses the newest packet time, so the
payload is deterministic for simulated streams; pass ``now`` to advance the decay while packets stop.
"""
from __future__ import annotations

import math
import time
from typing import Mapping, Optional

from .heatmap import Heatmap
from .links import LinkId, LinkMonitor, NO_SIGNAL, UNCALIBRATED, WARMUP
from .parser import CSIPacket
from .room import Room

STATES = ("EMPTY", "MOTION", WARMUP, UNCALIBRATED, NO_SIGNAL)
PAYLOAD_DECIMALS = 4
HEALTH_NUMBERS = ("seq_rate", "seq_loss", "clip_pct")      # link-health numbers, rounded like the others
HEALTH_INTS = ("agc_gain", "fft_gain", "noise_floor")       # link-health integers


class DashboardState:
    def __init__(self, room: Room, thresholds: Optional[Mapping[str, float]] = None, *, mode: str = "fixed",
                 **heatmap_params) -> None:
        self._room = room
        expected = [LinkId(rx.name, tx.mac) for rx, tx in room.links()]
        self._monitor = LinkMonitor(thresholds or {}, mode=mode, expected=expected)
        self._heat = Heatmap(room, **heatmap_params)
        self._senders = {tx.mac: tx.name for tx in room.senders}
        self._receivers = {rx.name for rx in room.receivers}
        self._room_labels = {link.label for link in expected}
        self._motion_at: dict[str, float] = {}   # link label -> host time it was last seen in MOTION
        self._last_host: Optional[float] = None

    def push(self, receiver: str, pkt: CSIPacket) -> None:
        self._monitor.push(receiver, pkt)
        self._last_host = pkt.host_ts
        if self._heat.due(pkt.host_ts):
            rows = self._monitor.snapshot(pkt.host_ts)
            self._note_motion(pkt.host_ts, rows)
            self._heat.advance(pkt.host_ts, rows)

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
            "coverage_eps": self._heat.coverage_eps,
            "min_coverage": self._heat.min_coverage,
            "baseline_freeze_s": self._heat.baseline_freeze_s,
            "busy_evidence": self._heat.busy_evidence,
            "busy_link_weight": self._heat.busy_link_weight,
        }

    def snapshot(self, now: Optional[float] = None) -> dict:
        if now is None:
            now = self._last_host if self._last_host is not None else time.time()
        rows = self._monitor.snapshot(now)
        self._note_motion(now, rows)
        self._heat.advance(now, rows)
        links = [self._link_payload(row, now) for row in rows]
        return {
            "room": {"width_m": self._room.width_m, "depth_m": self._room.depth_m},
            "nodes": self._nodes(),
            "links": links,
            "heatmap": self._heatmap_payload(),
            "status": self._status_payload(now, rows),
        }

    def _note_motion(self, now: float, rows: list[Mapping]) -> None:
        for row in rows:
            if row["elevated"] and now >= self._motion_at.get(row["id"], -math.inf):
                self._motion_at[row["id"]] = now

    def _since_motion(self, label: str, now: float) -> Optional[float]:
        at = self._motion_at.get(label)
        return None if at is None else max(0.0, now - at)

    def _link_payload(self, row: Mapping, now: float) -> dict:
        score, threshold = row["score"], row["threshold"]
        normalised = None if score is None or not threshold else score / threshold
        health = {key: _health(row.get(key)) for key in HEALTH_NUMBERS}
        health.update({key: _health(row.get(key), integer=True) for key in HEALTH_INTS})
        return {
            "id": row["id"],
            "receiver": row["receiver"],
            "sender": self._senders.get(row["mac"]),
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
            "since_motion_s": _round(self._since_motion(row["id"], now)),
            **health,
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
            "busy_nodes": list(heat.busy_nodes),
        }

    def _status_payload(self, now: float, rows: list[Mapping]) -> dict:
        by_state = {state: 0 for state in STATES}
        for row in rows:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        return {
            "time_s": now,
            "links_by_state": by_state,
            "parameters": self.parameters(),
            "room": {"state": self._monitor.room_state, "elevated": list(self._monitor.room_elevated),
                     "mode": self._monitor.mode, "since_motion_s": _round(self._room_since_motion(now))},
        }

    def _room_since_motion(self, now: float) -> Optional[float]:
        ages = [self._since_motion(label, now) for label in self._room_labels if label in self._motion_at]
        return min(ages) if ages else None


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), PAYLOAD_DECIMALS)


def _health(value, integer: bool = False):
    """A link-health number from a monitor row, or None when the row has none (an older monitor) or it is
    not a finite number (a NaN would make the payload invalid JSON for the page)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(round(number)) if integer else round(number, PAYLOAD_DECIMALS)
