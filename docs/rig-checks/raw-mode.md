# Rig check: raw mode on one link of a two-node rig (ticket #17)

Raw mode ([ADR-0032](../adr/0032-raw-mode-in-the-join-slot-within-a-byte-budget.md),
[`round-broadcast.md`](../round-broadcast.md#raw-mode)) streams one link's full CSI. The host signs a raw mode request,
the gateway sends it in the join slot, and the receiving node appends the I/Q of that link to its round broadcasts,
which the round recording keeps. This check runs it on real boards. It passes when raw mode on one link of a
2-node rig records CSI without disturbing the other link's packet rate.

**Result:** not yet run. Record the result at the end of this page and tick the ticket's rig-check box.

## You need

- Two Seeed XIAO ESP32-C5 transceivers (A and B) and one gateway, all flashed with the same `firmware/node` image
  built from this branch ([`two-nodes.md`](two-nodes.md) steps 1 and 2 for building and flashing).
- The kit key on both transceivers ([`authentication.md`](authentication.md)). The gateway needs none.
- The gateway set with `python scripts/node.py <PORT> set role=gateway`; the transceivers on the defaults.
- A room file with A and B and their serials (copy `config/room_tr2.json`; `python scripts/ports.py` lists serials).
  Below, A is `n1` and B is `n2`, so the links are `n1<-n2` and `n2<-n1`.
- Steady power for both transceivers.

## 1. The rig without raw mode

Run `python scripts/dashboard.py --node-gateway <GATEWAY PORT> --room <room file> --record data/raw-check.rounds`
and open the page. Wait until both links are EMPTY.

- Note the **link rate** of each link (node table, or `pkt_rate` of `GET /api/state` links): expect about 40/s at
  the default 40 Hz. Write both down: they are the baseline.
- The boot log of each board (`python scripts/node.py <PORT> status` before starting the dashboard, or the gateway's
  text records) must not show `ESP-NOW v1`: the image needs ESP-NOW v2 for frames over 250 bytes.

## 2. Raw mode on n1<-n2

Click the cell row n1, column n2 in the "Links by node pair" matrix, then "Stream raw CSI" (or run
`curl -X POST -H "Content-Type: application/json" -d "{\"link\": \"n1<-n2\", \"on\": true}" http://127.0.0.1:8000/api/raw-mode`).

- The reply / page says the request went out via `gateway`, 3 copies. `status.messages` shows
  `command: raw mode on n1<-n2 (sent via gateway, 3 copies)`.
- Within about a second the raw mode list under the link detail shows `n1<-n2 on`, its CSI blocks counting up at
  about the round rate (40/s). If it says `no answer` after 3 s: the request did not reach A. Check that the gateway's
  status `sent` counter grew by 3 (`python scripts/node.py` cannot share the port with the dashboard; the dashboard's
  `GET /api/state` has no gateway counters, so stop the dashboard and run `python scripts/node.py <GATEWAY PORT>
  status --seconds 5` if needed) and that A logged `raw mode: 1 link(s)`.
- `status.broadcasts.raw_csi.blocks` grows; `rejected` stays 0 for every reason except perhaps a few `stale`.
- **The check:** over one minute, the link rate of `n2<-n1` (the other link) stays within 2/s of its baseline from
  step 1, and `n1<-n2` too. Write down both rates.
- Let it run 30 s, then press "Stop raw CSI" (or the same curl with `false`). The list shows `stopping`, then `off`;
  the block count stops growing.

## 3. The recording

Stop the dashboard (Ctrl+C). Then:

```
python -c "from csi.round_recording import load_rounds; from csi.round_auth import load_kit_key; r = load_rounds('data/raw-check.rounds'); s = r.raw_csi(load_kit_key('data/kit.key')); print(len(s), {x.link for x in s}, len(s[0].csi.iq), s[0].csi.values[:8])"
```

- It prints the number of raw CSI blocks (about 40 per second of raw mode), the set `{'n1<-n2'}`, the I/Q length
  (expect 114 or 128: write down which: it is the C5's HT20 CSI length, never seen before), and the first values
  (not all zero).
- `python scripts/dashboard.py --replay-rounds data/raw-check.rounds --kit-key data/kit.key --speed max` shows the
  same block count in the raw mode list and in `status.broadcasts.raw_csi`.

## 4. Optional: the budget and the node limit

With the dashboard running again, ask for `n1<-n2` and `n2<-n1`: both go `on`. On a 2-node rig the budget cannot be
exceeded (two links); the refusal (`409`, reason `budget` or `node_limit`) is covered by the tests.

## Record

| Date | Image (commit) | Baselines n1<-n2 / n2<-n1 (/s) | With raw mode (/s) | Blocks recorded | I/Q length | Pass |
|---|---|---|---|---|---|---|
| | | | | | | |
