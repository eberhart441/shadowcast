# 05 — Sensing radio / platform survey (size vs capability)

Research date: 2026-11-10. Scope: which radio to put in each node of a post-hackathon redo of the WiFi-CSI "is anyone moving in there (ideally through a wall)" system. Antennas, network architecture and the hackathon forensics are covered in other reports.

How to read the citations: **[P]** means a primary source (datasheet, vendor doc, tool README, or peer-reviewed/arXiv paper by the tool authors). **[S]** means a secondary source (news, distributor, forum, third-party project). **[E]** means my own estimate or derivation; the arithmetic is shown.

---

## 0. TL;DR

- **The ESP32-C5 is still the best tiny node.** It is the only Espressif part with 5 GHz, and Espressif ranks it top for CSI ("ESP32-C5 > ESP32-C6 > ESP32-C3 ≈ ESP32-S3 > ESP32") [P][1]. It offers HT40 CSI (114 usable subcarriers) or HE20-SU CSI (242 usable subcarriers) [P][2], and an official AGC/FFT gain-lock component [P][3]. It is 1T1R [P][4]. The hackathon stalled at 4 nodes for reasons covered in the other reports, not because of the radio.
- **The biggest upgrade is coherent multi-antenna, not a fancier chip.** Every Espressif part is 1T1R. Real MIMO-style CSI means either several ESP32s sharing a clock (Espressif **esp-crab**, 2× C5 on one crystal [P][5]; Uni Stuttgart **ESPARGOS**, 8× ESP32 in a 2×4 phase-coherent array [P][6]) or a multi-chain NIC.
- **The richest COTS CSI is the Intel AX210.** It is 2x2, up to 160 MHz, covers 2.4/5/6 GHz, and has two tools: **FeitCSI** (GPL-3.0, x86-64 and ARM) [P][7] and **PicoScenes** (largely closed) [P][8]. It needs a Linux host with a PCIe slot (M.2 2230, 22×30 mm [P][9]), so it only fits the "puck" tier.
- **New finding: NXP has official, vendor-supported CSI on MCU-class parts.** This includes the RW612 (an MCU with 1x1 dual-band Wi-Fi 6 at 20 MHz) under FreeRTOS, and IW61x/88W9098/AW693 under Linux. NXP also ships a built-in "Ambient Motion Index" [P][10][11]. RW612 modules are about 12×11 mm [S][12]. It is the only serious non-Espressif tiny-tier candidate, but the community is far smaller.
- **Ruled out for nodes.** The nRF7002 has no CSI [S][13]. ESP32-P4 + C6 over ESP-Hosted has no CSI (the CSI API is listed as "unsupported" in hosted) [P][14]. The Raspberry Pi Zero 2 W has no working Nexmon CSI [S][15]. The Intel 5300 and Atheros tools are legacy (Mini-PCIe or old kernels).
- **Non-WiFi.** 60 GHz radar (BGT60TR13C 6.5×5 mm [S][16], Acconeer A121 5.5×5.2 mm [P][17], TI IWR6843AOP 15×15 mm [P][18]) is tiny and very good at line-of-sight presence and breathing, but it does not usefully penetrate walls [S][19]. Sub-10 GHz UWB radar (Novelda X7, Qorvo DW3000 CIR) is the non-WiFi option with real wall penetration [S][19][20][21]. BLE Channel Sounding does device-to-device ranging, not device-free sensing.

---

## 1. Physics that drives the choice

These figures are [E] and computed from first principles. c = 3e8 m/s.

| Parameter | 2.4 GHz | 5.5 GHz | 6.5 GHz | Why it matters |
|---|---|---|---|---|
| Wavelength λ | 12.5 cm | 5.45 cm | 4.6 cm | Antenna and array size |
| λ/2 element spacing (AoA array) | 6.2 cm | 2.7 cm | 2.3 cm | A 4-element 2.4 GHz ULA is about 19 cm long; at 5 GHz it is about 8 cm. **At 5 GHz an AoA array fits a puck or credit card.** |
| Max bistatic Doppler for a person walking at 1.5 m/s, ≈2v/λ | ≈24 Hz | ≈55 Hz | ≈65 Hz | Needs ≥2× that in complex CSI samples/s: about 50–150 Hz minimum. 100 Hz is a sane floor; 200–1000 Hz is used for gestures [S][22]. |
| Wall loss | lower | higher | higher | Loss in brick, concrete and drywall rises with frequency across 0.5–8 GHz (NIST measurements) [P][23]. 60 GHz is effectively line-of-sight only [S][19]. |

| Bandwidth | Delay (range) resolution c/(2B) |
|---|---|
| 20 MHz | 7.5 m |
| 40 MHz | 3.75 m |
| 80 MHz | 1.9 m |
| 160 MHz | 0.94 m |

**Implications:**
- **Subcarrier count is not bandwidth.** HE20 gives 242 subcarriers but still spans about 20 MHz. It samples the channel 4× more finely in frequency, which helps averaging and SNR, but it does **not** improve delay resolution. HT40 does.
- **Wider bandwidth measurably helps CSI localization.** In an SDR study, accuracy rose with bandwidth up to 320 MHz, but 20 MHz still gave >80% position-classification accuracy and 80 MHz was "likely sufficient" [P][24].
- **Through-wall favours 2.4 GHz for penetration and 5 GHz for Doppler sensitivity and small arrays.** No controlled ESP32 study comparing the two bands through walls was found. Next2You found 5 GHz better in small and medium rooms and worse in large, obstacle-heavy ones [P][25]. **Recommendation: the redo must be able to run both bands, which favours the ESP32-C5.**

---

## 2. Espressif family: CSI capabilities

All of these chips do CSI through the same ESP-IDF API (`esp_wifi_set_csi_*`) and the esp-csi examples [P][26]. The official chip ranking for CSI quality is "ESP32-C5 ＞ ESP32-C6 ＞ ESP32-C3 ≈ ESP32-S3 ＞ ESP32" [P][1].

