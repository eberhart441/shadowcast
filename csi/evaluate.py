"""Offline evaluation of the live detectors on recorded sessions.

    evaluate_sessions([folder, ...]) -> EvaluationReport
    format_report(report) -> str

Per session: the first ``empty`` recording (by start time) calibrates the motion
threshold via ``calibrate_packets``. Every other recording is replayed through
``detect_packets`` with that threshold, exactly as the live CLI would. Each
decision the detector emits (one per packet, each over its trailing windows) is
compared to the label of the file it came from. Nothing is split at random.

Label mapping: walking -> MOTION, still_* -> STILL_PRESENT, empty -> EMPTY. The
confusion matrix has one row per label, so the two still labels stay separate.

Motion-only is the default (``breathing=False``): STILL_PRESENT is never predicted,
so the still_* rows have no valid prediction. They are printed but marked
"breathing disabled" rather than scored as 0%. ``breathing=True`` scores them.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pandas as pd

from .breathing import WINDOW_S as BREATH_WINDOW_S
from .detect import EMPTY, MOTION, STILL_PRESENT, Calibration, CalibrationError, calibrate_packets, detect_packets
from .recording import LABELS, META_NAME, Recording, Session, load_session

LABEL_STATE: dict[str, str] = {
    "empty": EMPTY,
    "walking": MOTION,
    "still_standing": STILL_PRESENT,
    "still_sitting_or_lying": STILL_PRESENT,
}
STATES = (EMPTY, MOTION, STILL_PRESENT)


class EvaluationError(ValueError):
    """A session cannot be evaluated; the message says what to do about it."""


@dataclass
class SessionResult:
    name: str
    calibration: Calibration
    calibration_file: str                       # the empty recording used for calibration
    scored_files: list[str]
    confusion: pd.DataFrame                     # rows: LABELS, columns: STATES, counts
    no_breathing_window: int = 0                # decisions made before a full 15 s breathing window
    notes: list[str] = field(default_factory=list)


@dataclass
class EvaluationReport:
    sessions: list[SessionResult]
    breathing: bool = False  # whether STILL_PRESENT predictions were made (and the still rows scored)

    def total(self) -> pd.DataFrame:
        """Confusion matrix summed over all sessions."""
        if not self.sessions:
            return _empty_matrix()
        return sum(s.confusion for s in self.sessions)


def _empty_matrix() -> pd.DataFrame:
    return pd.DataFrame(0, index=list(LABELS), columns=list(STATES), dtype=int)


def _confusion(pairs: Sequence[tuple[str, str]]) -> pd.DataFrame:
    """Counts of (true label, predicted state) pairs as a LABELS x STATES table."""
    counts = Counter(pairs)
    matrix = _empty_matrix()
    for (label, state), n in counts.items():
        matrix.loc[label, state] = n
    return matrix


def _missing_empty_message(name: str) -> str:
    return (f"session {name!r} has no empty recording to calibrate on; record the empty room "
            f"first with: scripts/record.py --session {name} --label empty")


def _evaluate_session(session: Session, breathing: bool) -> SessionResult:
    name = session.path.name
    empties = [r for r in session.recordings if r.label == "empty"]
    if not empties:
        raise EvaluationError(_missing_empty_message(name))
    calib_rec = empties[0]
    try:
        calibration = calibrate_packets(calib_rec.packets())
    except CalibrationError as e:
        raise EvaluationError(f"session {name!r}: cannot calibrate on {calib_rec.path.name}: {e}") from e

    pairs: list[tuple[str, str]] = []
    no_breathing = 0
    scored: list[Recording] = [r for r in session.recordings if r is not calib_rec]
    for rec in scored:
        for reading in detect_packets(rec.packets(), calibration.threshold, breathing=breathing):
            pairs.append((rec.label, reading.state))
            no_breathing += not reading.enough_data

    notes: list[str] = []
    if len(empties) == 1:
        notes.append("only one empty recording: it calibrated the threshold, so no empty "
                     "windows were scored from this session; record a second empty recording to score empty")
    return SessionResult(
        name=name,
        calibration=calibration,
        calibration_file=calib_rec.path.name,
        scored_files=[r.path.name for r in scored],
        confusion=_confusion(pairs),
        no_breathing_window=no_breathing,
        notes=notes,
    )


def evaluate_sessions(folders: Sequence[Path | str], breathing: bool = False) -> EvaluationReport:
    """Evaluate each session folder. Raises EvaluationError listing every unusable session.

    ``breathing=False`` (default) scores motion-only; ``True`` also makes STILL_PRESENT predictions.
    """
    problems: list[str] = []
    sessions: list[Session] = []
    for folder in map(Path, folders):
        if not (folder / META_NAME).exists():
            problems.append(f"{folder} is not a session folder (no {META_NAME})")
            continue
        session = load_session(folder)
        if not any(r.label == "empty" for r in session.recordings):
            problems.append(_missing_empty_message(folder.name))
        sessions.append(session)
    if problems:
        raise EvaluationError("\n".join(problems))
    return EvaluationReport([_evaluate_session(s, breathing) for s in sessions], breathing)


STILL_LABELS = tuple(label for label, state in LABEL_STATE.items() if state == STILL_PRESENT)
BREATHING_DISABLED = "breathing disabled"


def _label_accuracy(matrix: pd.DataFrame, label: str, breathing: bool) -> str:
    if label in STILL_LABELS and not breathing:
        return BREATHING_DISABLED  # no valid prediction exists for these rows without breathing
    row = matrix.loc[label]
    total = int(row.sum())
    if total == 0:
        return "-"
    return f"{row[LABEL_STATE[label]] / total:.0%}"


def _format_matrix(matrix: pd.DataFrame, breathing: bool) -> str:
    table = matrix.copy()
    table["correct"] = [_label_accuracy(matrix, label, breathing) for label in table.index]
    table.insert(0, "expected", [LABEL_STATE[label] for label in table.index])
    return table.to_string()


def format_report(report: EvaluationReport) -> str:
    out: list[str] = []
    if not report.breathing:
        out.append("mode: motion-only (breathing disabled; STILL_PRESENT is never predicted). "
                   "Pass --breathing to score the still_* labels.")
        out.append("")
    for s in report.sessions:
        c = s.calibration
        out.append(f"session {s.name}: threshold {c.threshold:.3f} "
                   f"(calibrated on {s.calibration_file}, {c.duration_s:.0f}s, loss {c.loss:.1%})")
        out.append(f"scored: {', '.join(s.scored_files) or 'nothing'}")
        out.append(_format_matrix(s.confusion, report.breathing))
        if report.breathing:
            out.append(f"decisions without a full {BREATH_WINDOW_S:.0f} s breathing window: {s.no_breathing_window}")
        out.extend(f"note: {n}" for n in s.notes)
        out.append("")
    if len(report.sessions) > 1:
        out.append("all sessions:")
        out.append(_format_matrix(report.total(), report.breathing))
    return "\n".join(out).rstrip() + "\n"
