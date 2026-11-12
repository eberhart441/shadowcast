"""The staged rollout: which nodes get the command in which stage, and when a stage has passed.
"""
from __future__ import annotations

import time
from typing import Optional

from ..room import board_serial
from .command import MAX_TARGETS, OTA_CMD_UPDATE, channels_clear, maintenance_command
from .image import ImageInfo
from .report import GOOD_OUTCOMES, Report

STAGE_SIZES = (2, 6)           # the first stages; then the rest, MAX_TARGETS per command
COPIES = 3                     # each command is sent this often (a broadcast has no ACK) ...
COPY_GAP_S = 0.2               # ... this far apart
REPORT_TIMEOUT_S = 300.0       # a target's report must arrive this long after its stage's command
REJOIN_TIMEOUT_S = 60.0        # and it must be heard on the round again this long after its report


def plan_stages(serials, sizes=STAGE_SIZES, per_command: int = MAX_TARGETS) -> list[list[str]]:
    """The nodes in stages: ``sizes`` first (2, then 6), then the rest, at most ``per_command`` per stage."""
    rest, stages = list(serials), []
    for size in sizes:
        if rest:
            stages.append(rest[:min(size, per_command)])
            rest = rest[len(stages[-1]):]
    while rest:
        stages.append(rest[:per_command])
        rest = rest[per_command:]
    return stages


class Rollout:
    """One command at a time, stage after stage, each stage only once the last one is fully back.

    The caller polls ``step(now)`` and feeds what it learns: ``report(r, now)`` for every report the image server
    receives, ``heard(serial, now)`` for every authenticated round broadcast the gateway hears. A stage passes when
    every target reported a good outcome (GOOD_OUTCOMES) for this command and was heard on the round after that.
    Anything else (a bad outcome, no report within ``report_timeout_s``, not back on the round within
    ``rejoin_timeout_s``) stops the rollout: ``state`` "failed", ``failure`` says why, and no further stage is sent.
    ``send(frame)`` hands one signed maintenance command to the gateway.
    """

    def __init__(self, stages, kit_key: bytes, *, image: Optional[ImageInfo], port: int, ap_channel: int,
                 sensing_channel: int, send, command: int = OTA_CMD_UPDATE, wall=None, log=print,
                 report_timeout_s: float = REPORT_TIMEOUT_S, rejoin_timeout_s: float = REJOIN_TIMEOUT_S,
                 copies: int = COPIES, copy_gap_s: float = COPY_GAP_S) -> None:
        if not channels_clear(sensing_channel, ap_channel):
            raise ValueError(f"a maintenance network on channel {ap_channel} overlaps sensing channel "
                             f"{sensing_channel}: put it on 5 GHz or 5+ channels away")
        if command == OTA_CMD_UPDATE and image is None:
            raise ValueError("an update needs a checked image")
        self._stages = [list(s) for s in stages if s]
        self._key = bytes(kit_key)
        self._image, self._command = image, command
        self._port, self._ap_channel, self._sensing = port, ap_channel, sensing_channel
        self._send, self._log = send, log
        self._wall = wall or time.time
        self._report_timeout, self._rejoin_timeout = report_timeout_s, rejoin_timeout_s
        self._copies, self._gap = copies, copy_gap_s
        self._stage = -1
        self._issued = 0
        self.state = "running" if self._stages else "done"
        self.failure = ""
        self.results: dict[str, str] = {}

    @property
    def stage(self) -> int:
        """The stage running or last run, 1-based; 0 before the first."""
        return self._stage + 1

    @property
    def stage_count(self) -> int:
        return len(self._stages)

    # -- the caller's events

    def report(self, r: Report, now: float) -> None:
        serial = board_serial(r.serial)
        if self.state != "running" or self._stage < 0 or r.issued != self._issued or serial not in self._targets:
            return
        self.results[serial] = r.outcome
        self._reported.setdefault(serial, now)
        self._log(f"{serial}: {r.outcome} (slot {r.slot}, version {r.version})")
        if not r.ok:
            self._fail(f"{serial} reported {r.outcome}")

    def heard(self, serial: str, now: float) -> None:
        serial = board_serial(serial)
        if self.state == "running" and self._stage >= 0 and serial in self._reported and now >= self._reported[serial]:
            self._rejoined.add(serial)

    # -- the clock

    def step(self, now: float) -> str:
        if self.state != "running":
            return self.state
        if self._stage < 0 or self._stage_done():
            if self._stage + 1 >= len(self._stages):
                self.state = "done"
                self._log("rollout done")
                return self.state
            self._start(self._stage + 1, now)
        while self._copies_left and now >= self._next_copy:
            self._send(self._frame)
            self._copies_left -= 1
            self._next_copy += self._gap
        self._check_timeouts(now)
        return self.state

    # -- inside

    def _start(self, stage: int, now: float) -> None:
        self._stage = stage
        self._targets = [board_serial(s) for s in self._stages[stage]]
        self._issued = max(int(self._wall()), self._issued + 1)
        image = self._image
        self._frame = maintenance_command(
            self._command, self._targets, self._key, port=self._port, ap_channel=self._ap_channel,
            issued=self._issued, image_size=image.size if image else 0,
            image_sha256=image.sha256 if image else bytes(32), round_no=self._issued,
            sensing_channel=self._sensing)
        self._copies_left, self._next_copy, self._started = self._copies, now, now
        self._reported: dict[str, float] = {}
        self._rejoined: set[str] = set()
        self._log(f"stage {stage + 1}/{len(self._stages)}: {', '.join(self._targets)}")

    def _stage_done(self) -> bool:
        return all(self.results.get(s) in GOOD_OUTCOMES and s in self._rejoined for s in self._targets)

    def _check_timeouts(self, now: float) -> None:
        for s in self._targets:
            if s not in self._reported and now - self._started >= self._report_timeout:
                return self._fail(f"no report from {s} within {self._report_timeout:.0f} s")
            if s in self._reported and s not in self._rejoined and now - self._reported[s] >= self._rejoin_timeout:
                return self._fail(f"{s} was not heard on the round within {self._rejoin_timeout:.0f} s of its report")

    def _fail(self, why: str) -> None:
        self.state, self.failure = "failed", why
        self._log(f"rollout stopped: {why}")


def target_serials(room, names) -> list[str]:
    """Board serials (upper case) of ``names`` (transceiver names of ``room`` or board serials), or of every
    transceiver with a serial, in room order, when ``names`` is empty. ValueError for a name that is neither."""
    by_name = room.transceiver_serials()
    if not names:
        return list(by_name.values())
    serials = []
    for name in names:
        name = str(name).strip()
        if name in by_name:
            serials.append(by_name[name])
        elif len(board_serial(name)) == 17:
            serials.append(board_serial(name))
        else:
            raise ValueError(f"{name!r} is neither a transceiver of the room nor a board serial")
    return serials
