# 01 · Firmware forensics: why the rig never scaled past 4 nodes

Scope: the ESP32 firmware only (`firmware/csi_send`, `firmware/csi_recv` and the unmerged transceiver
and turn-taking variants). Host pipeline, RF airtime and history are covered by the other reports;
where this one touches them it says so.

Conventions:
- **[E] Established**: read in code, config, commit messages or vendor docs. Cited by `branch:path:line`,
  commit hash or URL.
- **[I] Inferred**: my estimate. The arithmetic is shown.
- Unless a branch is named, code citations are to `feat/wireless-receivers` (`8ed6488`), whose firmware
  matches `origin/main` (the wireless receivers merged as PR #34, `c0ad162`).
- ESP-IDF citations point to the local ESP-IDF v6.0 tree the boards were built with
  (`C:/esp/v6.0/esp-idf`; `firmware/csi_recv/build_c5/project_description.json` gives `"git_revision": "v6.0"`),
  plus public docs.

---

## TL;DR: bottlenecks ranked by how hard they cap node count

| # | Bottleneck | Cap it implies | Confidence |
|---|---|---|---|
| 1 | **Wired receiver: one ~850-byte ASCII line per packet, printed with blocking `ets_printf` inside the Wi-Fi task**, which also drives UART0 at 921600 baud | about 108 lines/s per receiver, so **at most 2 senders at 40 Hz**; a third saturates every receiver | High |
| 2 | **Wireless forwarding sends every CSI sample as an ~870-byte unicast ESP-NOW frame on the sensing channel** at HT40 MCS0, about 0.6-0.75 ms of airtime each (10x a probe). Load grows as receivers x senders. | roughly R x S of 12-20 at 38.5 Hz before the channel is about half full (estimate) | Medium |
| 3 | **Forced RX gain**: one global gain, locked at packet 100 from the median of all senders, applied to every frame the radio receives, including ACKs | not a fixed number. Each extra transmitter at a different distance widens the range one gain must cover. It already caused NO_MEM next to the gateway and deaf transceiver nodes. | Medium-high as the cause of the observed failures |
| 4 | **Forwarder pacing**: one send in flight with a 1-tick wait (the tick is 10 ms on receivers), 16-deep queue, default 32 dynamic TX buffers | about 100 forwards/s per receiver if send-done callbacks are lost (2.6 senders); late callbacks put several sends in flight and lead to `ESP_ERR_ESPNOW_NO_MEM` | Medium |
| 5 | **Gateway**: one radio and one USB-Serial-JTAG output for all R x S x 38.5 lines/s, 16-item queue, 100 ms write timeout | 134 KB/s at 2x2 and 536 KB/s at 4x4. USJ throughput has never been measured. | Low-medium |
| 6 | **Uncoordinated senders** (free-running `usleep` loop, broadcast, no ACK or retry) | Real collision loss, but the main evidence for it (65% loss at 100 Hz) is confounded by bottleneck #1 | Medium |

The key cross-cutting finding: **the team's main capacity evidence ("two senders at 100 Hz lose 65%",
ADR 0003) was measured while every receiver also printed a per-packet gain log line. At 2 x 90.9 packets/s
that offered about 182 lines/s against a receiver that can print about 100/s.** At least about 45% loss
follows from the receiver's own serial output before any RF collision is counted (§2.1). The turn-taking
(TDMA) and multi-channel work targeted the air. The binding constraint was the per-receiver output path,
which TDMA does not change.

---

## 1. Per-packet work on a receiver

### 1.1 What the code does [E]

The CSI callback `wifi_csi_rx_cb` (`firmware/csi_recv/main/app_main.c:244-329`) is registered with
`esp_wifi_set_csi_rx_cb` (`:427`), with promiscuous mode on (`:380`). ESP-IDF documents that this callback
"runs from Wi-Fi task. So, do not do lengthy operations in the callback function. Instead, post necessary
data to a queue and handle it from a lower priority task"
(`docs/en/api-guides/wifi-driver/wifi-vendor-features.rst:230`;
https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html).

