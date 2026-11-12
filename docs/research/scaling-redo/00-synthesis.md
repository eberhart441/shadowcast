# Why the rig stopped at ~4 nodes, and what the redo should be

Synthesis of eight research reports (2026-11-10/11), plus a later prior-art survey (09). Every claim below is
cited in the numbered report it points to; this file only joins them up. Estimates stay estimates: no new
hardware measurements were taken.

**Reading the citations in this repo.** The reports were written against the hackathon checkouts, so they give
local paths (`C:\Users\simon\Documents\hackathon\Team-12-tracker\...`: read them as paths in this repo) and the
hackathon's branch names. The experiment branches live here under `archive/`:

| Branch named in the reports | Branch in this repo |
|---|---|
| `feat/transceiver-5plus` (and its ancestor `feat/all-transceiver`) | `archive/transceiver-5plus` |
| `design/multichannel-tdma` | `archive/multichannel-tdma` |
| `design/wireless-csi-forwarding` | `archive/wireless-csi-forwarding` |
| `feat/ht20-channel-param` | `archive/ht20-channel-param` |
| `fix/scaling-and-accuracy` | `archive/scaling-and-accuracy` |
| `siv/turn-taking` | `archive/turn-taking` |
| `data/two-board-sessions` | `archive/data-two-board-sessions` |
| `feat/wireless-receivers`, `csi-sensing` | merged into `main` |

| # | Report | Kind |
|---|---|---|
| 01 | [Firmware forensics](01-firmware-forensics.md) | code reading + arithmetic |
| 02 | [Host pipeline](02-host-pipeline.md) | code reading + **measured** benchmarks (laptop; Pi assumed 2.5x slower) |
| 03 | [RF airtime](03-rf-airtime.md) + [model scripts](03-rf-airtime-model/) | airtime model + CSMA simulation |
| 04 | [History and evidence](04-history-and-evidence.md) | git + session transcripts: what actually happened |
| 05 | [Radio platforms](05-radio-platforms.md) | survey, 70 sources |
| 06 | [Antennas and RF front-end](06-antennas-rf.md) | survey + link-budget estimates |
| 07 | [Network architecture](07-network-architecture.md) | survey + scaling estimates |
| 08 | [Software redo](08-software-redo.md) | keep / rewrite / drop + proposed stack |
| 09 | [Prior art](09-prior-art.md) | survey: academic, ESP32, commercial, first-responder radar, patents |

## The verdict on the hypothesis

> "A combination of badly optimised software and shitty micro ESP32s that can't handle the packets."

**Software: yes, decisively. The chip: no.** The ESP32-C5 radio and CPU were never the binding constraint.
What capped the rig was a data path inherited from Espressif's demo code: every received packet became an
~850-byte ASCII line, printed value by value inside the WiFi task, echoed to a 921600-baud UART, read by
Python on one core, and (once receivers went wireless) re-transmitted as a ~870-byte unicast frame on the
very channel being sensed. The C5 is still the best tiny CSI chip available (05).

A third factor the hypothesis misses: **physical unreliability** (power banks, cables, missing antennas,
dead boards) is what actually ended both 5-board runs (04).

Also a correction to the premise: the rig *did* run 5 boards (3 receivers x 2 senders, 6 links) near full
rate twice (34-38 pkt/s per link, Nov 7 16:00; 35-37 pkt/s, Nov 8 01:22). Neither run was stopped by node
count. Only the 5-board all-transceiver rig clearly failed (0-27 pkt/s, 60-64 % loss) (04).

## The walls, in the order you hit them

Each wall is independent; removing one exposes the next. N is the number of all-transceiver nodes
(each hears N-1 senders) unless stated.

