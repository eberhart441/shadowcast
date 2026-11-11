# 03 — The radio channel: airtime, collisions and the physics budget

Scope: can the shared 2.4 GHz channel explain why the rig never went past 4 sensing boards? This file covers the PHY parameters the code uses, an airtime model with a small CSMA/CA simulation, how each observed symptom fits the model, near-far and hidden-node effects at the gateway, and the node counts that each scaling architecture allows.

Every number marked **(est.)** is my own arithmetic. The model scripts are in `research/scaling-redo/03-rf-airtime-model/`: `airtime.py` (PHY timing, load, DCF Monte-Carlo), `run_scen.py`, `run_sim.py` and `maxn.py`. Run them with the `_venv` Python. Code references are `path:line@branch`. The branch the demo Pi runs is `feat/wireless-receivers`, and all firmware references below are to it unless noted.

---

## 0. Verdict

**Airtime alone does not explain the 4-node ceiling.** On the demo rig (2 senders, 2 forwarding receivers and a gateway, 40 Hz), the sensing frames take about **0.6 %** of the channel. Forwarding every CSI line as ASCII brings the total to about **11 % busy, or 13 % counting DIFS and backoff (est.)**. The simulation delivers 39.8 of 40 pkt/s per link with a clean channel, and 38.6 with 30 % guest Wi-Fi. Collapse territory, meaning more than 50–60 % busy, is reached only:

- by per-sample ASCII forwarding with **5 or more all-transceiver nodes at 40 Hz** (N=5 is about 53 %; N=6 is 81 %, or 94 % counting access overhead);
- by a **4 senders × 4 receivers** split rig at 40 Hz (43 % busy, 51 % counting access overhead) with any real guest traffic on top;
- by a forwarding receiver stuck in ACK failure, which can burn **about 27 % of the channel on its own** (est., §2.3).

So the channel is the *next* ceiling. The quadratic cost of forwarding every link's CSI line as ~900 B of ASCII is waiting just past 4–5 nodes. The *observed* failures, however, line up with non-airtime mechanisms:

| Symptom | Mechanism | Confidence |
|---|---|---|
| 3rd receiver cut the other receivers' rates | Not air: wired receivers never transmit. Host serial CPU and per-receiver UART budget (commit `867eea3`) | high |
| 65 % loss with 2 senders at 100 Hz | Not air (1.5 % busy). The UART was carrying 2× the lines, about 184 KB/s against a 92 KB/s ceiling | medium-high |
| NO_MEM near the gateway | Forced RX gain (AGC off) plus the gateway's ACK at a few cm: ACKs are not decoded, so frames are retried, the TX buffers fill and sends fail | medium |
| 10 pkt/s link at RSSI -57 | Not a collision or SNR effect. A link-specific mechanism: gain lock, local interference, or a sender-side stall | low-medium |
| All-transceiver stalls | ESP-NOW send-callback and TX-buffer bugs, and UART (code comments). Sensing airtime under 1 % | high (for "not air") |

Confidence in the verdict: **medium-high**. The arithmetic is robust: a 2× error in line length or ACK rate does not move the demo rig out of the ~10–25 % range. The attribution of each symptom is medium, because there is no sniffer capture.

---

## 1. Actual PHY parameters (from code)

| Parameter | Value | Source |
|---|---|---|
| Channel | 6, hard-coded | `firmware/csi_send/main/app_main.c:27`, `firmware/csi_recv/main/app_main.c:58` @feat/wireless-receivers |
| Bandwidth | **HT40, secondary channel below** (not HT20) | send `:31,:39,:101`; recv `:61,:69,:134`. HT20 exists only on unmerged `feat/ht20-channel-param` (`firmware/csi_send/main/app_main.c:34-35`). That branch still sets `CONFIG_ESP_NOW_PHYMODE WIFI_PHY_MODE_HT40` (`:42`), which mismatches its HT20 bandwidth |
| Sensing frame | ESP-NOW **broadcast**, 4-byte counter, HT40 **MCS0 LGI** (13.5 Mbit/s), 40 Hz free-running `usleep` | send `:40-41,:158,:167,:172` |
| Forward frame | ESP-NOW **unicast** to gateway `1a:00:00:00:02:00`, HT40 MCS0 LGI, line up to `FWD_LINE_MAX` 1400 B | recv `:49,:51,:486-492,:230` |
| Forward pacing | one send, then wait at most 1 tick (10 ms) for the send callback, then send the next; 16-deep queue | recv `:220-239,:493` |
| CSI captured | HT40 HT-LTF: 234 raw int8 values = 117 subcarriers, indices 57–59 null | recv `:389`; `csi/parser.py:38` @feat/wireless-receivers |
| RX gain | forced after the first 100 packets (`CONFIG_FORCE_GAIN 1`) | recv `:71,:264-271` |
| TX buffers | sender 128 dynamic; receiver at IDF default 32 | `firmware/csi_send/sdkconfig.defaults:5`; IDF `components/esp_wifi/Kconfig:117-121` (local v6.0 tree) |
| All-transceiver | HT40 MCS0, rate = `LINE_BUDGET 60 / (N-1)`, capped at 40 Hz (4 nodes → 20 Hz, 5 → 15 Hz) because of the UART | `firmware/csi_transceiver/main/app_main.c:70-71,:98-104,:554-558` @feat/transceiver-5plus |

