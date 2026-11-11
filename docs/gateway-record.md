# Gateway record format

**Version 1.** This page is the one definition of the bytes a node writes to its USB port. A gateway writes one
**heard** record per ESP-NOW frame it receives; every node (gateway or transceiver) also writes a **status** record
once a second and **text** records for replies to settings commands and log lines. Nothing else is written on
purpose; the boot ROM's own messages may appear before the first record, and a reader skips them. The host's
reader is `csi/gateway_records.py`; the node's encoder is `firmware/core/gateway_record.{h,c}`. Both are pinned
to the golden fixtures in [`tests/fixtures/gateway_record_v1.json`](../tests/fixtures/gateway_record_v1.json).
Change this page, the fixtures and both sides together, and bump the version byte when the layout changes.

The round broadcast inside a heard record is defined in [`round-broadcast.md`](round-broadcast.md).

## Framing

Every multi-byte field is little-endian.

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 2 | sync | `53 43` (ASCII `SC`). |
| 2 | 1 | version | `1`. |
| 3 | 1 | type | `1` heard, `2` status, `3` text. Other values are rejected (reason `type`). |
| 4 | 2 | length | Body length *n*, at most 1500 (longer is reason `too_long`). |
| 6 | *n* | body | Depends on the type, below. |
| 6 + *n* | 2 | CRC | CRC-16/CCITT-FALSE (polynomial `0x1021`, initial value `0xFFFF`, no reflection, no final XOR) over the bytes from the version to the end of the body. |

A reader looks for the sync bytes, checks version, length and CRC, and on any failure skips one byte and looks
again, so a corrupt or partial record costs only itself. Rejections are counted by reason: `version`,
`too_long`, `crc`, `type`, `short_body`; bytes skipped while looking for a record are counted too.

## Heard record (type 1)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 8 | receive time | The gateway's device time in µs (`esp_timer_get_time`) when the frame arrived, u64. |
| 8 | 1 | rssi | i8, dBm of the frame at the gateway. |
| 9 | 1 | reserved | 0. |
| 10 | rest | payload | The ESP-NOW body exactly as received: for our nodes, a round broadcast. |

A body shorter than 10 bytes is reason `short_body`. The gateway does not decode the payload: the host does, in
one place, and counts what it rejects (ADR-0022).

## Status record (type 2)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 6 | serial | The node's board serial (base MAC), transmission order. |
| 6 | 8 | uptime | Device time in µs, u64. |
| 14 | 1 | reset reason | `esp_reset_reason()` of the last boot (1 power-on, 3 software, 4 panic, 7 task watchdog, ...). |
| 15 | 1 | role | `0` transceiver, `1` gateway. |
| 16 | 1 | channel | The 2.4 GHz channel the node is on. |
| 17 | 1 | reserved | 0. |
| 18 | 4 × *k* | counters | u32 each, counted since boot (never reset), in this order; a reader ignores counters past the ones it knows. |

| # | Counter | Gateway | Transceiver |
|---|---|---|---|
| 0 | heard | ESP-NOW frames received | round broadcasts heard from other members of the round |
| 1 | queue full | frames dropped because the queue to the USB writer was full | CSI frames dropped because the queue to the node task was full |
| 2 | USB dropped | records not written because the USB buffer was full (no host reading) | same |
| 3 | sent | 0 | round broadcasts sent |
| 4 | send errors | 0 | `esp_now_send` errors, failed send callbacks and missed slots |
| 5 | rejected | frames longer than 250 bytes (not ours) | ESP-NOW frames that are not a v1 round broadcast, or come from a node outside the round |
| 6 | CSI skipped | 0 | CSI frames not scored: layout warm-up, a layout other than the locked one, or too many subcarriers |

A body shorter than 18 bytes is reason `short_body`.

## Text record (type 3)

The body is UTF-8 text, one line without its line end: a reply to a settings command (`ok ...` or `error ...`) or
a log line.