| Chip | Bands | Wi-Fi / max BW | Antennas | CSI content (usable subcarriers) | Gain lock (esp_csi_gain_ctrl) | Smallest module / board | RX current (datasheet) | Price (board) |
|---|---|---|---|---|---|---|---|---|
| **ESP32-C5** | 2.4 + 5 GHz | 11ax **HE20 only**; 11ac VHT20; 11n **20/40 MHz** [P][4] | 1T1R, antenna-diversity switch [P][4] | LLTF 52 (53 bins); HT20 56; **HT40 114**; **HE20-SU 242** (245 bins, −1/0/+1 invalid). `acquire_csi_force_lltf` selects LLTF or HT/VHT/HE-LTF [P][2] | Yes [P][3] | Chip QFN48 6×6 mm [P][4]. **ESP32-C5-MINI-1U 15.4×15.4×2.4 mm** (U.FL) [S/P][27]. MINI-1 (PCB antenna) 15.4×21.3 mm [S][27]. **XIAO ESP32-C5 21×17.8 mm, U.FL antenna, Li-ion charger, 8 MB flash + 8 MB PSRAM** [P][28] | 2.4 GHz RX 87 mA (HT20/HE20), 91 mA (HT40). **5 GHz RX 110 mA** (113 mA HT40) [P][4] | XIAO ≈ US$7–8 [S][29] |
| ESP32-C6 | 2.4 GHz | 11ax HE20 only; 11n 20/40 MHz [P][30] | 1T1R, diversity [P][30] | Same CSI API family. HE20/HT40 like the C5 but 2.4 GHz only (assumed same layout, not separately verified) | Yes [P][3] | Chip QFN40/32 5×5 mm [P][30]. **XIAO ESP32-C6 21×17.8 mm, ceramic antenna + U.FL selectable via GPIO14 (GPIO3 enables the switch), deep sleep 15 µA** [P][31] | RX ≈ 78–82 mA [P][30] | ≈ US$5 [E] |
| ESP32-C61 | 2.4 GHz | 11ax "20 MHz-only non-AP"; 11n 20/40 MHz [P][32] | 1T1R, diversity [P][32] | As C6 | Yes, since gain-ctrl v0.1.4 [S][33] | Chip 5×5 mm [P][32]. MINI-1 13.2×16.6×2.4 mm [S][34] | RX 88 mA HT20/HE20, 90 mA HT40 [P][32] | Module ≈ US$2 [S][34] |
| ESP32-S3 | 2.4 GHz | 11n 20/40 MHz (no 11ax) [P][35] | 1T1R, diversity [P][35] | LLTF + HT-LTF + STBC-HT-LTF. Up to 612 bytes per packet for HT40 STBC. **`first_word_invalid`** hardware quirk [P][36] | Yes (component lists S3) [P][3] | Chip QFN56 7×7 mm [P][35]. XIAO ESP32-S3 21×17.8 mm (U.FL) | RX 88 mA HT20, 91 mA HT40 [P][35] | ≈ US$7 [E] |
| ESP32 (classic) | 2.4 GHz | 11n 20/40 MHz [P][37] | 1T1R, diversity [P][37] | LLTF/HT-LTF (as S3) | Component lists ESP32 [P][3], but ESPectre reports **AGC/FFT gain lock unavailable** on the original ESP32 [S][38] | QFN 5×5 / 6×6 mm [P][37] | RX 95–100 mA [P][37] | cheap; **lowest CSI rank, avoid** |
| ESP32-S2 | 2.4 GHz | 11n | 1T1R | CSI supported [P][26] | Yes [P][3] | — | — | Used ×8 in ESPARGOS [S][39] |
| ESP32-P4 (+C6/C5 co-proc) | via co-proc | via co-proc | — | **No CSI.** In ESP-Hosted-MCU's coverage manifest, `esp_wifi_set_csi` is listed as "unsupported: CSI API is not implemented in hosted" [P][14]. You would need custom co-processor firmware that forwards CSI itself. | — | — | — | Not worth it for sensing nodes |
| **ESP32-E22** (new, 2026) | 2.4/5/6 GHz | Wi-Fi 6E, **160 MHz, 2×2 MU-MIMO** [S][40] | **2x2** | **Unknown.** No CSI info published. It is a PCIe/SDIO radio co-processor with an early Linux driver (STA mode) [S][40] | — | M.2 module (ESP32-E22-M2-1) [S][40] | — | Pre-GA, no pricing [S][40]. **Watch this part:** if Espressif exposes CSI it would be the first 2x2 160 MHz Espressif CSI radio. |

### Espressif details that matter for the redo

- **Frame sources.** CSI is produced for any received PPDU that passes the filter: ESP-NOW, beacons, data, and ACKs on newer chips (field `dump_ack_en` referenced in the C5 API; not verified on the docs page). A station receives nothing while disconnected, so the docs suggest promiscuous mode to get more CSI [P][36]. HE-LTF CSI requires the transmitter to send **HE SU PPDUs**. Legacy ESP-NOW frames give only LLTF/HT-LTF. Check that the sender's PHY mode is set to HE20 when 242-subcarrier CSI is wanted [E; verify `esp_now_set_peer_rate_config` phymode on C5].
- **Data format.** int8 imaginary then int8 real per subcarrier [P][2]. The `rx_ctrl` struct carries RSSI, noise floor, a µs RX timestamp (local clock), channel/secondary, and signal mode [P][2][36]. The timestamp is a local free-running µs counter, so cross-node alignment needs your own sync, which is a networking-report topic.
- **Gain lock.** `esp_csi_gain_ctrl` reads the AGC and FFT gain from each packet's rx_ctrl, builds a baseline, and either compensates CSI amplitude or forces a fixed RX gain. Forcing gain may cause packet loss [P][3]. The esp-radar changelog added gain compensation "to improve detection accuracy" and C5/C6/C61 support in v0.3.0 [S][33]. AGC is the dominant source of cross-device CSI differences, and a simple gain alignment recovers up to 75% of lost cross-device accuracy [P][41]. **Use gain lock or compensation on every node.**
- **Packet rate.** Espressif publishes no maximum CSI rate. ESPectre targets about 100 pps by default and allows 0–1000 pps [S][42]. Older ESP32 tools reported 85–180 pps over serial [S][43]. In practice the limits are airtime (all nodes share one channel), the Wi-Fi-task callback (do heavy work elsewhere [P][36]) and the UART/USB/network drain. **Plan on 100–200 Hz per link and treat >500 Hz as an experiment** [E].
- **Multiple antennas on one chip.** Up to 16 antennas can be attached via `esp_phy_set_ant_gpio` and an external RF switch, but only one or two are enabled at a time [P][44]. This gives time-multiplexed **diversity**, not simultaneous coherent MIMO. Each packet gets a fresh CFO and phase offset, so cross-antenna phase from different packets is not directly usable [E]. Espressif's own motion-detection board uses an ESP32-C3 with three directional antennas behind a switch [P][1].
- **Coherent multi-ESP32 (real MIMO-like CSI):**
  - **esp-crab (Espressif).** Two ESP32-C5s share one crystal ("co-crystal oscillator") on one board. Two modes: self-TX/RX (claimed "millimeter-level" disturbance sensing) and single-TX/dual-RX, which reports **inter-receiver phase difference** [P][5]. Caveats: the hardware files are not in the repo, and users report the slave C5 failing to boot from a shared clock [S][45]. It also needs 100 packets at power-up to set RX gain [P][5].
  - **ESPARGOS (Univ. Stuttgart, 2025).** A 2×4 array of ceramic antennas on one 4-layer FR4 board, built from ESP32 receivers (reported as ESP32-S2FH4 [S][39]) daisy-chained on a 40 MHz reference clock. A Wi-Fi phase-reference signal is distributed over coax and RF switches for periodic calibration. Multiple boards can be combined coherently, and MUSIC AoA separates transmitter directions [P][6]. Host side: an SPI header, with an Ethernet controller board reported [S][39]. Python library: pyespargos [S][39]. **This is the existence proof that a phase-coherent ESP32 AoA array is buildable.** A 5 GHz C5 version would be about half the size [E].

