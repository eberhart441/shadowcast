"""Fake CSI stream for hardware-free development.

Model: static per-subcarrier baseline + Gaussian noise. While a person is
``still`` a ~0.3 Hz sinusoid (breathing) is added, with per-subcarrier gain and
phase so some subcarriers respond more than others. During ``motion`` noise is
amplified and slow random-walk drift is added. ``empty`` is baseline + noise.

The transceiver rig (``simulate_rounds``) is simulated one level up, as the nodes' own output: each node
broadcasts once per round in its slot, carrying a link report per other node whose score is the link's quiet
level times lognormal noise, raised by a person near the link (the same Gaussian falloff as above). The stream is
(host time, round broadcast bytes), as a gateway delivers it. ``SimulatedRig`` is that stream plus the gateway's
sending side: raw mode requests sent to it reach the simulated nodes, which then stream synthetic raw CSI.
"""
from __future__ import annotations

import heapq
import itertools
import time
from collections import deque
from dataclasses import dataclass
from operator import itemgetter
from typing import Callable, Iterable, Iterator, Mapping, Optional, Sequence, Union

import numpy as np

from .broadcast import (FLAG_RAW_MODE, FLAG_WARMING, FORMAT_VERSION, FRAME_JOIN_GRANT, FRAME_JOIN_REQUEST,
                        FRAME_RAW_REQUEST, FRAME_ROUND,
                        FRAME_ROUND_RAW, HEARD_WINDOW_ROUNDS, MAX_NODES, RAW_MAX_LINKS, RAW_REQUEST_MAX_AGE_ROUNDS,
                        SINCE_MAX, BroadcastError, LinkReport, RawCsi, RoundBroadcast, decode_broadcast,
                        encode_broadcast)
from .score_code import encode_score
from .round_auth import STALE, TEST_KIT_KEY, Freshness, sign_broadcast, tag_ok
from .links import LinkId
from .parser import DEFAULT_HEADER, CSIPacket, CSIParser, parse_stream
from .room import FALLOFF_SIGMA_M, Person, Room, board_serial, link_responses, load_room, movement

N_SUBCARRIERS = 64
NULL_SUBCARRIERS = tuple(range(0, 6)) + (32,) + tuple(range(59, 64))  # 12 nulls (LLTF-like)
BREATH_HZ = 0.3
DEFAULT_MAC = "1a:00:00:00:00:00"
SENDER_MACS = ("1a:00:00:00:00:00", "1a:00:00:00:00:01")  # the two ESP-NOW senders (S3, C5)
DEFAULT_RECEIVERS = ("rx1", "rx2")


@dataclass(frozen=True)
class Segment:
    state: str        # "empty" | "motion" | "still"
    duration_s: float


DEFAULT_SCENARIO = (Segment("empty", 20), Segment("motion", 10), Segment("still", 30))


