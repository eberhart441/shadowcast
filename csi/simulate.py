"""Fake CSI stream for hardware-free development.

Model: static per-subcarrier baseline + Gaussian noise. While a person is
``still`` a ~0.3 Hz sinusoid (breathing) is added, with per-subcarrier gain and
phase so some subcarriers respond more than others. During ``motion`` noise is
amplified and slow random-walk drift is added. ``empty`` is baseline + noise.
"""
from __future__ import annotations

import heapq
import time
from dataclasses import dataclass
from operator import itemgetter
from typing import Callable, Iterator, Optional, Sequence

import numpy as np

from .parser import DEFAULT_HEADER, CSIPacket, CSIParser, parse_stream

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
        amp += rng.normal(0, 3.0, (n, k)) + 4.0 * np.cumsum(rng.normal(0, 0.15, (n, 1)), axis=0)
    elif state != "empty":
        raise ValueError(f"unknown state {state!r}")
    return np.clip(amp, 0.0, None)


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


def simulate_link_streams(
    senders: Sequence[str] = SENDER_MACS,
    receivers: Sequence[str] = DEFAULT_RECEIVERS,
    scenario: Sequence[Segment] = DEFAULT_SCENARIO,
    rate_hz: float = 100.0,
    seed: int = 0,
    realtime: bool = False,
    loop: bool = False,
    t0: float = 1_700_000_000.0,
) -> dict[str, Iterator[tuple[float, str]]]:
    """One (host_ts, line) stream per receiver, each carrying every sender's packets.

    Every (receiver, sender) link has its own seed, so its baseline and noise are independent.
    The scenario applies to all links at once. ``realtime`` paces each stream to the wall clock
    and stamps lines with ``time.time()``; otherwise host_ts is synthetic (t0 + index / rate).
    """
    start = time.time()

    def paced(lines: Iterator[tuple[float, str]]) -> Iterator[tuple[float, str]]:
        for host_ts, line in lines:
            if realtime:
                time.sleep(max(0.0, start + (host_ts - t0) - time.time()))
                host_ts = time.time()
            yield host_ts, line

    streams: dict[str, Iterator[tuple[float, str]]] = {}
    for r_idx, name in enumerate(receivers):
        gens = [_sender_lines(mac, int(np.random.SeedSequence([seed, r_idx, s_idx]).generate_state(1)[0]),
                              scenario, rate_hz, loop, t0)
                for s_idx, mac in enumerate(senders)]

        def receiver_stream(gens=gens) -> Iterator[tuple[float, str]]:
            yield (time.time() if realtime else t0), DEFAULT_HEADER
            yield from paced(heapq.merge(*gens, key=itemgetter(0)))

        streams[name] = receiver_stream()
    return streams


def simulate_links(
    senders: Sequence[str] = SENDER_MACS,
    receivers: Sequence[str] = DEFAULT_RECEIVERS,
    scenario: Sequence[Segment] = DEFAULT_SCENARIO,
    rate_hz: float = 100.0,
    seed: int = 0,
    realtime: bool = False,
    loop: bool = False,
    t0: float = 1_700_000_000.0,
) -> Iterator[tuple[str, CSIPacket]]:
    """(receiver, packet) for every link, merged in host-time order.

    Goes through the text seam: each receiver's lines are parsed by its own CSIParser, so the
    packet's mac comes from the line exactly as it would from hardware.
    """
    streams = simulate_link_streams(senders, receivers, scenario, rate_hz, seed, realtime, loop, t0)
    per_receiver = [_tagged(name, lines) for name, lines in streams.items()]
    yield from heapq.merge(*per_receiver, key=lambda item: item[1].host_ts)


def _tagged(name: str, lines: Iterator[tuple[float, str]]) -> Iterator[tuple[str, CSIPacket]]:
    for pkt in parse_stream(lines, CSIParser()):
        yield name, pkt
