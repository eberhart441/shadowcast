# Rig check: two nodes and a gateway (ticket #13)

The first hardware run of the transceiver rig of spec #2: two transceivers run a fixed two-slot round on one
2.4 GHz HT20 channel, score each other's round broadcasts on the board, and a listen-only gateway hands every round
broadcast to the dashboard. The check passes when both links show on the dashboard and react to someone walking
between the two transceivers.

All three boards run the same image (`firmware/node`); what each one does is a runtime setting, set with
`scripts/node.py`. Nothing in this procedure needs a per-board build.

**Result:** not yet run. Record the result at the end of this page and tick the ticket's rig-check box.

## You need

- Three Seeed XIAO ESP32-C5 boards with their FPC antennas fitted (pressed until they click) and fixed flat, so
  they cannot move: a moving antenna reads as motion.
- One data USB cable per board for the setup steps; afterwards only the gateway stays on the dashboard host.
- Steady power for the two transceivers: USB chargers, or a battery that does not cut out. Not pass-through-charging
  power banks (the hackathon's 5-board runs ended on those).
- ESP-IDF 6.0 to build the image, or the `node-image-esp32c5` artifact of the CI job `node-image`.
- A free room about 3 m long, and a second person or a tripod-free walking path between the boards.

## 1. Build the image once

```
cd firmware/node
idf.py -B build set-target esp32c5
idf.py -B build build
```

On Windows build in a short path (the worktree root is fine; the session scratchpad is too long). The same
`build` directory flashes every board.

## 2. Flash and set each board

One board at a time, so the port is never a guess:

1. Plug in the board and find its port and serial: `python scripts/ports.py`, or
   `python -m serial.tools.list_ports -v` (the `SER=` value is the board serial). Neither opens the port.
2. Flash it from inside the build directory:
   `esptool --chip esp32c5 -p <PORT> -b 460800 write-flash @flash_args`.
3. Set it. Both transceivers get the same kit-wide values (channel, round_hz); the round scheduler (#14) gives
   out the node indices, so there is no index to set:

   | Board | Command |
   |---|---|
   | transceiver A | `python scripts/node.py <PORT> set role=transceiver channel=6 round_hz=40` |
   | transceiver B | `python scripts/node.py <PORT> set role=transceiver channel=6 round_hz=40` |
   | gateway | `python scripts/node.py <PORT> set role=gateway channel=6` |

   Expect `ok role=... (saved; restarting)`.
   Then give both transceivers the kit key (format v2, [`docs/round-broadcast.md`](../round-broadcast.md)
   "Kit key"): once per kit `python scripts/node.py keygen` (writes `data/kit.key`), then for each transceiver
   `python scripts/node.py <PORT> key data/kit.key`. Expect `ok key check=<8 hex> (saved; restarting)`; both must
   show the same check. The gateway needs no key. Then `python scripts/node.py <PORT> show` must print the board's
   serial and the settings, without `configured=no`.
4. Write the transceivers' serials into `config/room_tr2.json` (`n1` and `n2`; which board is which does not
   matter, the dashboard learns each node's index from its own round broadcasts). Leave their positions as the
   real ones, in metres.

Channel 6 is the default. If a survey shows another 2.4 GHz channel is quieter, use it on all three boards.

## 3. Check each transceiver alone (optional, but it isolates faults)

With transceiver A on USB and B powered elsewhere, run `python scripts/node.py <PORT A> status --seconds 5`. Each
second a status line prints:

- `sent` grows by about 40 per second (one round broadcast per round at 40 Hz);
- `heard` grows by about 40 per second once B is powered (B's round broadcasts);
- `rejected` stays near 0 (ESP-NOW frames that are not v2 round broadcasts, such as an old hackathon sender switched to HT20, a board with another kit key);
- `csi_skipped` grows for the first 20 frames (the CSI layout warm-up), then stays put. If it keeps growing, the
  CSI layout is not one the node can score: note the `W`/`E` lines and stop here;
- `queue_full`, `send_errors` stay at 0.

Repeat for B. Then unplug both from the host and power them from their chargers.

## 4. Run the dashboard on the gateway

1. Place the two transceivers about 3 m apart at the positions in `config/room_tr2.json`, antennas facing each other,
   at the same height (a table or a chair is fine). Put the gateway on the dashboard host at least 1 m from both.
2. Start the dashboard: `python scripts/dashboard.py --node-gateway <GATEWAY PORT> --room config/room_tr2.json`.
3. Open the page. Expect two links, `n1<-n2` and `n2<-n1`. Each starts in WARMUP (the scorer's 1 s window, then
   about 10 s of auto-mode baseline) and then reads EMPTY in a still room.
4. Read the link cards: the packet rate should be close to 40 per second on each link, with few drops. The status
   counters at `GET /api/state` under `status.broadcasts` should show `accepted` growing by about 80 per second,
   no `unknown_nodes` (a serial there is missing from `config/room_tr2.json`) and few `rejected`.

## 5. The check

1. Leave the room still for 30 s. Both links should stay EMPTY.
2. Walk slowly back and forth across the line between the two transceivers for 30 s, about 1 m to each side.
   Both links must go to MOTION within about a second of the walk starting, and the room state to MOTION.
3. Stop outside the room. Both links must return to EMPTY.
4. Repeat once, walking parallel to the line 1.5 m away: expect a weaker reaction, which is the expected falloff,
   not a failure.

Pass: both links show, run near 40 per second, and react in step 2. Write down the packet rates, the drops, the
RSSI on each link card, and anything odd.

## If something is wrong

| Symptom | Look at |
|---|---|
| No links at all | `GET /api/state` `status.broadcasts`: `accepted` 0 and `unknown_nodes` set means the serials in `config/room_tr2.json` are wrong. Nothing at all: is the gateway set to `role=gateway` on the same channel (`node.py show`, with the dashboard stopped)? |
| One link only | The silent transceiver: `node.py <PORT> status` with it on USB. `sent` 0 means it is not configured (`configured=no`). |
| Both links NO_SIGNAL after a few seconds | The transceivers stopped hearing each other. If it starts about 2.5 s after boot it fits the forced gain lock (ticket #15 replaces it): note it, and the RSSI. |
| Packet rate well below 40 | `queue_full` or `send_errors` growing on a transceiver, or `status.broadcasts.rejected` on the host. |
| Links stay in WARMUP | `csi_skipped` growing on a transceiver: its CSI layout is not one it can score. |

The gateway's own counters (`heard`, `queue_full`, `usb_dropped`) arrive as status records. The dashboard keeps the
latest one on its source; with the dashboard stopped, `python scripts/node.py <GATEWAY PORT> status` prints them.

## Results

_Not run yet._ When it is: date, boards (serials), positions, channel, packet rate and drops per link, RSSI per
link, whether both links reacted, and anything odd.