def _amplitudes(t: np.ndarray, state: str, base: np.ndarray, gain: np.ndarray,
                phase: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Amplitude matrix (len(t), n_sc) for one segment."""
    n, k = len(t), len(base)
    amp = base + rng.normal(0, 0.4, (n, k))
    if state == "still":
        amp += 1.5 * gain * np.sin(2 * np.pi * BREATH_HZ * t[:, None] + phase)
    elif state == "motion":
        amp += _motion_term(n, k, rng)
    elif state != "empty":
        raise ValueError(f"unknown state {state!r}")
    return np.clip(amp, 0.0, None)


def _motion_term(n: int, k: int, rng: np.random.Generator) -> np.ndarray:
    """Gross-movement variation (n, k): high-variance noise plus slow drift. Scaled per link by a person."""
    return rng.normal(0, 3.0, (n, k)) + 4.0 * np.cumsum(rng.normal(0, 0.15, (n, 1)), axis=0)


def simulate_packets(
    scenario: Sequence[Segment] = DEFAULT_SCENARIO,
    rate_hz: float = 100.0,
    seed: int = 0,
    drop_prob: float = 0.0,
    realtime: bool = False,
    loop: bool = False,
    t0: float = 1_700_000_000.0,
    device_clock: Optional[Callable[[int], int]] = None,
    mac: str = "",
) -> Iterator[tuple[CSIPacket, str]]:
    """Yield (packet, state_label). ``loop`` repeats the scenario forever.

    ``drop_prob`` randomly omits packets (device_ts keeps advancing, so gaps are
    visible). ``realtime`` sleeps to emulate the wire rate and uses the wall
    clock as host_ts; otherwise host_ts is synthetic and runs as fast as possible.
    ``device_clock`` maps the ideal device time (us) to the value the receiver
    reports, e.g. a 32-bit wrap or a reboot to 0; host_ts is unaffected.
    """
    rng = np.random.default_rng(seed)
    base = rng.uniform(15, 40, N_SUBCARRIERS)
    gain = rng.uniform(0.2, 1.0, N_SUBCARRIERS)
    phase = rng.uniform(0, 2 * np.pi, N_SUBCARRIERS)
    dt = 1.0 / rate_hz
    i = 0
    start = time.time()
    while True:
        for seg in scenario:
            n = int(round(seg.duration_s * rate_hz))
            t = (i + np.arange(n)) * dt
            block = _amplitudes(t, seg.state, base, gain, phase, rng)
            for j in range(n):
                idx = i + j
                if realtime:
                    time.sleep(max(0.0, start + (idx + 1) * dt - time.time()))
                if drop_prob and rng.random() < drop_prob:
                    continue
                host = time.time() if realtime else t0 + idx * dt
                local = int(idx * dt * 1e6 + rng.integers(0, 500))
                yield CSIPacket(
                    host_ts=host,
                    device_ts=device_clock(local) if device_clock else local,
                    rssi=int(-45 - 3 * (seg.state == "motion") + rng.integers(-1, 2)),
                    channel=6,
                    amplitude=block[j][_keep_mask()].astype(np.float32),
                    mac=mac,
                ), seg.state
            i += n
        if not loop:
            return


def _keep_mask() -> np.ndarray:
    mask = np.ones(N_SUBCARRIERS, dtype=bool)
    mask[list(NULL_SUBCARRIERS)] = False
    return mask


def packet_to_line(pkt: CSIPacket, idx: int = 0, mac: Optional[str] = None) -> str:
    """Render a packet as an esp-csi ``CSI_DATA`` line (nulls re-inserted as 0).

    ``mac`` is the sender written in the mac column; default is the packet's own mac, or DEFAULT_MAC.
    """
    full = np.zeros(N_SUBCARRIERS)
    full[_keep_mask()] = pkt.amplitude
    # split amplitude across I/Q at a fixed angle, rounded to int8
    iq = np.zeros((N_SUBCARRIERS, 2), dtype=int)
    iq[:, 0] = np.clip(np.round(full * 0.6), -128, 127)
    iq[:, 1] = np.clip(np.round(full * 0.8), -128, 127)
    data = ",".join(map(str, iq.reshape(-1)))
    cols = DEFAULT_HEADER.split(",")
    vals = {"type": "CSI_DATA", "id": idx, "mac": mac or pkt.mac or DEFAULT_MAC, "rssi": pkt.rssi,
            "channel": pkt.channel, "local_timestamp": pkt.device_ts, "len": iq.size,
            "first_word": 0}
    prefix = ",".join(str(vals.get(c, 0)) for c in cols[:-1])
    return f'{prefix},"[{data}]"'


def simulate_stream(header: bool = True, **kwargs) -> Iterator[tuple[float, str]]:
    """Simulated (host_ts, line) stream, same shape as ``serial_stream``.

    Goes through text so the real parser is exercised end to end. Amplitudes are
    quantized to int8 I/Q, which is also what the hardware does.
    """
    if header:
        yield kwargs.get("t0", 1_700_000_000.0), DEFAULT_HEADER
    for idx, (pkt, _) in enumerate(simulate_packets(**kwargs)):
        yield pkt.host_ts, packet_to_line(pkt, idx=idx)


def simulate_labeled_stream(**kwargs) -> Iterator[tuple[CSIPacket, str]]:
    """Packets plus ground-truth state, for tests and evaluation."""
    return simulate_packets(**kwargs)


def _sender_lines(mac: str, seed: int, scenario: Sequence[Segment], rate_hz: float, loop: bool,
                  t0: float) -> Iterator[tuple[float, str]]:
    """(synthetic host_ts, line) for one sender on one receiver. Each link gets its own seed."""
    for idx, (pkt, _) in enumerate(simulate_packets(scenario=scenario, rate_hz=rate_hz, seed=seed,
                                                    loop=loop, t0=t0, mac=mac)):
        yield pkt.host_ts, packet_to_line(pkt, idx=idx, mac=mac)


def link_layout(room: Optional[Room], senders: Optional[Sequence[str]] = None,
                receivers: Optional[Sequence[str]] = None) -> tuple[list[str], list[str]]:
    """(sender MACs, receiver names) for the simulator. Explicit arguments win; the rest come from ``room``
    (its enabled senders and receivers) when one is given, else from the built-in rig (SENDER_MACS and
    DEFAULT_RECEIVERS)."""
    if room is not None:
        senders = [tx.mac for tx in room.senders] if senders is None else senders
        receivers = [rx.name for rx in room.receivers] if receivers is None else receivers
    else:
        senders = SENDER_MACS if senders is None else senders
        receivers = DEFAULT_RECEIVERS if receivers is None else receivers
    return list(senders), list(receivers)


def simulate_link_streams(
    senders: Optional[Sequence[str]] = None,
    receivers: Optional[Sequence[str]] = None,
    scenario: Sequence[Segment] = DEFAULT_SCENARIO,
    rate_hz: float = 100.0,
    seed: int = 0,
    realtime: bool = False,
    loop: bool = False,
    t0: float = 1_700_000_000.0,
    room: Optional[Room] = None,
) -> dict[str, Iterator[tuple[float, str]]]:
    """One (host_ts, line) stream per receiver, each carrying every sender's packets.

    Senders and receivers come from ``room`` when one is given (its enabled nodes), else from the
    built-in rig; explicit ``senders`` / ``receivers`` override either.
    Every (receiver, sender) link has its own seed, so its baseline and noise are independent.
    The scenario applies to all links at once. ``realtime`` paces each stream to the wall clock
    and stamps lines with ``time.time()``; otherwise host_ts is synthetic (t0 + index / rate).
    """
    senders, receivers = link_layout(room, senders, receivers)
    streams: dict[str, Iterator[tuple[float, str]]] = {}
    for r_idx, name in enumerate(receivers):
        gens = [_sender_lines(mac, int(np.random.SeedSequence([seed, r_idx, s_idx]).generate_state(1)[0]),
                              scenario, rate_hz, loop, t0)
                for s_idx, mac in enumerate(senders)]
        streams[name] = _receiver_stream(gens, t0, realtime)
    return streams


def _receiver_stream(gens: Sequence[Iterator[tuple[float, str]]], t0: float,
                     realtime: bool) -> Iterator[tuple[float, str]]:
    """Header, then the senders' lines merged in host order, paced to the wall clock if ``realtime``."""
    yield (time.time() if realtime else t0), DEFAULT_HEADER
    yield from _paced(heapq.merge(*gens, key=itemgetter(0)), t0, realtime)


def _paced(lines: Iterator[tuple[float, str]], t0: float, realtime: bool) -> Iterator[tuple[float, str]]:
    start = time.time()
    for host_ts, line in lines:
        if realtime:
            time.sleep(max(0.0, start + (host_ts - t0) - time.time()))
            host_ts = time.time()
        yield host_ts, line


def _room_link_lines(receiver: str, mac: str, seed: int, response: np.ndarray, rate_hz: float,
                     t0: float, stop_idx: Optional[int], device_offset_us: int = 0) -> Iterator[tuple[float, str]]:
    """(host_ts, line) for one link. Its motion term is scaled by the person's response at each sample.

    The per-link baseline and noise come from ``seed``, so links are independent. ``stop_idx`` ends the
    link's transmissions (a dropped link stops sending from that sample on).
    """
    rng = np.random.default_rng(seed)
    base = rng.uniform(15, 40, N_SUBCARRIERS)
    n = len(response)
    amp = base + rng.normal(0, 0.4, (n, N_SUBCARRIERS)) + response[:, None] * _motion_term(n, N_SUBCARRIERS, rng)
    amp = np.clip(amp, 0.0, None)[:, _keep_mask()].astype(np.float32)
    dt = 1.0 / rate_hz
    last = n if stop_idx is None else min(n, stop_idx)
    for idx in range(last):
        pkt = CSIPacket(
            host_ts=t0 + idx * dt,
            device_ts=device_offset_us + int(idx * dt * 1e6 + rng.integers(0, 500)),
            rssi=int(-45 - 3 * response[idx] + rng.integers(-1, 2)),
            channel=6,
            amplitude=amp[idx],
            mac=mac,
        )
        yield pkt.host_ts, packet_to_line(pkt, idx=idx, mac=mac)


def simulate_room(
    person: Optional[Person] = None,
    *,
    duration_s: float = 20.0,
    drop_after: Optional[Mapping[str, float]] = None,
    room: Optional[Room] = None,
    gain: float = 1.0,
    rate_hz: float = 100.0,
    seed: int = 0,
    realtime: bool = False,
    t0: float = 1_700_000_000.0,
    device_offset_us: int = 0,
) -> dict[str, Iterator[tuple[float, str]]]:
    """One (host_ts, line) stream per receiver, like ``simulate_link_streams``, with a person in the room.

    ``device_offset_us`` is added to every device timestamp, so consecutive segments share one device timeline.

    Positions and nodes come from ``room`` (default: config/room.json). ``person=None`` is an empty room.
    ``drop_after`` maps a link label such as ``"rx1<-01"`` to the seconds after t0 when that link stops
    transmitting. ``realtime`` paces lines to the wall clock as in ``simulate_link_streams``.
    """
    room = load_room() if room is None else room
    drop_after = drop_after or {}
    n = int(round(duration_s * rate_hz))
    t = np.arange(n) / rate_hz
    responses = link_responses(room, person, t, gain)
    streams: dict[str, Iterator[tuple[float, str]]] = {}
    for r_idx, rx in enumerate(room.receivers):
        gens = []
        for s_idx, tx in enumerate(room.senders):
            link_seed = int(np.random.SeedSequence([seed, r_idx, s_idx]).generate_state(1)[0])
            stop = drop_after.get(LinkId(rx.name, tx.mac).label)
            stop_idx = None if stop is None else int(round(stop * rate_hz))
            gens.append(_room_link_lines(rx.name, tx.mac, link_seed, responses[(rx.name, tx.mac)],
                                         rate_hz, t0, stop_idx, device_offset_us))
        streams[rx.name] = _receiver_stream(gens, t0, realtime)
    return streams


def simulate_room_links(**kwargs) -> Iterator[tuple[str, CSIPacket]]:
    """(receiver, packet) for every link with a person in the room, merged in host-time order.

    Goes through the same text seam as ``simulate_links``: each receiver's lines are parsed by its own
    CSIParser.
    """
    streams = simulate_room(**kwargs)
    per_receiver = [_tagged(name, lines) for name, lines in streams.items()]
    yield from heapq.merge(*per_receiver, key=lambda item: item[1].host_ts)


def simulate_links(
    senders: Optional[Sequence[str]] = None,
    receivers: Optional[Sequence[str]] = None,
    scenario: Sequence[Segment] = DEFAULT_SCENARIO,
    rate_hz: float = 100.0,
    seed: int = 0,
    realtime: bool = False,
    loop: bool = False,
    t0: float = 1_700_000_000.0,
    room: Optional[Room] = None,
) -> Iterator[tuple[str, CSIPacket]]:
    """(receiver, packet) for every link, merged in host-time order. Layout as in ``simulate_link_streams``.

    Goes through the text seam: each receiver's lines are parsed by its own CSIParser, so the
    packet's mac comes from the line exactly as it would from hardware.
    """
    streams = simulate_link_streams(senders, receivers, scenario, rate_hz, seed, realtime, loop, t0, room=room)
    per_receiver = [_tagged(name, lines) for name, lines in streams.items()]
    yield from heapq.merge(*per_receiver, key=lambda item: item[1].host_ts)


def _tagged(name: str, lines: Iterator[tuple[float, str]]) -> Iterator[tuple[str, CSIPacket]]:
    for pkt in parse_stream(lines, CSIParser()):
        yield name, pkt


# ---------------------------------------------------------------------- transceiver rig: round broadcasts

ROUND_HZ = 20.0            # rounds per second: each link is measured at this rate
SIM_WARMING_S = 1.0        # a node's link scores need one 1 s window first: warming flag, no score
ROUND_QUIET_SCORE = (0.5, 3.0)   # each link's quiet score level is drawn from this range (its own scale)
ROUND_NOISE_SIGMA = 0.2    # lognormal spread of a quiet score from round to round
ROUND_MOTION_GAIN = 20.0   # a person on the link multiplies its score by 1 + this
GATEWAY_LATENCY_S = 0.0005  # up to this much is added to a broadcast's host time
SIM_GATEWAY_ID = "02:00:00:00:00:80"   # the simulated gateway's board serial (raw mode requests carry it)
SIM_RAW_SUBCARRIERS = 64    # synthetic raw CSI: one HT20 long training field, 128 I/Q bytes
NOISY_NODE_GAIN = 8.0      # a noisy node multiplies the score of every link it is on (both directions) by this
GLITCH_S = 1.5             # one glitch raises one link's score for this long (about a 1 s window plus its onset)
GLITCH_GAIN = 6.0          # ... by this factor: enough to elevate the link above its enter level


def simulate_rounds(
    room: Room,
    person: Union[Person, Sequence[Person], None] = None,
    *,
    duration_s: Optional[float] = None,
    quiet_first_s: float = 0.0,
    round_hz: float = ROUND_HZ,
    dead_after: Optional[Mapping[str, float]] = None,
    drop_prob: float = 0.0,
    noisy: Optional[Mapping[str, tuple[float, float]]] = None,
    glitch_rate_hz: float = 0.0,
    seed: int = 0,
    realtime: bool = False,
    t0: float = 1_700_000_000.0,
    kit_key: bytes = TEST_KIT_KEY,
    raw: Optional[Mapping[str, Iterable[str]]] = None,
    commands: Optional[deque] = None,
    join_after: Optional[Mapping[str, float]] = None,
    split: Iterable[str] = (),
    merge_after: Optional[float] = None,
) -> Iterator[tuple[float, bytes]]:
    """(host time, round broadcast bytes) from a simulated rig of the room's transceivers, as one gateway hears it.

    Every transceiver is a member and broadcasts once per round in its slot (node index = slot = its position in
    ``room.transceivers``; one join slot ends the round). ``person`` is in the room from ``quiet_first_s`` on
    (its time 0 is then); a sequence of people are all there; ``None`` is an empty room. ``duration_s=None`` runs forever. ``dead_after`` maps a node
    name to the seconds after which it stops broadcasting; it stays in the membership (the simulated
    scheduler never drops it), so the others' heard counters for it fall to 0. ``drop_prob`` is the chance that a node misses
    one round broadcast from another. ``noisy`` maps a node name to (from_s, to_s): in that time every link the
    node is on, as receiver or transmitter, has its score multiplied by ``NOISY_NODE_GAIN`` (a front-end fault or a
    gain step on that one board). ``glitch_rate_hz`` is the rate, per link, of independent glitches: each raises one
    link's score by ``GLITCH_GAIN`` for ``GLITCH_S`` (onsets are a Poisson process; the room is otherwise unchanged).
    Seeded: the same arguments give the same bytes, and noise or glitches do not change the rest of the stream.
    ``realtime`` paces the stream to the wall clock and stamps it with ``time.time()``. Every broadcast is signed with
    ``kit_key`` (default the published test key, which the host also defaults to).

    Joining nodes and colliding schedules: ``join_after`` maps a node name to the seconds before which it is off
    (silent, not a member). It then asks to join in the join slot of the next even round (a join request), the
    scheduler grants it in the next odd round (a join grant: the new membership and schedule version), and it
    broadcasts from the round after. ``split`` names nodes that run a second schedule of their own (its own schedule
    version and membership; the two groups do not hear each other, the gateway hears both) until ``merge_after``
    seconds: then the group whose lowest board serial is higher (the losing scheduler ID) leaves its schedule for one
    round and is granted into the other's, under a new schedule version. Node indices are the nodes' positions in
    ``room.transceivers`` throughout; only membership and schedule version change.

    Raw mode: ``raw`` maps a receiving node's name to the transmitters whose links it streams from the start
    (at most 2). ``commands`` is a deque of frames a gateway sent (``SimulatedRig.send_frame``); before each round
    the nodes take the raw mode requests among them as a real node does: the tag must verify under ``kit_key``,
    the round number must be fresh for its sender, and the target round within RAW_REQUEST_MAX_AGE_ROUNDS of the
    target's own. A node in raw mode flags those links' reports and sends a round broadcast with raw CSI carrying
    synthetic I/Q (seeded apart, so the rest of the stream is the same with raw mode or without).
    """
    nodes = list(room.transceivers)
    n = len(nodes)
    if not 2 <= n <= MAX_NODES:
        raise ValueError(f"the simulated rig needs 2 to {MAX_NODES} transceivers, the room has {n}")
    dead_after, noisy = dict(dead_after or {}), dict(noisy or {})
    join_after, split = dict(join_after or {}), set(split)
    for arg, names in (("dead_after", dead_after), ("noisy", noisy), ("join_after", join_after), ("split", split)):
        unknown = sorted(set(names) - {node.name for node in nodes})
        if unknown:
            raise ValueError(f"{arg} names no transceiver: {', '.join(unknown)}")
    rng = np.random.default_rng(seed)
    glitch_rng = np.random.default_rng([seed, 1])   # its own stream: glitches leave the other draws unchanged
    dead_round = [int(round(dead_after[node.name] * round_hz)) if node.name in dead_after else None
                  for node in nodes]
    pairs = [(r, t) for r in range(n) for t in range(n) if r != t]          # (receiving, transmitting) indices
    a = np.array([(nodes[r].x, nodes[r].y) for r, _ in pairs])
    b = np.array([(nodes[t].x, nodes[t].y) for _, t in pairs])
    quiet = rng.uniform(*ROUND_QUIET_SCORE, len(pairs))
    rssi = np.round(-40 - 20 * np.log10(np.maximum(np.linalg.norm(b - a, axis=1), 0.1))).astype(int)
    heard = np.zeros((len(pairs), HEARD_WINDOW_ROUNDS), dtype=bool)  # ring of the last 15 rounds per link
    since = np.full(len(pairs), SINCE_MAX)
    period, slot_s = 1.0 / round_hz, 1.0 / (round_hz * (n + 1))
    warming_rounds = int(round(SIM_WARMING_S * round_hz))
    index = {node.name: i for i, node in enumerate(nodes)}
    on_node = {name: np.array([index[name] in pair for pair in pairs]) for name in noisy}
    glitch_until = np.full(len(pairs), -1.0)
    last_round = None if duration_s is None else int(round(duration_s * round_hz))
    index_of = {node.name: i for i, node in enumerate(nodes)}
    raw_sets = [set() for _ in nodes]
    for name, transmitters in (raw or {}).items():
        raw_sets[index_of[name]] = {index_of[t] for t in transmitters}
        if len(raw_sets[index_of[name]]) > RAW_MAX_LINKS or index_of[name] in raw_sets[index_of[name]]:
            raise ValueError(f"raw mode on {name}: at most {RAW_MAX_LINKS} other transceivers")
    raw_rng = np.random.default_rng([seed, 17])
    fresh = Freshness()
    plan = _SchedulePlan(nodes, join_after, split, merge_after, round_hz)
    receivers, transmitters = np.array([r for r, _ in pairs]), np.array([t for _, t in pairs])
    start = time.time()
    for round_no in itertools.count():
        if last_round is not None and round_no >= last_round:
            return
        t = round_no * period
        while commands:
            _take_request(commands.popleft(), nodes, raw_sets, round_no, t0 + t, kit_key, fresh)
        sends, group, versions, memberships = plan.round(round_no)
        alive = np.array([(dead_round[i] is None or round_no < dead_round[i]) and sends[i] for i in range(n)])
        got = (alive[transmitters] & (group[receivers] == group[transmitters])
               & (rng.random(len(pairs)) >= drop_prob))
        heard[:, round_no % HEARD_WINDOW_ROUNDS] = got
        since = np.where(got, 0, np.minimum(since + 1, SINCE_MAX))
        response = _round_response(person, t - quiet_first_s, a, b)
        scores = quiet * rng.lognormal(0.0, ROUND_NOISE_SIGMA, len(pairs)) * (1.0 + ROUND_MOTION_GAIN * response)
        for name, (start_s, stop_s) in noisy.items():
            if start_s <= t < stop_s:
                scores = np.where(on_node[name], scores * NOISY_NODE_GAIN, scores)
        if glitch_rate_hz > 0:
            onset = glitch_rng.random(len(pairs)) < glitch_rate_hz * period
            glitch_until = np.where(onset, t + GLITCH_S, glitch_until)
            scores = np.where(t < glitch_until, scores * GLITCH_GAIN, scores)
        counts = heard.sum(axis=1)
        for slot, node in enumerate(nodes):
            if not alive[slot]:
                continue
            reports, blocks = [], []
            membership = memberships[group[slot]]
            for k in range(slot * (n - 1), (slot + 1) * (n - 1)):   # pairs are grouped by receiving node
                _, tx = pairs[k]
                if not membership >> tx & 1:
                    continue
                flag = FLAG_RAW_MODE if tx in raw_sets[slot] else 0
                if round_no < warming_rounds:
                    reports.append(LinkReport(tx, 0, int(rssi[k]), int(counts[k]), int(since[k]),
                                              FLAG_WARMING | flag))
                elif counts[k] == 0:
                    reports.append(LinkReport(tx, 0, None, 0, int(since[k]), flag))
                else:
                    reports.append(LinkReport(tx, encode_score(float(scores[k])), int(rssi[k]), int(counts[k]),
                                              int(since[k]), flag))
                if flag and got[k]:
                    blocks.append(RawCsi(tx, round_no % 65536, int(rssi[k]), _raw_iq(raw_rng, float(response[k]))))
            data = sign_broadcast(encode_broadcast(RoundBroadcast(
                FORMAT_VERSION, FRAME_ROUND_RAW if blocks else FRAME_ROUND, round_no % 65536, slot, n + 1,
                versions[group[slot]], slot, membership, node.serial, bytes(8), tuple(reports), tuple(blocks))),
                kit_key)
            host_ts = t0 + t + slot * slot_s + float(rng.uniform(0.0, GATEWAY_LATENCY_S))
            if realtime:
                time.sleep(max(0.0, start + (host_ts - t0) - time.time()))
                host_ts = time.time()
            yield host_ts, data
        for k, (frame_type, joiner, version, joined) in enumerate(plan.join_frames(round_no)):
            data = sign_broadcast(encode_broadcast(RoundBroadcast(
                FORMAT_VERSION, frame_type, round_no % 65536, n, n + 1, version, joiner, joined,
                nodes[joiner].serial, bytes(8), ())), kit_key)
            host_ts = t0 + t + n * slot_s + k * slot_s / 8 + float(rng.uniform(0.0, GATEWAY_LATENCY_S))
            if realtime:
                time.sleep(max(0.0, start + (host_ts - t0) - time.time()))
                host_ts = time.time()
            yield host_ts, data


class _SchedulePlan:
    """Who is in which schedule in each round of the simulated rig: joins (``join_after``) and a split rig that
    merges (``split``, ``merge_after``). Group 0 is the schedule of the lowest board serial, group 1 the other."""

    def __init__(self, nodes: Sequence, join_after: Mapping[str, float], split: set, merge_after: Optional[float],
                 round_hz: float) -> None:
        n = len(nodes)
        index = {node.name: i for i, node in enumerate(nodes)}
        self._n = n
        lowest = min(range(n), key=lambda i: board_serial(nodes[i].serial))
        in_split = np.array([node.name in split for node in nodes])
        if split and (in_split.all() or merge_after is None):
            raise ValueError("split needs merge_after and must leave some nodes in the other schedule")
        # group 1 is the schedule that loses the merge: the one without the lowest serial
        self._group = (in_split != in_split[lowest]).astype(int) if split else np.zeros(n, dtype=int)
        self._merge = None if merge_after is None or not split else int(np.ceil(merge_after * round_hz))
        self._join = {}      # node index -> round of its join request (even); granted next round, sends after
        for name, after in join_after.items():
            if self._group[index[name]] == 1:
                raise ValueError(f"{name} cannot both join and be in the schedule that merges")
            first = int(np.ceil(after * round_hz))
            self._join[index[name]] = first + first % 2
        self._grants = sorted((r + 1, i) for i, r in self._join.items())   # (round, node) of each join grant

    def _versions(self, round_no: int) -> tuple[int, int]:
        """Schedule versions of the two groups for broadcasts in ``round_no``: one more for every grant before it."""
        joins = sum(1 for r, _ in self._grants if r < round_no)
        if self._merge is not None and round_no > self._merge:
            return 129 + joins, 129 + joins
        return 1 + joins, 128

    def round(self, round_no: int):
        """(sends, group, versions, memberships) for the round broadcasts of ``round_no``."""
        merged = self._merge is not None and round_no > self._merge
        group = np.zeros(self._n, dtype=int) if merged else self._group
        member = np.array([i not in self._join or round_no > self._join[i] + 1 for i in range(self._n)])
        sends = member.copy()
        if self._merge is not None and round_no == self._merge:
            sends &= self._group == 0               # the losing schedule leaves its own to be granted in
        memberships = [sum(1 << i for i in range(self._n) if member[i] and group[i] == g) for g in (0, 1)]
        return sends, group, self._versions(round_no), memberships

    def join_frames(self, round_no: int) -> list[tuple[int, int, int, int]]:
        """(frame type, node index, schedule version, membership) of the join frames in ``round_no``'s join slot."""
        out = []
        _, _, versions, memberships = self.round(round_no)
        after = self.round(round_no + 1)
        for i, r in self._join.items():
            if round_no == r:
                out.append((FRAME_JOIN_REQUEST, i, versions[0], memberships[0]))
            elif round_no == r + 1:
                out.append((FRAME_JOIN_GRANT, i, after[2][0], after[3][0]))
        if self._merge is not None and round_no == self._merge:
            out.extend((FRAME_JOIN_GRANT, i, after[2][0], after[3][0]) for i in range(self._n) if self._group[i])
        return out


def _take_request(frame: bytes, nodes: Sequence, raw_sets: list, round_no: int, host_ts: float, kit_key: bytes,
                  fresh: Freshness) -> None:
    """A frame the simulated gateway sent: a raw mode request a node takes as the node image does, or nothing."""
    try:
        request = decode_broadcast(frame)
    except BroadcastError:
        return
    if request.frame_type != FRAME_RAW_REQUEST or not tag_ok(frame, kit_key):
        return
    if fresh.check(request.node_id, request.round, host_ts) == STALE:
        return
    serials = {board_serial(node.serial): i for i, node in enumerate(nodes)}
    target = serials.get(board_serial(request.request.target))
    if target is None or (round_no - request.request.target_round) % 65536 > RAW_REQUEST_MAX_AGE_ROUNDS:
        return
    raw_sets[target] = {serials[board_serial(s)] for s in request.request.transmitters
                        if board_serial(s) in serials and serials[board_serial(s)] != target}


def _raw_iq(rng: np.random.Generator, response: float) -> bytes:
    """Synthetic int8 I/Q for one round broadcast: a fixed-ish amplitude per subcarrier, random phase, nulls 0; a
    person near the link widens the amplitude's spread."""
    amp = 40.0 * (1.0 + (0.1 + response) * rng.standard_normal(SIM_RAW_SUBCARRIERS))
    phase = rng.uniform(0.0, 2 * np.pi, SIM_RAW_SUBCARRIERS)
    amp[list(NULL_SUBCARRIERS)] = 0.0
    iq = np.empty(2 * SIM_RAW_SUBCARRIERS)
    iq[0::2], iq[1::2] = amp * np.sin(phase), amp * np.cos(phase)   # imaginary, then real (ESP-IDF order)
    return np.clip(np.round(iq), -127, 127).astype(np.int8).tobytes()


class SimulatedRig:
    """The simulated rig as one gateway hears it (iterate it: ``simulate_rounds``), with that gateway's sending
    side: ``send_frame`` queues a frame for the nodes, which take raw mode requests before their next round. Use
    it as a source and as the transmitter of ``DashboardServer``. Thread-safe between the reader and the sender."""

    def __init__(self, room: Room, person: Union[Person, Sequence[Person], None] = None, *,
                 gateway_serial: str = SIM_GATEWAY_ID, **kwargs) -> None:
        self.gateway_serial: Optional[str] = gateway_serial
        self.sent: list[bytes] = []
        self._commands: deque = deque()
        self._room, self._person, self._kwargs = room, person, kwargs

    def send_frame(self, frame: bytes) -> None:
        self.sent.append(bytes(frame))
        self._commands.append(bytes(frame))

    def __iter__(self) -> Iterator[tuple[float, bytes]]:
        return simulate_rounds(self._room, self._person, commands=self._commands, **self._kwargs)


def _round_response(person: Union[Person, Sequence[Person], None], t: float, a: np.ndarray,
                    b: np.ndarray) -> np.ndarray:
    """Per link (segment a-b), the response in 0..1 at person time ``t``; zeros before anyone arrives. Several
    people combine as independent causes: 1 - (1 - r1)(1 - r2)..."""
    if person is None or t < 0:
        return np.zeros(len(a))
    if isinstance(person, Sequence):
        quiet = np.ones(len(a))
        for one in person:
            quiet *= 1.0 - _round_response(one, t, a, b)
        return 1.0 - quiet
    p = person.position(np.array([t]))[0]
    ab = b - a
    u = np.clip(np.einsum("ij,ij->i", p - a, ab) / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-12), 0.0, 1.0)
    d = np.linalg.norm(p - (a + u[:, None] * ab), axis=1)
    return movement(person, np.array([t]))[0] * np.exp(-d ** 2 / (2 * FALLOFF_SIGMA_M ** 2))


