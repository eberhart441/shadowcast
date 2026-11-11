# Rig check: nodes join, drop out and come back on their own (ticket #14)

The round scheduler (`firmware/core/round_scheduler.h`, [ADR-0027](../adr/0027-round-scheduler.md)) gives every
transceiver its node index and slot with no per-board setting. This check runs it on real boards. It passes when,
with three or more transceivers running, unplugging one and plugging it back in restarts none of the others and
its link reports resume.

**Result:** not yet run. Record the result at the end of this page and tick the ticket's rig-check box.

## You need

- Three or more Seeed XIAO ESP32-C5 transceivers and one gateway, all flashed with the same `firmware/node` image
  built from this branch (see [`two-nodes.md`](two-nodes.md) steps 1 and 2 for building and flashing).
- The kit key on every transceiver ([`authentication.md`](authentication.md); `python scripts/node.py keygen` once,
  then `python scripts/node.py <PORT> key data/kit.key` per transceiver). A transceiver without it only listens.
- The gateway set with `python scripts/node.py <PORT> set role=gateway`. The transceivers need no `set` at all:
  the defaults are channel 6, 40 rounds per second, a 250 µs slot guard. If you change one of those, give every
  board the same value.
- Steady power for every transceiver (USB chargers, not pass-through-charging power banks).
- `config/room_tr2.json` copied to a room file with one entry per transceiver and its serial (`scripts/ports.py`
  lists the serials), positions in metres.

Boards flashed with the image of ticket #13 keep their role, channel and round rate; their `index` and `nodes`
settings are ignored now.

## 1. One board on its own

With transceiver A alone on USB (the others off), run `python scripts/node.py <PORT A> status --seconds 10`.

- Within about 0.2 s of boot a text line `round: member, schedule <v>, index 0, membership 00000001` appears: A
  heard nobody and founded its own schedule.
- `sent` grows by about 40 per second.

## 2. Bring the round up

Power the other transceivers one at a time, a few seconds apart, with A still on USB and `status` running.

- Each new board appears in A's log as a new `membership` with one more bit and a new schedule version, within a
  fraction of a second of it booting (expect about 6 rounds, 0.15 s at 40 Hz).
- A's own index never changes, and its `joins` count stays 1.
- `heard` on A grows by about 40 per second per other board.

Then start the dashboard: `python scripts/dashboard.py --node-gateway <GATEWAY PORT> --room <your room file>`.
Every link appears and settles to EMPTY; `node_health` (the node table) shows every node `ok`, heard by all the
others, and a **slot lateness** number for each (expect a few hundred µs, about the same for every node; write
the numbers down: they are the first measurement of the slot jitter for #19).

## 3. The check: unplug one and plug it back in

1. With the room still, note every node's state and the link rates on the page.
2. Unplug one transceiver that is not A (call it C). Within about 0.1 s (3 rounds) it is dropped: A logs a new
   membership without C's bit. On the page C goes `missing` after 2 s and its links NO_SIGNAL; coverage reads
   degraded. Every other link keeps its rate.
3. Wait at least 3 s, then plug C back in. Within about 1 s of it booting it is a member again (A logs its bit
   back), and within a few seconds its links leave NO_SIGNAL: the link reports about it resume, then WARMUP, then
   EMPTY.
4. Pass when, through steps 2 and 3, **no other node restarted or left the round**: A's log shows no `joining` or
   `listening` line and its `joins` count stays 1, the other nodes keep their indices (A's membership changes only
   in C's bit), and no other link went NO_SIGNAL.
5. Repeat once with A itself (the scheduler, if it founded the round): unplug it, watch the next node take over
   (the round keeps running on the page, A's links go NO_SIGNAL), plug A back in after 3 s and see it rejoin as an
   ordinary member.

A board plugged back in less than 2 s after it was pulled starts a new round count, which every receiver takes as
a replay (#16) until 2 s after its last broadcast; it then joins as above. That is expected, not a failure.

## If something is wrong

| Symptom | Look at |
|---|---|
| A board never becomes a member | `node.py status`: no kit key (the nag line says so), another channel or round rate than the others, or the round is full (40 Hz holds 24 members, 20 Hz 32). |
| Members keep joining and leaving | `late` in the `round:` log lines growing: the node task is too slow for the guard; raise `guard_us` on every board. `status.broadcasts.rejected.replay` growing on the host: round numbers stepping back (report it with the log). |
| Two schedules side by side | Two `round:` founders that never hear each other: check they are on the same channel. Split groups merge into the one whose scheduler has the lower serial once they hear each other. |
| Slot lateness far apart between nodes | A node whose broadcasts start late (busy channel, a slow board): note it for #19. |

## Results

_Not run yet._ When it is: date, boards (serials), channel and round rate, how long each join took, the slot
lateness per node, what happened in steps 3 and 5, and anything odd.
