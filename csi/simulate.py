"""Fake CSI stream for hardware-free development.

Model: static per-subcarrier baseline + Gaussian noise. While a person is
``still`` a ~0.3 Hz sinusoid (breathing) is added, with per-subcarrier gain and
phase so some subcarriers respond more than others. During ``motion`` noise is
amplified and slow random-walk drift is added. ``empty`` is baseline + noise.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Sequence

import numpy as np

from .parser import DEFAULT_HEADER, CSIPacket

N_SUBCARRIERS = 64
NULL_SUBCARRIERS = tuple(range(0, 6)) + (32,) + tuple(range(59, 64))  # 12 nulls (LLTF-like)
BREATH_HZ = 0.3


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
                ), seg.state
            i += n
        if not loop:
            return


def _keep_mask() -> np.ndarray:
    mask = np.ones(N_SUBCARRIERS, dtype=bool)
    mask[list(NULL_SUBCARRIERS)] = False
    return mask


def packet_to_line(pkt: CSIPacket, idx: int = 0) -> str:
    """Render a packet as an esp-csi ``CSI_DATA`` line (nulls re-inserted as 0)."""
    full = np.zeros(N_SUBCARRIERS)
    full[_keep_mask()] = pkt.amplitude
    # split amplitude across I/Q at a fixed angle, rounded to int8
    iq = np.zeros((N_SUBCARRIERS, 2), dtype=int)
    iq[:, 0] = np.clip(np.round(full * 0.6), -128, 127)
    iq[:, 1] = np.clip(np.round(full * 0.8), -128, 127)
    data = ",".join(map(str, iq.reshape(-1)))
    cols = DEFAULT_HEADER.split(",")
    vals = {"type": "CSI_DATA", "id": idx, "mac": "1a:00:00:00:00:00", "rssi": pkt.rssi,
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
