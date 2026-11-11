# 07. Network architecture that scales to 8–32 nodes

Research slice: system / network architecture for the post-hackathon redo. Other slices cover chips, antennas, and forensics of the old build.
Date: 2026-11-10. Every factual claim has a source. Numbers I derived are marked **[est]** and show their arithmetic. Nothing here has been measured on hardware.

---

## 0. Summary

**Recommendation: one radio per node, every node a transceiver, one 2.4 GHz HT20 channel, a slotted token round, and features computed on the node and carried in the sensing frame.** The gateway is a passive listener.

- Every node broadcasts once per round in its own slot. All the others capture CSI from that frame, so a round of N frames measures N(N-1) directed links.
- Each node's frame also carries compact per-link features (motion score, RSSI, packet count) that it computed from the *previous* round. Sensing and backhaul are therefore the same frame, and backhaul costs no extra airtime.
- A gateway on the host's USB only listens. It sends nothing, ACKs nothing, and never forwards raw CSI.
- This is the University of Utah RTI design (token passing, piggybacked RSS, a sink that overhears) brought over to CSI on ESP-NOW ([Wilson & Patwari VRTI](https://arxiv.org/abs/0909.5417); [Bocca et al., multi-Spin](https://arxiv.org/abs/1302.4720); [Anderson et al., USNA roadside RTI](https://arxiv.org/abs/1701.00691)).

| | Hackathon build | Recommended (A) |
|---|---|---|
| Medium access | Free-running CSMA senders | Slotted token round, broadcast with no ACK |
| Links per frame | R receivers | N-1 |
| Backhaul | Raw ASCII CSI lines (≤1400 B each) re-sent over ESP-NOW on the sensing channel | ~4 B per link of features, piggybacked |
| 32 nodes at 20 Hz | Impossible (see §3.1) | ~17 % airtime, ~92 kB/s into the host **[est]** |
| Node ceiling | 4 measured | ~32 at 40 Hz, ~48–64 at 20 Hz on one channel **[est]** |

---

## 1. Sensing topology

### 1.1 Link count and airtime

| Topology | Frames per round | Directed links | Links per frame |
|---|---|---|---|
| Dedicated TX/RX: T senders, R receivers | T | T·R | R |
| All-transceiver, N nodes | N | N(N-1) | N-1 |
| Star (clients sound one AP, as Cognitive Systems does) | N | N | 1 |

- RTI counts each node pair as one link: M = (K²-K)/2 unique pairs for K nodes ([Wilson & Patwari 2008 RTI tech report, SPAN lab](https://span.ece.utah.edu/uploads/RTI.pdf)). A→B and B→A cross the same geometry, but they are two independent samples taken in different slots on different RX chains. They are not wasted.
- **Example, 16 nodes [est]:**
  - Split 8 TX / 8 RX: 64 links from 8 frames.
  - All-transceiver: 240 directed links (120 pairs) from 16 frames.
  - All-transceiver gets about 2× the links per frame and puts a link along every perimeter chord, which is the geometry RTI wants.
- **The cost:** every node needs both TX and RX power and firmware. With ESP32-C5 boards this costs nothing extra in hardware: one board, one firmware, one role flag. It also removes the "is this board a sender or a receiver?" failure that ADR 0003 records (ghost senders). That matters for field deployment.
- **Why not the star:** the star (Cognitive Systems' WiFi Motion: client "leaf" devices sound the AP and the AP runs the ML; [Microwave Journal, Manku & Kravets 2023](https://microwavejournal.com/articles/40518-wi-fi-sensing-the-next-big-evolution-of-wi-fi); [GlobeNewswire 2018 leaf nodes](https://globenewswire.com/news-release/2018/06/05/1516728/0/en/Cognitive-Systems-Extends-WiFi-Motion-Leadership-with-Leaf-Nodes-Bringing-Precise-Motion-Detection-to-the-Smart-Home.html)) scales links linearly. It gives coarse "near which device" localisation only.

### 1.2 What update rate motion detection needs

| Source | Rate used | What it is |
|---|---|---|
| Cognitive Systems (commercial, millions of homes) | "optimal sounding rate is around 10 Hz or 100 ms". Faster rates cost Wi-Fi throughput for little latency gain. | [Microwave Journal 2023](https://microwavejournal.com/articles/40518-wi-fi-sensing-the-next-big-evolution-of-wi-fi) |
| Bocca/Kaltiokallio/Patwari multi-target RTI, 30–32 nodes | A TDMA cycle of ~100 ms per channel (~10 Hz per link per channel). 2.9 ms between transmissions. 5 channels. | [arXiv 1302.4720 §IV-A, §V](https://arxiv.org/abs/1302.4720) |
| ESPectre (open source, ESP32) | Targets 100 pps. 1 s window evaluated every 250 ms. Needs ≥70 % valid slots. | [ALGORITHMS.md](https://github.com/francescopace/espectre/blob/main/docs/ALGORITHMS.md) |
| 802.11bf usage-model table (secondary) | Presence: ~10–100 ms periodicity | [arXiv 2507.22591](https://arxiv.org/html/2507.22591v2) |
| Hackathon | 40 Hz. A score needs ≥5 packets per 1 s window. Calibration rejects links under 15 pkt/s. | ADR 0003 |

**Physics [est]:** a reflector moving at speed v changes the reflected path length at up to ~2v. That gives a Doppler of f_D ≈ 2v/λ.
- λ = c / 2.437 GHz = 0.123 m.
- Walking at 1 m/s: f_D ≈ 16 Hz. A 2 m/s rush: f_D ≈ 33 Hz.
- An amplitude-variance motion score (what the hackathon and ESPectre use) does **not** need Nyquist sampling. It only needs enough samples per window to estimate a variance.
- Doppler or velocity features would need about 2·f_D, i.e. 33–65 Hz.

**Design target:**
- ≥20 Hz per link floor (2× the commercial 10 Hz figure, which leaves room for loss).
- 40 Hz if Doppler features are wanted later.
- The UI updates at 4–10 Hz.

---

## 2. Medium access for the sensing frames

### 2.1 Why the hackathon collapsed

- Free-running `esp_now_send` + `usleep` loops lost about 65 % of packets with 2 senders at 100 Hz, but only 5–10 % at 40 Hz (ADR 0003).
- The team's own analysis (`design/multichannel-tdma`) estimates nominal channel utilisation at only ~3.5 %. It suspects phase-locked collisions between free-running loops.
- Wireless receivers then re-sent raw ASCII CSI lines of up to 1400 B (`FWD_LINE_MAX` in `firmware/csi_recv/main/app_main.c`) as *unicast* ESP-NOW on the same channel 6 (ADR 0019). That adds ACK traffic and the near-gateway AGC lock-up recorded in 0019.

### 2.2 Precedent: token passing with timeouts (Spin family)

**Wilson & Patwari VRTI** (34 nodes through a wall, 802.15.4) ([arXiv 0909.5417 §V-A](https://arxiv.org/abs/0909.5417)):
- "a simple token passing protocol is used. Each node is assigned an ID number and programmed with a known order of transmission … If the next node does not transmit, or the packet is corrupted, a timeout causes each receiver to move to the next node in the schedule so that the cycle is not halted."
- "A base-station node that receives all broadcasts is used to gather signal strength information."

**multi-Spin** ([Bocca et al. arXiv 1302.4720 §IV-A](https://arxiv.org/abs/1302.4720)):
- Nodes "transmit in TDMA fashion based on their ID number. Each transmitted packet contains the ID number of the transmitting node and the most recent RSS measurements of the packets received from the other sensors. At the end of each communication cycle, the sensors switch synchronously to the next frequency channel."
- "the time interval between two consecutive transmissions is 2.9 ms. A sink node that overhears all the packets … stores the RSS measurements."
- The cycle is ~100 ms per channel for ~30 nodes.

**USNA roadside RTI** ([Anderson et al., IEEE JSTSP 2014, arXiv 1701.00691 §III-B](https://arxiv.org/abs/1701.00691)) uses a controller "start packet" as coarse sync:
- Each node's slot is a fixed delay after it receives the start packet.
- "sufficient guard time is built into each time slot … albeit at the cost of a slower network scan time."
- Each packet carries "the most recent RSS values it has recorded from the prior scan (one RSS value for every other node)", plus a checksum.

**Savazzi et al.** use a timed-token TDMA MAC in which nodes regenerate a lost token after a timeout ([Savazzi et al.](https://savazzi.faculty.polimi.it/articles/p2384-savazzi.pdf)).

**RuView ADR-029** (open-source ESP32, *proposed, not validated*) plans the following ([ADR-029](https://github.com/ruvnet/RuView/blob/main/docs/adr/ADR-029-ruvsense-multistatic-sensing-mode.md)):
- 4 nodes, 4 ms TX slots, a 50 ms cycle (20 Hz), a GPIO sync pulse, and 12 multistatic links.
- Channel-switch gaps of 1–5 ms, which are guessed, not measured.

**The pattern is well established. Bring it over directly.**

### 2.3 Proposed MAC: a hybrid of anchored round and implicit token

1. **Round header in every frame:** `round_id` (u16), `slot_idx` (u8), `n_slots` (u8), `schedule_version` (u8), a membership bitmap (4 B for ≤32 nodes, 8 B for ≤64).
2. **Timing from any frame:**
   - Each node timestamps every received frame with `rx_ctrl.timestamp`, the local µs counter already printed by `csi_recv`.
   - It computes `round_start = t_rx - slot_idx * T_slot`.
   - So **any** frame heard acts as a sync beacon. The design needs no coordinator for timing and no cascade failure if one node dies (the VRTI timeout property).
3. **Slot timing:**
   - A node transmits at `round_start + my_slot * T_slot`.
   - If it has heard nothing for one round, it stays silent and listens. It never transmits blind.
4. **Join slot:** a short contention slot at the end of each round (~1 ms) for `JOIN` requests.
   - The lowest-ID node acts as scheduler and assigns slots by bumping `schedule_version`. Failover is simply the next-lowest ID.
   - A node absent for 3 rounds is evicted and the slots compact.
   - This is the same "join + heal" idea as Utah's token protocol. It is also how TSCH-style networks manage slot frames.
5. **Frames are ESP-NOW broadcast.** 802.11 group-addressed frames are not ACKed, so they get no retries and no ACK storms, and every slot has deterministic airtime. ESP-NOW's send callback reports MAC-level success only ([ESP-NOW docs, ESP32-C5](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html)).
6. **Sync accuracy needed:**
   - Crystal drift at a typical ±10 ppm over a 50 ms round is 0.5 µs **[est: 10e-6 × 50 ms]**, matching RuView's ADR estimate. Drift is irrelevant.
   - Software jitter dominates: Wi-Fi task scheduling, callback-to-air latency, and CCA deferral if a foreign transmitter is on the channel.
   - The guard therefore budgets for jitter, assumed 250 µs **[est, must measure]**.
   - The heavier options are not needed:
     - FTM: forum reports suggest >10 µs jitter ([esp32.com thread](https://esp32.com/viewtopic.php?p=114868)).
     - Beacon TSF: needs AP association.

### 2.4 Airtime per slot

HT20, MCS0 (6.5 Mbps, 26 data bits per 4 µs symbol):
- Preamble: L-STF 8 + L-LTF 8 + L-SIG 4 + HT-SIG 8 + HT-STF 4 + HT-LTF 4 = 36 µs.
- ESP-NOW overhead: ≈ MAC header 24 B + vendor action/IE 15 B + FCS 4 B = 43 B. Add 22 service/tail bits.

| Payload | Airtime (MCS0) | Slot (+250 µs guard) |
|---|---|---|
| 4 B (hackathon counter) | 16 sym → 64+36 = **100 µs** | 350 µs |
| 144 B (32 nodes × 4 B features + 20 B header) | 59 sym → **272 µs** | 522 µs |
| 250 B (ESP-NOW v1 max) | 91 sym → **400 µs** | 650 µs |
| 392 B (32 nodes × 12 B rich features) | 135 sym → **576 µs** | 826 µs |

ESP-NOW v2 allows 1470 B payloads. v1 allows 250 B ([ESP-NOW docs](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html)). Use HT20, not the hackathon's HT40. HT20 frees 2.4 GHz spectrum for channel diversity and matches what ESPectre validates ([CSI.md](https://github.com/francescopace/espectre/blob/main/docs/CSI.md)). The CSI-quality impact belongs to the chips slice.

### 2.5 Multi-channel options

- **Clusters on separate channels:** these lose every cross-cluster link, because a radio hears only its own channel. They suit only physically separate areas (floor A, floor B).
- **Synchronized hopping per round (multi-Spin):**
  - Every node moves to the next channel in {1, 6, 11} after each round.
  - Channel diversity reduces deep-fade links and improves RTI accuracy ([Kaltiokallio, Bocca & Patwari, MASS 2012, via arXiv 1302.4720](https://arxiv.org/abs/1302.4720)).
  - It also survives one jammed or busy channel.
  - **The cost:** per-channel rate is divided by 3, and a channel-switch gap occurs each round. The ESP32-C5 switch time is unknown (RuView guesses 1–5 ms). Measure it.
- **5 GHz sensing:** the C5 can sense on 5 GHz, and ESPectre supports it (`5g` band on C5). 5 GHz gives more clean 20 MHz channels, shorter range through walls, and higher RX current (110 vs 87 mA, §5.4). This is a chips/antenna question. The MAC design is band-agnostic.

### 2.6 802.11bf (WLAN sensing): what to borrow, what to wait for

**Status:** IEEE 802.11bf-2025 was approved on 28 May 2025 and published on 26 Sept 2025 ([ANSI webstore](https://webstore.ansi.org/standards/ieee/ieee80211bf2025); [mystandards listing](https://www.mystandards.biz/standard/ieee-802-11bf-2025-26.9.2025.html)).

**Procedure** ([Du et al., "An Overview on IEEE 802.11bf", arXiv 2310.17661](https://arxiv.org/html/2310.17661v1)):
- The phases are capability exchange, measurement session setup, measurement exchange, and termination.
- **Trigger-based (TB) exchange:**
  - The AP polls responders.
  - It runs NDPA sounding (an AP→STA NDP) and/or trigger-frame sounding, in which STAs send NDPs back to the AP.
  - Reports are immediate or delayed.
  - An optional **threshold-based reporting** mode exists: responders first send a "CSI variation" value, and only those above a threshold send full measurements.
- **Non-TB exchange:** an STA-initiated NDPA plus an NDP exchange with the AP.
- **Sensing by proxy (SBP):** a non-AP STA asks the AP to run sensing on its behalf and report results.

**Chips:**
- Infineon's AIROC ACW741x (Wi-Fi 7 IoT, sampling in Jan 2026) lists "IEEE 802.11bf Wi-Fi sensing using CSI" ([CNX Software 2026-01-26](https://www.cnx-software.com/2026/01/26/infineon-airoc-acw741x-wi-fi-7-ultra-low-power-tri-radio-iot-soc-family-support-mlo-wi-fi-sensing-ble-6-0-and-thread/)).
- I found no Espressif 802.11bf support.

**Take-aways:**
- (a) Don't wait for 802.11bf silicon. It is AP-centric (AP polls STAs), and a firefighter drop-kit has no trusted AP.
- (b) **Copy threshold-based reporting.** Each round, send the 1-byte score for every link. Send rich features or raw CSI only for links whose variation crossed a threshold, or on host request. That is the bandwidth lever in §3.
- (c) The NDP idea, sounding with a payload-free frame, is the same goal as keeping our sensing frames short. On ESP32 the nearest equivalent is a short broadcast frame. ESPectre's `wifi_raw` Null Data mode is another ([CSI.md](https://github.com/francescopace/espectre/blob/main/docs/CSI.md)).

---

## 3. Backhaul separate from sensing

### 3.1 Why raw CSI cannot be backhauled at scale [est]

Per link-sample:
- Binary HT20 L-LTF: 64 subcarriers × (int8 I + int8 Q) = 128 B.
- ASCII (hackathon format): ≤1400 B.

| Nodes | Directed links | Raw binary at 20 Hz | ASCII (~1 kB/line) at 20 Hz |
|---|---|---|---|
| 4 (hackathon, 2×2 at 40 Hz) | 4 | 20 kB/s | ~160 kB/s ≈ 1.3 Mbit/s on air, on the sensing channel |
| 8 | 56 | 143 kB/s ≈ 1.1 Mbit/s | 1.1 MB/s |
| 16 | 240 | 614 kB/s ≈ 4.9 Mbit/s | 4.8 MB/s |
| 32 | 992 | 2.5 MB/s ≈ 20 Mbit/s | 20 MB/s |

- 20 Mbit/s of user data on a 2.4 GHz HT20 channel is more than the whole channel delivers in practice. And that same channel is supposed to carry the sensing schedule.
- **Conclusion: beyond ~8 nodes, features must be computed on the node.** Raw CSI becomes a debug path: one link at a time, on request.

### 3.2 On-node DSP budget [est]

- **Per received frame:** amplitude for 52–64 subcarriers (one `sqrtf` or a fast magnitude each), then an update of a running mean/variance (Welford) per subcarrier or per sub-band.
- **ESPectre already does more than this on-device:**
  - A Hampel filter (window 7).
  - The coefficient of variation over 12 fixed subcarriers.
  - Lag-1 autocorrelation and a robust IQR on a 1 s window.
  - Or an 8→24→12→1 MLP with 529 parameters.
  - Sources: [ALGORITHMS.md](https://github.com/francescopace/espectre/blob/main/docs/ALGORITHMS.md); README: "Everything runs on the device" ([repo](https://github.com/francescopace/espectre)).
- **Load at 32 nodes, 20 Hz:** 31 links × 20 Hz × 64 subcarriers ≈ 40k amplitude computations per second. At ~50 cycles each, that is ~2 M cycles/s, about 1 % of a 240 MHz core **[est]**.
- **Doppler/FFT (optional):**
  - esp-dsp measures a 64-point float FFT at 6062 cycles (ANSI RISC-V) and 256 points at 30779 cycles ([esp-dsp benchmarks](https://docs.espressif.com/projects/esp-dsp/en/latest/esp32/esp-dsp-benchmarks.html)).
  - One 64-point FFT per link per 250 ms over 31 links is 31 × 4 × 6062 ≈ 0.75 M cycles/s **[est]**, again ~0.3 %.
- **The real limit is RAM, not CPU [est]:**
  - A 1 s window of 64 float amplitudes × 20 samples × 31 links is 159 kB, which is too much for comfort.
  - Keep streaming statistics instead: 64 × 2 floats × 31 = 16 kB.
  - Or keep 12 subcarriers, as ESPectre does.

### 3.3 Feature record on the wire (binary, not ASCII)

| Field | Bytes |
|---|---|
| motion score, log-quantised u8 | 1 |
| RSSI i8 | 1 |
| frames received last round (u4) + age (u4) | 1 |
| flags / amplitude-mean shift (baseline drift, node-moved hint) | 1 |
| **Compact total per link** | **4** |
| optional: 8 sub-band variances u8 (the "rich" record) | +8 |

- The frame header is ~20 B: round header (§2.3) + node id + an 8 B truncated MAC tag (§5.6).
- **Frame size:** N=32 compact gives 20 + 31×4 = 144 B, which fits ESP-NOW v1. N=64 gives 272 B, which needs v2.
- **Host side:** the gateway sends to the host over USB, and USB Serial/JTAG is full-speed USB. The gateway already uses `usb_serial_jtag_write_bytes`. The hackathon `(host_ts, line)` seam (ADR 0012) stays, with binary records replacing lines.

### 3.4 Backhaul options compared

| Option | Extra airtime on sensing channel | Feasible at 32 nodes | Notes |
|---|---|---|---|
| **Piggyback + passive gateway** (recommended) | 0 (features ride in sensing frames) | Yes, ~92 kB/s to host | Gateway must hear every node, so run several gateways (§5.3). Adds one round of latency (50 ms). No ACKs, which removes the ADR 0019 near-gateway failure. |
| ESP-NOW unicast forward of raw lines (hackathon) | Large; ACK traffic | No (§3.1) | Today's design |
| 5 GHz STA uplink from the *same* C5 | n/a | **Not possible concurrently.** `WIFI_BAND_MODE_AUTO` "does not mean simultaneous dual-band support" ([C5 Wi-Fi overview](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/wifi-driver/overview.html)). The module is "1T1R in 2.4 and 5 GHz dual band" (C5-WROOM-1 datasheet v1.3). | Off-channel hops would drop CSI and add a timing domain, the same reason ADR 0004 rejected AP hopping |
| Second chip per node (C5 #2 on 5 GHz STA to a field AP) | 0 | Yes; per node 31×20×128 B = 79 kB/s even for raw CSI | 2× BOM, +110 mA RX at 5 GHz. Needs a field AP. |
| Thread / 802.15.4 or BLE on the same C5 | Shares the RF by **time division** ("Different modules cannot use the RF path … at the same time") ([C5 coexistence](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/coexist.html)) | No. 802.15.4 runs at 250 kbit/s, but compact features for 32 nodes need 992×4 B×20 Hz = 79 kB/s ≈ 635 kbit/s | Steals sensing RX time |
| ESP-WIFI-MESH | Same channel as the root's router; 10–30 ms per hop | Poor | Network build <60 s; heal <5–10 s ([ESP-WIFI-MESH guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/esp-wifi-mesh.html)). Mesh traffic competes with sensing. |
| Wired: SPI Ethernet (W5500, `espressif/w5500` "supports all targets") / PoE | 0 | Yes | Fixed installs only ([component](https://components.espressif.com/components/espressif/w5500)). Deploy time and cable reach rule it out for responders. |

---

## 4. Existing systems: how they scheduled and scaled

| System | Nodes | Schedule / sync | Backhaul | Lesson |
|---|---|---|---|---|
| Utah VRTI (Wilson & Patwari) | 34 | Ordered token; timeout skips dead nodes | Sink overhears all broadcasts | Token with timeout needs no clock sync ([0909.5417](https://arxiv.org/abs/0909.5417)) |
| multi-Spin (Bocca, Kaltiokallio, Patwari) | 30–32 | TDMA by ID, 2.9 ms per TX, ~100 ms cycle, synchronized hop over 4–5 channels | Each packet carries the RSS of all other nodes; a sink overhears | **The template for architecture A** ([1302.4720](https://arxiv.org/abs/1302.4720)) |
| USNA roadside RTI | 18 | Start packet + fixed slot offsets + guard time | Piggybacked RSS from the previous scan + checksum | Beacon-anchored slots trade speed for simplicity ([1701.00691](https://arxiv.org/abs/1701.00691)) |
| Espressif esp-csi | 1 TX + several RX | One broadcaster ("only one packet sending device will cause little interference"); router ping mode | Serial / RainMaker cloud | No multi-TX scheduling ([repo](https://github.com/espressif/esp-csi)) |
| esp-crab (Espressif) | 2 RX on one shared crystal | n/a | n/a | Phase-coherent multi-RX on one board ([ESP-Techpedia](https://docs.espressif.com/projects/esp-techpedia/en/latest/esp-friends/solution-introduction/esp-csi/esp-csi-solution.html)). Hardware files reportedly not published ([forum](https://esp32.com/viewtopic.php?p=154694)). |
| ESPARGOS (Stuttgart) | 8 ESP32-S2 per array; arrays chain via a shared clock | Receive-only, passive | Wired | A phase-coherent array is a different architecture, a single "super-node" ([arXiv 2502.09405](https://arxiv.org/abs/2502.09405)) |
| ESPectre | 1 per room | Ping/DNS/null-data at ~100 pps to the gateway or one peer. Modelled ESP-NOW airtime: 1.5–1.6 % per device at 100 pps, so 10 devices ≈ 15–16 % | On-device detection; only results go to Home Assistant | On-device scoring plus a 10 s auto-threshold after boot. No layout training. ([CSI.md](https://github.com/francescopace/espectre/blob/main/docs/CSI.md)) |
| Wi-ESP (KIST) | 1 | Client/server traffic through an AP | Serial | A research capture tool ([JCDE 2020, doi:10.1093/jcde/qwaa048](https://api.crossref.org/works/10.1093%2FJCDE%2FQWAA048); [repo](https://github.com/wrlab/Wi-ESP)) |
| ESP32-CSI-Tool (Hernandez) | 1 | AP/station | Serial at 921600+ baud or SD card | Its own README says baud rate "extremely important" for keeping up ([repo](https://github.com/StevenMHernandez/ESP32-CSI-Tool)) |
| RuView | 3–6 ESP32-S3 (+ a 9-node C6 fleet mentioned) | *Proposed* 4-slot TDM, 50 ms cycle, channel hopping; "99.56 % measured ESP-NOW sync match" | UDP to a sink | Many claims are unvalidated; the README itself retracted a "100 % presence" figure ([repo](https://github.com/ruvnet/RuView)) |
| Cognitive Systems WiFi Motion | Existing home APs + clients | Clients send standard frames to the AP at an adjustable rate; ~10 Hz optimal | Software on the AP | Star topology; needs the infrastructure to be there ([Microwave Journal](https://microwavejournal.com/articles/40518-wi-fi-sensing-the-next-big-evolution-of-wi-fi)) |
| Origin Wireless, Plume | n/a | No primary architecture detail found | n/a | Gap; not researched further |

---

## 5. Field deployment

### 5.1 Plug-and-play joining (no per-node config)

- **Identity:** a node's ID is its factory MAC, read from eFuse. No room file lists serials. ADR 0019's failure mode, where an unknown serial is dropped silently, goes away.
- **Joining:**
  1. The node boots and listens for one round, learning the timing and membership bitmap from any frame (§2.3).
  2. It sends `JOIN` in the contention slot.
  3. It gets a slot in the next `schedule_version`.
  - Time to join ≈ Wi-Fi init + 2–3 rounds ≈ 1–2 s **[est; Wi-Fi start time unmeasured]**. Compare ESP-WIFI-MESH's "<60 s" network build.
- **Leaving:** a node is evicted after 3 missed rounds. A rejoin is just a join.
- **Positions:**
  - The heatmap needs node coordinates (ADR 0014). Fastest: the operator drops numbered nodes onto a floor sketch on a tablet. The node ID is printed on the case or shown on a node LED blink.
  - Automatic layout by RSSI or FTM ranging is coarse. FTM indoors is reported at 2–3 m accuracy ([TU Berlin, Rösler 2025](https://www.tkn.tu-berlin.de/bib/roesler2025improving/roesler2025improving.pdf)), which is too coarse for 3 m rooms but fine for "which side of the building".
  - **Open question.**

### 5.2 No per-layout training (ADR 0020) and re-baselining

- **Baseline per link, learned online:** use robust long-window statistics, e.g. the per-link median and IQR of the motion score over the quietest windows of the last few minutes.
  - ESPectre's Lightweight profile auto-calibrates from ~10 s of quiet after boot. It extends in 5 s steps up to 30 s. If it finds no quiet window, it keeps a base threshold. Its MLP profile needs no threshold calibration at all ([ALGORITHMS.md](https://github.com/francescopace/espectre/blob/main/docs/ALGORITHMS.md)).
  - In a fire scene there may be no quiet period, so default fixed thresholds must work out of the box (ADR 0009).
- **Node moved or knocked over:** this shows up as a step change on *all* of that node's links at once, in both RSSI and mean amplitude. A single person crossing moves only a subset of links. The node then sets its "moved" flag (§3.3) and the host re-baselines only those ~2(N-1) links.
  - A $1 accelerometer on the node would make this detection trivial. That is a hardware-slice question.
- Joins, leaves and reboots are routine and must never trigger a global recalibration (ADR 0020's lesson).

### 5.3 Robustness

- **Multiple gateways:** any node with a host link (USB, or a second chip) is a gateway. The host de-duplicates on `(node_id, round_id)`. Put 2 gateways at opposite ends.
- **Range / hidden nodes:** links that never close simply don't exist. Timing still works because it comes from any heard frame.
  - Partitioned groups would form two schedules that collide where they overlap. The bitmap/version merge rule (lower-ID scheduler wins) resolves this. **Needs simulation.**
- **Foreign Wi-Fi on the channel:** the ESP32 still does CCA/backoff. A busy channel delays our frames into the guard. Track per-slot lateness on the host. If the median lateness exceeds the guard, hop channels.
  - Whether ESP32 lets you shorten or disable CCA for ESP-NOW is **unknown**.
- **Scheduler loss:** the next-lowest live ID takes over after 3 rounds with no header update. Timing does not depend on the scheduler.

### 5.4 Battery [est]

Currents from the ESP32-C5-WROOM-1 datasheet v1.3, tables 6-4, 6-5 and 6-8 ([PDF](https://documentation.espressif.com/esp32-c5-wroom-1_wroom-1u_datasheet_en.pdf)):

| Mode | Current |
|---|---|
| 2.4 GHz HT20 RX | 87 mA |
| 5 GHz HT20 RX | 110 mA |
| TX HT20 at 16 dBm | 258 mA |
| Modem-sleep, 160 MHz busy loop | 20–32 mA |
| Light-sleep | 0.25 mA |

**N=32, 20 Hz, compact frames:**
- RX is always on: 87 mA.
- CPU work: +~15 mA.
- TX: 272 µs / 50 ms = 0.54 % × 258 mA = 1.4 mA.
- Total ≈ **~105 mA at 3.3 V ≈ 0.35 W**.

| Battery | Energy | Runtime (85 % converter) |
|---|---|---|
| 18650, 3000 mAh × 3.6 V | 10.8 Wh | 10.8 × 0.85 / 0.35 ≈ **26 h** |
| 1000 mAh LiPo | 3.6 Wh | ≈ **8.7 h** |

- **RX duty-cycling between rounds:** a round is busy for 17 ms of every 50 ms, so in theory the radio could sleep about 60 % of the time. ESP-NOW does support power-save via `esp_now_set_wake_window` ([ESP-NOW docs](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html)). Whether the wake latency fits a 33 ms gap is **unknown**.
- **Brown-outs:** memory notes say "boards die on batteries" at the hackathon. The cause is not established here. Likely candidates:
  - power-bank auto-off at low current;
  - brown-out at the 258–313 mA TX peaks.
  - Forensics slice.

### 5.5 Time-to-deploy target

Drop 8–16 nodes around a perimeter. Each boots in ~1–2 s and self-joins. The operator places the dots on a floor sketch (~1–2 min). Baselines settle in ~10–30 s. **Target: under 5 min to first heatmap. [est]**

### 5.6 Security (spec-ops / fire)

- ESP-NOW encryption is not supported for broadcast/multicast. It is limited to ≤17 encrypted unicast peers ([ESP-NOW docs](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/network/esp_now.html)).
- Use an application-layer truncated MAC (e.g. AES-CMAC or HMAC, 8 B) over each frame, plus `round_id` for replay protection, with a kit-wide key provisioned at charge time. This gives authenticity, not confidentiality.
- Hiding is impossible: a 20–40 Hz broadcast beacon train is trivially detectable. Flag this for the spec-ops use case.

---

## 6. Candidate architectures

### A. Single-channel token round, piggybacked features, passive gateway(s): **recommended**

- **Nodes:** identical ESP32-C5s, all transceivers, 2.4 GHz HT20 on one surveyed channel, MCS0 broadcast, ESP-NOW v1 frames (v2 above 32 nodes).
- **Scaling [est, §2.4 arithmetic, 250 µs guard, compact 4 B/link]:**

| N | Frame | Slot | Round (+1 ms join) | Max round rate | At target rate | Directed links | Host bytes/s |
|---|---|---|---|---|---|---|---|
| 8 | 48 B → 152 µs | 402 µs | 4.2 ms | ~240 Hz | 40 Hz: 5 % airtime | 56 | 8×48×40 = 15 kB/s |
| 16 | 80 B → 192 µs | 442 µs | 8.1 ms | ~120 Hz | 40 Hz: 12 % | 240 | 16×80×40 = 51 kB/s |
| 32 | 144 B → 272 µs | 522 µs | 17.7 ms | ~56 Hz | 20 Hz: 17 %; 40 Hz: 35 % | 992 | 32×144×20 = 92 kB/s |
| 64 | 272 B → 428 µs | 678 µs | 44 ms | ~22 Hz | 20 Hz: 55 % airtime, fully scheduled | 4032 | 64×272×20 = 348 kB/s |

- **Practical ceiling:** ~32 nodes at 40 Hz, or ~48–64 at 20 Hz. Rich 12 B/link records at N=32 give 576 µs frames: max ~36 Hz, or 37 % airtime at 20 Hz.
- **Failure modes:**
  - Gateway out of range of a node: mitigated by multiple gateways.
  - Unmeasured software TX jitter bigger than the guard means slot overlap. Measure first, then widen the guard or move to pure token triggering.
  - CCA deferral from foreign Wi-Fi.
  - Merging partitioned schedules.
  - One-round feature latency.
  - The whole kit on one channel is a single point of interference.

### B. A + synchronized channel hopping (multi-Spin on Wi-Fi)

- Same as A, but every round moves to the next of {1, 6, 11}, or to 5 GHz UNII-1 channels on the C5.
- **Scaling [est]:** at N=32 a round is 17.7 ms + an unknown switch time (assume 3 ms) ≈ 21 ms, i.e. ~48 rounds/s. That gives ~16 Hz per link per channel and 48 Hz per link across channels.
- **Gains:** frequency diversity against deep fades (RTI accuracy, Kaltiokallio 2012) and survival of one jammed channel.
- **Failure modes:**
  - The switch time and the post-switch AGC/gain-lock behaviour are unknown. The C5 gain-lock quirk is already seen in ADR 0019.
  - Gateways must hop too.
  - Baselines become per (link, channel), which triples the state and the settling time.
- Adopt this only after A works.

### C. A + out-of-band backhaul (dual-chip nodes or wired), for research or fixed installs

- The sensing radio is as in A. A second chip (a C5 on 5 GHz as STA to a field AP, or W5500 Ethernet/PoE) carries features *and* optional raw binary CSI.
- **Scaling [est]:** raw binary CSI at N=8 and 40 Hz is 56 links × 40 × 128 B = 287 kB/s ≈ 2.3 Mbit/s total, or 36 kB/s per node. That is easy over a 5 GHz AP or Ethernet. At N=16 it is 1.2 MB/s ≈ 9.8 Mbit/s total, which is plausible for one 5 GHz AP. At N=32 raw is ~20 Mbit/s **[est]**. Use features.
- **Failure modes:**
  - 2× cost and power (+110 mA RX).
  - Needs a field AP: infrastructure, setup time, a security surface.
  - Cabling is not viable for rapid deployment.
- **Use cases:** collecting datasets, developing algorithms, permanent training-ground installs.

### Rejected

- **Dedicated TX/RX split:** half the links per frame, and receivers would have to transmit to backhaul anyway.
- **Infrastructure-AP star or ESP-WIFI-MESH:** depends on an AP and puts mesh traffic on the sensing channel.
- **802.11bf:** no Espressif silicon, and it is AP-centric.
- **Thread/BLE backhaul on the same chip:** it time-shares the RF and is too slow.

---

## 7. Open questions (to settle by experiment, in this order)

1. **TX jitter:** time from "slot due" (an `esp_timer` callback) to frame on air with ESP-NOW broadcast on a C5. This sets the guard. Measure with a second C5 timestamping `rx_ctrl.timestamp`, or an SDR. Re-run the ADR 0003 experiment: 2 nodes at 100 Hz, CSMA vs slotted.
2. **Does CSI capture keep up with back-to-back frames ~400–500 µs apart from different senders?** CSI callback cost and queue depth under 31 frames per round.
3. **Channel-switch time and the AGC settling after a switch** on the C5 (needed for B).
4. **RX sleep between rounds:** the wake latency of ESP-NOW's wake window vs a ~30 ms gap. Battery life roughly doubles if it works.
5. **Can CCA/backoff be bypassed or bounded** for ESP-NOW? If not, how does a busy foreign channel stretch slots?
6. **Feature design:** is the 1-byte log score plus RSSI enough for the host heatmap (ADR 0011), or do we need sub-band variances? Answer it offline by replaying the hackathon recordings (ADR 0018) through an on-node-equivalent feature extractor.
7. **Node placement entry** in under 2 min without training, and whether auto-layout via RSSI/FTM is worth it.
8. **Partition/merge behaviour** of the schedule: simulate it before building.
9. Origin Wireless / Plume architecture details. Not found in primary sources; low priority.
