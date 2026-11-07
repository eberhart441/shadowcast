# Multi-channel + sequenced (TDMA) transmission: proposal and estimate

Status: **design proposal, not implemented**. Written from reading the vendored firmware
(`firmware/csi_send/main/app_main.c`, `firmware/csi_recv/main/app_main.c`) and the measured
numbers in `README.md`. No hardware access from this session: the numbers below are
order-of-magnitude engineering estimates from 802.11 timing parameters, not measurements.
Treat every figure here as "worth testing," not "proven."

## The problem, restated precisely

Today, every sender is a free-running loop with no coordination:

```c
for (uint32_t count = 0; ; ++count) {
    esp_now_send(peer.peer_addr, (const uint8_t *)&count, sizeof(count));
    usleep(1000 * 1000 / CONFIG_SEND_FREQUENCY);
}
```

Collisions are purely probabilistic (whatever randomized CSMA/CA backoff the Wi-Fi driver
does per transmission), and all senders share one HT40 channel. Measured (`README.md`):

| Senders | Rate | Loss |
|---|---|---|
| 2 | 100 Hz | 65% |
| 2 | 40 Hz | 5-10% |
| 4 (final rig) | 40 Hz | ~5% |

## Why the loss is larger than naive airtime math predicts

A rough airtime budget for one CSI probe frame at HT40, MCS0, long guard interval:

- HT-mixed preamble (L-STF/L-LTF/L-SIG/HT-SIG/HT-STF/HT-LTF, 1 spatial stream): ~36 µs
- Payload (ESP-NOW broadcast, tiny: a 4-byte counter plus MAC/vendor-IE overhead, call it
  ~60 bytes = 480 bits) at ~13.5 Mbps: ~36 µs
- **PPDU airtime: roughly 70-90 µs**
- Plus CSMA channel access (DIFS ~28 µs + average random backoff over CWmin=15, ~67.5 µs):
  **~95-100 µs**
- **Total cost per transmission attempt: very roughly ~175 µs**

At 2 senders x 100 Hz = 200 attempts/s, that is only ~35 ms of the second "spent," i.e. ~3.5%
nominal channel utilization — which should not, by itself, produce 65% loss under textbook
slotted-ALOHA-style collision math. **This is a flag, not a resolved number**: it suggests the
real loss mechanism is not simple occasional random collision, but likely *correlated/drifting
phase* between two free-running `usleep()` loops — if two boards' clocks hold a near-constant
phase relationship for a while (plausible for nearby crystal frequencies with no dithering),
they can collide on every cycle for a sustained stretch rather than only occasionally. This
would explain sustained high loss at a nominal-3.5%-utilization channel far better than
independent random backoff would. **This should be confirmed with an actual RF capture
(e.g. a sniffer alongside the rig) before trusting the TDMA ceiling below as achievable** —
if it's phase drift, TDMA (deterministic slots) fixes it directly; if it's something else
(e.g. hidden-node-style asymmetry, or non-randomized backoff in this driver path), the fix
needs to target that instead.

## The proposal: sequenced (TDMA) slots, split across channels

**Sequencing ("token passing").** One always-on node (a receiver — already USB-powered and
always listening) broadcasts a short periodic sync marker. Every sender is assigned a fixed
offset after that marker and transmits only in its own slot:

```
beacon ---[slot 0: tx0]---[slot 1: tx1]---[slot 2: tx2]---...---[slot N-1]--- (repeat)
```

This removes contention entirely among scheduled senders — no backoff, no randomness, no
dependence on independent clocks staying in some lucky phase relationship.

**Multi-channel.** Independently, split senders across 2-3 non-overlapping 2.4 GHz channels
(this needs `CONFIG_WIFI_2G_BANDWIDTHS` dropped from `WIFI_BW_HT40` to `WIFI_BW_HT20` first —
HT40 already consumes most of the non-overlapping 2.4 GHz space). Each channel runs its own
independent TDMA schedule in parallel. A receiver only hears senders on its own tuned channel,
so receiver count must scale with channel-group count, not just node count.

This combination (multi-channel + token-passing TDMA) is not a novel idea — it is exactly
the architecture of the original Radio Tomographic Imaging testbeds (Wilson & Patwari, Utah):
"the RF sensors run a multi-channel TDMA communication mesh network... through a multichannel
TDMA token-passing protocol." See the sources from the earlier research pass. This design
matches established practice in this exact sub-field, not a guess.

## Estimated improvement (order of magnitude, needs hardware validation)

Slot budget per channel, targeting 40 Hz per sender (25 ms repeat period):

- Per-slot cost: ~70-90 µs PPDU + a guard band. Boards have no hardware clock sync, so the
  guard needs to absorb `usleep()`/FreeRTOS scheduling jitter, not crystal drift (crystal drift
  over 25 ms is sub-microsecond at typical tolerances — jitter from software timing is the
  real budget item here). Assume a conservative ~70-100 µs guard: **~150-200 µs per slot.**
- Slots available in 25 ms: `25,000 µs / ~175 µs ≈ 140`.
- **That implies on the order of 100+ senders at 40 Hz on a single channel under ideal TDMA**
  — before accounting for real-world scheduling jitter, sync-beacon overhead, and the
  phase-drift question above. Treat 100+ as a generous upper bound, not a target: a realistic
  first pilot should aim much lower (8-16 senders per channel) with wide guard bands, then
  tighten once real jitter is measured.

Compare to today: **CSMA contention collapses to unacceptable loss somewhere between 2 and 4
senders already**, and the measured 65%-loss case (2 senders, 100 Hz) suggests the practical
ceiling is closer to "a handful" than "dozens" under the current scheme. The gap between
"a handful" (measured, contention-bound) and "~100+" (estimated, schedule-bound) is the
headroom TDMA is expected to recover — the current ceiling looks like an artifact of
*how* senders access the channel, not a hard limit on *how many* could share it.

With 2-3 channels (via HT20) multiplying that per-channel capacity roughly linearly (each
channel is its own independent TDMA domain), the combined estimate is:

```
total practical senders ≈ (channels) x (senders per channel, conservative pilot: 8-16)
                        ≈ 2-3 x 8-16 ≈ 16-48
```

against a current practical ceiling around 2-4. **This is the headline number, and it is an
estimate to go test, not a commitment.**

## What this does not change

- Receiver count still must scale with channel-group count (a receiver hears only its own
  channel) — this doesn't remove the "one USB port per receiver" constraint discussed
  separately (`docs/agents/hardware.md`); wireless receiver forwarding is still the fix for that.
- This proposal is pure firmware/scheduling work: no changes needed in `csi/` (the Python
  side already treats packet loss as "gaps in device timestamps," which continues to work
  whether loss comes from collisions or anything else).

## Before building this

1. Capture real RF airtime/collision behavior (sniffer, or at minimum vary `CONFIG_SEND_FREQUENCY`
   across more data points than the two already measured) to confirm the phase-drift hypothesis
   above — it changes whether TDMA alone is sufficient or whether something else is also needed.
2. Prototype the sync-beacon + slot assignment with the *existing* 2-sender rig first (cheap,
   no new boards needed) and measure loss at 100 Hz with TDMA vs. the already-measured 65% CSMA
   baseline — a clean, directly comparable experiment.
3. Only then attempt the multi-channel split, since it multiplies whatever the single-channel
   TDMA result turns out to be.
