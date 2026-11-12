"""Raw mode: stream one link's full CSI (int8 I/Q) for recording and algorithm work, within a per-round byte budget.

The operator (the dashboard page, or a POST to /api/raw-mode on loopback, ADR-0015) asks for raw mode on a link
``n1<-n5``. The host builds a **raw mode request** (round broadcast frame type 4, docs/round-broadcast.md) for the
receiving node n1, naming every transmitter whose link n1 is to stream, signs it with the kit key and hands it to
a gateway, which sends it in the join slot of a round the scheduler numbers even (the slot joiners use; never a
member's slot). n1 then sends **round broadcasts with raw CSI** (frame type 5): its link reports, flagged raw mode
on that link, followed by the I/Q of the last round broadcast it heard from n5. The host places the reports as
usual, counts the raw CSI per link, and a recording keeps the bytes as they came, so raw CSI is recorded and
replays with the round broadcasts (docs/round-recording.md). Turning raw mode off sends n1 its remaining set.

    raw = RawMode(room, kit_key)                         # DashboardState owns one
    frames = raw.request("n1<-n5", True, ingest, now, gateway_serial="02:00:00:00:00:80")   # or RawModeRefused
    for frame in frames: gateway.send_frame(frame)
    raw.observe(host_ts, placed_reports, raw_blocks)     # every round broadcast
    raw.status(now)                                      # payload "raw_mode"

**Budget.** Every raw link costs at most ``RAW_BLOCK_MAX_BYTES`` = 133 bytes per round (5 bytes of block head and
up to 128 I/Q bytes: one HT20 long training field, 64 subcarriers). The whole rig may stream at most
``RAW_BUDGET_BYTES`` = 400 bytes of raw CSI per round, so three links at once, and one node at most
``RAW_MAX_LINKS`` = 2 (both must fit in its slot beside its link reports). A request beyond either is refused with
a reason. With report 03's airtime model (``ppdu_us``: HT20 MCS0, ESP-NOW framing), one raw link adds 172 us of
airtime to its node's round broadcast at 30 nodes (142 -> 275 bytes of payload: 274 -> 446 us); the full budget,
three links on three nodes, adds 516 us per round: at 20 Hz 1.0 % of the channel (the 30-node rig's 16.4 % becomes
17.5 %), at 16 nodes and 40 Hz 2.0 % (13.2 % becomes 15.2 %). A round broadcast with raw CSI is longer than ESP-NOW
v1's 250 bytes at most rig sizes: the node image sends ESP-NOW v2 (up to 1470 bytes), which every node runs.

Requests are sent ``REQUEST_COPIES`` times (each with the next round number of the gateway, so each is fresh under
the replay rule) because broadcasts are not acknowledged; the acknowledgement is the raw mode flag in the target's
link reports. A link asked for and not flagged within ``ACK_TIMEOUT_S`` shows "no answer".
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from .broadcast import (FORMAT_VERSION, FRAME_RAW_REQUEST, RAW_BLOCK_MAX_BYTES, RAW_MAX_LINKS, LinkReport, RawCsi,
                        RawModeRequest, RoundBroadcast, encode_broadcast)
from .links import NodeLinkId
from .errors import ReasonError
from .room import Room
from .round_auth import TEST_KIT_KEY, sign_broadcast

RAW_BUDGET_BYTES = 400        # raw CSI per round over the whole rig: 3 links of RAW_BLOCK_MAX_BYTES
REQUEST_COPIES = 3            # a raw mode request goes out this many times (unacknowledged broadcasts)
ACK_TIMEOUT_S = 3.0           # a request not answered by the raw mode flag within this shows "no answer"
FLAG_FRESH_S = 2.0            # the raw mode flag counts as current for this long after the report carrying it
RATE_WINDOW_S = 2.0           # raw CSI blocks per second over this window
REFUSALS = ("unknown_link", "budget", "node_limit", "not_heard", "no_gateway")


class SendingGateway(Protocol):
    """A source that can also send: a gateway on USB, or the simulated rig. ``gateway_serial`` is its board serial
    (the raw mode request's node id), None until known."""
    gateway_serial: Optional[str]

    def send_frame(self, frame: bytes) -> None: ...


class RawModeRefused(ReasonError):
    """A raw mode request the host will not send. ``reason`` is one of REFUSALS."""


def espnow_frame_bytes(payload: int) -> int:
    """Bytes of an ESP-NOW frame carrying ``payload`` bytes (report 03 section 2.1): MAC header, category, OUI,
    random value and FCS (36 bytes) and a 7-byte vendor element per 250 bytes of payload."""
    return 36 + 7 * max(1, math.ceil(payload / 250)) + payload


def ppdu_us(payload: int) -> int:
    """Time on air in microseconds of an ESP-NOW frame with ``payload`` bytes at HT20 MCS0 (report 03 section 2.1:
    36 us of HT-mixed preamble, 26 data bits per 4 us symbol after 22 service and tail bits, 6 us signal
    extension)."""
    return 36 + 4 * math.ceil((22 + 8 * espnow_frame_bytes(payload)) / 26) + 6


def raw_airtime_us(members: int, links: int = 1) -> int:
    """Airtime raw mode adds per round with ``links`` raw links, each on its own node, in a round of ``members``."""
    base = 26 + 4 * (members - 1)
    return links * (ppdu_us(base + RAW_BLOCK_MAX_BYTES) - ppdu_us(base))


@dataclass
class _Link:
    """What the host knows about raw mode on one link."""
    wanted: bool = False                 # this dashboard asked for it on
    asked_at: Optional[float] = None     # host time of the last request (on or off) for it
    flagged: bool = False                # the target's latest report on this link has the raw mode flag
    flag_at: Optional[float] = None
    blocks: int = 0
    iq_bytes: int = 0
    last_at: Optional[float] = None
    recent: deque = field(default_factory=deque)   # host times of the blocks in the last RATE_WINDOW_S

    def flagged_at(self, now: float) -> bool:
        return self.flagged and self.flag_at is not None and now - self.flag_at <= FLAG_FRESH_S


def _parse(label: str) -> Optional[NodeLinkId]:
    receiver, sep, transmitter = label.partition("<-")
    return NodeLinkId(receiver, transmitter) if sep and receiver and transmitter else None


class RawMode:
    def __init__(self, room: Room, kit_key: bytes = TEST_KIT_KEY, seed: Optional[int] = None) -> None:
        self._key = bytes(kit_key)
        self._serial = {node.name: node.serial.upper() for node in room.transceivers if node.serial}
        self._links: dict[str, _Link] = {}
        self._rounds: dict[str, int] = {}      # gateway serial -> the round number of its next request
        self._rng = random.Random(seed)

    # ------------------------------------------------------------------ requests

    def request(self, label: str, on: bool, last_round: Mapping[str, int], now: float,
                gateway_serial: Optional[str]) -> list[bytes]:
        """The signed raw mode request frames that turn raw mode on (or off) for link ``label``.

        ``last_round`` maps a node's board serial (upper case) to the round number of its latest round broadcast
        (``RoundIngest.last_rounds``). ``gateway_serial`` is the serial of the gateway that will send the frames.
        Raises RawModeRefused, and changes nothing, when the request is not sent."""
        link = _parse(label)
        if link is None or link.receiver not in self._serial or link.transmitter not in self._serial \
                or link.receiver == link.transmitter:
            raise RawModeRefused("unknown_link", f"{label!r} is not a link between two transceivers of this room")
        target = self._serial[link.receiver]
        wanted = {lbl for lbl, state in self._links.items() if state.wanted}
        streaming = set(self._costing(now))
        if on and label not in streaming:
            mine = sorted(lbl for lbl in streaming | {label} if _parse(lbl).receiver == link.receiver)
            if len(mine) > RAW_MAX_LINKS:
                raise RawModeRefused("node_limit", f"raw mode on {label} refused: {link.receiver} already streams "
                                                   f"{RAW_MAX_LINKS} links ({', '.join(m for m in mine if m != label)}),"
                                                   f" the most one round broadcast carries; turn one off first")
            used = RAW_BLOCK_MAX_BYTES * len(streaming)
            if used + RAW_BLOCK_MAX_BYTES > RAW_BUDGET_BYTES:
                raise RawModeRefused("budget", f"raw mode on {label} refused: it needs {RAW_BLOCK_MAX_BYTES} B per "
                                               f"round and {used} of the {RAW_BUDGET_BYTES} B raw mode budget are in "
                                               f"use ({', '.join(sorted(streaming))}); turn one off first")
        if target not in last_round:
            raise RawModeRefused("not_heard", f"raw mode on {label}: no round broadcast from {link.receiver} has "
                                              "reached the host yet, so it cannot be addressed")
        if not gateway_serial:
            raise RawModeRefused("no_gateway", "raw mode needs a gateway that can send (a node gateway that has "
                                               "sent its status record); this source cannot")
        after = (wanted | {label}) if on else (wanted - {label})
        transmitters = tuple(self._serial[_parse(lbl).transmitter] for lbl in sorted(after)
                             if _parse(lbl).receiver == link.receiver)
        body = RawModeRequest(target, last_round[target] % 65536, transmitters)
        frames = [self._frame(gateway_serial.upper(), body) for _ in range(REQUEST_COPIES)]
        state = self._links.setdefault(label, _Link())
        state.wanted, state.asked_at = on, now
        return frames

    def _frame(self, gateway_serial: str, body: RawModeRequest) -> bytes:
        round_no = self._rounds.get(gateway_serial)
        if round_no is None:
            round_no = self._rng.randrange(65536)
        self._rounds[gateway_serial] = (round_no + 1) % 65536
        # a request's slot, slots per round, schedule version, node index and membership are 0 and unchecked
        frame = RoundBroadcast(version=FORMAT_VERSION, frame_type=FRAME_RAW_REQUEST, round=round_no, slot=0,
                               slots_per_round=0, schedule_version=0, node_index=0, membership=0,
                               node_id=gateway_serial, tag=bytes(8), reports=(), request=body)
        return sign_broadcast(encode_broadcast(frame), self._key)

    def _costing(self, now: float) -> list[str]:
        """Links that use the budget: asked for here, or flagged raw by their node (it streams them)."""
        return [lbl for lbl, s in self._links.items() if s.wanted or s.flagged_at(now)]

    # ------------------------------------------------------------------ observations

    def observe(self, host_ts: float, placed: Iterable[tuple[NodeLinkId, LinkReport]],
                raw: Sequence[tuple[NodeLinkId, RawCsi]] = ()) -> None:
        """One round broadcast's placed link reports and raw CSI blocks."""
        for link_id, report in placed:
            state = self._links.get(link_id.label)
            if report.raw_mode:
                if state is None:
                    state = self._links[link_id.label] = _Link()
                state.flagged, state.flag_at = True, host_ts
            elif state is not None and state.flagged:
                state.flagged, state.flag_at = False, host_ts
        for link_id, block in raw:
            state = self._links.setdefault(link_id.label, _Link())
            state.blocks += 1
            state.iq_bytes += len(block.iq)
            state.last_at = host_ts
            state.recent.append(host_ts)
            while state.recent and state.recent[0] < host_ts - RATE_WINDOW_S:
                state.recent.popleft()

    # ------------------------------------------------------------------ payload

    def status(self, now: float) -> dict:
        """``raw_mode`` in the payload: the budget and every link that is, was or is asked to be in raw mode.

        state: "on" (its node flags it raw), "requested" (asked, not flagged yet), "no answer" (asked
        ACK_TIMEOUT_S ago and still not flagged), "stopping" (asked off, still flagged), "off"."""
        rows = []
        for label in sorted(self._links):
            s = self._links[label]
            flagged = s.flagged_at(now)
            asked_recently = s.asked_at is not None and now - s.asked_at <= ACK_TIMEOUT_S
            if s.wanted:
                state = "on" if flagged else ("requested" if asked_recently else "no answer")
            else:
                state = ("stopping" if asked_recently else "on") if flagged else "off"
            rate = sum(1 for t in s.recent if t > now - RATE_WINDOW_S) / RATE_WINDOW_S
            rows.append({"id": label, "state": state, "wanted": s.wanted, "blocks": s.blocks,
                         "iq_bytes": s.iq_bytes, "last_s": None if s.last_at is None else round(now - s.last_at, 3),
                         "rate_hz": round(rate, 2)})
        used = RAW_BLOCK_MAX_BYTES * len(self._costing(now))
        return {"budget_bytes": RAW_BUDGET_BYTES, "used_bytes": used, "link_bytes": RAW_BLOCK_MAX_BYTES,
                "max_links_per_node": RAW_MAX_LINKS, "links": rows}


def raw_messages(status: Mapping) -> list[str]:
    """``status.messages`` lines for raw mode links that need the operator: no answer from the node."""
    return [f"raw mode {row['id']}: no answer from {row['id'].split('<-')[0]} (no raw mode flag in its link reports "
            f"after {ACK_TIMEOUT_S:.0f} s); is it in range of the gateway? Ask again or turn it off"
            for row in status.get("links", ()) if row["state"] == "no answer"]
