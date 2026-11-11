# 0027. The round scheduler: slot 0 schedules, anyone keeps time, lowest scheduler ID wins

- **Status:** Accepted
- **Date:** 2026-11-11
- **Recorded in:** eberhart441/shadowcast issue #14 (spec #2), branch `ticket/14-scheduler`
- **Related:** [0022](0022-host-time-round-broadcast-seam.md), [0028](0028-hmac-sha256-tag-and-round-number-freshness.md) (its freshness rule shapes the round numbers), [0026](0026-one-node-image-runtime-settings-gateway-records.md) (its fixed round and its `index`/`nodes` settings are superseded here), report 07 §2 (`docs/research/scaling-redo/07-network-architecture.md`)

## Context

Spec #2 wants a node that powers on to join a running round within a few rounds, a node missing for 3 rounds to be
dropped, two groups that could not hear each other to merge with the lower scheduler ID winning, timing from any
heard round broadcast with no timing master, a slot guard that is a setting (250 µs to start), and slot lateness
per node on the dashboard. The fixed round of #13 had none of this: a set member count, node index = slot from a
per-board setting, no join slot. Report 07 §2 brings over the Spin family of radio tomography protocols (Wilson and
Patwari's token passing with timeouts, multi-Spin's TDMA by ID with RSS carried in each packet) and proposes timing
from any frame, a join slot at the end of each round, the lowest-ID node as scheduler with next-lowest failover,
and eviction after 3 rounds.