For every frame from a `1a:00:00:00:00:xx` source (`:251`), the callback:
1. Calls the gain library: get gain, record it for the first 100 packets, then force the gain at packet 100,
   and compute a compensation factor (`:260-276`).
2. Formats the line. **Wired mode:** `OUT(...)` is `ets_printf` (`:241`): one call for the header fields
   (`:289-292`), **one `ets_printf(",%d", ...)` per CSI value** (`:319-322`), and a closing `]"\n` (`:324`).
   That is 236 ROM printf calls per packet, each computing `(int16_t)(compensate_gain * buf[i])` in float.
   The ESP32-C5 is single-core with no FPU (`components/soc/esp32c5/include/soc/soc_caps.h:168`
   `SOC_CPU_CORES_NUM 1`; there is no `SOC_CPU_HAS_FPU`), so these are software float multiplies.
   **Forward mode** (`CSI_FORWARD=1`): `OUT` is `snprintf` into a static 1400-byte buffer (`:176-178`),
   called the same 236 times. Then `forward_line()` copies the line twice: into a static item, then into
   a FreeRTOS queue (`:195-208`).
3. All of this runs **in the Wi-Fi task**. Nothing is deferred to another task in either mode.

### 1.2 Bytes per line

- [E] The C5 layout has 234 raw ints (117 subcarriers) per packet (`csi/parser.py:38`). The team's own
  figure is "about 0.85 KB on a C5" (`origin/fix/scaling-and-accuracy:firmware/README.md:94`; `404aa8d`
  commit message; `feat/transceiver-5plus:firmware/csi_transceiver/main/app_main.c:98-100`).
- [I] A check from first principles: the header `CSI_DATA,<seq>,<mac>,<rssi>,...,<len>,<first_word>,"[`
  is about 73 characters. The values are int8 numbers of 1-2 digits, about half of them negative, which
  averages roughly 3.2 characters with the comma. 234 x 3.2 + 73 + 4 ≈ 830 B. So **~850 B wired**, and
  **~870 B forwarded** with the `RX,<serial>,` prefix (21 B, `:287`). ROM printf turns `\n` into
  `\r\n`, adding 1 B on the wire.
- [E] Until `867eea3` (2026-11-08) the receiver also printed `ESP_LOGI(... "compensate_gain %f, agc_gain %d,
  fft_gain %d")` for every packet (`f076006:firmware/csi_recv/main/app_main.c:167`). The commit message
  says: "That floor is the receivers' serial output: 140 lines/s each, half of them the per-packet
  compensate_gain log line." [I] That log line is about 75 B, so about 925 B per packet before `867eea3`.

### 1.3 Where it blocks [E + I]

- [E] The console is UART0 at 921600 baud. The USB-Serial-JTAG (USJ) port is the *secondary* console
  (`firmware/csi_recv/sdkconfig.defaults:5-6`; the generated
  `firmware/csi_recv/build_c5/config/sdkconfig.h` has `CONFIG_ESP_CONSOLE_UART_CUSTOM 1`,
  `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG 1` and `CONFIG_ESP_CONSOLE_UART_BAUDRATE 921600`;
  the TX pin defaults to U0TXD, `components/esp_system/port/cpu_start.c:839-841`).
- [E] The host reads the boards over **USJ**, not a UART bridge. Each board's "serial" is its base MAC
  (`config/room.json`, e.g. `"serial": "10:BD:A3:CC:85:BC"`), and that is the USJ descriptor. The docs
  also mention resets from "opening a native-USB port" (`docs/agents/hardware.md:9`).
- [E] IDF installs the ROM printf with **both** outputs enabled: `g_uart_print = true; g_usb_print = true;
  ets_install_uart_printf();` (`components/esp_rom/patches/esp_rom_sys.c:72-87`, called from
  `cpu_start.c:260-262`). The ROM UART character output "wait[s] until fifo not full"
  (`components/esp_rom/esp32c5/include/esp32c5/rom/uart.h:201-208`). The UART FIFO is 128 B
  (`soc_caps.h:542`).
