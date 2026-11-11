# Round broadcast format

**Version 2.** This page is the one definition of the bytes a transceiver sends in its slot each round. The
host's copy is `csi/broadcast.py` (layout) and `csi/round_auth.py` (authentication); the golden byte fixtures in
[`tests/fixtures/round_broadcast_v2.json`](../tests/fixtures/round_broadcast_v2.json) pin the host decoder,
encoder and verifier, and the firmware (`firmware/core/round_broadcast.h`, `firmware/core/round_auth.h`) must
reproduce the same fixtures. Change this page, the fixtures, the host modules and the firmware together, and bump
the version byte whenever the layout or the meaning of a field changes.

Version 2 has the layout of version 1; it makes the authentication tag real (see
[Authentication](#authentication), [ADR-0028](adr/0028-hmac-sha256-tag-and-round-number-freshness.md)). The v1
fixtures, [`tests/fixtures/round_broadcast_v1.json`](../tests/fixtures/round_broadcast_v1.json), are kept frozen:
each v2 valid case is its v1 case with version 2 and a real tag, and a v1 broadcast (a node on old firmware) is
rejected as `version`. Terms are as in [`GLOSSARY.md`](../GLOSSARY.md) (transceiver, round, slot, round
broadcast, link report).

A round broadcast is the payload of one ESP-NOW broadcast: a 26-byte **round header**, then one 4-byte **link
report** per other member of the round. The round scheduler's **join frames** (below) use the same header and
carry no link reports. Every multi-byte field is little-endian. With N members the payload
is 26 + 4 × (N − 1) bytes: 54 B at 8 nodes, 142 B at 30, inside ESP-NOW v1's 250 B.

## Round header (26 bytes)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | version | Format version, `2`. A receiver rejects any other value (reason `version`). |
| 1 | 1 | frame type | `1` = round broadcast with link reports, `2` = join request, `3` = join grant ([join frames](#join-frames)), `4` = raw mode request, `5` = round broadcast with raw CSI ([raw mode](#raw-mode)), `8` = maintenance command ([below](#maintenance-command), decoded by its own codec). Other values are rejected (reason `frame_type`). |
| 2 | 2 | round | Round number, u16, wraps at 65535. Each node counts its own, one up per round, never back (the round scheduler); numbers are not shared between nodes, which keeps them fresh (below). |
| 4 | 1 | slot | The slot this broadcast was sent in. |
| 5 | 1 | slots per round | Slots in this round, including the join slot. |
| 6 | 1 | schedule version | u8. Node indices only mean something within one schedule version. |
| 7 | 1 | node index | The sender's index, 0 to 31. Its bit must be set in the membership (reason `membership`). |
| 8 | 4 | membership | u32 bitmap: bit *i* is set when node index *i* is a member of the round. |
| 12 | 6 | node id | The sender's board serial (its base MAC), in transmission order: `10:BD:A3:CC:85:BC` is `10 bd a3 cc 85 bc`. |
| 18 | 8 | authentication tag | The first 8 bytes of HMAC-SHA256 under the kit key ([Authentication](#authentication)). A receiver rejects a broadcast whose tag does not verify (reason `auth`). In v1 this field was zeros and unchecked. |

The node id is not in the spec's header list. It is carried so that a round broadcast names its sender by full
node identity on its own, wherever it came from (a gateway, a recording, the simulator), and so that the
authentication tag covers the identity too. The ESP-NOW source address is not authenticated.

## Link reports (4 bytes each)

There is one link report for every member of the round other than the sender, in ascending node index. The
k-th report is about the link from the k-th such member (the transmitter) to the sender of the round broadcast
(the receiving node). A member the sender never heard still gets a report, with `heard` 0. The payload length
must be exactly 26 + 4 × (members − 1) (reason `length`).

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | score | Quantised score, u8. `0` means no score yet. |
| 1 | 1 | rssi | i8, dBm of the last round broadcast heard from the transmitter. `-128` means none. |
| 2 | 1 | heard / since | High nibble: round broadcasts heard from the transmitter over the last 15 rounds (0 to 15). Low nibble: rounds since it was last heard (0 = this round; 15 = 15 or more, or never). |
| 3 | 1 | flags, slot lateness | Bit 0: baseline warming. Bit 1: possible move. Bit 2: raw mode active. Bits 3 to 7: the transmitter's [slot lateness](#slot-lateness) code (0 = not measured). |

The link reports describe the previous round: each node sends, in its slot, what it measured on the round
broadcasts it heard since its own last slot.

The spec says "heard in the last 16 rounds" in a 4-bit field. Version 1 counts over the last **15** rounds, so
that 15 means "heard every round" exactly instead of saturating.

### Slot lateness

Bits 3 to 7 of byte 3 carry a code *c* (0 to 31) for how far into its slot the transmitter's last round broadcast
started on air, as the receiving node's schedule places that slot: code 0 means not measured (the transmitter was
not heard since the last report, or the sender is not a member yet); code *c* from 1 to 30 means (*c* − 1) × 32 to
*c* × 32 µs; 31 means 960 µs or more. The receiving node takes the start on air as its receive time minus the
frame's airtime minus its own measured send latency (`firmware/core/round_scheduler.h`). The host shows the median
over the receiving nodes as the node's slot lateness (`node_health[].slot_lateness`). These bits were reserved
(sent as 0, ignored) before ticket #14, so a reader that ignores them still decodes v1 correctly; the version
stays 1.

### Score quantisation

The score byte is the **score code** of [ADR-0021](adr/0021-on-node-score-streaming-window-and-score-code.md)
(`csi/score_code.py` on the host, `firmware/core/motion_score.h` on the node): 10 codes per octave. Code `0` is no score yet; code *c* from 1 to 255 stands for
the score 2^((*c* − 1)/10 − 10), from 2⁻¹⁰ ≈ 0.001 (code 1) to 2¹⁵·⁴ ≈ 43,000 (code 255). A score *s* encodes as
floor(10 × (log₂ *s* + 10) + 0.5) + 1, clamped to 1 to 255 (*s* ≤ 0 gives 1), so a decoded score is within
about 3.5 % of the score. `csi/broadcast.py` (`quantise_score`, `dequantise_score`) calls `csi/score_code.py`.

## Authentication

ESP-NOW cannot encrypt broadcasts, so a round broadcast carries its own tag. It gives authenticity, not secrecy:
anyone can read a round broadcast, but only a holder of the kit key can make one that passes. A steady train of
round broadcasts is easy to detect whatever its content.

**Tag.** The tag is the first 8 bytes of HMAC-SHA256(kit key, *m*), where *m* is the whole round broadcast with
its tag bytes 18 to 25 set to zero. It covers every other byte: version, frame type, round number, slot,
membership, node id and every link report, reserved flag bits included. A node signs a broadcast after encoding
it; a receiver decodes the layout first (a malformed one is rejected by its decode reason), then checks the tag.
The node computes HMAC-SHA256 with mbedTLS on the ESP32-C5's SHA accelerator; the host uses Python's `hmac`.

**Kit key.** 16 random bytes shared by every node of a kit and the dashboard host. `python scripts/node.py
keygen` writes it to `data/kit.key` (not in git; it never overwrites a key file); `python scripts/node.py PORT key
data/kit.key` stores it in a node's flash (NVS) and the node restarts. `dashboard.py --node-gateway` reads it from
`data/kit.key` (or `--kit-key PATH`) and refuses to start without it. Keys are compared by their **check value**,
the first 4 bytes of HMAC-SHA256(key, `shadowcast kit key check`) as 8 hex digits: a node answers `node.py PORT
key` with its check, and the dashboard shows the host's in `status.messages`. Neither ever shows the key. A
transceiver without a key only listens; a gateway needs none (it forwards bytes undecoded).

The **test kit key** `000102030405060708090a0b0c0d0e0f` is published: the fixtures and the simulator use it, the
host defaults to it for `--simulate` and `--replay-rounds` (pass `--kit-key` to replay a real kit's recording),
and neither `node.py` nor a node accepts it as a kit key.

**Freshness (replay).** A copied round broadcast keeps a valid tag, so the round number guards against replay.
Every receiver (the host, per board serial; a node, per node id) keeps the last fresh round number of each sender
and when it was heard. A broadcast whose tag verifies is **fresh** if its round number is 1 to 16 + *t* ahead of
that last fresh one (mod 2^16), where *t* is the milliseconds since it; anything else, the same round again, an
older one, or one too far ahead, is a **replay** (reason `replay`) and is not used. The exception is a sender
silent for more than 2 s, with no fresh broadcast: its next one is taken at any round number, because a node that
restarted or renumbered its rounds starts again from wherever it is. The first broadcast heard from a sender is
fresh. On the host, de-duplication runs first, so a second gateway's copy is a `duplicate`, not a replay.

What it does not stop: while a node is silent for more than 2 s, a replay of its own old broadcasts can stand in
for it until it is heard again; and a copy recorded a whole number of round-number wraps ago (55 min at 20 Hz,
27 min at 40 Hz) can pass if it lands just ahead of the live round. A longer counter would close both and needs a
new format version. A node whose round number steps back loses its next 2 s of broadcasts at every receiver; the
round scheduler never steps back, so only a power cycle does that.

## Join frames

The round scheduler (`firmware/core/round_scheduler.h`, [ADR-0027](adr/0027-round-scheduler.md)) sends two more
frame types in the **join slot**, the last slot of every round. Both are the 26-byte round header alone (any other
length is reason `length`), with these meanings:

| Field | Join request (frame type 2) | Join grant (frame type 3) |
|---|---|---|
| sent by | a node that is not a member, in the join slot of a round the scheduler numbers **even** | the schedule's scheduler (the member in slot 0), in the join slot of the next round it numbers **odd** |
| round | the joiner's own round number | the joiner's round number: the request's plus the rounds since |
| slot, slots per round | the join slot, and the slots of the schedule it asks to join | the join slot, and the slots of the schedule before the grant |
| schedule version, membership | of the schedule it asks to join | of the **new** schedule: one version later, with the joiner's index set |
| node index | the index it asks for (the lowest free one; the scheduler may give another). 0 to 31, else reason `membership` | the index given to the joiner; it must be in the membership (reason `membership`) |
| node id | the joining node's board serial | the joining node's board serial (not the scheduler's) |

Join frames are signed like round broadcasts and a forged one is rejected as `auth`. The host counts the others
(`status.broadcasts.join_frames`, every copy) and never places, de-duplicates or checks them for freshness; a
grant tells it the joiner's index in the new schedule version. A node checks join frames for freshness like any
frame (both carry the joiner's round numbers, so they count on with its own) and never takes its timing from one.

## Raw mode

Raw mode streams one link's full CSI for recording and algorithm work (ticket #17,
[ADR-0032](adr/0032-raw-mode-in-the-join-slot-within-a-byte-budget.md)). The host asks the receiving node of the link
with a **raw mode request**; that node then sends **round broadcasts with raw CSI**. Both are signed and checked
like every other frame ([Authentication](#authentication)).

### Round broadcast with raw CSI (frame type 5)

A round broadcast (header with frame type 5, then the link reports, exactly as frame type 1) followed by one or two
**raw CSI blocks**, which fill the rest of the frame exactly (else reason `length`):

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | transmitter index | The node index of the link's transmitter: a member other than the sender (else reason `membership`). |
| 1 | 2 | round | The transmitter's round number of the round broadcast the CSI was measured on. |
| 3 | 1 | rssi | i8, dBm of that round broadcast; `-128` means none. |
| 4 | 1 | count *n* | I/Q bytes that follow: even, 2 to 128 (else reason `length`). |
| 5 | *n* | I/Q | int8 pairs per subcarrier, imaginary then real, exactly as the ESP-IDF CSI callback gave them (`wifi_csi_info_t.buf`), cut to the first 128 bytes: one HT20 long training field, 64 subcarriers, nulls included. No gain compensation is applied. |

The link reports of those links carry the raw mode flag (byte 3, bit 0x04); it is the host's acknowledgement. A node
sends the CSI of the last round broadcast it heard from each raw transmitter since its own last slot, once, and only
what fits its slot (`rs_frame_cap`: the frame's airtime plus the guard within the slot); the rest is dropped and
counted, never sent late. Everything else about the frame (de-duplication, freshness, slot, scheduler) is that of a
round broadcast. The longest such frame is 416 bytes (32 members, two blocks of 128): longer than ESP-NOW v1's 250, so
the node image sends ESP-NOW v2, which splits it over several vendor elements.

### Raw mode request (frame type 4)

The round header, then:

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 26 | 6 | target | The board serial of the node that is to stream (the receiving node of the links). |
| 32 | 2 | target round | The target's own round number, as the host last heard it. |
| 34 | 1 | count *k* | Transmitters named, 0 to 2. `0` turns raw mode off. |
| 35 | 6 × *k* | transmitters | The board serials of the transmitters whose links the target is to stream. |

The length must be exactly 35 + 6 × *k* with *k* ≤ 2 (reason `length`). In the header, the node id is the serial of
the gateway that sends the request and the round number is the host's own count for that gateway (one up per frame,
so every copy is fresh); slot, slots per round, schedule version, node index and membership are 0 and not checked.
Each request carries the target's whole raw set, so copies and repeats change nothing. A node takes a request whose
tag and freshness check out, that names it, and whose target round is its own latest round number or up to 64 rounds
before it (a recorded request replayed later is stale). Raw mode lives in the node's RAM: a restart turns it off.

**Where it is sent.** The gateway sends the request in the **join slot of a round the scheduler numbers even**, the
slot joiners already use: never in a member's slot, never in the odd rounds whose join slot carries grants. It takes
the round's start from slot 0's round broadcast as it hears it, so it is never early, and sends at most one frame per
round. A request is 47 bytes on air (158 µs at HT20 MCS0). The host sends three copies.

**Budget.** A raw link costs at most 133 bytes per round (block head 5 + 128). The host allows at most 400 bytes of raw
CSI per round in the whole rig (three links) and two links per node, and refuses anything beyond with a reason. By the
airtime model of report 03 (HT20 MCS0, ESP-NOW framing), one raw link adds 172 µs to its node's round broadcast at
30 nodes (142 to 275 bytes: 274 to 446 µs); the whole budget adds 516 µs per round, 1.0 % of the channel at 20 Hz (the
30-node rig goes from 16.4 % to 17.5 %) and 2.0 % at 16 nodes and 40 Hz (13.2 % to 15.2 %).
## Maintenance command

Frame type 8 tells chosen nodes to update their firmware or to report it ([ADR-0031](adr/0031-ota-maintenance-network-signed-images.md),
[`docs/ota.md`](ota.md)). The host builds and signs it (`csi/ota.py` `maintenance_command`) and the gateway sends it
(`ota send <hex>` on its USB port), three copies 0.2 s apart; nodes decode it with `firmware/core/ota_core.h`, not
with the round broadcast decoder. The golden cases are
[`tests/fixtures/ota_command.json`](../tests/fixtures/ota_command.json). The round header's slot, slots per round,
schedule version, node index and membership are zero (else reason `field`); the node id is the host's, all zero;
the round is the host's (the low 16 bits of `issued`); the tag is the kit key's like every frame's, and a node
checks it and the round number for freshness before it acts. Then:

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 26 | 1 | command | `1` = update to the image named here, `2` = report only. Else reason `command`. |
| 27 | 1 | targets | Number of target serials, 1 to 13 (else reason `targets`). The length must be 72 + 6 × targets (reason `length`): at most 150 bytes. |
| 28 | 2 | port | HTTP port of the host's image server; 0 is reason `field`. |
| 30 | 1 | network channel | Channel of the maintenance network; 0 = the node scans for it passively. A node ignores a command whose channel overlaps its sensing channel. |
| 31 | 1 | reserved | 0, else reason `field`. |
| 32 | 4 | issued | u32 host time (Unix seconds). A node runs a command only if `issued` is later than the last one it ran. |
| 36 | 4 | image size | Bytes of the signed image file; 0 in an update is reason `field`. |
| 40 | 32 | image SHA-256 | Of the signed image file. A node refuses a download whose SHA-256 differs. Zero in a report command. |
| 72 | 6 × targets | target serials | Board serials, transmission order. A node not named ignores the command. |

A gateway never acts on a maintenance command; a transceiver ignores one while a new image runs its health check.
A second gateway that hears the command passes it to the host, which counts it as rejected `frame_type` (three per
command).

## How the host reads it

- A link is (receiving node, transmitting node), labelled `<receiver><-<transmitter>` with their names in
  `config/room.json`, found by board serial. The receiving node is the round broadcast's node id; the
  transmitting node is the node whose own round broadcasts, in the same schedule version, carry that node
  index. Until the host has heard a node's own round broadcast, reports about it cannot be placed and are
  counted as unplaced.
- A round broadcast with raw CSI is placed like any round broadcast; each raw CSI block is placed on its link the
  same way (`status.broadcasts.raw_csi {blocks, iq_bytes, unplaced}`, and per link in the payload's `raw_mode`). A
  raw mode request another gateway sent is counted in `status.broadcasts.raw_requests` and not used, and an
  authenticated maintenance command (frame type 8) in `status.broadcasts.maintenance_commands`: neither is a
  rejection.
- `heard` 0 makes the link NO_SIGNAL. A warming flag or score 0 makes it WARMUP. Otherwise the score drives
  the link's detector as a packet's score does in the line pipeline.
- Bytes that fail to decode are counted by reason (`short`, `version`, `frame_type`, `length`, `membership`)
  and dropped. Then the tag is checked: one that does not verify is counted as `auth` before anything else
  looks at it, so a forgery cannot take the place of the real broadcast. A round broadcast whose node id is not in
  the room config is counted as an unknown node; one that is stale for its sender as `replay`.
- Several gateways may hear one round broadcast. The host places the first copy of (node id, schedule version,
  round number); any other copy within 2 s of host time, from any gateway, is counted as a `duplicate` and not
  placed again. Two gateways therefore never double-count a link report. A node must not send two different
  round broadcasts with the same round number in one schedule version; the round scheduler plans no round at or
  before the last one it sent in the same schedule version.
- A live round broadcast that arrives while the host's queue is full (the host fell behind) is dropped and
  counted as `queue_overflow`; a recorded one waits. Every reason, unknown nodes and per-gateway counts are in
  the dashboard payload at `status.broadcasts` (`csi/rounds.py`), and unknown nodes, rejections and drops also
  raise a line in `status.messages`.
- What a node gateway lost before the host ever saw it is in `status.broadcasts.gateways {source: {queue_full,
  usb_dropped, csi_skipped, records_rejected {reason: n}, skipped_bytes}}`: the first three from the gateway's
  status record (counted since its boot), the last two from the host's reader of its USB records (a record that
  fails its CRC, for example). Any of them non-zero raises a line in `status.messages` naming the gateway.
