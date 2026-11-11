# 06: Antennas and RF front-end for a small WiFi-CSI node

Slice: antennas, RF front-end, band choice, enclosure. Chip choice, network architecture and forensics are covered in the other files.
Baseline: ESP32-C5 (XIAO ESP32-C5 plus a u.FL FPC antenna), 2.4 GHz channel 6. Without the external antenna, a sender was "barely heard".

Conventions: **[src]** means the claim comes from the linked primary or vendor document. **[est]** means it is my estimate or derivation. Treat [est] numbers as planning numbers and verify them on the bench.

---

## TL;DR

1. **At 2.4 GHz the node is the antenna.** Chip antennas and PCB IFAs use the board ground as part of the radiator. A board that is much shorter than about λ/4 (31 mm) costs several dB. The u.FL FPC worked so well because it is a ground-independent ~42 mm half-wave dipole. The smallest node that performs well at 2.4 GHz is therefore about **45–55 mm long**, not 25 mm.
2. **Band: keep 2.4 GHz as the through-wall workhorse and use dual-band antennas.** Through brick and concrete, 5 GHz loses about 10 dB more per wall, plus about 7 dB more free-space loss with equal-gain antennas. 5 GHz is better inside one room or through drywall: 2.2× more phase change per mm of motion, cleaner spectrum, and smaller antennas. Choose the band at runtime. Skip 6 GHz: the ESP32-C5 does not support it, and US LPI access points cannot run on battery.
3. **Antenna gain and TX power have limited value under regulation. RX gain is free.** The EU caps 2.4 GHz at 20 dBm EIRP, and the C5 already delivers 19–20 dBm conducted, so an external PA buys nothing in the EU. Receive-side gain (directional or patch antennas) is not regulated. The US allows up to 36 dBm EIRP at 2.4 GHz, but a PA costs battery and the C5 has no documented FEM control.
4. **For CSI, pattern stability and consistent polarization matter more than peak gain.** Use average gain (efficiency) as the metric. Mount nodes identically and keep antennas at least 15 mm from walls, bodies and metal. Use polarization diversity (two orthogonal FPCs) as the compact substitute for λ/2 spatial diversity.
5. **Recommended tiers:** (A) a ~25 mm micro node with a 2.4 GHz chip antenna, only for same-room work, about 5–7 dB worse than (B). (B) **The default:** a ~50 mm stick or puck with one or two dual-band FPC dipoles on the housing wall. (C) A ~100–150 mm wall brick with 2 orthogonal FPCs plus a 6–8 dBi through-wall patch, switched through C5 antenna-select GPIOs.

---

## 1. Antenna options for a small node

### 1.1 Concrete parts (datasheet numbers)