- [I] So every `ets_printf` byte is busy-waited into UART0 at 921600 baud even though nobody listens on
  UART0, and it is also pushed to USJ. That the CSI lines arrive on the USJ port at all confirms the
  dual output. The UART is the slower of the two, so it sets the pace:
  **851 B x 10 bit / 921600 = 9.2 ms per line, about 108 lines/s.** This matches the team's
  "~110 lines/s" (`404aa8d`).
- [I] The C5 has one core, and the Wi-Fi task is spinning in `ets_printf` for those 9.2 ms. At 2 senders
  (77 lines/s) that is **~71% of the core blocked inside the Wi-Fi task**. Frames arriving meanwhile
  wait in the RX buffers (10 static + 128 dynamic, from `build_c5/config/sdkconfig.h`:
  `CONFIG_ESP_WIFI_STATIC_RX_BUFFER_NUM 10`, `..._DYNAMIC_RX_BUFFER_NUM 128`).
- [E] The transceiver work measured the same effect: printing a line takes "on the order of 15-20 ms for a
  ~234-subcarrier line. Doing that inline in the CSI RX callback stole enough CPU time from the
  WiFi/ESP-NOW stack to starve this node's own sends (ESP_ERR_ESPNOW_NO_MEM, seen on real hardware)"
  (`feat/transceiver-5plus:firmware/csi_transceiver/main/app_main.c:329-336`).
  [I] 15-20 ms is longer than the 9.2 ms UART floor. That would fit the ROM's USJ path also stalling
  while it waits for the host to poll each 64-byte packet, but this is not measured.
- [I] Forward mode avoids the UART. The cost is ~236 `snprintf("%d")` calls plus 234 soft-float
  multiplies, which I estimate at roughly 0.5-1 ms per line in the Wi-Fi task: 10-20x cheaper than the
  wired path. This was not measured on the board.

---

## 2. Output bandwidth ceilings

### 2.1 Wired receiver [I, from §1.3]

Lines per second at one receiver = S x 38.5. The real rate at a nominal 40 Hz is 38.5/s, because
`usleep()` adds a 1 ms tick to each period (`origin/fix/scaling-and-accuracy:firmware/csi_send/main/app_main.c:51-52`).
The gateway docs expect the same 38.5 (`docs/agents/hardware.md:27`).

| Senders at 40 Hz | Lines/s per receiver | UART load at 108/s | Before `867eea3` (≈925 B/pkt, ~99/s cap) |
|---|---|---|---|
| 1 | 38.5 | 36% | 39% |
| 2 | 77 | **71%** | **78%** |
| 3 | 115.5 | **107%: saturated** | 117% |
| 4 | 154 | 143% | 156% |
| 2 at 100 Hz (90.9 real) | 182 | 168% | **184%: at least ~45% loss** |

- **A wired C5 receiver saturates at the 3rd sender at 40 Hz.** The team reached the same conclusion
  on the unmerged branch: "plan for at most about 80 packets/s per receiver"
  (`origin/fix/scaling-and-accuracy:firmware/README.md:92-104`), which is why that branch made 20 Hz
  the default.
- **The "two senders at 100 Hz lose 65%" figure** (`docs/adr/0003-one-shared-channel-at-40-hz.md:34`)
  predates `867eea3`. With roughly 99 lines/s of capacity against 182 offered, 1 - 99/182 ≈ 46% of the loss
  comes from receiver output alone. RX-buffer overflow while the Wi-Fi task is pinned plausibly adds the
  rest. Issue #16 noted that 65% "is more than naive channel-utilisation arithmetic predicts" (ADR 0003:21;
  `design/multichannel-tdma:docs/design/multichannel-tdma.md`, "Why the loss is larger..."). **The
  firmware explains that gap without phase-locked collisions.** Pass this to the RF agent: the 100 Hz
  data point should not be used as a collision measurement.
- An S3 receiver prints longer lines (it uses the 25-column non-C5 header, `:296-308`, and a longer CSI
  buffer). The team suggests keeping it near 55/s (`origin/fix/scaling-and-accuracy:firmware/README.md:94`).

