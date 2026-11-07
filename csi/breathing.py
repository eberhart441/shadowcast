"""Breathing rate from a window of amplitude vectors. No I/O, no state.

    estimate_breathing(amplitude, timestamps_s) -> BreathingEstimate

``amplitude`` is (n_packets, n_subcarriers); ``timestamps_s`` is (n_packets,)
device time in seconds. The window is resampled onto a uniform grid at the rate
implied by the timestamps, so dropped packets leave gaps that interpolation
fills instead of stretching the frequency axis.

Pipeline:
1. linear detrend, Hann taper, keep 0.1-0.5 Hz (FFT band selection per subcarrier)
2. keep the TOP_K subcarriers with the most in-band power, standardise them
3. SVD: the first principal component is the breathing candidate
4. Hann-windowed, zero-padded FFT of that component, peak in band, parabolic refine

Confidence is the share of in-band variance carried by the first two components
across the selected subcarriers. Breathing moves many subcarriers coherently, so
it concentrates there; independent noise spreads out. Below ``MIN_CONFIDENCE``
no rate is declared (``bpm`` is None).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

WINDOW_S = 15.0
BAND_HZ = (0.1, 0.5)          # 6-30 breaths per minute
TOP_K = 10                    # subcarriers kept before the PCA step
PAD_FACTOR = 8                # zero-padding for finer peak localisation
MIN_CONFIDENCE = 0.85         # below this, no breathing is declared
ENOUGH_FRACTION = 0.97        # tolerate one packet interval short of WINDOW_S
MIN_PACKETS = 8


@dataclass(frozen=True)
class BreathingEstimate:
    bpm: Optional[float]      # breaths per minute, None if not declared
    confidence: float         # 0..1, see module docstring
    enough_data: bool         # window spans (about) WINDOW_S


NOT_ENOUGH = BreathingEstimate(bpm=None, confidence=0.0, enough_data=False)


def _uniform_grid(timestamps_s: np.ndarray) -> tuple[np.ndarray, float]:
    """Grid spanning the window at the median-interval sample rate."""
    diffs = np.diff(timestamps_s)
    diffs = diffs[diffs > 0]
    fs = 1.0 / float(np.median(diffs))
    span = timestamps_s[-1] - timestamps_s[0]
    n_out = int(np.floor(span * fs)) + 1
    return timestamps_s[0] + np.arange(n_out) / fs, fs


def _resample(amplitude: np.ndarray, timestamps_s: np.ndarray, grid: np.ndarray) -> np.ndarray:
    return np.stack([np.interp(grid, timestamps_s, col) for col in amplitude.T], axis=1)


def _band_limit(x: np.ndarray, fs: float) -> np.ndarray:
    """Detrend, taper and keep only BAND_HZ per column (frequency-domain bandpass)."""
    n = x.shape[0]
    x = x - x.mean(axis=0)
    t = np.arange(n)
    slope = np.polyfit(t, x, 1)  # linear detrend per column
    x = x - (slope[0][None, :] * t[:, None] + slope[1][None, :])
    spec = np.fft.rfft(x * np.hanning(n)[:, None], axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    spec[(freqs < BAND_HZ[0]) | (freqs > BAND_HZ[1])] = 0
    return np.fft.irfft(spec, n=n, axis=0)


def _select_top_variance(x: np.ndarray) -> np.ndarray:
    """Standardised copies of the TOP_K highest-variance columns of ``x``."""
    top = np.argsort(x.var(axis=0))[::-1][:TOP_K]
    cols = x[:, top]
    std = cols.std(axis=0)
    return cols / np.where(std > 0, std, 1.0)


def _principal_components(x: np.ndarray) -> tuple[np.ndarray, float]:
    """(first principal component time series, share of variance in top two)."""
    u, s, _ = np.linalg.svd(x - x.mean(axis=0), full_matrices=False)
    energy = s ** 2
    total = float(energy.sum())
    if total <= 0 or energy.size < 2:
        return np.zeros(x.shape[0]), 0.0
    return u[:, 0] * s[0], float(energy[:2].sum() / total)


def _peak_frequency(x: np.ndarray, fs: float) -> float:
    """Refined frequency (Hz) of the strongest in-band bin of ``x``."""
    n = x.size
    nfft = PAD_FACTOR * n
    power = np.abs(np.fft.rfft(x * np.hanning(n), n=nfft)) ** 2
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    band = np.flatnonzero((freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1]))
    k = band[np.argmax(power[band])]
    delta = 0.0
    if 0 < k < len(power) - 1:
        a, b, c = power[k - 1], power[k], power[k + 1]
        denom = a - 2 * b + c
        if denom != 0:
            delta = 0.5 * (a - c) / denom
    return float(freqs[k] + delta * (freqs[1] - freqs[0]))


def estimate_breathing(amplitude: np.ndarray, timestamps_s: np.ndarray) -> BreathingEstimate:
    """Breaths per minute and confidence for one window of CSI amplitudes."""
    timestamps_s = np.asarray(timestamps_s, dtype=np.float64)
    amplitude = np.asarray(amplitude, dtype=np.float64)
    if timestamps_s.size < MIN_PACKETS:
        return NOT_ENOUGH
    span = timestamps_s[-1] - timestamps_s[0]
    if span < ENOUGH_FRACTION * WINDOW_S:
        return NOT_ENOUGH
    grid, fs = _uniform_grid(timestamps_s)
    x = _band_limit(_resample(amplitude, timestamps_s, grid), fs)
    pc, confidence = _principal_components(_select_top_variance(x))
    freq = _peak_frequency(pc, fs)
    bpm = 60.0 * freq if confidence >= MIN_CONFIDENCE else None
    return BreathingEstimate(bpm=bpm, confidence=confidence, enough_data=True)
