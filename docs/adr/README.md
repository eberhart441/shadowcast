# Architecture Decision Records

One record per decision that would be hard to reverse, surprising without its context, or the result of a real trade-off. Each says what was decided, what else was weighed, what it costs, and what was measured. Vocabulary follows [`GLOSSARY.md`](../../GLOSSARY.md).

All of these decisions were made during the CodeMetalForge hackathon and recorded on 2026-11-08 from the code, the commit history, the GitHub issues and the docs. Each record's date is the day of its deciding commit, given under "Recorded in".

| # | Title | Status | Date |
|---|---|---|---|
| [0001](0001-a-miss-costs-more-than-a-false-alarm.md) | A missed person costs more than a false alarm | Accepted | 2026-11-07 |
| [0002](0002-esp32-c5-receivers-only.md) | ESP32-C5 receivers only | Accepted | 2026-11-07 |
| [0003](0003-one-shared-channel-at-40-hz.md) | One shared channel (6), senders at 40 Hz | Accepted | 2026-11-07 |
| [0004](0004-wired-receivers-wireless-deferred.md) | Receivers stay wired; wireless receivers deferred | Superseded by [0019](0019-wireless-receivers-over-esp-now.md) | 2026-11-07 |
| [0005](0005-vendor-the-firmware.md) | Vendor the ESP32 firmware in the repository | Accepted | 2026-11-07 |
| [0006](0006-identify-boards-by-usb-serial.md) | Identify boards by USB serial, not by port name | Accepted | 2026-11-07 |
| [0007](0007-amplitude-only-motion-score.md) | Motion score from CSI amplitude only, with the breathing band removed | Accepted | 2026-11-07 |
| [0008](0008-motion-only-by-default.md) | Motion-only by default; breathing is opt-in | Accepted | 2026-11-07 |
| [0009](0009-fixed-thresholds-by-default.md) | Fixed per-link thresholds by default; adaptive mode is experimental | Accepted | 2026-11-07 |
| [0010](0010-two-noisy-room-calibration-methods.md) | Two noisy-room calibration methods, no winner declared | Experimental | 2026-11-07 |
| [0011](0011-heatmap-evidence-from-quiet-baseline.md) | Heatmap evidence from each link's quiet baseline, not its threshold | Accepted | 2026-11-07 |
| [0012](0012-host-ts-line-stream-seam.md) | Every packet source is a `(host_ts, line)` stream | Accepted for the line pipeline; superseded by [0022](0022-host-time-round-broadcast-seam.md) for the transceiver pipeline | 2026-11-07 |
| [0013](0013-packet-time-not-wall-clock.md) | Pipeline time comes from the packets, not the wall clock | Accepted | 2026-11-07 |
| [0014](0014-room-model-as-config.md) | The rig is configuration: `config/room.json` | Accepted | 2026-11-07 |
| [0015](0015-stdlib-http-server-loopback-commands.md) | Standard-library HTTP server; commands only from this machine by default | Accepted | 2026-11-07 |
| [0016](0016-calibration-off-the-server-lock.md) | Compute a finished calibration off the server lock | Accepted | 2026-11-07 |
| [0017](0017-tests-pinned-to-frozen-fixture.md) | Tests are pinned to a frozen four-node room | Accepted | 2026-11-07 |
| [0018](0018-evaluate-by-recording.md) | Evaluate by recording, never by random window | Accepted | 2026-11-07 |
| [0019](0019-wireless-receivers-over-esp-now.md) | Wireless receivers: forward CSI over ESP-NOW to one gateway | Accepted | 2026-11-08 |
| [0020](0020-plug-and-play-no-per-layout-training.md) | Plug and play: no per-layout training | Accepted | 2026-11-08 |
| [0021](0021-on-node-score-streaming-window-and-score-code.md) | On-node score: streaming window statistics, and a log-scale score code | Accepted | 2026-11-11 |
| [0022](0022-host-time-round-broadcast-seam.md) | Every transceiver source is a `(host time, round broadcast)` stream | Accepted | 2026-11-11 |
| [0023](0023-round-recordings.md) | Round recordings: the undecoded bytes, host time and source, in one versioned binary file | Accepted | 2026-11-11 |

Reading order: 0001 is the principle the detection records (0007 to 0011) argue from. 0002 to 0006 are the hardware and radio. 0012 to 0018 are the software architecture and how it is tested.

## Statuses

- **Accepted:** in force; the code follows it.
- **Experimental:** shipped, but not validated against labelled data; expect it to change.
- **Deferred:** deliberately not done yet, with the condition for revisiting it.

A record is never edited to reverse it. A new record supersedes it, and the old one's status says "Superseded by NNNN".

## Format

Title, Status, Date, Recorded in (the deciding commit or issue), Related, then:

- **Context:** the forces at play.
- **Decision:** what was decided, specifically enough to check against the code.
- **Alternatives considered:** what was weighed, and whether it was tried, proposed or ruled out.
- **Consequences:** what it costs and what follows from it, including known tensions.
- **Evidence:** measured numbers with their source. Where something was never measured, the record says so.

## Sources

- Short hashes such as `bd1cb61` are commits on `main`. A few records also cite unmerged branches; they say so where they do.
- `#N` is a GitHub issue in `CodeMetalForge-Hackathon/Team-12`.
- hw3 and hw5 are the two single-link recorded sessions on the branch `data/two-board-sessions`. No labelled recording of the current four-link rig exists, so no record quotes an accuracy figure for it.