| Wall | Where it bites | Evidence | Sources |
|---|---|---|---|
| **1. Receiver output path.** ~850 B ASCII line per packet, formatted in the WiFi task, echoed to UART at 921600 baud: ~9.2 ms/line, ~108 lines/s per receiver. | 3rd sender at 40 Hz; all-transceiver N=4 (148 % load) | Arithmetic from code; the teammates' unmerged `fix/scaling-and-accuracy` reached the same budget | 01, 02 |
| **2. Host serial reader.** Python `readline()`: 165 % CPU on the Pi, 19-27 % loss; chunked reader 5-12 %. The 5+ branch never got the fix. | N≈3-4 on a Pi | **Measured** A/B on the Pi (commit 867eea3) and on the laptop (321 µs vs 0.8 µs per line) | 02, 04 |
| **3. One Python core.** 36/46/69 % of a laptop core at N=4/5/6; falls behind real time at N=8. Room decision recomputed per packet over all links (cost ~ links²); auto-threshold mode loops in Python. | Pi 5: ~16-25 links (N=4-5) | **Measured** on laptop, Pi scaled by assumption | 02 |
| **4. Gain lock.** One forced gain per receiver, locked at packet 100 (~1.25 s), applied to every frame it hears. | Gateway-adjacent receivers (`NO_MEM`); transceivers "deaf" after 2-3 s; possibly the 10 pkt/s link | Timing matches "cb_ok only in the first second" | 01, 03 |
| **5. Airtime of forwarded ASCII.** Each forward costs ~9x the sensing frame. All-transceiver N=5 ≈ 53 % busy, N=6 ≈ 81-94 %. Sensing frames alone: 16 nodes < 5 %. | N=5-6 wireless | Model + CSMA sim that reproduces the observed 37-40 pkt/s | 03, 01 |
| **6. Silent drops and unbounded queues.** Gateway parser drops lines from full queues uncounted (7-44 % in the test); downstream queue unbounded. | Any overload | Benchmark | 02 |
| **Physical.** Power banks dying or cutting out while charging (up to 91 % loss), flaky/charge-only cables, two dead boards, XIAO C5s without antennas, a receiver 40 dB down from placement, noisy S3 receivers. | Ended both 5-board runs | Transcripts | 04 |

The team's founding belief, that radio collisions were the limit, rests on one unrecorded test (65 % loss,
two senders at 100 Hz). That test offered ~182 lines/s to a ~99 lines/s output path with 1.5 % airtime:
it was serial overflow, not collisions (01, 03, 04). The turn-taking and multi-channel work was aimed at the
wrong wall.

## What the reports agree the redo should be

All eight point the same way: **stop moving raw text, compute on the node, schedule the air.** None of that
needs a different chip.

**Architecture (07, 03).** Every node identical and a transceiver; round-robin slots on one surveyed 2.4 GHz
channel; unacknowledged MCS0 broadcasts; each node packs ~4 B/link of features from the previous round into
its own sensing frame, so backhaul costs no extra airtime; one or more listen-only gateways on the host.
Estimated: 16 nodes / 240 links at 40 Hz in 12 % airtime; 32 nodes / 992 links at 20 Hz in 17 %.
Raw CSI becomes a debug mode, one link on request. Literature puts motion sensing at 10-20 Hz per link.

**Node hardware (05, 06).**
- Chip: **ESP32-C5** (dual-band, 40 MHz CSI with 114 subcarriers or HE20 with 242, official gain control).
  Alternatives worth one look: NXP RW612 (12x11 mm module, vendor CSI + motion index, small community).
- Size floor: at 2.4 GHz the board is half the antenna; much below ~31 mm of board the node loses several
  dB. **The smallest node that performs well is ~45-55 mm**, with 1-2 dual-band FPC antennas (Molex 146153,
  Taoglas FXP830) fixed to the housing, two at 90° for diversity. A ~25 mm chip-antenna node works for
  same-room or drywall only.
- Band: 2.4 GHz through walls (brick/concrete cost 4-12 dB more per wall at 5 GHz); 5 GHz better within a
  room (2.2x phase per mm of motion). Fit dual-band antennas and choose at runtime. One C5 cannot use both
  bands at once, so no "sense on 2.4, backhaul on 5" (07).
- No external PA: the C5 already transmits at the EU EIRP cap; no documented FEM control (06).
- Multi-antenna (coherent) CSI needs several radios on one clock (esp-crab / ESPARGOS style, ~8 cm array
  at 5 GHz). It buys direction finding without training, which fits the no-walk-through rule, at the cost
  of credit-card size and immature hardware (05).
- Heat: battery and u.FL cable fail first; PEEK housing, soldered antenna (06).

**Software (08, 02).** Keep the ADRs, glossary, evaluate-by-recording, the pure `(time, score)` detector
and its explicit states, the median+MAD adaptive baseline (make it the only mode: per-link fixed thresholds
mean N² calibrations). Port the scorer to node C with golden-vector tests against numpy. Binary versioned
wire protocol (MAC, boot_id, seq, 64-bit timestamp, CRC). Host stays Python+numpy, split into ingest, pure
core and API, with the per-packet O(links²) and window re-stacking hot spots removed (02). Heatmap: use
more than the top 2 links per cell (VRTI). Link identity by full MAC (today it is the last octet). Signed
OTA, identity by MAC, no per-board build flags.

## Where the reports pull against each other or against you

- **Size vs performance.** You want the smallest package; 06 puts the sweet spot at ~50 mm, not XIAO size,
  and coherent multi-antenna nodes (05) push to credit-card size. This is the central decision.
