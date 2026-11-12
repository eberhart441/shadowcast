"""The report a node POSTs to the image server after maintenance mode, tagged with the kit key.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from ..room import board_serial

REPORT_TAG_LABEL = b"shadowcast ota report\n"
# firmware/core/ota_core.h ota_outcome_t, in order
OUTCOMES = ("none", "committed", "rolled_back", "already", "checked_in", "no_network", "overlap", "no_server",
            "refused_size", "refused_digest", "refused_image", "write_failed")
GOOD_OUTCOMES = frozenset({"committed", "already", "checked_in"})
_REPORT_FIELDS = ("serial", "issued", "outcome", "slot", "image", "version")


class ReportError(ValueError):
    """A report line that is malformed or does not carry the kit's tag."""


@dataclass(frozen=True)
class Report:
    serial: str
    issued: int        # the command it answers
    outcome: str       # one of OUTCOMES
    slot: str          # the app slot the node runs now, ota_0 or ota_1
    image: str         # first 8 bytes of the running image's SHA-256 as hex, "-" when the node does not know it
    version: str       # the running image's version string

    @property
    def ok(self) -> bool:
        """The node runs the commanded image (or was only asked to report)."""
        return self.outcome in GOOD_OUTCOMES


def report_tag(line: str, kit_key: bytes) -> str:
    return hmac.new(kit_key, REPORT_TAG_LABEL + line.encode(), hashlib.sha256).digest()[:8].hex()


def parse_report(line: str, kit_key: bytes) -> Report:
    """The report a node POSTs: "ota-report 1 serial=.. issued=.. outcome=.. slot=.. image=.. version=.. tag=..",
    whose tag is HMAC-SHA256(kit key, REPORT_TAG_LABEL + the line before " tag=")[:8]."""
    line = line.strip()
    body, sep, tag = line.rpartition(" tag=")
    if not sep or not hmac.compare_digest(report_tag(body, kit_key), tag):
        raise ReportError("the report does not carry the kit's tag")
    words = body.split(" ")
    if words[:2] != ["ota-report", "1"]:
        raise ReportError(f"not a version 1 report: {body[:40]!r}")
    fields = dict(w.split("=", 1) for w in words[2:] if "=" in w)
    if any(k not in fields for k in _REPORT_FIELDS):
        raise ReportError(f"the report lacks a field: {body!r}")
    try:
        issued = int(fields["issued"])
    except ValueError:
        raise ReportError(f"issued is not a number: {fields['issued']!r}") from None
    return Report(board_serial(fields["serial"]), issued, fields["outcome"], fields["slot"], fields["image"],
                  fields["version"])