### 2.2 Forwarding receiver (`CSI_FORWARD=1`)

- [E] One line becomes one ESP-NOW v2 unicast to the gateway (`:230`), at HT40 MCS0 LGI (`:486-492`),
  through a 16-deep queue of 1402-byte items (`:493`). ESP-NOW v2 carries up to 1470 B
  (`esp_now.h:37`; https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html).
- [I] Airtime per forward. The frame is about 870 B of payload, plus about 43 B of MAC and action-frame
  overhead, plus about 21 B of extra vendor-IE headers (v2 splits payload over IEs with 7 B each;
  `esp_now.rst:40` "x = 1512 (1470 + 6*7)"), so about 934 B = 7472 bits. HT40 MCS0 carries 54 bits per
  4 µs symbol, so 139 symbols ≈ 556 µs, plus the HT-mixed preamble (~36 µs), ≈ **0.59 ms**. Add SIFS and
  a 6 Mbps ACK (~54 µs) and DIFS plus mean backoff (~95 µs): **≈0.75 ms per forward**. The code comment
  says "about 0.5 ms" (`:488`). For comparison, a probe is `sig_len` 47 B (sample line,
  `tests/test_parser.py:104`): 8 symbols + preamble ≈ **68 µs**. **A forward costs about 10x the probe
  it reports.**
- [I] Channel time spent on forwards = R x S x 38.5 x 0.75 ms:

  | R x S | Forwards/s | Forward airtime |
  |---|---|---|
  | 2x2 (today) | 154 | 12% |
  | 3x3 | 347 | 26% |
  | 4x4 | 616 | **46%** |
  | 5x5 | 963 | **72%** |

  The probes themselves need only S x 38.5 x 68 µs, under 2%. The quadratic forward term dominates. With
  CSMA, collisions and deferrals climb steeply past about 40-50% occupancy, so I put the practical ceiling
  at **R x S ≈ 12-20 at 38.5 Hz**. The RF agent should confirm with its own model.
- [E] Unicast retries multiply this. At MCS2 "over half the unicast sends failed after retries"
  (`68c2b5f` message). A frame that exhausts its retries costs its airtime once per attempt. [I] One
  receiver that cannot hear the gateway's ACKs (see §3.3) therefore burns several times its share of the
  channel.
- [E] Pacing: `forward_task` waits at most 1 tick for the send-done callback (`:220-238`). The comment
  says "with CSI capture on it often never fires". Receivers run `CONFIG_FREERTOS_HZ 100`, since
  `csi_recv/sdkconfig.defaults` does not set it; senders set 1000 (`csi_send/sdkconfig.defaults:8`). So
  one tick is **10 ms**.
  [I] If the callback never fires, each loop iteration blocks until the next tick: **at most ~100
  forwards/s per receiver, enough for 2.6 senders at 38.5 Hz.** This is the same ceiling as the wired
  UART, from a different cause. With the callback firing, the limit is airtime.

### 2.3 Gateway (`CSI_GATEWAY=1`)

- [E] The gateway runs one radio and no CSI capture (it returns before `wifi_csi_init`, `:476`). The
  receive callback copies each frame into a 16-item queue (`:350-363`). `gateway_task` (priority idle+2)
  writes the queue to USJ with a 16 KB TX ring and a 100 ms timeout, and counts short writes (`:365-375`,
  `:464-468`).
