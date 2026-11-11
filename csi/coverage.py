"""Per-node health and coverage: can the rig see the whole room right now?

At 30 nodes the operator cannot read 870 link cards; a dead or badly placed node has to show at a glance, and a
rig that cannot see must never read as a quiet room (ADR-0001: a miss costs more than a false alarm).

    node_health(nodes, rows, heard_at, now)   [{name, state, heard_by, hears, peers, last_heard_s,
                                                 link_rate_hz, slot_lateness}]  one per transceiver
    coverage(health, rows, unknown_nodes)     {state, reasons, missing_nodes, unknown_nodes, silent_links,
                                               unexplained_silent_links}
    link_health(rows)                         {links, heard, silent, rate_hz {min, median, max}}  all links at once
    link_matrix(nodes, rows)                  {nodes, states, codes}: one character per link, by node pair

``rows`` are LinkMonitor.snapshot rows. ``heard_at`` maps a node name to the host time its last round broadcast
reached the host. A node is **missing** when none of its round broadcasts reached the host for more than
``NO_SIGNAL_S`` (or never): its own link reports are then lost, and the others stop hearing it if it is dead.

Coverage is **degraded** when a node is missing, a node not in the room config is broadcasting (its position is
unknown, so its links cannot be used), or more than ``SILENT_SHARE`` of the links between nodes that are not
missing are NO_SIGNAL (nodes that are up but cannot hear each other). Otherwise it is ok. It errs towards
degraded: a few silent links already count.

Slot lateness comes from the nodes' round scheduler (ticket #14), which does not report it yet: it reads
``"unknown"`` until it does, then a number of microseconds.
"""
from __future__ import annotations

import statistics
from typing import Mapping, Optional, Sequence

from .links import NO_SIGNAL, NO_SIGNAL_S
from .room import Node

OK = "ok"
MISSING = "missing"
DEGRADED = "degraded"
SILENT_SHARE = 0.05          # share of links between live nodes that may be NO_SIGNAL before coverage is degraded
SLOT_LATENESS_UNKNOWN = "unknown"
STATE_CODES = {"E": "EMPTY", "M": "MOTION", "W": "WARMUP", "U": "UNCALIBRATED", "N": NO_SIGNAL}
_CODE_OF = {state: code for code, state in STATE_CODES.items()}
NO_LINK = "-"                # the matrix diagonal (a node does not hear itself), or a pair with no link



def node_health(nodes: Sequence[Node], rows: Sequence[Mapping], heard_at: Mapping[str, float],
                now: float) -> list[dict]:
    """Health of each node, from its links' rows and when its round broadcasts last reached the host."""
    by_rx: dict[str, list[Mapping]] = {n.name: [] for n in nodes}
    by_tx: dict[str, list[Mapping]] = {n.name: [] for n in nodes}
    for row in rows:
        rx, tx = row["receiver"], row.get("transmitter")
        if rx in by_rx and tx in by_tx:
            by_rx[rx].append(row)
            by_tx[tx].append(row)
    out = []
    for node in nodes:
        last = heard_at.get(node.name)
        age = None if last is None else max(0.0, now - last)
        own = by_rx[node.name] + by_tx[node.name]
        rate = sum(row["pkt_rate"] for row in own) / len(own) if own else 0.0
        out.append({
            "name": node.name,
            "state": MISSING if age is None or age > NO_SIGNAL_S else OK,
            "heard_by": sum(row["state"] != NO_SIGNAL for row in by_tx[node.name]),
            "hears": sum(row["state"] != NO_SIGNAL for row in by_rx[node.name]),
            "peers": len(nodes) - 1,
            "last_heard_s": age,
            "link_rate_hz": rate,
            "slot_lateness": SLOT_LATENESS_UNKNOWN,
        })
    return out


def coverage(health: Sequence[Mapping], rows: Sequence[Mapping],
             unknown_nodes: Optional[Mapping[str, int]] = None) -> dict:
    """Whether the rig covers the room now, and why not (see the module doc)."""
    missing = sorted(node["name"] for node in health if node["state"] == MISSING)
    unknown = sorted(unknown_nodes or {})
    gone = set(missing)
    silent = [row for row in rows if row["state"] == NO_SIGNAL]
    live = [row for row in rows if row["receiver"] not in gone and row.get("transmitter") not in gone]
    unexplained = [row for row in live if row["state"] == NO_SIGNAL]
    reasons = []
    if missing:
        reasons.append(f"{len(missing)} node(s) missing, no round broadcast for over {NO_SIGNAL_S:g} s: "
                       f"{', '.join(missing)}")
    if unknown:
        reasons.append(f"{len(unknown)} unknown node(s) broadcasting, not in the room config: {', '.join(unknown)}")
    if live and len(unexplained) > SILENT_SHARE * len(live):
        reasons.append(f"{len(unexplained)} of {len(live)} links between live nodes are silent (NO_SIGNAL)")
    return {
        "state": DEGRADED if reasons else OK,
        "reasons": reasons,
        "missing_nodes": missing,
        "unknown_nodes": unknown,
        "silent_links": len(silent),
        "unexplained_silent_links": len(unexplained),
    }


def link_health(rows: Sequence[Mapping]) -> dict:
    """Aggregate health of all links: how many are heard, how many silent, and the spread of their packet rate."""
    heard = [row for row in rows if row["state"] != NO_SIGNAL]
    rates = [row["pkt_rate"] for row in heard]
    return {
        "links": len(rows),
        "heard": len(heard),
        "silent": len(rows) - len(heard),
        "rate_hz": {"min": min(rates), "median": statistics.median(rates), "max": max(rates)} if rates
                   else {"min": None, "median": None, "max": None},
    }


def link_matrix(nodes: Sequence[Node], rows: Sequence[Mapping]) -> dict:
    """Every transceiver link's state as one character (``STATE_CODES``): ``states[r][t]`` is the link from node
    ``t`` (transmitting) to node ``r`` (receiving), in the order of ``nodes``; ``-`` on the diagonal."""
    index = {node.name: k for k, node in enumerate(nodes)}
    grid = [[NO_LINK] * len(nodes) for _ in nodes]
    for row in rows:
        r, t = index.get(row["receiver"]), index.get(row.get("transmitter"))
        if r is not None and t is not None:
            grid[r][t] = _CODE_OF.get(row["state"], "?")
    return {"nodes": [node.name for node in nodes], "states": ["".join(line) for line in grid],
            "codes": dict(STATE_CODES)}
