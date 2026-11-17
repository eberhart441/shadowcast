"""The one error type for bytes or requests the host refuses with a reason it counts or reports.

    raise BroadcastError("length", "53 bytes; membership 0x000000ff needs 54")   # a ReasonError
    except ReasonError as e: counters[e.reason] += 1

Each module names its reasons (``broadcast.DECODE_REASONS``, ``gateway_records`` record reasons,
``ota.IMAGE_REASONS`` and ``ota.COMMAND_REASONS``, ``raw_mode.REFUSALS``) and subclasses this, so a caller can
catch one kind or all of them, and ``reason`` is always the machine-readable word and the message the human text.
"""
from __future__ import annotations


class ReasonError(ValueError):
    """A refusal with a ``reason`` (one word, for counters and HTTP bodies) and a message (for the operator)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