- [I] Bytes per second = R x S x 38.5 x ~870: **134 KB/s at 2x2, 302 KB/s at 3x3, 536 KB/s at 4x4.**
  USJ is a USB 2.0 full-speed device (12 Mbit/s, 64-byte bulk packets), with a theoretical bulk ceiling
  around 1 MB/s. I found no published measurement of real throughput through `usb_serial_jtag_write_bytes`.
  An IDF issue confirms that the USJ path drops data when the host does not read fast enough
  (https://github.com/espressif/esp-idf/issues/11192). The 16-item queue holds only about 100 ms of
  traffic at 2x2, so any host stall longer than that drops lines. **Measure the `GW 1s: short_writes`
  counter at 3x3 before trusting the gateway beyond the current rig.**

---

## 3. ESP-NOW and Wi-Fi configuration

### 3.1 PHY rate, ACKs and retries [E]

- The default ESP-NOW rate is 1 Mbps (`docs/en/api-reference/network/esp_now.rst:16`). This rig overrides
  it per peer: senders send broadcast at **HT40 MCS0 LGI (13.5 Mbps)** (`csi_send/main/app_main.c:39-40,124-130`).
  Receivers set the same on the broadcast peer (`csi_recv/main/app_main.c:69-70,164-171`) and on the
  gateway peer (`:486-492`; MCS2 was tried and dropped, `68c2b5f`).
- Channel 6 with HT40, secondary channel below (`:58-62,129-135`), on every board.
- CSI is captured only from HT frames (`acquire_csi_legacy = false`, `acquire_csi_ht20/ht40 = true`,
  `:384-399`). [I] A probe sent at the legacy 1 Mbps default would give no CSI at all, so the rate
  override is load-bearing.
- Broadcast probes are not acknowledged and not retried. The send status only says the frame left on the
  MAC layer (`esp_now.rst:118`). Every collision is a lost sample.
- Forwards are unicast, acknowledged and retried by the MAC. The docs advise sending the next frame only
  after the previous send callback returns (`esp_now.rst:120`).

### 3.2 Buffers and queues [E]

From the generated `firmware/csi_recv/build_c5_fwd/config/sdkconfig.h`:

| Setting | Value |
|---|---|
| `STATIC_RX_BUFFER_NUM` | 10 |
| `DYNAMIC_RX_BUFFER_NUM` | 128 (raised in `csi_recv/sdkconfig.defaults:8`) |
| `DYNAMIC_TX_BUFFER_NUM` | **32 (the default)** on receivers and forwarders; 128 on senders (`csi_send/sdkconfig.defaults:5`), which send 4-byte probes |
| `AMPDU_TX` | off |
| `RX_BA_WIN` | 6 |
| `FREERTOS_HZ` | 100 on receivers |

Application queues: forwarder 16 x 1402 B (`:493`); gateway 16 x 1402 B (`:467`).

### 3.3 What causes `ESP_ERR_ESPNOW_NO_MEM`

- [E] The API says only: "out of memory, when this happens, you can delay a while before sending the next
  data" (`components/esp_wifi/include/esp_now.h:221`). Forum reports describe it as the internal TX buffers
  filling, and pacing the sends clears it (https://esp32.com/viewtopic.php?p=52942).
- [E] Observed on the rig: a receiver a few cm from the gateway shows "`cb_ok` only in the first second,
  then `send_err` with `ESP_ERR_ESPNOW_NO_MEM` every second" (`docs/agents/hardware.md:24`;
  ADR 0019:100).
- [I] **Proposed mechanism.**
  1. The receiver locks its RX gain at packet 100 (`:264-272`). With 2 senders at 38.5 Hz that happens
     after 100 / 77 ≈ **1.3 s**, which matches "only in the first second".
  2. The locked gain is the median gain over the sender probes. The gateway's ACKs, from a few
     centimetres away, arrive tens of dB hotter, saturate the front end, and cannot be decoded. The
     forced gain applies to every received frame, not only CSI frames.
  3. Every forward is then retried to the limit and reported `cb_fail`.
  4. `forward_task` waits only ≤1 tick, then sends the next frame. A late callback also leaves the binary
     semaphore given, so the next `xSemaphoreTake` returns at once (`:217,233`). In-flight sends pile up
     while each holds a TX buffer.
  5. The 32 dynamic TX buffers run out, and the result is `NO_MEM`.

  The same pattern, with printing as the trigger instead of ACK loss, hit the transceiver
  (`98d0e89`: "one node's send queue seems to starve while it's busy printing captured CSI over the
  921600-baud UART inside the CSI RX callback").
- [E] The transceiver commit also cites ESP-IDF issues (#18682, #19238, #19239, #19269) about unpaced
  `esp_now_send` with no send callback wedging TX buffers (`3064795` message). I could not verify those
  issue numbers in this pass. Treat them as unverified.

---

## 4. What the scaling attempts changed, what broke, and why they did not land

| Branch / commit | What it changed | What broke or remained | Status |
|---|---|---|---|
| `origin/siv/turn-taking` `f5f299a` (Siv, 11-07 17:45) | Senders become a token ring. Each sender listens to the others through the ESP-NOW receive callback and sends one slot after its predecessor. A full-round fallback timer covers lost turns. `NUM_SENDERS` is compiled in (default **3**, `:73`). The slot is 1e6 / (40 x N) µs (8.3 ms at N=3, `:79`). No receiver change. | Only sender-to-sender collisions are addressed. [I] Each receiver still prints S x 38.5 lines/s, so §2.1's cap is unchanged: a 3rd sender saturates every receiver whether or not senders take turns. `esp_timer_stop` followed by `start_once` runs from two contexts (`:181-185`, `:208-225`). No measurement is recorded. | Never merged. Absorbed into all-transceiver. |
| `feat/all-transceiver` `98d0e89`, `3064795`, `35d5cb6` (11-07 18:51 to 21:37) | One image that sends in its own slot and captures every peer, giving N(N-1) links. Fixes: printing moved to a queue and a low-priority task; send pacing gated on the send callback; a critical section around the timer re-arm; `CONFIG_FORCE_GAIN` set to 0; scheduling switched to the CSI callback because "on_packet_heard ... stopped firing reliably a couple of seconds after promiscuous CSI capture was enabled"; random jitter added for nodes stuck in a bad phase. | NO_MEM under load. Lost send-done callbacks (38-39 of every 40 turns skipped) throttled one node to about 1 send/s. **Forced gain made a node deaf 2-3 s in.** Disabling it gave up amplitude stability, which was never re-validated. Verified working: 2 boards at ~35 pkt/s each direction (`3064795`). | Paused "per direction to switch to fix/scaling-and-accuracy" (`35d5cb6`). |
| `feat/transceiver-5plus` `404aa8d` (11-07 23:00) | Send rate = `LINE_BUDGET(60)/(N-1)`, capped at 40 Hz, so 5 nodes run at 15 Hz (`csi_transceiver/main/app_main.c:98-104,554-558`). Node ID and count come from NVS (`flash_all.py`). Own prefix `1a:0c:12:00:01:xx`. Re-enabled the stale-callback override. No logging from the CSI callback. | The commit states the bottleneck itself: "The bottleneck is each node's UART (~110 lines/s at 921600), not the air: 5 nodes at 40 Hz need 160 lines/s per node." Last test: after the prefix change "the C5 heard nothing (heard=0)", cause unconfirmed. The 5-board test never ran. | Shelved. |
| `design/multichannel-tdma` `d556bea` | Proposal only: TDMA plus a 2-3 channel split at HT20, estimating 16-48 senders. | Its premise, that the 65% loss is an RF phase-collision effect, is confounded by §2.1. It also needs one receiver group per channel. | Not built. |
| `feat/ht20-channel-param` `8a30d5b` | HT20 and a `-DCHANNEL` build flag on both firmwares. | Groundwork only. HT20 changes the CSI layout, which the parser handles through re-lock. | Not merged. |
| `origin/fix/scaling-and-accuracy` `fe257f2`, `6fa2d58` | 20 Hz default senders; `CSI_PRINT_STRIDE` (stride 4 makes lines ~0.27 KB, about 3x more budget); team MAC prefix with receivers at `:fe`; `CSI_GAIN_LOCK_STRONGEST`; `CSI_FORCE_GAIN`/`CSI_PRINT_RAW` flags; a written per-receiver packet budget. | Only the log-level change and the chunked host reader were cherry-picked into main (`867eea3`). The stride, 20 Hz and prefix firmware never landed (the commit appears only on `origin/fix/scaling-and-accuracy` and `origin/UI-Overhaul`). | Not merged. |
| `feat/wireless-receivers` `165ce75`, `68c2b5f` | Forward and gateway builds (§2.2-2.3). | MCS2 gave >50% failed unicasts, so the forward rate went to MCS0. The send-done wait went from 50 ms to 1 tick. Receivers must sit ≥1 m from the gateway (§3.3). Verified only at 2 receivers x 2 senders (ADR 0019:105). | **Merged** (PR #34). |

Common thread [I]: every attempt kept the **per-packet ASCII line at about 850 B**. TDMA and multi-channel
reduce collisions. The transceiver mesh makes each node's output load grow as (N-1) x rate. Forwarding
moves the output from the UART onto the shared channel. Only the stride flag on the unmerged scaling
branch attacked the line size, and it never shipped.

---

## 5. Gain control and more transmitters

- [E] Stock esp-csi ships `CONFIG_FORCE_GAIN 0`. This rig sets 1 (`firmware/README.md:11`; `:71`;
  `origin/fix/scaling-and-accuracy:firmware/README.md:67` "our default; upstream esp-csi ships `0`").
- [E] The baseline is the **median** of the gains recorded over the first 100 packets
  (`managed_components/espressif__esp_csi_gain_ctrl/include/esp_csi_gain_ctrl.h:39-53`). It is one value
  per receiver, taken across **all senders mixed together**. `esp_csi_gain_ctrl_set_rx_force_gain` then
  fixes the AGC (`:76`).
- [I] Why this gets worse with each added transmitter:
  1. **One gain has to fit every sender.** Near senders clip at the int8 range and far ones lose
     resolution and possibly packet detection. More senders at more varied distances widen the spread
     that one median must cover. The team saw the clipping problem and added `CSI_GAIN_LOCK_STRONGEST`
     on the unmerged scaling branch.
  2. **The locked gain also applies to frames the receiver must hear for other reasons**: gateway ACKs
     (the NO_MEM case, §3.3) and peer probes in the transceiver mesh (deaf after 2-3 s, `3064795`).
     Every node added to the RF neighbourhood is another signal level the fixed gain may not suit.
  3. **The baseline belongs to the boot-time population.** A sender added or moved later is never folded
     in. Every receiver must reset, which makes thresholds stale (`firmware/README.md:79-81`).
  4. The ~10 pkt/s link (`rx1<-01` at -57 dBm, ADR 0003:36) was on a receiver hearing one near and one far
     sender. A gain locked toward the near sender would fit that pattern. This is a hypothesis with low
     confidence: nothing logged the locked gain.
- Mitigations that already exist on branches: `CSI_FORCE_GAIN=0` with `CSI_PRINT_RAW=1` (the host would
  then need the per-line `agc_gain`/`fft_gain`), or `CSI_GAIN_LOCK_STRONGEST` with an offset.

---

## 6. Bugs and inefficiencies

| # | Finding | Where | Effect |
|---|---|---|---|
| B1 | Formatting and blocking UART output inside the Wi-Fi callback, against the vendor guidance | `csi_recv/main/app_main.c:241,244-329` | Cap #1. Starves the Wi-Fi task on a single-core chip. |
| B2 | Output also goes to a UART nobody reads. The host reads USJ, but the ROM path still busy-waits on UART0 at 921600. | `sdkconfig.defaults:5-6`; `esp_rom_sys.c:72-87` | [I] Setting the console to USJ only, or to none and using the USJ driver, removes the 92 KB/s ceiling. Needs a bench test. |
| B3 | 236 printf calls and 234 soft-float multiplies per packet; ASCII decimal is about 3.6x larger than the raw 234 B | `:289-324` | Binary or base64 lines would cut serial bytes and forward airtime about 3x. |
| B4 | Forwarder: binary semaphore with a 1-tick (10 ms) timeout. A late callback leaves the semaphore given, so sends overlap. TX buffers stay at the default 32. | `:217,233`; `build_c5_fwd/config/sdkconfig.h` | Cap #4; NO_MEM under ACK loss. |
| B5 | `FREERTOS_HZ` is 100 on receivers and 1000 on senders | `csi_recv` vs `csi_send/sdkconfig.defaults:8` | The "1 tick" in B4 means 10 ms, not 1 ms. |
| B6 | Receivers hear every forward. Promiscuous mode plus `acquire_csi_ht40` means each forward from another receiver produces a CSI callback before the `memcmp` filter. | `:251,380,384-399` | [I] Small CPU cost. The RX buffers are shared with the probes. |
| B7 | Every wired receiver sets its own MAC to `1a:00:00:00:00:00`, which is tx0's address | `:87,156`; `firmware/README.md:12` | Harmless while receivers never transmit. Breaks any design where receivers send (the transceiver, or ACK-based schemes). Fixed on the scaling branch (`:fe`). |
| B8 | Forwarder MAC is `1a:00:00:00:01:<last base-MAC byte>` | `:153` | About 1/256 chance per pair of a MAC collision between two receivers. |
| B9 | Senders run `usleep` loops with no jitter and no feedback | `csi_send/main/app_main.c:166-173` | [I] Two crystals with small offsets can stay in step for long stretches. Real but secondary to B1. |
| B10 | Turn-taking timer re-arm races (stop then start from several contexts). The fix wrapped `esp_timer` calls in `portENTER_CRITICAL`. | `siv/turn-taking:181-185`; `transceiver-5plus:235-241` | [I] `esp_timer` calls are internally locked, so the worst case is a reschedule landing late. The critical section masks interrupts around API calls, which is unnecessary and slightly risky. |
| B11 | Diagnostic counters are reset from the `esp_timer` task while the Wi-Fi task increments them | `:185-192`, `:335-348` | Diagnostic counts only, off by one or two. |
| B12 | `csi_send` does not build with IDF 6.0 (WIFI_BW enum rename) | `fw_tx0/build.log`; `docs/agents/hardware.md:34` | Friction: senders and receivers end up built with different IDFs. |

---

## 7. Cross-cutting notes for the other agents

- **Host and pipeline agent:** a wired receiver is RF-silent. It never calls `esp_now_send` in wired
  mode; it only adds a broadcast peer (`:454-461`). So "an S3 third receiver cut the other receivers'
  packet rate" (`docs/hardware.md:227`) cannot be a firmware or RF coupling. [I] It points at the host:
  `867eea3` measured `readline()` at 165% CPU with 19-27% loss on three receivers. S3 lines are longer,
  which means more host parsing work.
- **RF agent:** do not use the 2 x 100 Hz = 65% figure as a collision measurement (§2.1). Forward frames
  dominate airtime in wireless mode (§2.2), and unicast retries multiply it.
- **Future-hardware agent:** the fastest firmware-only multipliers are:
  - binary lines or subcarrier stride, about 3x;
  - USJ-only console with formatting on a separate task, about 5-10x on the serial path (unmeasured);
  - per-sender gain handling instead of one global lock;
  - for wireless, batching several CSI samples per ESP-NOW frame, which amortises the ~36 µs preamble,
    the ACK and the backoff, and cutting the bytes per sample.

## Sources

- ESP-IDF v6.0 tree used for the builds: `components/esp_rom/patches/esp_rom_sys.c`,
  `components/esp_system/port/cpu_start.c`, `components/esp_rom/esp32c5/include/esp32c5/rom/{uart,ets_sys}.h`,
  `components/soc/esp32c5/include/soc/soc_caps.h`, `components/esp_wifi/include/esp_now.h`,
  `docs/en/api-reference/network/esp_now.rst`, `docs/en/api-guides/wifi-driver/wifi-vendor-features.rst`,
  `docs/en/api-guides/usb-serial-jtag-console.rst`.
- ESP-NOW guide: https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html
- Wi-Fi vendor features (CSI callback context): https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html
- USB Serial/JTAG console: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-guides/usb-serial-jtag-console.html
- USJ drops data when the host is slow: https://github.com/espressif/esp-idf/issues/11192
- ESP-NOW NO_MEM under fast sends (forum): https://esp32.com/viewtopic.php?p=52942
- esp_csi_gain_ctrl API: `firmware/csi_recv/managed_components/espressif__esp_csi_gain_ctrl/include/esp_csi_gain_ctrl.h`