| Type | Part | Size | Bands | Efficiency / gain (datasheet) | Ground / keep-out | Notes |
|---|---|---|---|---|---|---|
| LTCC chip, corner mount | Johanson **2450AT18A0100001E** (legacy 2450AT18A100) | 3.2 × 1.6 × 1.3 mm | 2.4–2.5 GHz | 74 % avg radiated efficiency, 1.5 dBi peak, 0.0 dBi avg (on Johanson eval board) | Corner of the PCB with clearance. "Ground vias are highly recommended to have better antenna efficiency" | −40 to +125 °C. [src](https://www.johansontechnology.com/docs/3827/Antenna-2450AT18A0100001E-Rev4.0.pdf). The legacy sheet says 76 %, 0.5 dBi peak. [src](https://datasheet.chipsmall.com/2450AT18A100E/604027.pdf) |
| Ground-plane "booster" chip | Ignion **NN02-224 RUN mXTEND** | 12.0 × 3.0 × 2.4 mm | 2.4–2.5 and 4.9–5.875 GHz (one port, matching network) | >70 % in both bands, 2.9 / 3.1 dBi peak | Uses the **ground plane as the radiator**. Ignion recommends a ground plane >100 mm "for comparable results" and ≥5 mm from metal. Matching network sits in the ground area, not the clearance | [src](https://static6.arrow.com/aropdfconversion/54225d09e52574e02ee8f9857bef33dab4acb393/ds_nn02-224.pdf), [eval-board manual](https://www.mouser.com/datasheet/2/1029/Ignion_03262021_EB_NN02_224-2255979.pdf). Too big a ground needed for a 25 mm node |
| Ceramic SMD, dual-band | Antenova **Mixtus A10194H** | 10 × 10 × 0.9 mm | 2.4 / 5 GHz | listed as 75 / 60 % | ground-plane dependent | catalogue figure, mapping between bands ambiguous. [src](https://www.antenova.com/storage/media/documents/WIFI%20&%20Bluetooth%20Antennas%20Catalogue.pdf) |
| Ceramic SMD, 2.4 GHz | Antenova **Rufa A5839H / A5887H** | small SMD | 2.4 GHz | ~66–75 % | no ground under the antenna; works with clearance D ≥ 2 mm | [src](https://www.antenova.com/storage/media/documents/Rufa%20A5839H%20A5887H-PS-4.02%2020260812AF1.pdf) |
| Ceramic dual-band | Kyocera AVX **M830520** | small SMD | 2.4 / 5 GHz | plots only, characterised on a 40 × 80 mm PCB | ground dependent | [src](https://farnell.com/datasheets/4437523.pdf) |
| PCB meander IFA | TI **AN043** design | 15.2 × 5.7 mm of copper | 2.4 GHz | ~2 dBi avg gain reported in FCC copies; efficiency falls as ground shrinks | needs ground; free of BOM cost | [src](https://ti.com/lit/pdf/swra117), [TI E2E: "ground plane is an important part of the antenna"](https://e2e.ti.com/support/wireless_connectivity/low_power_rf_tools/f/155/t/210694) |
| **FPC dipole, stick-on, u.FL** | Taoglas **FXP830.07.0100C** | 42 × 7 × 0.1 mm, 100 mm Ø1.37 coax | 2.4 / 5.15–5.85 / 5.925–7.125 GHz | 2.4 GHz: **79 % on 2 mm ABS** (−1.0 dB avg, 3.5 dBi peak) vs 53 % in free space. 5 GHz: 63 % on ABS (−2.0 dB avg, 6.9 dBi peak) | "Operates in free space (ground-plane independent)" | −40 to +85 °C. [src](https://www.taoglas.com/datasheets/FXP830.07.0100C.pdf) |
| FPC dipole, balanced | Molex **146153** series | 34.9 × 9 mm | 2.4 / 5 / 6 GHz | ≥75 % (2.4) / ≥70 % (5); about 63 / 59 % with a 250 mm cable | ground-plane independent, adhesive | [src](https://www.digikey.lt/en/product-highlight/m/molex/2-4-5ghz-wi-fi-flexible-antenna-with-balanced-transmission), [Molex](https://www.content.molex.com/dxdam/literature/987650-5892.pdf) |
| Board-bundled FPC | Seeed XIAO ESP32-C5 FPC | ~FPC | spec table says 2.4 GHz (labelling conflicts) | 2.9 dBi | u.FL | [src](https://core-electronics.com.au/seeed-studio-xiao-esp32-c5.html), [Seeed wiki](https://wiki.seeedstudio.com/xiao_esp32c5_wifi_usage/). **Check it is truly dual-band before using 5 GHz** |
| External whip / rod | generic RP-SMA 2.4/5 GHz | 100–200 mm | dual | typically 2–5 dBi | ground independent | Seeed recommends it for "much higher gain" than the FPC. [src](https://wiki.seeedstudio.com/xiao_esp32c5_wifi_usage/). Too big and too fragile for a hand-thrown node |

**Key observations**

- **Chip-antenna efficiency numbers come from the vendor's eval board** (Johanson note 2). Ignion's booster uses a 60 × 131 mm or larger ground. On a 15–25 mm node, the effective efficiency is substantially lower **[est: −3 to −6 dB average gain vs datasheet]**. The physical reason: these parts excite currents on the ground plane, and a ground much shorter than about λ/4 (31 mm at 2.4 GHz) is a poor radiator. TI says the same for its PCB IFA ([E2E](https://e2e.ti.com/support/wireless_connectivity/low_power_rf_tools/f/155/t/210694)).
- **FPC dipoles do not depend on the ground plane.** That is why the u.FL FPC transformed the hackathon link. Taoglas's own data shows that the plastic it is stuck to changes performance (2 mm ABS: 79 % vs free space 53 % at 2.4 GHz). **Tune and measure in the final housing.**
- Taoglas warns that "peak gain ... can degrade by at least 1 or 2 dBi when put inside a device" ([FXP830 datasheet](https://www.taoglas.com/datasheets/FXP830.07.0100C.pdf)).
- Espressif's C5 layout guide says: put the antenna at the board edge, never in a board centre with a cut-out, use a CLC tuning network near the antenna, and keep **≥15 mm clearance to the housing in all directions** around a PCB antenna. Keep USB, UART and crystals away from the antenna. [src](https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32c5/pcb-layout-design.html)
- The ESP32-C5 has **separate RF pins: ANT_2G (pin 42) and ANT_5G (pin 48)** ([C5 datasheet](https://documentation.espressif.com/esp32-c5_datasheet_en.html)). The WROOM-1 module combines them through a diplexer onto one antenna. A custom board can use a single dual-band antenna with a diplexer, or **two band-specific antennas** with no diplexer loss.

### 1.2 Small-antenna theory (the Chu limit, in plain terms)

The Chu limit says that an antenna fitting inside a sphere of radius *a* has a minimum quality factor Q ≥ 1/(ka)³ + 1/(ka), where k = 2π/λ. High Q means narrow bandwidth and touchy tuning. Lowering Q by adding loss lowers efficiency. [src](https://loop-antennas.readthedocs.io/en/latest/dictionary/chu-limit.html), [MITRE](https://mitre.org/sites/default/files/pdf/09_4198.pdf)

What it means for this node **[est, my calculation]**:

- The 2.4 GHz ISM band (2400–2483.5 MHz) is 3.4 % wide. With VSWR 2, BW ≈ 1/(Q√2), so Q ≲ 21. That gives **ka ≳ 0.38**, a sphere radius of at least 7.6 mm, about **15 mm diameter**, for a lossless antenna.
- The 5 GHz band (5150–5850 MHz) is 12.7 % wide. That needs ka ≳ 0.63, a sphere about **11 mm diameter**.
- Real antennas sit 2–3× above the bound. The "sphere" also includes the ground currents. So an efficient 2.4 GHz radiating structure realistically needs **~30–45 mm** of largest dimension, which matches the λ/4-ground guidance. A smaller node is possible, but it pays in efficiency and in sensitivity to detuning.
- Detuning is the practical killer for a node that gets hand-placed on walls. The smaller the antenna, the higher its Q and the further a nearby hand, wall or wet surface pushes it off-band.

---

## 2. What matters for CSI sensing (vs. plain comms)

1. **Pattern and impedance stability beats peak gain.** CSI motion detection looks at *changes* in the channel. Anything that moves the antenna's near field also changes CSI: a hand, a cable flexing, the node shifting on a wall. That looks exactly like a person. Recommendations: rigid mounting, the antenna bonded to the housing (no dangling pigtail), and ≥15 mm keep-out from mounting surfaces ([Espressif](https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32c5/pcb-layout-design.html)). **[est]** A flapping u.FL cable is a CSI noise source.
2. **Use average gain (radiated efficiency) for omni coverage.** Peak gain only says where the pattern lobes are.
3. **Polarization.** A linear antenna's polarization should match across nodes. Indoors, multipath partially depolarizes signals, and orthogonal polarizations show "no significant envelope correlation" in NLOS Rayleigh channels ([IRCTR 2002, via search summary](https://repository.up.ac.za/bitstreams/12c1c696-175e-44f7-b143-5cd16b459538/download)). That makes **two orthogonal antennas a compact diversity pair**. A 2.4 GHz measurement found envelope correlation <0.5 both for λ/2-spaced dipoles and for co-located orthogonal ones ([core.ac.uk](https://core.ac.uk/download/528145461.pdf)). Wilson and Patwari's through-wall RTI paper suggests "directional and dual-polarized antenna designs would most likely improve images" ([arXiv 0909.5417](https://ar5iv.arxiv.org/html/0909.5417)).
4. **Antenna diversity and MIMO.** CSI papers show that different antennas have different sensitivity to an activity, and that orientation or dead zones degrade single-link accuracy. Multiple antenna pairs stabilise it: WiSDAR reports about 96 % stable accuracy ([IEEE IoT-J](https://unpaywall.org/10.1109%2FJIOT.2018.2871445); [IEEE Access 2023](https://doaj.org/article/01ba36140ab149a0bcd76028c0552dd6)).
   - The **ESP32-C5 is 1T1R per band**. It supports **up to 16 antennas through an external RF switch**: 4 `antenna_select` lines mapped to GPIOs, with `esp_phy_set_ant_gpio()` / `esp_phy_set_ant()`. Fixed modes are recommended, and AUTO is "currently not recommended" ([ESP-IDF C5 PHY guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/phy.html)).
   - So on the C5, diversity is **switched or time-multiplexed**, not simultaneous MIMO CSI. True simultaneous multi-antenna CSI (phase difference, AoA) needs a multi-chain chip; see the chip-choice slice.
5. **Spacing and package size.** For spatial decorrelation, the rule of thumb is ≥ λ/2: **62.5 mm at 2.4 GHz and ≈29 mm at 5.5 GHz**. A two-antenna 2.4 GHz spatial pair forces a ≥70 mm package. At 5 GHz it fits a 35–40 mm puck. Orthogonal polarization lets you co-locate two antennas, for example two FPCs at 90° on the inside of a 50 mm puck.
6. **Directional vs omni.** An omni antenna covers the room from any placement and suits nodes that are thrown or hand-placed. A directional antenna (patch, biquad, or PIFA with reflector) concentrates on the target area behind a wall and rejects clutter from the side the operator stands on. TU Wien used an ESP32-S3 with a biquad and reached **92.0 ± 3.5 %** NLOS through-wall HAR across an 18 m, five-room office, vs **86.8 ± 4.7 %** for the onboard PIFA with a plane reflector ([arXiv 2401.01388](https://arxiv.org/abs/2401.01388)). A directional antenna is roughly wavelength-sized: Wi-Vi notes "approximately 12 cm" at Wi-Fi frequencies, and it used 6 dBi LP0965 log-periodics ([Wi-Vi paper](https://www.mit.edu/~fadel/papers/wivi-paper.pdf)).
7. **Mounting height.** RTI nodes were placed "at approximately human torso level" ([Wilson & Patwari](https://ar5iv.arxiv.org/html/0909.5417)). **[est]** The first Fresnel zone radius at the middle of a 5 m link is about 0.40 m at 2.4 GHz and 0.26 m at 5.5 GHz. A node lying on the floor puts half of that zone into the floor, so raise nodes to about 0.8–1.5 m, or use a wall-mount bracket. A floor node with a horizontal FPC has horizontal polarization. Do not mix it with vertically mounted wall nodes. If nodes will be mixed, design for dual-pol.

---

## 3. 2.4 vs 5 vs 6 GHz

### 3.1 Wall penetration (one-way loss)

| Material | 2.4 GHz | 5 GHz | Source |
|---|---|---|---|
| Drywall 12.8 mm (panel only) | 0.49 dB (2.3 GHz) | 0.51 dB (5.25 GHz) | Magis Networks 2003, Table 1 [src](https://www.am1.us/wp-content/uploads/2017/11/U11607_Making_OFDM_Work_Webpage.pdf) |
| Interior hollow wall (6") | 9 dB | n/a | Wi-Vi Table 1, from NIST [src](https://www.mit.edu/~fadel/papers/wivi-paper.pdf) |
| Solid wood door 1.75" | 6 dB | n/a | Wi-Vi Table 1 |
| Glass | 3 dB (Wi-Vi); 0.5 dB (Magis) | 1.7 dB (Magis) | as above |
| Red brick (dry / wet) | 4.4 / 4.5 dB | **14.6 / 14.6 dB** | Magis Table 1 |
| Cinder block (dry / wet) | 6.7 / 7.4 dB | 10.3 / 12.4 dB | Magis Table 1 |
| Stucco (wire lath) | ~14.6 dB | ~13.6 dB | Magis Table 1 |
| Concrete 18" | 18 dB | n/a | Wi-Vi Table 1, from NIST |
| Reinforced concrete | 40 dB | n/a | Wi-Vi Table 1 |
| Concrete, bulk absorption, 200 mm | **~13 dB** | **~25 dB** | [est] from ITU-R P.2040-3 coefficients: ε′ = 5.24, σ = 0.0462·f^0.7822 S/m ([MathWorks table of P.2040-3](https://www.mathworks.com/help/comm/ref/buildingmaterialpermittivity.html)), with α ≈ 1636·σ/√ε′ dB/m. That gives 66 dB/m at 2.44 GHz and 125 dB/m at 5.5 GHz. Excludes interface reflection (+2–4 dB) and rebar |
| Brick, bulk, 100 mm | ~2.3 dB | ~2.6 dB | [est] same method (ε′ 3.91, σ = 0.0238·f^0.16). The Magis measurement shows a much larger 5 GHz penalty, so trust the measurement |

NIST (Stone, NISTIR 6055) found most materials fairly transparent at 0.5–2 GHz. A 13 mm drywall panel dissipated only 6 % of the power, and a 267 mm triple-brick wall passed about 40 %. Concrete absorbed most strongly, and "signal attenuation (dB/mm) increases with increasing frequency". A 305 mm concrete wall costs about 90 dB at 8 GHz. [src](https://nvlpubs.nist.gov/nistpubs/Legacy/IR/nistir6055.pdf)

**Summary:** drywall, wood and glass are roughly band-neutral. **Brick, block and concrete cost about 4–12 dB more per wall at 5 GHz.** On top of that, free-space loss with equal-gain antennas is 20·log(5.5/2.44) ≈ **7 dB worse at 5.5 GHz** **[est, Friis]**. Reinforced concrete is marginal in either band.

### 3.2 Smoke, fire, water

- No reliable 2.4 GHz-specific figure for smoke alone was found. Hong et al. (ETRI, JEES 2021) measured UHF, PS-LTE, LoRa, Wi-Fi and LTE through a compartment fire. They found "ionized particles were the dominant attenuation factor", with "large attenuation ... at particular frequencies when burning lumber and urethane foam" ([src](https://ksp.etri.re.kr/ksp/article/read?id=64687)).
- Flame plasma at 8–10.5 GHz caused 1.6–5.8 dB loss ([Mphale & Heron 2007](https://researchonline.jcu.edu.au/2587/1/17424_Mphale_%26_Heron_2007.pdf)). **[est]** Budget about 3–6 dB extra margin for a path crossing an active fire. Smoke without flame is likely small at 2.4 GHz.
- Wet masonry adds little at 2.4 GHz (brick: 4.4 dry vs 4.5 wet) but up to 2 dB at 5 GHz (cinder block). Magis Table 1, see 3.1.
- Do not place nodes behind or wrapped in **aluminised fire-shelter or turnout reflective material**. It is a metal reflector **[est]**.

### 3.3 Motion sensitivity

- The CSI phase of a reflected path changes by 2π·Δd/λ. Around the Fresnel-zone boundaries, motion produces phase steps of π or 2π ([BeSense, arXiv 1907.06005](https://arxiv.org/pdf/1907.06005); Fresnel-zone sensing model from Zhang's group). λ is 12.3 cm at 2.44 GHz and 5.45 cm at 5.5 GHz. So **5 GHz gives about 2.2× more phase change per mm of motion** **[est, physics]**, which helps breathing and small motions. The Next2You authors report that 5 GHz CSI separated co-located devices better because the shorter wavelength "is more easily perturbed by small-sized objects" ([arXiv 2111.05224](https://arxiv.org/pdf/2111.05224)). No peer-reviewed side-by-side **through-wall** 2.4 vs 5 GHz CSI study was found. That is a gap; run it on our own bench.
- For gross motion of a person walking (tens of cm), both bands saturate the phase. Detection is then limited by SNR and interference, not wavelength, and 2.4 GHz's better link budget wins **[est]**.
- 2.4 GHz channel 6 is congested and only 20 MHz wide. Google's 2.4 GHz CSI patent names bandwidth, interference and congestion as the main 2.4 GHz sensing problems ([US11894902B1](https://patents.google.com/patent/US11894902)). 5 GHz has many clean 20 MHz channels.

### 3.4 Regulatory TX limits (does antenna gain help?)

| Region / band | Limit | Antenna gain effect | Source |
|---|---|---|---|
| **EU 2.4 GHz** (EN 300 328) | **20 dBm EIRP** (wideband modulation) | Hard EIRP cap. Every dBi of TX antenna gain must come off conducted power. **RX gain is unregulated** | [Intertek](https://intertek.com/ict/etsi-en-300-328), [mbedded.ninja summary](https://blog.mbedded.ninja/electronics/compliance/rf-spectrum-regulations/) |
| EU 5 GHz (EN 301 893) | 23 dBm EIRP in 5150–5350 (indoor only; DFS+TPC on 5250–5350). 30 dBm in 5470–5725 with DFS+TPC; less without TPC | Same EIRP logic. DFS channels complicate an ad-hoc network | [mbedded.ninja](https://blog.mbedded.ninja/electronics/compliance/rf-spectrum-regulations/) |
| **US 2.4 GHz** (47 CFR 15.247) | 1 W (30 dBm) conducted with ≤6 dBi antenna, so 36 dBm EIRP | Point-to-multipoint: −1 dB power per dB of gain above 6 dBi. Fixed point-to-point: −1 dB per 3 dB | [eCFR 15.247](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-A/part-15/subpart-C/subject-group-ECFR2f2e5828339709e/section-15.247), [O'Reilly worked example](https://www.oreilly.com/library/view/building-wireless-community/0596005024/apas05.html) |
| US 5 GHz (15.407) | U-NII-1 AP up to 1 W conducted; U-NII-3 point-to-point allows >6 dBi without reduction | | [15.247 summary incl. 5.8 GHz](https://www.law.cornell.edu/cfr/text/47/15.247) |
| US 6 GHz LPI | AP 5 dBm/MHz EIRP (about 18 dBm at 20 MHz); clients 6 dB lower; **indoor only, APs cannot be battery powered** | **Not usable** for battery nodes in an ad-hoc mesh. ESP32-C5 does not support 6 GHz anyway | [Cisco 6 GHz white paper](https://www.cisco.com/c/en/us/products/collateral/wireless/catalyst-9100ax-access-points/ghz-unlicensed-spectrum-reg-wp.html) |

Note: whatever the hardware can do, a product with a modular FCC/CE grant must stay within the antenna types and gains listed in the module's grant. A custom antenna means re-certification **[est, general practice]**.

---

## 4. RF front-end (PA / LNA / FEM)

**ESP32-C5 integrated RF** ([datasheet](https://documentation.espressif.com/esp32-c5_datasheet_en.html)): it has an internal PA, LNA, T/R switch and balun.

| | 2.4 GHz TX (typ) | 2.4 GHz RX sens. | 5 GHz TX | 5 GHz RX sens. |
|---|---|---|---|---|
| 11b 1 Mbps | 20.0 dBm | −100.5 dBm | n/a | n/a |
| HT20 MCS0 | 19.0 dBm | −95.0 dBm | 18.0 dBm | −94.0 dBm |
| HE20 MCS0 | 19.0 dBm | −94.0 dBm | 18.0 dBm | −94.0 dBm |
| HT20 MCS7 | 17.0 dBm | −76.5 dBm | 16.0 dBm | −75.5 dBm |

**Does an external FEM help CSI?**

- **EU:** no for TX. The 2.4 GHz chip already reaches the 20 dBm EIRP cap with a 0–1 dBi antenna.
- **US:** up to +16 dB TX headroom at 2.4 GHz (36 dBm EIRP). **[est]** The cost is about 1–1.5 W DC per PA burst at about 27 dBm out. At 100+ CSI packets/s that dominates the battery budget. More TX power also raises interference to the other nodes' CSI streams on a shared channel.
- **LNA:** **[est]** the 20 MHz noise floor is −101 dBm plus NF. The C5's MCS0 sensitivity (−94 dBm) implies an NF of about 3–5 dB, so an external LNA (NF ~1.5–2 dB, minus switch loss) gains perhaps 1–3 dB. That matters only when the link is noise-limited, i.e. deep through-wall. On 2.4 GHz channel 6 links are often **interference-limited**, and an LNA does nothing there.
- **More SNR does not always mean better motion SNR [est].** The motion signal is the body-scattered path riding on a static channel. Raising TX power scales both the static and the dynamic parts. It helps only until the CSI is no longer noise-limited (roughly RSSI ≥ −70 dBm). Past that, AGC, phase noise and quantisation dominate.
- **Integration risk:** Espressif documents no external-FEM control (PA_EN/LNA_EN timing) for the C5. Its PHY guide covers only antenna-switch select lines ([ESP-IDF PHY](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/phy.html)). Users report the RF stack is a binary blob that makes PA timing hard ([esp32.com thread](https://esp32.com/viewtopic.php?p=50863)). Dual-band FEMs exist, e.g. Skyworks SKY85806-11 (PA, filtering, detector, T/R switch, diplexer) ([src](https://www.skyworksinc.com/Products/Front-end-Modules/SKY85806-11)), and the ESP32 "Reach Out" board pairs an ESP32 with a PA/LNA FEM ([Crowd Supply](https://www.crowdsupply.com/bison-science/esp32-reach-out)). **Recommendation: no FEM in the redo.** Spend the effort on antenna efficiency and RX-side directivity instead, which are free under regulation.
- **Filtering and coexistence:** the C5 radiates on separate 2.4 and 5 GHz pins, and the WROOM module adds a diplexer. **[est]** If a node carries other radios (LoRa, BLE, a GNSS stick, a firefighter's portable radio nearby), add a 2.4 GHz SAW/BAW band-pass filter in the RX path, costing about 1.5–2 dB. A 400/800 MHz LMR handset next to the node can desensitise the 2.4 GHz LNA.
- **Shielding:** put a shield can over the C5 and crystal. Keep USB, UART and battery leads away from the antenna ([Espressif](https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32c5/pcb-layout-design.html)). Put no metal (battery can, heat shield) within 15 mm of the radiating part.

---

## 5. Enclosure

- **RF-transparent materials.** Low εr and low tan δ: PTFE (εr ≈ 2.1, tan δ <0.001), PEEK (εr ≈ 3.2–3.5, tan δ ≈ 0.003–0.007 near 2.45 GHz), glass-filled PPS (εr ≈ 3.8–4.2). Radome guidance is εr ≲ 3.1–3.5 and tan δ <0.01 ([Curbell radome white paper](https://www.curbellplastics.com/wp-content/uploads/2022/11/Plastics-for-High-Performance-Radomes-White-Paper.pdf); [Patsnap PEEK/PEK data](https://eureka.patsnap.com/materials/peek-dielectric-material)).
- **[est]** A 2–3 mm wall is electrically thin (λ/2 in PEEK is about 34 mm at 2.4 GHz and 15 mm at 5.5 GHz). Absorption is negligible, but the wall **detunes** a nearby antenna. Taoglas data shows 2 mm ABS moving FXP830 efficiency from 53 % to 79 % at 2.4 GHz ([src](https://www.taoglas.com/datasheets/FXP830.07.0100C.pdf)). Tune the matching network with the final housing on.
- **Avoid:** metallised paint, carbon-filled plastics, aluminium heat shields or labels over the antenna, and conductive potting over the antenna.
- **Heat (firefighters):** the closest benchmark is NFPA 1982 PASS devices, which must survive a **260 °C (500 °F) circulating-air oven for 5 min** and keep working ([NIST TN 1785](https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.1785.pdf); [Fire Engineering](https://www.fireengineering.com/fire-safety/pass-alarm-signals-fail-at-high-temperatures/)).
  - Component ratings: Johanson LTCC chip −40 to +125 °C, FXP830 FPC −40 to +85 °C (datasheets above).
  - **[est]** The battery (Li-ion, ~60 °C discharge limit) and the u.FL/PE-dielectric coax fail first. For heat tolerance, prefer a **soldered chip antenna or a PCB/LDS antenna over adhesive FPC plus u.FL**, or bond the FPC with a high-temperature adhesive and use a PTFE-jacketed coax.
  - Use a PEEK or PPS shell with an aerogel/silicone thermal liner around the electronics and battery, and keep the liner off the antenna face if it is wet or conductive.
- **Size bounds performance:** see §1.2. At 2.4 GHz, below about 40–45 mm of largest dimension, every mm costs efficiency and robustness against detuning. At 5 GHz, about 25 mm is fine.

---

## 6. Recommended node RF designs

Assumptions for every link budget below **[est]**:
- 2.4 GHz uses HT20 MCS0 at 19 dBm conducted (≤20 dBm EIRP EU). 5 GHz uses 18 dBm.
- Indoor path-loss exponent is 3 beyond 1 m: Friis free-space loss plus 10·log(d) excess.
- Walls come from §3.1: drywall/stud 9 dB, 200 mm concrete 16–20 dB at 2.4 GHz and 28–35 dB at 5 GHz, brick +4.4 / +14.6 dB.
- The CSI quality target is RSSI ≥ −75 dBm (about 20+ dB SNR over a ~−96 dBm noise floor). Below −85 dBm, CSI is mostly noise.

### Tier A — "Micro" ~25 × 15 × 10 mm (keyfob or stick)

- RF design: C5 chip with a single 2.4 GHz LTCC chip antenna (Johanson 2450AT18A0100001E, 3.2 × 1.6 mm) at a board corner with Espressif-style clearance, plus a CLC tuning network. Optionally use a separate 5 GHz chip on ANT_5G.
- **[est]** Expected average gain on a ~25 mm ground is about −3 to −5 dBi (datasheet 0.0 dBi is on the eval board).
- Use: dense same-room motion grids, drop-in sensors. **Not** the through-wall node.
- Risk: the highest detuning sensitivity, so placement repeatability matters.

### Tier B — "Stick or puck" ~45–55 mm (recommended default)

- **Antenna:** one **dual-band FPC dipole** on the inside of the housing wall: Molex 146153 (34.9 × 9 mm, 50–100 mm cable) or Taoglas FXP830 (42 × 7 mm). Keep it ≥15 mm from the PCB ground and battery.
- **Option B2:** two FPCs mounted at 90° (polarization diversity), selected by an SPDT RF switch on the C5 `antenna_select` GPIOs. Use fixed antenna modes and alternate per CSI window.
- **Better for heat and robustness:** a custom flex or LDS dipole printed on the housing, soldered rather than u.FL.
- **[est]** Expected average gain is about −1 dBi at 2.4 GHz and about −2 dBi at 5 GHz (Taoglas on-ABS data).
- This is the physics behind the hackathon finding. Keep it, but make the antenna rigid and part of the housing.

### Tier C — "Wall brick" ~100–150 × 70 × 25 mm (through-wall or perimeter node)

- **Antennas:**
  - 2 orthogonal dual-band FPCs (omni, polarization diversity), plus
  - 1 **directional 2.4 GHz patch** (about 6–8 dBi, ~60 × 60 mm on a λ/4-spaced ground reflector), aimed through the wall.
- A 4-way switch on the C5 antenna-select lines. ESP-IDF supports up to 16 antennas.
- **TX/RX roles:** RX gain is free. On TX through the patch in the EU, back conducted power off to ≤12–14 dBm so EIRP stays ≤20 dBm. In the US, full power is allowed up to 6 dBi.
- If simultaneous MIMO CSI is wanted, this is the tier where a 2×2 chip fits (λ/2 = 62.5 mm baseline at 2.4 GHz). See the chip slice.
- Basis: TU Wien biquad vs PIFA+reflector, 92 % vs 87 % NLOS ([arXiv 2401.01388](https://arxiv.org/abs/2401.01388)).

### Link budget per tier (received power, MCS0) [est]

| Scenario | A: chip, 2.4 GHz (−4 dBi avg each end) | B: FPC, 2.4 GHz (−1 dBi) | B: FPC, 5.5 GHz (−2 dBi, 18 dBm) | C: patch RX (+7 dBi) + FPC TX, 2.4 GHz |
|---|---|---|---|---|
| Same room, 8 m LOS | **−56 dBm** | **−50 dBm** | −60 dBm | −42 dBm |
| 12 m through 1 drywall/stud wall | −71 dBm | −65 dBm | −76 dBm | −57 dBm |
| 12 m through 1 brick wall (Magis) | −66 dBm | −60 dBm | −80 dBm | −52 dBm |
| 12 m through 200 mm concrete | −78 to −82 dBm (marginal) | −72 to −76 dBm (OK/edge) | −94 to −101 dBm (**fails**) | −64 to −68 dBm (good) |
| 12 m through reinforced concrete (~40 dB) | ~−102 (fails) | ~−96 (fails) | fails | ~−88 (fails/marginal) |

Arithmetic:
- Tier B 2.4 GHz, drywall case: 19 dBm − 1 − FSPL(12 m) 61.8 − 10.8 excess − 9 wall − 1 = −64.6 dBm.
- 5 GHz uses FSPL(12 m) = 68.8 dB. For drywall, assume a 10 dB hollow wall at 5 GHz.
- Add 3–6 dB margin for a path through active fire (§3.2) and about 3–10 dB for bodies or furniture in the path.

**Reading the table:**
- Tier B on 2.4 GHz covers a room and one ordinary wall with 10+ dB margin over the −75 dBm CSI target.
- Tier A works only in the same room or through drywall.
- 5 GHz is fine in-room and through drywall, but **drops out behind masonry or concrete**.
- Tier C's RX directivity is the only legal way (EU) to gain about 8 dB through heavy walls.

---

## 7. Caveats and open items

- Chip-antenna efficiency on a ~25 mm board has not been measured. Before committing to Tier A, build a 2-board A/B test in the final housing (Johanson chip vs FPC), measuring CSI amplitude variance with an empty room vs a walker.
- Wall-loss data is old (Magis 2003, NIST 1997) and sample-specific. The ITU-R P.2040 bulk numbers exclude reflections and rebar. Calibrate on the target building types.
- No primary source was found that compares 2.4 vs 5 GHz **through-wall CSI** motion detection directly. Run that comparison on the C5, which can do both.
- The XIAO bundled FPC may be 2.4 GHz-only (Seeed's labelling conflicts). Verify before running 5 GHz tests, or 5 GHz will look artificially bad.
- Regulatory: a custom antenna, or gain above the module grant, means new FCC/CE testing. A directional TX antenna in the EU needs a conducted-power back-off.
- The heat-survival numbers (battery, u.FL coax) are estimates. Benchmark them against an NFPA 1982-style 260 °C / 5 min oven test.
- The Ignion NN02-224 and Molex figures come from distributor-hosted datasheets, and revisions differ. Re-check the current PDFs before buying.