The 40 MHz channel sits on ch 6 with ch 2 as its secondary, spanning 2407–2447 MHz (est.). Any 20 MHz network on channels 1–10 overlaps it, so it overlaps nearly every guest AP in a typical venue. Wikipedia notes that 40 MHz in 2.4 GHz "can take up to 82 %" of the band and needs relatively free spectrum ([IEEE 802.11n-2009](https://en.wikipedia.org/wiki/IEEE_802.11n-2009)). ADR 0003 records that ch 6 "was picked, not surveyed" (`docs/adr/0003-one-shared-channel-at-40-hz.md:22`).

**Line length.** A forwarded line is `RX,<serial>,CSI_DATA,…,"[234 ints]"`, measured at **871 B mean** on synthetic C5 lines (`research/scaling-redo/bench_results_line_parser_reader_gateway.json`, `gateway_line_bytes_mean`). The cap is 1400 B. The transceiver firmware says "~0.85 KB" (`app_main.c:98` @feat/transceiver-5plus). The design doc's figure of 1500–2000 B (`docs/design/wireless-csi-forwarding.md` @design/wireless-csi-forwarding) is too high for the C5's 234-value layout.

---

## 2. Airtime model

### 2.1 Formulas

PPDU duration in µs, for payload L bytes with SERVICE (16) and tail (6) bits. The 6 µs signal extension (SE) applies to OFDM in 2.4 GHz.

- 11b DSSS 1 Mbit/s, long preamble: `T = 192 + 8L`
- Legacy OFDM (6/24 Mbit/s): `T = 20 + 4·⌈(22+8L)/N_DBPS⌉ + 6`, with N_DBPS 24 (6 M) and 96 (24 M)
- HT-mixed, 1 stream, 800 ns GI: `T = 36 + 4·⌈(22+8L)/N_DBPS⌉ + 6`. The preamble is L-STF 8 + L-LTF 8 + L-SIG 4 + HT-SIG 8 + HT-STF 4 + HT-LTF 4. N_DBPS for HT20 is 26 (MCS0) and 260 (MCS7); for HT40 it is 54 (MCS0) and 540 (MCS7).
- HE SU, HE20, 1 stream, 0.8 µs GI, 2× HE-LTF, PE=0: `T = 43.2 + 13.6·⌈(22+8L)/N_DBPS⌉ + 6`, with N_DBPS 117 (MCS0, 8.6 Mbit/s) and 1170 (MCS7, 86 Mbit/s).
- ACK, 14 B: 50 µs at 6 Mbit/s OFDM, or 304 µs if sent at 1 Mbit/s 11b. Which basic rate the ESP uses for ACKs with no BSS is **unknown**. The model uses 50 µs; with 304 µs, forward airtime rises about 39 %.
- Unicast exchange: `T_data + SIFS(10) + T_ACK`. Access overhead per frame: `DIFS(28) + mean backoff 7.5 × 9 µs = 95.5 µs` (CWmin 15, short slot).
- Utilization: `U = Σ rate_i × T_i`. "Busy" counts PPDU and ACK time; "occupancy" adds DIFS and mean backoff.
- ESP-NOW frame size: `36 + 7·⌈payload/250⌉ + payload`. The 36 B are MAC header 24, category 1, OUI 3, random 4 and FCS 4; each 7 B is one vendor element per 250 B of body. That gives a **sensing frame of 47 B**.

Sources: the ESP-NOW frame layout ("MAC Header 24, Category 1, OUI 3, Random 4, Vendor Specific Content 7~x, FCS 4"; v2 x = 1470 + 6·7) and the "default ESP-NOW bit rate is 1 Mbps" are from [ESP-IDF ESP-NOW docs (C5)](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html). SIFS 10, DIFS 28, the 6 µs signal extension and the ACK durations are from [HU Berlin: Packet transmission time in 802.11](https://sarwiki.informatik.hu-berlin.de/Packet_transmission_time_in_802.11) and [Wikipedia: DCF Interframe Space](https://en.wikipedia.org/wiki/DCF_Interframe_Space). HT-STF and HT-LTF at 4 µs each are from [MathWorks: HT PPDU structure](https://www.mathworks.com/help/wlan/gs/ht-ppdu-structure.html); the other preamble fields and the HE timing are IEEE 802.11-2020 cl. 19 and 802.11ax-2021 cl. 27 values that I did not fetch (standard values). HT rates are from [Wikipedia 802.11n](https://en.wikipedia.org/wiki/IEEE_802.11n-2009). C5 CSI sizes (HT40 one LTF = 234 B, HE20 = 490 B, HT20 = 114/228 B) are from the [ESP-IDF v6.0 C5 Wi-Fi vendor features, CSI table](https://docs.espressif.com/projects/esp-idf/en/v6.0/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html). The C5 runs 802.11ax at 20 MHz only and 802.11n at 20/40 MHz, in 2.4 and 5 GHz ([Espressif ESP32-C5 announcement](https://espressif.com/en/news/ESP32-C5), [ESP32-C5-WROOM-1 datasheet](https://www.espressif.com/sites/default/files/documentation/esp32-c5-wroom-1_wroom-1u_datasheet_en.pdf)).

### 2.2 PPDU durations (µs, est.)

| PHY | sensing 47 B | ACK | ASCII 871 B | ASCII 1400 B | raw I/Q 250 B | amp117 u8 133 B | amp64 i8 80 B | amp52 i8 68 B | feature 16 B |
|---|---|---|---|---|---|---|---|---|---|
| 11b 1 M (ESP-NOW default) | 568 | 304 | **7672** | 12016 | 2536 | 1600 | 1176 | 1080 | 664 |
| OFDM 6 M | 94 | 50 | 1278 | 2002 | 422 | 266 | 194 | 178 | 110 |
| OFDM 24 M | 46 | 50 | 342 | 522 | 126 | 86 | 70 | 66 | 50 |
| HT20 MCS0 | 106 | 50 | 1198 | 1866 | 406 | 262 | 198 | 182 | 118 |
| HT20 MCS7 | 50 | 50 | 158 | 226 | 82 | 66 | 58 | 58 | 50 |
| **HT40 MCS0 (in use)** | **74** | 50 | **598** | 922 | 218 | 150 | 118 | 110 | 82 |
| HT40 MCS7 | 46 | 50 | 98 | 130 | 62 | 54 | 50 | 50 | 46 |
| HE20 MCS0 | 104 | 50 | 933 | 1436 | 335 | 226 | 172 | 158 | 117 |
| HE20 MCS7 | 63 | 50 | 144 | 199 | 90 | 76 | 63 | 63 | 63 |

The firmware comment "about 0.5 ms on air per line" (`csi_recv/main/app_main.c:488`) agrees with 598 µs. One forwarded CSI line costs **658 µs** with SIFS and ACK, which is **8.9× the 74 µs sensing frame that produced it**. Because each sensing frame yields M lines, the backhaul costs 8.9·M times the sensing itself.

### 2.3 Split rig (S senders, M forwarding receivers), HT40 MCS0, ASCII forwards (est.)

`U = S·R·74 µs + S·M·R·658 µs`

| S | M | R (Hz) | sense frames/s | forwards/s | busy, sensing only | busy, with forwards | occupancy (incl. DIFS + backoff) |
|---|---|---|---|---|---|---|---|
| 2 | 2 | 40 | 80 | 160 | 0.6 % | **11.1 %** | 13.4 % |
| 2 | 3 | 40 | 80 | 240 | 0.6 % | 16.4 % | 19.4 % |
| 3 | 3 | 40 | 120 | 360 | 0.9 % | 24.6 % | 29.2 % |
| 4 | 4 | 40 | 160 | 640 | 1.2 % | **43.3 %** | 50.9 % |
| 4 | 4 | 20 | 80 | 320 | 0.6 % | 21.6 % | 25.5 % |
| 8 | 8 | 20 | 160 | 1280 | 1.2 % | 85 % | 99 % |

Rule of thumb (est.): forwarded links per channel ≤ `U_budget / (R × 658 µs)`. At 40 Hz that is **11 links within a 30 % budget** (room for guest Wi-Fi) and **19 links within 50 %**, whatever the topology.

### 2.4 All-transceiver N nodes (N senders, each node forwards N−1 lines per round) — busy % / occupancy % (est.)

R = 40 Hz:

| N | links | sensing only (HT40 MCS0) | + ASCII forwards, HT40 MCS0 (today) | + ASCII at 11b 1 M (default rate) | + ASCII HT20 MCS0 | + ASCII HT20 MCS7 | + raw I/Q 250 B, HT20 MCS0 | + amp117 u8, HT20 MCS0 | + amp52 i8, HT20 MCS7 | + 16 B feature, HT20 MCS0 |
|---|---|---|---|---|---|---|---|---|---|---|
| 4 | 12 | 1.2 | **33 / 39** | 385 / 391 | 62 / 68 | 12 / 18 | 24 / 30 | 17 / 23 | 7 / 13 | 10 / 16 |
| 6 | 30 | 1.8 | **81 / 94** | 960 | 154 | 29 / 42 | 58 / 72 | 41 / 55 | 17 / 30 | 24 / 38 |
| 8 | 56 | 2.4 | 150 / 174 | 1791 | 285 | 52 / 77 | 108 | 76 / 100 | 30 / 54 | 43 / 68 |
| 12 | 132 | 3.6 | 351 | 4220 | 669 | 120 | 251 | 175 | 67 / 122 | 99 / 154 |
| 16 | 240 | 4.7 | 636 | 7671 | 1214 | 216 | 454 | 316 | 120 / 218 | 178 / 275 |

R = 20 Hz: halve every number. For example, today's forwarding gives N=4: 16 %, N=6: 40 %, N=8: 75 %, N=12: 175 %.

Reading the table:

- **Sensing is never the problem.** 16 nodes at 40 Hz is under 5 % of the channel.
- **Per-sample unicast forwarding is the problem.** It is quadratic in N, and each tiny report still pays about 170 µs of fixed cost: preamble, SIFS, ACK and access. That is why even 16 B features exceed 100 % at N=16.
- Had the forward rate stayed at the **ESP-NOW default of 1 Mbit/s**, forwarding would have failed at N=2. The per-peer `esp_now_set_peer_rate_config` call (`csi_recv:492`) is what keeps today's rig working.
- **Narrowing to HT20 for multi-channel *doubles* forwarding airtime** at MCS0. The HT20 plan only pays off if forwards also move to a higher MCS or to binary.

### 2.5 DCF Monte-Carlo (all-hear-all, no capture, no hidden nodes; 3 seeds × 8 s)

The model is in `airtime.py:simulate`. Senders free-run with a random phase and 0.2 ms jitter. Each receiver queues a forward 0.3–1.5 ms after every sensing frame it decodes. Backoff is drawn uniformly from [0, CW], with CW 15 doubling on unicast failure up to 1023 and a retry limit of 7 (assumed). The 16-frame queue drops on overflow. "Guest" is one extra CSMA station with Poisson 400 µs frames. Two frames collide if they start within 4 µs of each other.

| Scenario (R=40 unless noted) | sim busy % | sensing pkt/s per link | sensing frames collided | forward attempts collided | forwards dropped (queue full) | end-to-end pkt/s per link |
|---|---|---|---|---|---|---|
| split 2×2, ASCII forwards (demo rig) | 11 | 39.8 | 0.6 % | 2.4 % | 0 | **39.8** |
| split 2×2, + 30 % guest | 41 | 38.6 | 3.5 % | 6.5 % | 0 | 38.6 |
| split 2×3, ASCII forwards | 17 | 39.6 | 1.0 % | 4.8 % | 0 | 39.6 |
| split 2×3, + 30 % guest | 46 | 38.4 | 4.1 % | 11.2 % | 0 | 38.4 |
| split 4×4, ASCII forwards | 44 | 38.6 | 3.5 % | 11.2 % | 0 | 38.6 |
| split 4×4, + 30 % guest | 73 | 35.7 | 10.8 % | 19.9 % | 0 | 35.7 |
| all-transceiver N=4, ASCII | 33 | 39.6 | 0.9 % | 5.7 % | 0 | 39.6 |
| all-transceiver N=6, ASCII | 82 | 36.0 | 10.1 % | 20.6 % | 0 | 36.0 |
| all-transceiver N=8, ASCII | 92 | 27.0 | 32.6 % | 34.0 % | **16 %** | **22.5** |
| all-transceiver N=6, ASCII, + 30 % guest | 91 | 28.3 | 29.3 % | 32.3 % | 3.5 % | 27.2 |
| all-transceiver N=8, ASCII, + 30 % guest | 92 | 25.3 | 36.9 % | 38.4 % | 25 % | 18.8 |
| all-transceiver N=16, no forwarding (wired) | 5 | 39.5 | 1.2 % | — | — | 39.5 |
| all-transceiver N=8, amp52 i8 @ HT20 MCS7 | 31 | 38.2 | 4.5 % | 17.4 % | 0 | 38.2 |
| all-transceiver N=12, amp52 i8 @ HT20 MCS7 | 63 | 30.0 | 25 % | 35.8 % | 0.3 % | 29.9 |
| all-transceiver N=8, ASCII, R=20 | 76 | 17.2 | 13.9 % | 27.4 % | 0 | 17.2 |

An idealized CSMA channel is **forgiving**. Even with heavy polite guest traffic (50–85 % background), the 2×2 and 2×3 rigs still deliver 35–37 pkt/s per link in the simulation (`03-rf-airtime-model`, guest-sensitivity run). So **whatever cut real links to 10–26 pkt/s at 4–5 boards was not ideal-CSMA contention.** It was something outside the model: UART, host, gain forcing, driver buffers, or hidden or non-Wi-Fi interference. The model is optimistic in that it assumes perfect carrier sense, no capture, no hidden nodes, infinite CPU and correct driver callbacks. Real collapse comes earlier than the table shows, so treat the ≥50 % rows as failing.

Saturated-DCF context: with fixed CW 15 for broadcasts, Bianchi's τ = 2/(W+1) = 0.125 gives a per-frame collision probability of 1−(1−τ)^(n−1): 0.33 at n=4 and 0.61 at n=8 (est.). That is why anything that saturates queues (stuck retries, 100 Hz bursts) turns lossy quickly ([Bianchi 2000, IEEE JSAC, doi:10.1109/49.840210](https://doi.org/10.1109/49.840210)).

---

## 3. The observed symptoms, through the model

**3.1 "A 3rd receiver cut the others' packet rates."** The evidence is an **ESP32-S3** third receiver, while the rig was still **wired**, cutting the other receiver from about 35 to 22–26 pkt/s; a third **C5** left every link at 34–38 (`docs/adr/0002-esp32-c5-receivers-only.md:33`). A wired `csi_recv` never calls `esp_now_send`: the only call is in `forward_task` under `CSI_FORWARD` (`csi_recv/main/app_main.c:175-239`), and broadcasts are never ACKed. A passive receiver therefore adds **0 µs** of airtime, and airtime cannot explain this. Commit `867eea3` measured the actual cause on the Pi: with three receivers, the `readline()` reader used 165 % CPU and lost 19–27 %. Each receiver also emitted 140 lines/s, half of them a per-packet gain log line. With forwarding, a third receiver would add 80 forwards/s, or 5.3 % airtime (11.1 → 16.4 %), which the simulation absorbs without loss.

**3.2 "65 % loss with 2 senders at 100 Hz"** (ADR 0003). Airtime is 200 × 74 µs = **1.5 %**. ADR 0003 and the TDMA design doc already flag that this is "more than naive channel-utilisation arithmetic predicts" (`docs/design/multichannel-tdma.md` @design/multichannel-tdma). The UART explains it instead (est.). A wired receiver printed about 850 B of CSI plus about 70 B of gain-log line per packet, so 200 pkt/s is about 184 KB/s against 92.16 KB/s for 921600 baud 8N1. That is about 50 % deliverable before `ets_printf` blocking and queueing losses, close to the observed 65 %. At 40 Hz, 80 × 920 B ≈ 74 KB/s is 80 % of the UART, which fits the observed 5–10 % loss. The phase-locked-collision hypothesis in the design doc is not needed. TDMA would not have fixed this.

**3.3 "Receivers close to the gateway got `ESP_ERR_ESPNOW_NO_MEM`; forwards failed."** The documented symptom is `cb_ok` only in the first second, then `send_err` NO_MEM every second, for a receiver a few cm from the gateway, with ≥1 m as the workaround (`docs/agents/hardware.md:24`). Airtime is not the cause: the whole rig is at 11 %. The timing points to the **gain lock**:

- The receiver forces its RX gain after 100 sensing frames (`csi_recv:264-271`). At 2 senders × 40 Hz that takes **1.25 s** (est.), which matches "cb_ok only in the first second."
- Espressif's own component warns that forcing gain "may cause packet loss" (`firmware/csi_recv/managed_components/espressif__esp_csi_gain_ctrl/README.md:78`; [component registry](https://components.espressif.com/components/espressif/esp_csi_gain_ctrl)).
- The gateway is idle except for ACKs: it returns before CSI init (`csi_recv:476`) and transmits only ACKs. At 5 cm, free-space loss at 2.4 GHz is 20·log10(4π·0.05/0.125) ≈ 14 dB (est.). A ~+20 dBm ACK therefore arrives near 0 dBm, about 50 dB above the −50 dBm sender frames the locked gain was set for. The receiver's front end clips, the ACK is not decoded, and the MAC times out and retries.
- Commit `68c2b5f` supports this: with the gateway a few cm away, **MCS2 forwards failed over half the time after retries, while MCS0 fell to 2–10 failures/s.** Higher-order modulation is more sensitive to clipping and EVM.

Once ACKs fail, each forward runs its full retry chain. Assuming a retry limit of 7, that means 8 × 658 µs on air plus backoff growing 15→1023, so the TX is held for about **19 ms per line, while lines arrive every 12.5 ms** (est., `03-rf-airtime-model`). The 10 ms callback wait (`csi_recv:233`) does not throttle the task, so frames pile into the 32 default dynamic TX buffers (IDF Kconfig default; `csi_recv` does not raise it) until `esp_now_send` returns NO_MEM. The ESP-NOW docs describe NO_MEM as running out of buffers ("delay a while before sending the next data", [ESP-NOW docs](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html)). The same queue build-up appears in the all-transceiver firmware's analysis (`firmware/csi_transceiver/main/app_main.c:119-140` @feat/transceiver-5plus). The stuck receiver also **costs everyone airtime**: about 8 × 0.66 ms per 19 ms, roughly **27 % of the channel** (est.), which would push the rig toward the 40 % region.

This is an inference: it fits the timing, the MCS2 result and the vendor warning, but it is unmeasured. A test is to set `CONFIG_FORCE_GAIN 0` on one receiver at 5 cm, or to lower the gateway's TX power, and watch `cb_ok`.

**3.4 "4-board rig ran at 37–40 pkt/s per link."** This fits the model exactly. The rig is at 11 % busy, and the simulation gives 39.8 clean and 38.6 with 30 % guest. The remaining 1–3 pkt/s is consistent with occasional collisions plus guest traffic, or host-side loss.

**3.5 "rx1←01 at about 10 pkt/s with RSSI −57"** (`docs/agents/project-context.md:21`; ADR 0003), recorded wired on 2026-11-07 and healthy the next day.

- −57 dBm is about 30 dB above the MCS0 decode threshold (est.), so this is not path loss.
- Collisions hit every receiver of a frame alike in a 3 m room, and the rig collision rate is about 1 %. Rig collisions cannot produce 75 % loss on one link while the same sender's other link is healthy.
- What remains are link-specific mechanisms:
  - (a) a gain baseline locked while tx0's frames dominated the 100-packet window, leaving tx1's weaker frames under-scaled, which the vendor warns may cause loss;
  - (b) a hidden interferer near rx1 only, such as a guest AP or client, or a non-Wi-Fi source such as BT or a microwave, possibly on the **HT40 secondary (ch 2)** (§4);
  - (c) a sender-side stall.

  Confidence is low without that session's counters. A receiver reset (re-baseline) restoring the link would point to (a).

**3.6 All-transceiver and "senders take turns".** The TDMA slot is `1e6/(rate·N)`: 13.3 ms at N=5 and 15 Hz (`app_main.c:554-558` @feat/transceiver-5plus), against a 74 µs frame. That is a 0.6 % duty cycle, so turn-taking solved no airtime problem. The branch's own comments place the bottlenecks in the UART (`:98-104`) and in ESP-NOW callback and TX-buffer bugs (`:119-140`). Had the branch forwarded per-sample ASCII to a gateway at its N=5, 15 Hz rate, it would have used 20 % (est.), which is fine. At N=6, 40 Hz it would have hit 81 %.

---

## 4. Hidden nodes, capture and near-far around the gateway

- **No hidden nodes inside the rig.** The room is 2.77 × 1.84 m and the worst link is about −57 dBm, far above the 802.11 OFDM preamble-detect CCA threshold of −82 dBm per 20 MHz (IEEE 802.11-2020 §17.3.10.6, standard value). Every board defers to every other.
- **Hidden nodes relative to guest Wi-Fi, made worse by HT40.** Guest APs and clients outside the room may be heard by some boards and not others, so collisions become location-dependent and per-link. With HT40, a transmitter senses the secondary 20 MHz by energy detection, about 20 dB less sensitive than preamble detection (802.11n secondary-channel CCA, standard value, ESP behavior unverified). A guest frame on ch 1–3 at, say, −70 dBm is not deferred to but still corrupts the 40 MHz reception. HT20 on a surveyed channel (11 is often quietest, but survey first) removes the secondary-channel exposure and doubles CCA headroom relative to the occupied band.
- **Capture at the gateway.** A receiver near the gateway arrives 20–40 dB stronger than a far one, so in a collision its frame is usually captured and the far receiver's is lost and retried. This adds unfairness, not collapse.
- **Near-far at the near receiver.** See §3.3. The gateway's ACK saturates a receiver whose AGC is frozen. The radio-level fixes are:
  - keep ≥1 m, as now;
  - lower the gateway's TX power (`esp_wifi_set_max_tx_power`), since ACKs need no range;
  - do not force gain on forwarding receivers, and use the gain *compensation* the code already computes (`csi_recv:273`) instead;
  - or move the backhaul off-air (§5).
- **Half-duplex.** A forwarding receiver cannot hear a sensing frame while it transmits. CSMA normally prevents overlap, but every forward collision or stuck retry is also a lost sensing frame on that receiver's own links.

---

## 5. What the physics permits — scaling architectures and max N

The criterion is rig airtime busy ≤ 30 %, which leaves room for typical guest Wi-Fi, or ≤ 50 % on a clean channel. The topology is all-transceiver (N(N−1) links) with one gateway. All figures are estimates from `03-rf-airtime-model/maxn.py`.

| Architecture | max N, R=40, ≤30 % | R=40, ≤50 % | R=20, ≤30 % | R=20, ≤50 % |
|---|---|---|---|---|
| A. **Today**: per-sample ASCII unicast, HT40 MCS0 | **3** | **4** | 5 | 6 |
| B. same, after narrowing to HT20 MCS0 | 2 | 3 | 3 | 4 |
| C. per-sample binary amplitudes (52 × int8 + header), HT20 MCS7 | 8 | 10 | 11 | 14 |
| D. **per-round batch**: one unicast per node per round carrying N−1 × 56 B, HT20 MCS7 | 21 | 27 | 28 | 41 |
| E. **piggyback**: each node's own sensing *broadcast* carries its N−1 reports from the previous round (gateway just listens, no ACK), HT20 MCS3 | 19 | 25 | 27 | 27 (cap: 1470 B payload) |
| F. piggyback 8 B feature per link, HT20 MCS0 | 23 | 31 | 34 | 45 |
| G. TDMA + E, 150 µs guard per slot | 15 | 21 | 24 | 27 |
| H. sensing only, backhaul off-air (USB/Ethernet/other band), HT20 MCS0 | 70 | 117 | 141 | ≥200 |
| H′. same, HT40 MCS0 | 101 | 168 | ≥200 | ≥200 |

Split topology with today's forwarding: the limit is links, not boards. At 40 Hz that is ≤ 11 links within 30 % and ≤ 19 links within 50 %; at 20 Hz, 22 and 37 links.

Notes per option:

- **Single TX, many RX broadcast** is already the sensing primitive: one 74 µs frame gives M links. Sensing airtime grows with the *number of transmitters*, not the number of links. Every architecture that keeps per-link data off the air (H) or folds it into the broadcast (E, F) inherits that linear scaling.
- **On-node feature reduction** is the single biggest lever on a shared channel. ASCII to 52 int8 amplitudes cuts bytes about 13× (871 → 68). Batching per round removes the fixed cost of about 170 µs per frame (preamble, SIFS, ACK, access) that dominates small frames (C → D is about 2.5× in N). Sending a scalar motion feature (F) makes backhaul nearly free. Raw I/Q at 250 B fits one v1 ESP-NOW element if phase is ever needed.
- **TDMA** does not add capacity at these loads. The channel is under 5 % busy for sensing, and TDMA only removes contention between rig nodes. It cannot schedule guest Wi-Fi, which still forces guards. It pays off only once the rig itself runs above about 40 % (D/E at N ≥ 20) or for phase-coherent sampling. It is not the fix for any observed symptom (§3.2, §3.6). The multichannel-TDMA design doc's estimate of "100+ senders at 40 Hz on one channel" (≈ 25 ms / 175 µs) is correct for *sensing-only*, which matches row H, but it ignores the backhaul.
- **Multi-channel sensing** (HT20 on 1/6/11, or 5 GHz) multiplies independent groups, but **links exist only inside a channel group**, because a single-radio C5 hears one channel at a time. k channels give k groups of N/k nodes, so k·(N/k)² links rather than N². That suits zoned coverage, not one dense mesh. 5 GHz on the C5 offers many non-overlapping 20 MHz channels: UNII-1 36/40/44/48 and UNII-3 149–165, nine non-DFS channels in the US (regulatory, not fetched). It usually has much less venue traffic, but higher wall loss.
- **A separate backhaul channel or band on the same C5 does not run concurrently.** The C5 is a single 1T1R radio on one channel at a time (C5 datasheet; `esp_now.rst`: "the channel must correspond to the local device's current channel"). IDF v6.0 has `esp_now_switch_channel_tx` and `esp_now_remain_on_channel` (`C:\esp\v6.0\esp-idf\components\esp_wifi\include\esp_now.h:387-410`), so a node can hop to a 5 GHz backhaul channel in a scheduled gap, at the cost of deaf time on the sensing channel (est. ≥ 1–2 ms per hop including retune). This only works with TDMA-style scheduling. A truly separate backhaul (USB hub, Ethernet, or a second radio per node) is what makes row H real.
- **NDP sounding** (802.11bf / 802.11ac-ax sounding; an NDP is a preamble-only PPDU) would shrink the sensing frame from 74 µs to about 40 µs (est.). That is irrelevant here because sensing is already under 5 %, and ESP-IDF does not expose NDP transmission or 802.11bf to applications as far as the docs I read show (unverified). Sources: [Du et al., "An overview on IEEE 802.11bf", arXiv:2207.04859](https://arxiv.org/pdf/2207.04859); [802.11bf sensing vs. data impact, arXiv:2403.19825](https://arxiv.org/pdf/2403.19825).
- **Precedent.** Radio tomographic imaging testbeds used multi-channel TDMA token passing among RF sensors (Wilson & Patwari, IEEE TMC, [doi:10.1109/TMC.2010.175](https://doi.org/10.1109/TMC.2010.175), as cited in `docs/design/multichannel-tdma.md` @design/multichannel-tdma). Their nodes reported RSS bytes, not ~900 B CSI lines, which is option F in this table.

**Recommended physics-respecting stack (est. capacity about 20–30 all-transceiver nodes at 20–40 Hz on one 2.4 GHz HT20 channel):**

1. Survey the channel and move to HT20, fixing `CONFIG_ESP_NOW_PHYMODE` to HT20 too.
2. Use binary amplitudes or features instead of ASCII.
3. Batch per round, or piggyback on the sensing broadcast, instead of sending per-sample unicasts.
4. Stop forcing RX gain on any node that must receive ACKs.
5. Add TDMA only past about 20 nodes.

Beyond about 30 nodes, or in a busy venue, move the backhaul off the sensing channel entirely.

---

## 6. Caveats

- No sniffer capture or hardware access. Every mechanism attribution in §3 is inferred from code, commit messages and docs.
- Unknowns that change numbers but not the verdict:
  - the ESP's ACK rate (50 vs 304 µs, which would raise forward airtime about 39 %);
  - the ESP-NOW hardware retry limit (assumed 7);
  - whether ESP-NOW frames always take a backoff;
  - the real guest occupancy at the venue (modelled at 0–85 %, polite);
  - the ESP's HT40 secondary-channel CCA behavior.
- The simulation is optimistic. It has perfect CSMA, no capture, no hidden nodes, and no driver or CPU limits.
