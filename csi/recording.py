"""Labeled recording: write CSI packets to a session folder, and load them back.

Session layout::

    <session>/session.json                      metadata (placement, channel, rate, layout, files)
    <session>/<label>_<YYYYmmddTHHMMSSZ>.csv    one file per label per recording

Each CSV is a flat table with columns ``host_ts, device_ts, rssi, channel`` and
one ``amp_<k>`` column per kept subcarrier. A file holds exactly one label; the
label is encoded in the file name. Timestamps in names/metadata are UTC.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, TextIO

import numpy as np
import pandas as pd

from .health import LinkHealth
from .parser import CSIPacket, CSIParser, Stream

LABELS = ("empty", "walking", "still_standing", "still_sitting_or_lying")
META_NAME = "session.json"
BASE_COLUMNS = ("host_ts", "device_ts", "rssi", "channel", "mac")
_NAME_RE = re.compile(r"^(?P<label>[a-z_]+)_\d{8}T\d{6}Z(-\d+)?$")
# Session metadata that an earlier run wrote and a resumed run keeps (see _Session._resume_existing).
_KEPT_FROM_EARLIER_RUN = ("date", "channel", "subcarrier_layout", "send_rate_hz", "files", "drops_estimated")


def validate_label(label: str) -> str:
    if label not in LABELS:
        raise ValueError(f"invalid label {label!r}; choose one of: {', '.join(LABELS)}")
    return label


def amp_columns(n: int) -> list[str]:
    return [f"amp_{i}" for i in range(n)]


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass
class RecordingReport:
    packets: int = 0            # packets written to files
    drops: int = 0              # estimated from device-timestamp gaps (link health)
    files: list[str] = field(default_factory=list)
    interrupted: bool = False

    def summary(self) -> str:
        return (f"recorded {self.packets} packets in {len(self.files)} file(s); "
                f"estimated drops: {self.drops}" + (" (interrupted)" if self.interrupted else ""))


class _LabelFile:
    """One open CSV holding a single label."""

    def __init__(self, session: Path, label: str, first_ts: float, n_amp: int) -> None:
        stamp = _utc(first_ts).strftime("%Y%m%dT%H%M%SZ")
        path, k = session / f"{label}_{stamp}.csv", 1
        while path.exists():
            k += 1
            path = session / f"{label}_{stamp}-{k}.csv"
        self.path, self.label, self.packets = path, label, 0
        self._fh: TextIO = open(path, "w", newline="")
        self._w = csv.writer(self._fh)
        self._w.writerow([*BASE_COLUMNS, *amp_columns(n_amp)])
        self._fh.flush()

    def write(self, pkt: CSIPacket) -> None:
        self._w.writerow([f"{pkt.host_ts:.6f}", pkt.device_ts, pkt.rssi, pkt.channel, pkt.mac,
                          *(f"{a:.3f}" for a in pkt.amplitude)])
        self.packets += 1

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class _Session:
    """Owns the session folder: current file, metadata, flush cadence."""

    def __init__(self, folder: Path, send_rate_hz: Optional[float], notes: str, countdown_s: float,
                 flush_interval_s: float, rate_now: Callable[[], Optional[float]]) -> None:
        self.folder, self.flush_interval_s = folder, flush_interval_s
        self._rate_now = rate_now  # configured or estimated send rate, as of now
        self.meta: dict = {"session": folder.name, "placement_notes": notes,
                           "send_rate_hz": send_rate_hz, "countdown_s": countdown_s,
                           "files": []}
        self.current: Optional[_LabelFile] = None
        self._last_flush = 0.0
        self._resume_existing(notes)

    def _resume_existing(self, notes: str) -> None:
        """Re-opening a session folder appends to it instead of replacing it.

        Kept from the earlier run: the files, accumulated drops, the session date, channel and
        subcarrier layout (every file must share one layout), and the session send rate. The new
        run's session/countdown fields apply, and per-file settings are recorded in ``files``.
        """
        path = self.folder / META_NAME
        if not path.exists():
            return
        prev = json.loads(path.read_text())
        prev_notes = prev.get("placement_notes", "")
        if notes and notes not in prev_notes:
            prev["placement_notes"] = f"{prev_notes}; {notes}" if prev_notes else notes
        for key in _KEPT_FROM_EARLIER_RUN:
            if prev.get(key) is not None:
                self.meta[key] = prev[key]
        self.meta["placement_notes"] = prev.get("placement_notes", self.meta["placement_notes"])

    def open_file(self, label: str, pkt: CSIPacket, kept: Optional[list[int]]) -> None:
        self.close_file()
        n = len(pkt.amplitude)
        layout = self.meta.get("subcarrier_layout")
        if layout is not None and layout["n_subcarriers"] != n:
            raise ValueError(
                f"session {self.folder.name!r} was recorded with {layout['n_subcarriers']} subcarriers "
                f"per packet but this run produced {n}; the widths cannot be loaded together. "
                f"Record into a new session folder.")
        self.folder.mkdir(parents=True, exist_ok=True)
        if layout is None:
            self.meta.update(date=_utc(pkt.host_ts).isoformat(), channel=pkt.channel,
                             subcarrier_layout={"n_subcarriers": n, "kept_subcarriers": kept,
                                                "amplitude_columns": amp_columns(n)})
        self.current = _LabelFile(self.folder, label, pkt.host_ts, n)
        self.meta["files"].append({
            "name": self.current.path.name, "label": label, "packets": 0,
            "channel": pkt.channel, "send_rate_hz": self._rate_now(),
            "subcarrier_layout": {"n_subcarriers": n, "kept_subcarriers": kept},
        })
        self._last_flush = pkt.host_ts
        self.write_meta()

    def write(self, pkt: CSIPacket) -> None:
        assert self.current is not None
        self.current.write(pkt)
        if pkt.host_ts - self._last_flush >= self.flush_interval_s:
            self.current.flush()
            self._last_flush = pkt.host_ts

    def close_file(self) -> None:
        if self.current is not None:
            self.current.close()
            entry = self.meta["files"][-1]
            entry["packets"] = self.current.packets
            entry["send_rate_hz"] = self._rate_now()
            self.current = None

    def write_meta(self) -> None:
        if self.folder.exists():
            (self.folder / META_NAME).write_text(json.dumps(self.meta, indent=2))

    def finish(self, drops: int) -> None:
        self.close_file()
        self.meta["drops_estimated"] = self.meta.get("drops_estimated", 0) + drops
        if self.meta.get("send_rate_hz") is None:
            self.meta["send_rate_hz"] = self._rate_now()  # estimated from device timestamps
        self.write_meta()


class _Countdown:
    """Discards packets until ``seconds`` of stream time have passed, announcing ticks."""

    def __init__(self, seconds: float, on_tick: Optional[Callable[[int], None]]) -> None:
        self.seconds, self.on_tick = seconds, on_tick
        self._until: Optional[float] = None
        self._announced: Optional[int] = None

    def start(self, now: float) -> None:
        self._until, self._announced = now + self.seconds, None

    def active(self, now: float) -> bool:
        if self._until is None:
            return False
        remaining = self._until - now
        if remaining <= 1e-9:
            self._until = None
            return False
        tick = int(np.ceil(remaining - 1e-9))
        if tick != self._announced and self.on_tick:
            self.on_tick(tick)
        self._announced = tick
        return True


def record_session(
    stream: Stream,
    session_dir: Path | str,
    label: str,
    *,
    send_rate_hz: Optional[float] = None,
    countdown_s: float = 3.0,
    placement_notes: str = "",
    duration_s: Optional[float] = None,
    poll_label: Optional[Callable[[], Optional[str]]] = None,
    on_countdown: Optional[Callable[[int], None]] = None,
    flush_interval_s: float = 1.0,
    parser: Optional[CSIParser] = None,
) -> RecordingReport:
    """Record a (host_ts, line) stream into ``session_dir``.

    ``send_rate_hz`` is the sender's nominal rate, used for drop estimation. None (the
    default, right for real hardware) estimates it from device timestamps; the rate used
    is written to session.json and to each file's entry.

    ``poll_label`` is called once per packet and returns a new label to switch
    to (closing the current file and starting a countdown) or None. Time (the
    countdown, ``duration_s``, flushing) is measured on stream host timestamps.
    Interrupting (Ctrl+C) closes and keeps all data.
    """
    label = validate_label(label)
    parser = parser or CSIParser()
    health = LinkHealth(send_rate_hz=send_rate_hz)
    session = _Session(Path(session_dir), send_rate_hz, placement_notes, countdown_s,
                       flush_interval_s, rate_now=health.rate_hz)
    countdown = _Countdown(countdown_s, on_countdown)
    report = RecordingReport()
    first_ts: Optional[float] = None
    try:
        for pkt in health.consume(stream, parser):
            if first_ts is None:
                first_ts = pkt.host_ts
                countdown.start(pkt.host_ts)
            if duration_s is not None and pkt.host_ts - first_ts >= duration_s:
                break
            label = _apply_switch(poll_label, label, session, countdown, pkt)
            if countdown.active(pkt.host_ts):
                continue
            if session.current is None:
                session.open_file(label, pkt, parser.kept_subcarriers)
            session.write(pkt)
            report.packets += 1
    except KeyboardInterrupt:
        report.interrupted = True
    finally:
        session.finish(health.drops)
    report.drops = health.drops
    report.files = [f["name"] for f in session.meta["files"]]
    return report


def _apply_switch(poll: Optional[Callable[[], Optional[str]]], label: str, session: _Session,
                  countdown: _Countdown, pkt: CSIPacket) -> str:
    new = poll() if poll else None
    if new is None or new == label:
        return label
    new = validate_label(new)
    session.close_file()
    countdown.start(pkt.host_ts)
    return new


# ---------------------------------------------------------------- loading

@dataclass
class Recording:
    """One labeled file loaded back as a table."""

    path: Path
    label: str
    table: pd.DataFrame

    @property
    def host_ts(self) -> np.ndarray:
        return self.table["host_ts"].to_numpy()

    @property
    def device_ts(self) -> np.ndarray:
        return self.table["device_ts"].to_numpy()

    @property
    def amplitude(self) -> np.ndarray:
        """float32 matrix (packets, kept subcarriers)."""
        cols = [c for c in self.table.columns if c.startswith("amp_")]
        return self.table[cols].to_numpy(dtype=np.float32)

    def packets(self) -> list[CSIPacket]:
        """Packets with their recorded sender MAC. Files from before the mac column load with ''."""
        t, amp = self.table, self.amplitude
        macs = t["mac"].fillna("").astype(str) if "mac" in t.columns else [""] * len(t)
        return [CSIPacket(host_ts=float(h), device_ts=int(d), rssi=int(r), channel=int(c),
                          amplitude=amp[i], mac=str(m))
                for i, (h, d, r, c, m) in enumerate(zip(t["host_ts"], t["device_ts"],
                                                        t["rssi"], t["channel"], macs))]


@dataclass
class Session:
    path: Path
    meta: dict
    recordings: list[Recording]


def label_from_filename(path: Path | str) -> str:
    m = _NAME_RE.match(Path(path).stem)
    if not m:
        raise ValueError(f"not a recording file name: {Path(path).name}")
    return validate_label(m["label"])


def is_recording_name(path: Path | str) -> bool:
    """True if ``path`` is named ``<label>_<YYYYmmddTHHMMSSZ>.csv`` with a known label."""
    try:
        label_from_filename(path)
    except ValueError:
        return False
    return Path(path).suffix == ".csv"


def load_recording(path: Path | str) -> Recording:
    path = Path(path)
    return Recording(path=path, label=label_from_filename(path), table=pd.read_csv(path))


def load_session(folder: Path | str) -> Session:
    """Load session metadata and every recording (ordered by start time).

    CSV files whose names are not ``<label>_<timestamp>.csv`` (notes, exports) are skipped.
    """
    folder = Path(folder)
    meta = json.loads((folder / META_NAME).read_text())
    recs = [load_recording(p) for p in sorted(folder.glob("*.csv")) if is_recording_name(p)]
    recs.sort(key=lambda r: (r.host_ts[0] if len(r.table) else 0.0, r.path.name))
    return Session(path=folder, meta=meta, recordings=recs)