def simulate_gateways(
    room: Room,
    person: Union[Person, Sequence[Person], None] = None,
    *,
    hears: Mapping[str, Iterable[str]],
    loss: Optional[Mapping[str, float]] = None,
    seed: int = 0,
    **kwargs,
) -> dict[str, Iterator[tuple[float, bytes]]]:
    """One (host time, round broadcast) stream per gateway of the simulated rig, for ``DashboardServer``'s sources.

    ``hears`` maps a gateway name to the names of the nodes it hears; gateways may overlap, and a node heard by
    two gateways reaches the host twice, as on a real rig. Every gateway sees the same rig (``simulate_rounds``
    with these ``kwargs`` and ``seed``, so the copies are byte-identical) and adds its own latency, up to
    ``GATEWAY_LATENCY_S``. ``loss`` maps a gateway name to the chance that it misses one round broadcast it would
    hear (default 0).
    """
    names = {node.name for node in room.transceivers}
    for gateway, heard in hears.items():
        unknown = sorted(set(heard) - names)
        if unknown:
            raise ValueError(f"gateway {gateway} hears no such transceiver: {', '.join(unknown)}")
    serials = room.transceiver_names()
    return {gateway: _gateway_view(simulate_rounds(room, person, seed=seed, **kwargs), set(heard), serials,
                                   (loss or {}).get(gateway, 0.0), np.random.default_rng([seed, k]))
            for k, (gateway, heard) in enumerate(hears.items())}


def _gateway_view(stream: Iterator[tuple[float, bytes]], heard: set[str], serials: Mapping[str, str], loss: float,
                  rng: np.random.Generator) -> Iterator[tuple[float, bytes]]:
    for host_ts, data in stream:
        if serials.get(board_serial(decode_broadcast(data).node_id)) in heard and rng.random() >= loss:
            yield host_ts + float(rng.uniform(0.0, GATEWAY_LATENCY_S)), data