The round header was fixed by #5 (26 bytes, ADR-0022), the host de-duplicates by (node, schedule version, round
number) and drops a second broadcast with the same key (#7), and #16 owns the authentication tag in parallel. So
the scheduler had to fit the header, not grow it.

## Decision

`firmware/core/round_scheduler.{h,c}`, a pure C11 state machine (heard frames and the device clock in; the next
transmission, node index, membership and schedule version out), behind the calls node_main.c made to the fixed
round.

- **Round layout.** `round_hz` rounds per second (kit-wide setting). One slot per member in ascending node index,
  then the join slot; all slots equal, period / (members + 1). Slot = rank of the node index in the membership, so
  dropping a member compacts the slots without renumbering anyone.
- **Timing from any round broadcast of the node's own schedule.** Start of that round = receive time − airtime −
  this node's own measured send latency − slot × slot length. The first one sets the phase outright, later ones
  move it halfway (as the fixed round). The send latency comes from the ESP-NOW send callback (time sent − airtime − planned slot time), clamped
  to 0..guard, averaged.
- **The scheduler is the member in slot 0; its board serial is the scheduler ID.** Only it admits joiners and drops
  missing members, each time bumping the u8 schedule version. Every other member takes a newer schedule version
  (serial arithmetic) from any round broadcast of a known member, from slot 0, or from a join grant that adds one
  index to its membership.
- **Round numbers are each node's own.** A node numbers its frames with its own count, one up per round, never
  back, across schedule versions, joins and merges; nodes do not share or adopt each other's numbers. The host keys
  everything by sender (de-duplication, freshness), so nothing needs a common number, and #16's freshness rule (a
  broadcast is fresh 1 to 16 + 1/ms rounds ahead of its sender's last, within 2 s) holds by construction. Only a
  power cycle restarts the count, which costs that node up to 2 s at the receivers (as #16 expects).
- **Joining.** A powered-on node listens. Hearing a round broadcast, it syncs to that schedule; once it has heard
  slot 0 it knows the scheduler's numbering and sends a join request (a new frame type 2, the header alone, naming
  its serial and the lowest free index) in the join slot of a round the scheduler numbers even, choosing one of the
  next 2 request slots, doubling up to 16 after each try. The scheduler answers with a join grant (frame type 3: the
  joiner's serial, its index, the new membership and version) in the join slot of the next round it numbers odd.
  Even and odd rounds keep requests and grants from colliding. Both join frames carry the joiner's round numbers
  (the grant: the request's number plus the rounds since), so they stay fresh for the joiner's serial too. A node that hears nothing for 3
  to 7 rounds (random, continuous, so two founders rarely share a phase) founds its own schedule alone in slot 0; a
  lone member that hears nothing for 16 rounds moves its phase at random, in case another lone founder is sending in
  step with it.
- **Dropping.** A member is missing when nobody has heard it for 3 rounds: the scheduler's own hearing, or any
  member's link report saying it heard it (`since` gives when). The scheduler drops missing members as it builds its
  slot 0 broadcast, so the new version goes out at the start of a round. **Failover:** a member all of whose
  earlier members (lower node indices) are missing drops them and is then slot 0.
- **Merging.** A node that hears slot 0 of another schedule whose scheduler ID is lower than its own schedule's
  leaves its schedule and joins that one (through the join slot, like any joiner). Split groups therefore end in the
  lowest-ID schedule; members that cannot hear that scheduler follow once their own scheduler has left.
- **Capacity and guard.** The scheduler admits no more members than fit with slot length ≥ max(1 ms, airtime of a
  full round broadcast + guard); refused requests are counted. A node whose slot timer fires more than the guard
  late skips the slot (counted) instead of running into the next one. `guard_us` is a kit-wide setting, 50 to
  5000, default 250. At 20 Hz the round holds 32 members, at 40 Hz 24.
- **The host contract.** The scheduler never plans a round number at or before the last one it sent, in any
  schedule version, so a node never sends two different round broadcasts with one key.
- **Slot lateness** is measured by every receiving node per transmitter (start on air by the above, minus the start
  of that slot by its own schedule) and carried in bits 3-7 of the link report's flags byte, 32 µs steps, 0 = not
  measured. Those bits were reserved and ignored by v1 readers, so the format version stays 1. The host shows the
  median over receiving nodes in `node_health[].slot_lateness`.
- **Settings v2:** `role`, `channel`, `round_hz`, `guard_us`. `index` and `nodes` are gone; a stored v1 blob keeps
  its role, channel and round rate. A transceiver needs no settings at all and transmits with the defaults (the
  fixed round's "unset transceivers only listen" rule existed only because indices were per-board).

## Alternatives considered

- **The joiner picks its own index; no grant frame.** Ruled out: two joiners that pick the same index in the same
  join slot cannot tell, because neither hears the other's slot. The grant names the joiner's serial.
- **A grant inside the scheduler's round broadcast.** Needs header or trailing bytes; the length rule of v1 rejects
  trailing bytes and #16 is changing the header region in parallel.
- **Every member may drop missing members.** Members with different hearing would bump the version in different
  ways. One scheduler with failover keeps a single writer.
- **The lowest serial in the group is always the scheduler** (report 07's wording). That needs every member to know
  every serial and changes the scheduler whenever a lower serial joins. Slot 0 is known to everyone from the header
  alone; "lowest ID" decides only between schedules (merging).
- **Fixed slot length, round rate falling with members** (`1 / ((N + 1) × slot)`). Closer to multi-Spin, but #13
  made the round rate a setting and the scorer and host are written against a set rate.
- **One round number for the whole round, adopted from what a node hears** (what the fixed round did, and the
  first version of this scheduler). A joiner or a merging group then jumps to another numbering, which #16's
  freshness rule rejects as a replay for 2 s at every receiver.
- **Lateness in a new header field.** A header change and a version bump, colliding with #16's tag work. The
  reserved report bits needed neither.
- **Host-side lateness from gateway receive times.** USB read batching blurs host time by milliseconds; the nodes
  see the slot timing at microsecond resolution.

## Consequences

- Bring-up of many nodes at once goes through one join slot per two rounds, slotted-ALOHA style: 30 nodes switched
  on within half a second share one schedule after about 12 s at 20 Hz in the simulation. A single node joins a
  running round in about 6 rounds (worst of 10 in the simulation; it first listens up to 7 rounds or until it hears
  the round). A node power-cycled within 2 s of its last frame starts a new round count, which every receiver takes
  as a replay until 2 s after that last frame (ADR-0028), so it joins only then.
- Every join or drop bumps the schedule version, so the host relearns indices per version (unplaced reports for
  about one round; a grant tells it the joiner's index at once). Two separate schedules can share a version number
  (1 in 256); the host's index map could then mix them until they merge. A tag (#16) does not change this.
- Partial hearing has limits: a node the scheduler cannot hear cannot join, and a member only some members hear stays
  a member as long as anyone's link reports say it is heard.
- Members of a losing schedule go silent while they rejoin, so a merge costs those links a few seconds.
- The send latency and slot lateness come from the send callback time and the CSI callback time, neither measured
  on a C5 yet; the rig check and the slot-jitter bench test (#19) must confirm them.
- `link_table_restart` now runs for indices that change hands; it resets the scorer but not the heard history.

## Evidence

- PC tests, `firmware/core/tests/test_round_scheduler.c`, over a simulated radio whose receivers apply #16's
  freshness rule (`rb_auth_fresh`) as the node does (per-node clocks at ±20 ppm, 150 to
  250 µs send latency, airtime, half-duplex, collisions, cut links): lone node founds; two nodes settle; a node
  joins a running round of 5 in ≤ 6 rounds (worst of 10) with no member leaving; a dead node is dropped after 3 and
  before 5 rounds; scheduler failover within 8 rounds; unplug and replug restarts nobody and the reports resume;
  two halves of 4 merge into the lower scheduler ID's schedule; every frame carrying a node's serial stays fresh by
  #16's rule through joins, a merge and a failover; 8 nodes for two minutes with ±40 ppm crystals lose
  no broadcast to overlap; 30 nodes switched on together fill a full round at 20 Hz and then every link is heard
  every round for 10 s (settled after 12.3 s); a full 40 Hz round refuses two more nodes.
- Nothing here has run on hardware: the rig check (`docs/rig-checks/round-scheduler.md`) is open.