---

## 3. Non-Espressif Wi-Fi CSI platforms

| Platform | Antennas / streams | CSI richness | Host | Size of radio | Power | Tooling / licence / hacks | Maturity, availability |
|---|---|---|---|---|---|---|---|
| **Intel AX210** (AX200 similar, no 6 GHz) | **2x2** (HE NSS 2 at 160 MHz [S][46]) | All formats 11a/g/n/ac/ax, 20–160 MHz, 2.4/5/**6 GHz** (5945–7125 MHz) [P][7][8]. PicoScenes slides claim up to 8192 subcarriers per packet (AX210/211) [S][47]. Up to ~1 kHz CSI measurement at 20 MHz (PicoScenes, single thread) [P][8] | Linux x86-64 or ARM with PCIe (FeitCSI: "every architecture where Linux can be installed", NIC "via PCI") [P][7] | **M.2 2230: 22×30×2.4 mm** [P][9] | Third-party module peak 1.54 A @3.3 V, about 5 W [S][48]. Typical is lower [E] | **FeitCSI: open source, GPL-3.0**, CSI + injection [P][7]. **PicoScenes**: Ubuntu 22.04, mostly closed source [P][8][24] | High. Most capable COTS CSI. AX210 in stock everywhere (≈US$15–25 [E]) |
| Intel 5300 (Halperin CSI Tool) | 3 RX | 30 subcarrier groups, 8-bit, 20/40 MHz [P][49] | Linux, old kernels, Mini-PCIe | Half-size Mini PCIe | — | Modified firmware + driver [P][49] | **Legacy and discontinued** [S][50]. Don't use. |
| Atheros CSI Tool (AR9580/AR9590/AR9344/QCA9558) | up to **3x3** (AR9580) [P][51] | 56 (20 MHz) / 114 (40 MHz) subcarriers × Ntx × Nrx [P][52] | Linux / OpenWrt (ath9k, "no firmware hacks") [P][52] | Mini-PCIe or router SoC | router-class | Open source | 802.11n only, legacy hardware. QCA9300 is also supported by PicoScenes with manual RX gain 0–66 dB [P][8] |
| **Nexmon CSI** (Broadcom) | bcm43455c0: **1 antenna** on Pi [S][53]. bcm4366c0 (Asus RT-AC86U): **4x4** [P][54] | Up to **80 MHz**: 64/128/256 subcarriers for 20/40/80 MHz, int16 I/Q on 43455c0. One UDP packet per core × spatial stream [P][54] | Raspberry Pi 3B+/4B/**5** (43455c0, recent kernels via Makefile.rpi) or Asus RT-AC86U [P][54] | Pi 4/5 board 85×56 mm. CM4/CM5 55×40 mm [E: well-known, not re-verified] | Pi 4/5 ≈ 3–6 W [E] | Patched firmware. Rate set by the transmitter. Router crashes reported at high rates [S][53]. **Pi Zero 2 W (bcm43436) not supported for CSI** [S][15] | Mature research tool. 11ac only (no HE) |
| **AX-CSI** (Broadcom BCM43684, Asus RT-AX86U) | **4x4** | 802.11ax, up to 160 MHz [P][55] | Router | Router | Router | On request (axcsi@unibs.it) [P][55] | Research only. A router, not a node |
| **ZTECSITool** (MediaTek MT7916, ZTE AX3000) | 3 TX/RX at 5 GHz, 2 at 2.4 GHz | 11n/ac/ax, **160 MHz, 512 subcarriers** [P][56] | PC collector over LAN. One band at a time, ≥500 ms between commands [P][56] | Router | Router | Experimental firmware by email [P][56] | Research only |
| **NXP Wi-Fi 6 family: RW610/RW612 (MCU), IW610/611/612, AW611, plus Wi-Fi 5 88W8997/88W9098/AW69x** | RW612 and IW612: **1x1** [P][57][58]. CSI header has `nRx`; NXP documents 2-RX × 4-TX cases [P][10]. 88W9098/AW693 are 2x2-class (not re-verified) | **Official vendor CSI** with header (RSSI, NF, FCF, BW, PSB) and tones following beamforming-feedback grouping. Wi-Fi 6: 20 MHz → 52/30/64 tones; 40 MHz → 108/58/122; 80 MHz → 234/250 (by Ng) [P][11]. Built-in **Ambient Motion Index (AMI)** [P][10][11] | **RW612: standalone MCU** (Cortex-M33 260 MHz, FreeRTOS, AN14281) [P][11][58]. IW61x and others: Linux (AN14011, mlan driver) or FreeRTOS host [P][10] | **RW612: Murata Type 2FR 12.0×11.0×1.5 mm**; Insight SiP ISP5261 12×12×1.8 mm [S][12]. **IW612: Murata Type 2EL 7.7×8.8×1.3 mm** [S][59] | — (not found) | **No hacks, vendor app notes** (AN14011 Rev 10.2, June 2026; AN14281 Rev 4.0, Jan 2026) [P][10][11]. A user had trouble getting callbacks on FRDM-RW612 [S][60] | Commercial parts in volume. Tiny research community. **Most underrated option** |
| openwifi (FPGA SDR, e.g. ADRV9364-Z7020) | AD9361/9364 RF, 1–2 chains | Full PHY access. CSI + equalizer + freq offset + 64-bit TSF via "side channel" [P][61] | Zynq Linux | SOM-sized, several cm | several W | Open source (AGPL; not re-verified) | Lab tool. Too big and power-hungry for nodes |
| PicoScenes + USRP/HackRF SDR | up to 4x4 (SDR) | Arbitrary, up to 802.11be in SDR [P][8][24] | PC | Large | High | Partly commercial | Lab only |
| **Nordic nRF7002** | 1x1 | **No CSI.** Nordic: "not available on the nRF70 series. It will only be available on the nRF71 series" [S][13]. Radio-test raw IQ capture exists but is not a sensing API [S][13] | — | — | — | — | Excluded |
| Nordic nRF71 (nRF7120 / nRF7120E) | 1x1 (assumed) | CSI promised per Nordic forum [S][13]. Not on the product pages [S][62] | Standalone M33 + NPU SoC | — | "20× lower standby" claim [S][62] | — | **Sampling only (announced Nov 2026)** [S][62]. Revisit in 2027 |
| Silicon Labs SiWx917 | 1x1 2.4 GHz | A SiLabs talk lists "Sensing with CSI" as a topic tied to 802.11bf [S][63]. No public CSI API found | MCU | — | Very low | — | Unconfirmed |
| Infineon AIROC (CYW55xxx), Realtek | — | **No CSI tool found** [S][63][64] | — | — | — | — | Excluded |
| **IEEE 802.11bf (WLAN sensing)** | — | Standard **approved 2025-05-28, published 2025-09-26** [S][65]. Defines sensing measurement setup and reporting between STAs [S][66]. No consumer chipset exposing 802.11bf to developers was found | — | — | — | — | Long-term path. Not usable now |

---

## 4. Non-WiFi alternatives for the same job

| Sensor | Band / type | Through walls? | Size | Power | Notes |
|---|---|---|---|---|---|
| **Novelda X7 (X7F202)** | UWB impulse radar (≈6–8 GHz class [E]) | **Some.** UWB below 10 GHz penetrates drywall and masonry better than mmWave [S][19]. Novelda markets it for presence and in-cabin use, not through-wall | Module with 2 patch antennas, LGA [S][20]. Dimensions not found | **<50 µW at 1 fps** presence (vendor claim) [S][20] | Eval kit range up to 12 m [S][20]. Closed presence firmware. Best power by far |
| **Qorvo DW3000 / DWM3000** | UWB 802.15.4z, ch 5/9 | Some (as above) | **DWM3000 23×13×2.9 mm** [P][21] | — | The CIR is accessible. Bistatic CIR radar for device-free localization is published [P][67], but there is no vendor "radar mode": you build the radar yourself [S][21]. Can also range to tags carried by firefighters |
| **Infineon BGT60TR13C** | 60 GHz FMCW (58–63.5 GHz), **1 TX / 3 RX, antenna-in-package** | **No** (drywall at best) [S][19] | **6.5×5.0×0.9 mm** [S][16] | low mW range [E] | 3 RX gives azimuth and elevation. Excellent for micro-motion and breathing in line of sight |
| **TI IWR6843AOP** | 60–64 GHz FMCW, **3 TX / 4 RX AoP**, R4F + C674x DSP on chip | **No** | **15×15 mm** package [P][18] | ~1 W class [E] | People counting and tracking with point clouds. Biggest and most capable mmWave single chip |
| **Acconeer A121** | 60 GHz pulsed coherent radar, AiP | **No** | **5.5×5.2×0.88 mm** [P][17] | "<80 µW" presence claim (company-level) [S][17] | Presence up to 7 m [P][17]. Tiny and ultra-low power |
| HLK-LD2410(B/C/S) | 24 GHz FMCW module | Weak (thin drywall) [E] | **7×35 mm** [S][68] | LD2410C ≈79 mA @5 V. LD2410S ≈0.12 mA average [S][68] | ~US$3. Proven "someone is in this room" sensor, line of sight |
| **BLE Channel Sounding** (BT 6.0, nRF54L15) | 2.4 GHz phase-based ranging + RTT | n/a | nRF54L15 small CSP/QFN (not verified) | low | **Ranges between two devices; not device-free.** Nordic: about 0.5 m best case, the SDK solution is "experimental" [S][69]. Good for locating your own team's tags, not for finding unknown people |
| Walabot-class / dedicated through-wall radar | 3–10 GHz UWB imaging (Walabot ≈6.3–8 GHz, 0.14 mW per a paper [S][19]) | Yes (designed for it) | Phone-sized | — | Shows the right band for through-wall: **< 10 GHz** [S][19] |

**Takeaway:** for *through-wall* detection the candidates are, in order, sub-6 GHz Wi-Fi CSI and sub-10 GHz UWB. mmWave (24/60 GHz) is a superb, tiny **same-room** sensor. It would suit a node that is thrown *into* a room, not one placed outside it.

---

## 5. What multi-antenna (MIMO) CSI buys

1. **Spatial diversity, so more reliable motion detection.** A single-antenna link can sit in a multipath null where a person's motion barely changes |H|. N independent receive antennas give N partly independent views and fewer missed detections [E; standard diversity argument]. Even a time-multiplexed switched antenna (ESP32 `esp_phy_set_ant_gpio`) gets part of this benefit.
2. **Clean phase.** On commodity Wi-Fi, per-packet CFO, SFO and PLL phase make raw CSI phase nearly random. **The phase difference between two antennas on the same clock cancels those common offsets.** This yields a stable phase observable for Doppler and micro-motion such as breathing, and for detecting slow movers that amplitude misses. esp-crab exists for exactly this [P][5]. ESPARGOS shows small inter-antenna phase variance after calibration [P][6].
3. **Angle of arrival.** You need at least 2 coherent antennas, and more for multipath resolution (MUSIC) [P][6]. Each node then gives a bearing instead of just "motion on link X", so 2–3 nodes can triangulate a mover instead of needing a dense mesh. Note: the trained walk-through "person dot" was rejected in the hackathon. AoA is a physics-based alternative that needs no training.
4. **More links per node.** Nt×Nr sub-links per packet. The AX210 gives 2×2 = 4, and the AC86U Nexmon setup gives up to 4×4.
5. **Caveats.**
   - Coherence must be engineered: same chip, or a shared reference clock plus a phase reference.
   - Independent ESP32s or NICs are *not* coherent [P][6].
   - Arrays cost size: about 2.7 cm per element at 5 GHz and 6.2 cm at 2.4 GHz [E].
   - One 2026 ESP32 study found that adding links *decreased* motion-classification accuracy. Link selection mattered 2.7× more than the algorithm [S][70]. More antennas or links help only if the pipeline picks or weights them well.

---

## 6. Ranked shortlist by size tier

### Tier A — tiny (≈ XIAO, 21×17.8 mm + LiPo)

1. **ESP32-C5 (XIAO ESP32-C5, or a custom board on ESP32-C5-MINI-1U 15.4×15.4 mm).**
   - Why: dual-band, HT40 (114 subcarriers) or HE20 (242), gain lock, top Espressif CSI rank, and the team already knows it.
   - Costs: 5 GHz RX is ~110 mA, so plan the battery. The XIAO has a U.FL only, so an external antenna is mandatory.
2. **ESP32-C6 (XIAO ESP32-C6).**
   - Why: 2.4 GHz only (better wall penetration, no 5 GHz), lower RX current (~80 mA), on-board ceramic **plus** U.FL with a GPIO switch (cheap 2-antenna diversity), and the most-used CSI chip in the community. The C61 is the cost-down variant.
3. **NXP RW612 (Murata Type 2FR, 12×11 mm).**
   - Why: dual-band Wi-Fi 6 at 20 MHz, 1x1, **official vendor CSI and a built-in motion index**, standalone MCU.
   - Costs: needs a custom carrier board, there is a small CSI community, and it has no shared-clock trick. Worth a 2-board evaluation if Espressif quirks keep biting.
   - (Honourable mention for a *non-WiFi* tiny same-room node: Acconeer A121 or BGT60TR13C.)

### Tier B — small (≈ credit card, 85×54 mm)

1. **Coherent 2–4 × ESP32-C5 array at 5 GHz (esp-crab style shared crystal, or ESPARGOS-style clock + phase-reference distribution).**
   - Why: true inter-antenna phase and AoA. A 4-element λ/2 array at 5.5 GHz is about 8 cm, so it fits.
   - Costs: custom RF board design, and the clock-sharing boot issue must be solved [S][45]. Highest payoff per gram.
2. **NXP IW61x (Murata 2EL 7.7×8.8 mm) or another NXP CSI part on a small Linux/MCU board.**
   - Why: official CSI up to 80 MHz (1.9 m delay bins), dual-band, vendor-supported.
   - Costs: single RX on IW612. Check 88W9098/AW693 for 2x2.
3. **Raspberry Pi 5 / CM5 + Nexmon CSI (bcm43455c0).**
   - Why: 80 MHz, 256 subcarriers, mature research tool.
   - Costs: 1 antenna, a few watts, a patched firmware fragile across kernel updates, and on-board-antenna limits. Better as a gateway or reference sniffer than a node.

### Tier C — "puck" (hockey-puck, ~76 mm Ø × 25 mm, with a real battery)

1. **Intel AX210 (M.2 2230) + small Linux SBC with PCIe (e.g. CM5 on a minimal carrier), FeitCSI.**
   - Why: 2x2, 20–160 MHz, 2.4/5/6 GHz, ~1 kHz-class CSI, injection, open source. Two antennas at 5–6 GHz fit a puck rim. The richest per-node CSI money can buy off the shelf.
   - Costs: ~3–8 W node (NIC + SBC) [E], Linux boot time, larger battery.
2. **4–8 element coherent ESP32-C5 array puck (ESPARGOS concept, ported to C5/5 GHz) with ESP32-C5 "illuminator" pucks.**
   - Why: AoA plus Doppler on MCU power budgets (8 RX × ~110 mA ≈ 0.9 A @3.3 V ≈ 3 W peak [E]). No Linux needed. Single-vendor toolchain shared with the tiny nodes.
3. **Hybrid puck: ESP32-C5 CSI + a UWB radar or CIR front-end (Novelda X7 for µW presence, or DW3000 bistatic CIR) + optional BGT60TR13C for the room it's placed in.**
   - Why: UWB adds sub-10 GHz through-wall radar with ~cm-scale range bins and very low power. The DW3000 doubles as firefighter tag ranging.
   - Costs: two sensing pipelines.

### Cross-tier recommendation

**Standardise on the ESP32-C5 as the sensing radio** for tiny nodes and as the building block of coherent arrays. Prototype one **AX210/FeitCSI puck** as a "ground-truth rich CSI" reference to measure how much 2x2 and 80–160 MHz actually improve through-wall detection versus C5 HT40/HE20 on your walls. Budget a small spike on **NXP RW612** as a second-source hedge. Track the **ESP32-E22** (2x2, 160 MHz) for CSI exposure.

---

## 7. Open questions and to-verify list

- Does the ESP32-C5 deliver HT40 CSI on **5 GHz** in practice? The datasheet lists 11n 20/40 MHz without a band restriction [P][4], and the CSI table has 40 MHz rows [P][2]. Test it on the bench.
- What sustained CSI rate does a C5 manage with gain lock on, at HE20 vs HT40, with N senders on one channel? Measure it; there is no vendor number.
- Is the esp-crab hardware reproducible (shared-clock boot issue) [S][45]? Ask Espressif or wait for the files.
- Will NXP RW612 CSI work out of the box on the FRDM-RW612 (AN14281 Rev 4.0)? What tones and rates does it actually deliver?
- ESP32-E22 CSI support and nRF71 CSI support: both unknown or unconfirmed as of 2026-11-10.
- The X7 module size, IW612 vs 88W9098 RX chain counts, and the PicoScenes licence were not verified here.

---

## Sources

1. Espressif, *Introduction to ESP-CSI Solution* (chip CSI ranking, C3 3-antenna motion board, esp-crab, gain ctrl): https://docs.espressif.com/projects/esp-techpedia/en/latest/esp-friends/solution-introduction/esp-csi/esp-csi-solution.html [P]
2. ESP-IDF v6.0 (ESP32-C5) *Wi-Fi Vendor Features → Wi-Fi Channel State Information* (subcarrier table: 11a/g 53/52, HT20 57/56, HT40 117/114, HE20-SU 245/242; byte counts 106–490; `acquire_csi_force_lltf`; `first_word_invalid`; HE STBC mode): https://docs.espressif.com/projects/esp-idf/en/v6.0/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html [P]
3. Espressif component `esp_csi_gain_ctrl` v0.1.5 readme (targets ESP32/S2/S3/C3/C5/C6/C61; force gain may cause packet loss): https://components.espressif.com/components/espressif/esp_csi_gain_ctrl/versions/0.1.5/readme [P]
4. ESP32-C5 Series Datasheet v1.5 (1T1R dual band, 11ax 20 MHz only, 11n 20/40 MHz, antenna diversity, FTM, QFN48 6×6, RX currents): https://documentation.espressif.com/esp32-c5_datasheet_en.pdf [P]
5. esp-csi `examples/esp-crab` README (2× C5 co-crystal, self-TX/RX and single-TX/dual-RX, phase difference, 100-packet gain setup): https://github.com/espressif/esp-csi/tree/master/examples/esp-crab [P]
6. Euchner et al., *ESPARGOS: An Ultra Low-Cost, Realtime-Capable Multi-Antenna WiFi Channel Sounder*, arXiv:2502.09405 (2025): https://arxiv.org/html/2502.09405v1 [P]
7. FeitCSI (AX200/AX210, 20–160 MHz, 11a/g/n/ac/ax, injection, x86-64 & ARM, GPL-3.0): https://feitcsi.kuskosoft.com/ and https://github.com/KuskoSoft/FeitCSI [P]
8. PicoScenes home page (AX200/AX210/QCA9300/IWL5300, SDR up to 4x4, 6 GHz on AX210, ~1 kHz CSI at 20 MHz, Ubuntu 22.04): https://ps.zpj.io/ [P]
9. Intel Wi-Fi 6E AX210 product brief / spec (M.2 2230 22×30×2.4 mm, 2x2): https://www.intel.com/content/www/us/en/products/docs/wireless/wi-fi-6e-ax210-module-brief.html and https://www.intel.com/content/www/us/en/products/sku/204836/intel-wifi-6e-ax210-gig-plus-module/specifications.html [P]
10. NXP AN14011 *Channel State Information (CSI) on Linux OS*, Rev 10.2, 25 Jun 2026 (supported chips incl. 88W8997, 88W9098, AW690, IW610/611/612, AW69x; AMI): https://www.nxp.com/docs/en/application-note/AN14011.pdf [P]
11. NXP AN14281 *Channel State Information (CSI) on FreeRTOS*, Rev 4.0, 7 Jan 2026 (RW610/RW612/IW61x/AW611; nTones tables; AMI): https://www.nxp.jp/docs/en/application-note/AN14281.pdf [P]
12. CNX Software and distributor pages for RW612 modules (Murata Type 2FR 12.0×11.0×1.5 mm; Insight SiP ISP5261 12×12×1.8 mm): https://www.cnx-software.com/2026/06/25/quectel-fcm365x-dual-band-wifi-6-ble-5-4-802-15-4-iot-module-nxp-rw612-psram/ and https://www.digikey.bg/en/product-highlight/i/insight-sip/isp5261-mini-wifi6-ble-module [S]
13. Nordic DevZone, "Wi-Fi subcarrier channel state information on nRF7002": https://devzone.nordicsemi.com/f/nordic-q-a/120956/wi-fi-subcarrier-channel-state-information-on-nrf7002 ; rx_cap thread https://devzone.nordicsemi.com/f/nordic-q-a/110542/nrf7002-radio-test-rx_cap-2-trigger/480915 [S]
14. ESP-Hosted-MCU `tools/wifi_coverage` ("esp_wifi_set_csi: unsupported: CSI API is not implemented in hosted"): https://github.com/espressif/esp-hosted-mcu/blob/main/tools/wifi_coverage/README.md [P]
15. Nexmon main page (bcm43436b0 patch for Pi Zero 2 W, no CSI) and nexmon_csi table: https://www.seemoo.tu-darmstadt.de/nexmon/ ; https://github.com/seemoo-lab/nexmon/issues/500 [S]
16. BGT60TR13C (58–63.5 GHz, 1 Tx / 3 Rx, AiP 6.5×5.0×0.9 mm): https://www.infineon.com/assets/row/public/documents/24/45/infineon-xensiv-bgt60tr13c-60ghz-radar-board-offerings-productbrief-en.pdf ; https://cdn.hackaday.io/files/1901478135249248/BGT60TR13C_Datasheet-v01.pdf [S/P]
17. Acconeer A121 datasheet (5.5×5.2×0.88 mm fcCSP, presence to 7 m): https://developer.acconeer.com/download/a121-datasheet ; mirror https://docs.sparkfun.com/SparkFun_Qwiic_Pulsed_Radar_Sensor_XM125/assets/component_documentation/A121%20datasheet.pdf [P]
18. TI IWR6843AOP product page (60–64 GHz, 3 TX/4 RX AoP, 15×15 mm FCBGA, R4F + C67x DSP): https://www.ti.com/product/IWR6843AOP [P]
19. Through-wall band evidence (Walabot ≈6.3–8 GHz at 0.14 mW; sub-10 GHz needed for sheetrock, plaster, brick; 60 GHz limits): MDPI Remote Sensing 16(8):1426 https://www.mdpi.com/2072-4292/16/8/1426 ; arXiv:1909.11175 https://arxiv.org/pdf/1909.11175 [S]
20. Novelda X7 (<50 µW at 1 fps; X7F202 kit range to 12 m; 2 patch antennas): https://www.fiercesensors.com/iot-wireless/novelda-wins-best-sensors-iot-x7-ultra-low-power-presence-sensor ; https://azcus.digikey.com/en/products/detail/novelda/X7F202-Development-Kit/25558545 [S]
21. Qorvo DWM3000 data sheet brief (23×13×2.9 mm): https://www.mouser.com/datasheet/2/412/DWM3000_Data_Sheet_Brief-2399958.pdf ; Qorvo UWB radar white paper https://www.qorvo.com/resources/d/ultra-wideband-radar-sensing-white-paper ; CIR presence forum https://forum.qorvo.com/t/person-sensing-presence-detection-using-cir/23121 [P/S]
22. Sampling-rate practice in CSI HAR (1000/2500 Hz; 50 Hz loses information): arXiv:2605.08308 https://arxiv.org/pdf/2605.08308 ; UCL passive WiFi radar https://discovery.ucl.ac.uk/id/eprint/10103371/ [S]
23. Stone, NISTIR 6055 *Electromagnetic Signal Attenuation in Construction Materials* (0.5–2 & 3–8 GHz): https://nvlpubs.nist.gov/nistpubs/Legacy/IR/nistir6055.pdf [P]
24. Cominelli, Raza, Lo Cigno, Gringoli, *A Glimpse into IEEE 802.11be Channels: Can They Improve CSI-based Sensing?* (bandwidth vs localization; PicoScenes "mostly closed-source"): https://ans.unibs.it/publications/cominelli25-320.pdf [P]
25. Next2You (5 GHz vs 2.4 GHz copresence): https://arxiv.org/pdf/2111.05224 [P]
26. espressif/esp-csi README (supported chips incl. C5/C6/C61; IPEX better than PCB antenna): https://github.com/espressif/esp-csi [P]
27. ESP32-C5-MINI-1/1U datasheet (MINI-1U 15.4×15.4×2.4; MINI-1 15.4×21.3×2.4 from CN v1.2): https://documentation.espressif.com/esp32-c5-mini-1_mini-1u_datasheet_en.pdf [P, figures via search snippet]
28. Seeed wiki, XIAO ESP32-C5 (21×17.8 mm, U.FL, SGM40567 charger, 8 MB/8 MB): https://wiki.seeedstudio.com/xiao_esp32c5_getting_started/ [P]
29. Retail price, XIAO ESP32-C5 ($7.24): https://www.robotshop.com/products/seeedstudio-seeed-studio-xiao-esp32-c5 [S]
30. ESP32-C6 Series Datasheet (1T1R 2.4 GHz, 11ax 20 MHz only, 11n 20/40, QFN 5×5, RX ~78–82 mA): https://documentation.espressif.com/esp32-c6_datasheet_en.pdf [P]
31. Seeed wiki, XIAO ESP32-C6 (ceramic + U.FL via GPIO3/GPIO14; 15 µA deep sleep): https://wiki.seeedstudio.com/xiao_esp32c6_getting_started/ [P]
32. ESP32-C61 Series Datasheet (1T1R 2.4 GHz, 20 MHz-only non-AP 11ax, RX 88/90 mA): https://documentation.espressif.com/esp32-c61_datasheet_en.pdf [P]
33. esp-radar changelog (gain compensation; C5/C6/C61 in v0.3.0) and gain-ctrl changelog: https://components.espressif.com/components/espressif/esp-radar/versions/0.3.4/changelog ; https://components.espressif.com/components/espressif/esp_csi_gain_ctrl/versions/0.1.5/changelog [P/S]
34. ESP32-C61-MINI-1 (13.2×16.6×2.4 mm, ~$2): https://www.cnx-software.com/2026/08/04/espressif-esp32-c61-mini-1-1u-wi-fi-6-and-ble-iot-module-2-dollars/ [S]
35. ESP32-S3 Series Datasheet (20/40 MHz 2.4 GHz, 1T1R, QFN56 7×7, RX 88/91 mA): https://documentation.espressif.com/esp32-s3_datasheet_en.pdf [P]
36. ESP-IDF v6.0 (ESP32-S3) Wi-Fi Vendor Features → CSI (LLTF/HT-LTF/STBC-HT-LTF, byte table up to 612, `first_word_invalid`, callback in Wi-Fi task, promiscuous suggestion): https://docs.espressif.com/projects/esp-idf/en/v6.0/esp32s3/api-guides/wifi-driver/wifi-vendor-features.html [P]
37. ESP32 Series Datasheet v5.3 (RX 95~100 mA, antenna diversity): https://documentation.espressif.com/esp32_datasheet_en.pdf [P]
38. ESPectre release notes / setup (gain lock on S3, C3, C5, C6; not original ESP32 or S2): https://newreleases.io/project/github/francescopace/espectre/release/2.2.0 [S]
39. ESPARGOS coverage (8× ESP32-S2FH4, controller with Ethernet, pyespargos): https://www.rtl-sdr.com/espargos-an-esp32-phased-array-for-seeing-wifi/ ; https://hackaday.com/tag/aoa/ ; https://github.com/ESPARGOS/pyespargos [S]
40. ESP32-E22 (Wi-Fi 6E, 160 MHz, 2×2 MU-MIMO; pre-GA; Linux driver STA over PCIe): https://www.electronics-lab.com/esp32-e22-gets-wi-fi-6e-certification-and-linux-drivers/ ; https://www.hackster.io/news/espressif-prepares-for-esp32-e22-general-availability-with-wi-fi-6e-certification-open-linux-driver-5e175fe7d011 [S]
41. Portner, Gringoli, Hollick, Asadi, *Same Signal, Different Story: Demystifying Receiver Effects in Wi-Fi CSI*, IEEE IoT-J 13(10) 2026 / arXiv:2605.26836: https://arxiv.org/abs/2605.26836 [P]
42. ESPHome ESPectre component (~100 pps target; 0–1000 pps range): https://esphome.io/components/espectre/ [S]
43. ESP32 CSI tools packet rates (85 pps; 180 pps web tool): https://www.mdpi.com/1424-8220/25/19/6220 ; https://github.com/StevenMHernandez/ESP32-CSI-Tool [S]
44. ESP-IDF PHY guide, multiple antennas (`esp_phy_set_ant_gpio`, up to 16 antennas, 1–2 enabled): https://docs.espressif.com/projects/esp-idf/en/stable/api-guides/phy.html [P]
45. esp32.com / DigiKey forum: esp-crab hardware files missing; shared-clock boot failure: https://esp32.com/viewtopic.php?p=154694 ; https://forum.digikey.com/t/esp32-c5-boot-failure-with-shared-external-clock/69516 [S]
46. Intel community AX210 capability dump (HE NSS 2 at 160 MHz): https://community.intel.com/t5/Wireless/Ax210-can-t-work-in-5GHz-mode-for-AP-Ubuntu20-04/m-p/1344566 [S]
47. PicoScenes tutorial slides, CPS-IoT Week 2022 (AX210/211 11ax 160 MHz, up to 8192 subcarriers): https://de.slideshare.net/ZhipingJiang1/picoscenes-tutorial-cpsiot-week2022 [S]
48. ADLINK EWK-M2-AX210 datasheet (peak 1.54 A @3.3 V, 5 W): https://www.mouser.com/datasheet/3/966/1/20230511_EWK_M2_AX210_Series.pdf [S]
49. Halperin et al., Linux 802.11n CSI Tool (Intel 5300; 30 groups; 8-bit): https://dhalperi.github.io/linux-80211n-csitool/ [P]
50. Intel 5300 discontinued note (ZTECSITool paper / tutorials): https://arxiv.org/pdf/2206.09532 [S]
51. Atheros CSI Tool supported device list (AR9580 3x3): https://wands.sg/research/wifi/AtherosCSI/DeviceList.html [P]
52. Atheros-CSI-Tool GitHub (AR9580/AR9590/AR9344/QCA9558; 56/114 subcarriers; ath9k): https://github.com/xieyaxiongfly/Atheros-CSI-Tool [P]
53. nexmon_csi issues/discussions (Pi 4 one antenna; AC86U crashes at high rate/80 MHz): https://github.com/seemoo-lab/nexmon_csi/issues/213 ; https://colinguolin.notion.site/phase-difference-for-AoA-ToF-and-Doppler-profile-Issue-222-seemoo-lab-nexmon_csi-2a4d3c256e084d478704b60601e0e61f [S]
54. seemoo-lab/nexmon_csi README (chips bcm4339/43455c0/4358/4366c0; Pi 3B+/4B/5; up to 80 MHz; UDP per core/stream; Makefile.rpi): https://github.com/seemoo-lab/nexmon_csi [P]
55. Gringoli et al., *AX-CSI* (WiNTECH '22; BCM43684, 160 MHz, 4x4): https://ans.unibs.it/projects/ax-csi/ ; https://ans.unibs.it/assets/documents/axcsi.pdf [P]
56. *Wi-Fi Sensing Tool Release: Gathering 802.11ax CSI from a Commercial AP* (ZTECSITool, MT7916): https://arxiv.org/html/2506.16957v1 [P]
57. NXP IW612 product page ("1x1 2.4/5 GHz, up to 80 MHz channel", SDIO 3.0): https://www.nxp.com/products/IW612 [P]
58. NXP RW612 product page ("1x1, 20 MHz, IEEE 802.11ax, 2.4 GHz/5 GHz", M33 260 MHz, 1.2 MB SRAM): https://www.nxp.com/products/RW612 [P]
59. Murata Type 2EL (IW612, 7.7×8.8×1.3 mm): https://www.murata.com/products/connectivitymodule/wi-fi-bluetooth/overview/lineup/type2el ; https://www.digikey.com/en/product-highlight/m/murata-electronics-north-america/type-2el-shielded-ultra-small-wi-fi-and-bluetooth-module [S]
60. NXP community, CSI on FRDM-RW612 callback issue: https://community.nxp.com/t5/Multi-Source-Translation-Content/2146061-en-US/ta-p/2235297 [S]
61. openwifi CSI app note (side channel, TSF, freq offset, equalizer): https://github.com/open-sdr/openwifi/blob/master/doc/app_notes/csi.md [P]
62. Nordic nRF7120 / nRF71 announcement (sampling, Nov 2026): https://www.nordicsemi.com/Products/nRF7120 ; https://www.cnx-software.com/2026/11/03/nordic-semi-nrf71-wireless-chips-to-deliver-20-times-lower-standby-power-for-battery-operated-wifi-devices/ [S]
63. Silicon Labs tech talk (CSI sensing tied to 802.11bf): https://www.silabs.com/documents/login/presentations/tech-talks-ai-ml-applications-on-an-ultra-low-power-wifi-6-solution.pdf [S]
64. No Realtek CSI tool found (survey of tools): https://arxiv.org/pdf/2506.16957 [S]
65. IEEE 802.11bf-2025 dates (board approval 2025-05-28, published 2025-09-26): https://grouper.ieee.org/groups/802/11/Reports/tgbf_update.htm [S]
66. IEEE SA, *802.11bf Aims to Enable WLAN Sensing*: https://standards.ieee.org/beyond-standards/ieee-802-11bf-aims-to-enable-a-new-application-of-wlan-technology-wlan-sensing [S]
67. UWB bistatic radar with DW3000 across channels (Univ. Trento): https://iris.unitn.it/retrieve/dacd7e0a-a18f-4a81-82c2-d92095b193f1/UWB_bistatic_radar_sensor_across_channels_evaluation.pdf [P]
68. HLK-LD2410 user manual (7×35 mm) and LD2410C/S current: https://www.elemon.com.ar/images/industry_news/11-HLK-LD2410-user-manual-V1.02.pdf ; https://www.tinytronics.nl/en/sensors/motion/hi-link-hlk-ld2410s-24ghz-radar-sensor-module-3.3v-low-power [S]
69. Nordic DevZone, nRF54L15 channel sounding accuracy threads: https://devzone.nordicsemi.com/f/nordic-q-a/125048/nrf54l15-channel-sounding-test ; https://devzone.nordicsemi.com/f/nordic-q-a/126232/nrf54l15-channel-sounding-test [S]
70. ESP32 link-selection finding (more links decreased accuracy; link count 2.7× algorithm effect), via topic summary: https://www.emergentmind.com/topics/commodity-esp32-wifi-sensors [S — secondary; locate original paper before relying on it]