- **The Kalman dot.** 08 proposes dropping it as a product feature. You chose to keep it over the trained
  locator. Open, not decided.
- **Stillness.** A person still for ~30 s fades into the baseline (08); amplitude-only features cannot fix
  it. 08 suggests a zone-level "possibly occupied" latch; coherent multi-antenna CSI (05) is the hardware
  route to breathing/Doppler. A miss is the costly error (ADR 0001), so this matters more than it looks.
- **Security and detectability (07).** Broadcast ESP-NOW cannot be encrypted (needs an app-layer
  authentication tag), and a steady 20-40 Hz beacon train is easy to detect, which matters for spec ops.

## Cheap experiments that would settle the biggest unknowns

Most can run on boards you already own.

1. **Prove the diagnosis on the old hardware (a day).** Stop the UART echo, emit binary or strided CSI,
   chunked reader, 20 Hz, then run 6+ all-transceiver nodes. If links hold rate, walls 1-3 are confirmed.
2. **Slot jitter:** how much an ESP-NOW broadcast's time on air jitters (sets the guard time); rerun the
   2-node 100 Hz test, CSMA vs slotted (07).
3. **CSI capture under a burst:** can a C5 capture ~31 frames 0.5 ms apart; what is its real max CSI rate
   (no vendor figure; plan on 100-200 Hz per link and measure) (05, 07).
4. **Gain lock with many transmitters:** per-sender gain, or no lock, vs today's one median lock (01).
5. **2.4 vs 5 GHz through a real wall** on the C5, with a genuinely dual-band antenna; no published study
   exists (06).
6. **Is ~4 B/link of features enough** for the heatmap, tested by replaying raw recordings (07).
7. **Antenna on the actual enclosure:** chip vs FPC at 25 mm and 50 mm board lengths, measured RSSI.

## Data gap: act on this first

No multi-node CSI was ever committed: `data/` is empty and every detection test runs on the simulator (08).
Every multi-node figure in 04 comes from transcripts or commit messages. The demo Pi had raw recording
(`~/raw_log_on`); whatever it captured is the only real multi-node data that exists. Pull it before the Pi is
reused.

## Seed decisions for the wayfinder map

Candidate tickets (type in brackets). The map itself decides order and blocking.

- Destination: what the redo has to deliver first (field-demo rig? product prototype? N nodes, room size,
  through how many walls?) [grilling]
- Node size tier and whether it needs coherent multi-antenna CSI [grilling, after experiments 5 and 7]
- Band policy: 2.4 only, 5 only, runtime choice [research + task: experiment 5]
- Medium access and backhaul: Spin-style slots with features in the sensing frame vs alternatives
  [prototype: experiments 1-3]
- What the node computes and sends: feature set, bytes per link, raw debug mode [prototype: experiment 6]
- Gain control strategy [task: experiment 4]
- Detection output: motion only, stillness latch, the Kalman dot, localisation without training [grilling]
- Power and deployment: battery size, sleep between rounds, join and position entry in the field [grilling]
- Security: authentication, detectability [research]
- Recover the Pi's raw recordings [task]
- Positioning against handheld through-wall radar ("why not buy one radar?") and freedom to operate around
  Cognitive Systems' variance-threshold patent (09) [grilling + research]

Done: the new repo and tracker are this one, eberhart441/shadowcast (2026-11-11).

## From the conversation after the reports (2026-11-11)

Stated by the user, not yet decided on the map:

- **Goal: about 30 nodes.** The user sees reaching ~30 as the demonstration that the technology works at a
  fundamental level. On the estimates above it is plausible (≈17 % airtime at 20 Hz); the deciding unknown is
  whether a C5 can capture CSI from ~580 frames/s (experiment 3), so that test goes first.
- **Small package matters**, with a tradeoff accepted. The user likes the Seeed XIAO ESP32-C5 (form factor, its
  bundled u.FL FPC antenna, the box it ships in).
- **Kalman dot:** the user kept it over the rejected trained locator; 08's "drop it" is open, not decided.

Recommended in the conversation, not yet agreed:

- Keep XIAO ESP32-C5 boards through prototyping, including the 6-8 node rig; defer a custom C5-MINI-1U board until
  node count, band and antenna count are decided. Fix each FPC flat to the enclosure wall (a moving antenna reads as
  motion).
- Keep the bundled antennas for 2.4 GHz (the through-wall band) and buy 4-6 **dual-band** FPCs (Molex 146153 or
  Taoglas FXP830, u.FL) for the 2.4 vs 5 GHz test; not 5 GHz-only parts.
- Position against radar on persistent, hands-free, whole-floor, multi-person coverage (one radar watches one wall
  while someone holds it).
