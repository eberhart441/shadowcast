# 04 — History and evidence: what the team actually tried to scale past 4 nodes

Slice: the empirical record from the hackathon (2026-11-07 → 2026-11-08). Other reports reason from first
principles; this one only says what was tried, what was measured, and what people believed at the time.

## Sources and conventions

- **Transcripts** (Claude Code sessions on Simon's laptop, `C:\Users\simon\.claude\projects\C--Users-simon-Documents-hackathon\`):
  - `49e8` = `49e88fd3-…jsonl`: Nov 7 11:23–21:48. TDMA design, all-transceiver firmware, 2/4/5-node transceiver tests.
  - `a466` = `a466baf5-…jsonl`: Nov 7 21:53 → Nov 8 14:54. Serial-bottleneck model, transceiver-5plus, Pi deploy, rx3 6-link run, wireless gateway, battery and dead-board day.
  - `6a6c` = `6a6ca4a7-…jsonl` (docs polish) and `8c3d` (trivial): nothing about scaling.
  - Only Simon's sessions are recorded. Teammates' work (andrewaburustum, ProgrammerGN, Siv, mumtez) is visible **only through git**.
- **Times are US Eastern (UTC−5)**, the same zone as the git commit dates. Transcript timestamps were converted from UTC.
- **Git:** `Team-12-tracker` (all branches), plus checkouts `Team-12` (csi-sensing), `Team-12-5plus` (feat/transceiver-5plus @404aa8d), `Team-12-siv-build` (@f5f299a), `Team-12-docs`, and `fw_tx0/*.log`.
- **Recorded data:** none for any multi-node rig. Every `data/` directory is empty. The only committed recordings are the single-link `hw3`/`hw5` sessions on `origin/data/two-board-sessions`. All multi-node numbers below come from tool output captured in the transcripts or from commit messages and READMEs. The Pi's raw `~/runs/*.tsv` recordings were never copied off the Pi.
- Tags used below: **[OBS]** observed or measured on hardware; **[DOC]** a number written in a README, ADR or commit with no raw data behind it; **[HYP]** what someone believed or modelled at the time; **[INF]** my inference from the record.

---

## 1. Bottom line

1. **"We never got past 4 boards" is not literally true.** The 5-board split-role rig (3 receivers × 2 senders = **6 links**) ran twice at close to full rate:
   - andrewaburustum, Nov 7 about 16:00: "a third C5 receiver… all links stayed at 34 to 38 packets/s" [DOC 60067fa].
   - Simon on the Pi, Nov 8 01:22, after the reader fix: rx1 35.5–37, rx3 36.5/36.5, rx2←01 37 pkt/s [OBS a466].

   Both runs were switched off for reasons other than a node-count limit:
   - The first was switched off with no reason recorded.
   - The second ended because of a flaky rx1 USB cable, a half-deaf rx2, and the user's verdict "having the 5th node causes too much problems".
2. **The 5-node *all-transceiver* rig did fail on hardware** (Nov 7, 21:00–21:34): 0–27 pkt/s per link against 40 Hz. It was already degraded at 4 transceivers (3.5–22 pkt/s, 20:04). Several causes were confounded in that run:
   - per-node serial output overload, the strongest candidate;
   - S3 boards in the rig;
   - boards on battery that were blinking;
   - the receive gain lock disabled;
   - a phase-lock bug in the turn-taking.
3. **The best-measured bottleneck is per-receiver serial output plus the host's serial reader, not radio collisions.** This was shown by an A/B test on the Pi with sender sequence counters (Nov 8, 01:13). The original "collisions" belief rests on one unrecorded measurement (100 Hz → 65% loss) that was very likely serial overflow (see §5).
4. **Physical unreliability ended more attempts than any protocol limit:**
   - batteries and power banks;
   - charge-only or flaky USB cables;
   - two boards that died;
   - XIAO C5 boards run without antennas;
   - a receiver 40 dB down because of where it sat;
   - ghost boards on the old firmware.
5. **Several proposed fixes were never tried on hardware:**
   - a third sender at any rate;
   - Siv's sender turn-taking on the split-role rig;
   - the `fix/scaling-and-accuracy` firmware (20 Hz senders, `CSI_PRINT_STRIDE`);
   - HT20 and a multi-channel split;
   - the `transceiver-5plus` firmware at 5 boards.

---

## 2. Timeline

### Friday 2026-11-07

| Time | Event | Source |
|---|---|---|
| 11:39 | Two-board (single-link) sessions hw3/hw5 recorded; the only committed CSI data. | 8075e4f |
| 12:06–13:44 | Chip roles swapped repeatedly (C5, S3, original ESP32). The original ESP32 as a sender gave 6–17 pkt/s against about 38 for an S3 sender, so it was retired. | e6fe844, ca82d04; ADR 0002 |
| 14:27 | README states **"At 100 Hz, two senders collided and lost about 65% of packets"**; the rate is set to 40 Hz, giving "5 to 10%" loss. No capture is committed. | fb3198a [DOC] |
| 15:09 | **rx3 enabled as an ESP32-S3** receiver at top-middle (5 boards, 6 links). | c3334f4 |
| 15:15 | rx3 moved 1.5 m beyond the top wall. | 6a8598d |
| 15:51 | S3 rx3 disabled: "The S3 receivers were much noisier than the C5 receiver". Docs later add "a third S3 receiver cut the other receiver's packet rate from about 35 to 22 to 26 packets/s". | 5c705c9, 60067fa [DOC] |
| 15:59 | **rx3 re-enabled as an ESP32-C5**, beyond the top wall. | 6b0bab3 |
| 16:04 | rx3 moved onto the top wall between rx1 and rx2. hardware.md later records it as "the strongest sensor, with walking scoring 25 to 31 against a quiet maximum of about 11". | 5fc978c; docs/hardware.md [DOC] |
| 16:14 | **rx3 disabled: "back to the two-C5-receiver rig that was working"**. No reason is recorded anywhere, yet the README written 12 minutes later says the C5 rx3 run kept "all links… at 34 to 38 packets/s". | c3220bc, 60067fa |
| 16:52 | Simon asks "why does it get harder to scale above 4". The assistant's answer (collisions, USB ports, S3 noise) is built from the README, not from measurements. | 49e8 |
| 17:25 | Simon: "according to the last guy who touched this, it is **impossible to scale above 4 nodes**". The assistant searched commits, issues and PRs and found no written source for this claim. | 49e8 |
| 17:36 | TDMA + multi-channel design doc. It notes that airtime is "~3.5% nominal channel utilization" at 2×100 Hz, which "shouldn't produce anywhere near the measured 65% loss". Issues #16–#21 filed. Never implemented; the issues were closed unbuilt on Nov 8 at 10:50. | d556bea; gh issues |
| 17:45 | Siv's branch "Senders take turn" lands. It was compiled at 18:16 under IDF 6.0, after an enum-rename fix, but never run on the split-role senders. | f5f299a, 33cfe96 |
| 18:43–18:48 | **All-transceiver, 2 boards.** One node hears the other at about 39 pkt/s, but `ESP_ERR_ESPNOW_NO_MEM` floods; a re-run collapses one direction to 4.8 pkt/s. "flaky… the same setup gives wildly different results run to run". | 49e8, 98d0e89 [OBS] |
| 19:06–19:24 | Diagnosis with 5 agents: (1) NO_MEM, a known ESP-IDF issue with unpaced broadcast sends; (2) an esp_timer race; (3) **CONFIG_FORCE_GAIN's lock at packet 100 makes a node deaf about 2–3 s in**. With the gain lock disabled: "3 consecutive 20s runs… ~35 pkt/s each direction… zero NO_MEM". The trade-off was flagged: the gain lock existed for amplitude stability. | 3064795 [OBS] |
| 19:31 | 2 transceivers through the real dashboard: cross-links at about 36 pkt/s. | 49e8 [OBS] |
| 19:50 | Board COM8 turns out to be an **ESP32-S3**, so the transceiver rig now mixes chips. node4 is later an S3 too. | 49e8 |
| 20:02 | node1 is not detected on USB ("I dont have a 4th cable"), yet it is still transmitting. | 49e8 |
| **20:04–20:05** | **All-transceiver, 4 boards** (3 on USB, on a desk). Per-link rates **3.5–22 pkt/s** against a 40 Hz target; the S3 node2 is worst at 3.5–10.5. The assistant called it a "Huge result… working". | 49e8 tool output [OBS] |
| 20:05 | Wireless forwarding design (ESP-NOW relay, or UDP to an AP on channel 6). | b185c10 |
| 20:12 | Simon: "first step is just testing with 6 and seeing if it craps out like it used to when we went above 4". | 49e8 |
| 20:17–20:51 | All 5 boards reflashed with `NUM_SENDERS=5`. A session limit was hit at 20:32. node0 got unplugged; node1's cable kept failing. Every board needs its own build (about 5 min each on Windows). | 49e8 |
| **21:00–21:06** | **All-transceiver, 5 boards** (node0 C5 and node4 S3 wired; the other three on battery). Best links 19.5–27.5 pkt/s; node4←00 fell from 0.5 to 0; node1 and node3 "zero packets, ever"; calibration rejected links as "too sparse… 60–64% packet loss". | 49e8 [OBS] |
| 21:07–21:10 | Simon: "light is blinking on bottom right and left". Moved off battery: "no they are just on usb power instead of battery". node1 then appears at 11.5 → 22.5 pkt/s. | 49e8 [OBS] |
| 21:11–21:13 | node0↔node4, previously the best pair, go silent to each other; node0 then hears nothing at all. The assistant suspected a "stable bad phase-lock" in the decentralised turn-taking. | 49e8 |
| 21:15–21:29 | A random-jitter fix is flashed, slowed by node1's flaky cable again. Four active nodes (node3 excluded) give **1.5–18.5 pkt/s** per link. | 35d5cb6; 49e8 [OBS] |
| 21:30–21:38 | Simon: "it isnt working… i would call this a failure… **i think the problem was we were biting off too much**". Back to `csi-sensing`. | 49e8 |
| 21:41 | The assistant's verdict: 2 nodes proven (3/3 runs); at 5 nodes, one good-looking run at best; "never got the kind of repeated, multi-run confirmation". | 49e8 |
| 20:07–23:01 (parallel) | ProgrammerGN's `fix/scaling-and-accuracy` (details below). | fe257f2, 560917e, f740336, 6fa2d58, 47cf162 |
| | - Claims "**The limit on scaling is each receiver's serial output, not the radio**": a C5 receiver caps at about 100–116 pkt/s summed over all its senders, an S3 at about 65–79; "Collisions between senders are negligible at these rates". | |
| | - Firmware: 20 Hz senders, a new MAC prefix, `CSI_PRINT_STRIDE`, and the gain log demoted to debug level. | |
| | - The 110 lines/s cap is **derived from line length × baud**; its README calls the stride gain "an estimate". | |
| | - The commits say "Not run here: … an ESP-IDF build". This firmware was **never flashed on the rig**. | |
| 21:57 | Simon's belief: "once you have 5 nodes it stops working and it is likely because the esps just cant handle that amount of data… sequencing to clear up bandwidth". | a466 [HYP] |
| 22:01 | A **toy model** (`bottleneck_model.py`, not hardware) gives: 5 transceivers at 40 Hz need 160 lines/s each, against about 120 the UART can carry, so 26–28% serial drop and 0% air loss. "Turn-taking and free-running timers give the same result." | a466 [HYP/model] |
| 22:08–22:30 | **transceiver-5plus**: the rate becomes 60/(N−1) lines/s, the queue-full log spam is removed, and ID and node count are read from NVS. Two boards ran at 40 Hz with `csi_dropped=0`. A **ghost board** ("RSSI −3… another powered board is also transmitting as node 0") forced a MAC-prefix change, after which the C5 heard nothing. | 404aa8d [OBS] |
| 23:00 | Simon: "**put all that on the shelf**", pivoting to the Pi demo. The 5-board test was never run. | a466, 404aa8d |
| 23:49–23:58 | Pi with 2 wired receivers: 4 links at 36–39 pkt/s. The venue `Guest` Wi-Fi isolates clients (Meraki). | a466 [OBS] |

### Saturday 2026-11-08

| Time | Event | Source |
|---|---|---|
| 00:26 | rx1 USB disconnect in the Pi kernel log. rx2 hears both senders at **−94 to −97 dBm** (normally −45 to −59) and loses 85% on one link. | a466 [OBS] |
| 00:59 | A new C5 receiver alone on the laptop hears tx0 at 36 pkt/s (−54 dBm) and tx1 at 39 pkt/s (−49 dBm). | a466, 28506bf [OBS] |
| **01:00** | **rx3 enabled (C5) → 5 boards, 6 links.** | 28506bf |
| 01:06 | rx1 28–30.5, rx3 22.5–23.5 pkt/s; rx2←01 38.5 at −94 dBm; rx2←00 dead. Simon: "the data for all these is pure slop… packet loss". | a466 [OBS] |
| **01:13** | **Controlled A/B on the Pi, 15 s per mode, loss from the senders' own counters.** `main`'s `readline()` threads: 164–166% CPU, **19–27% loss**. Chunked reader: 4–6% CPU, **5–12% loss**. Receivers that hear 2 senders print **140 lines/s, about 56–65 KB/s, half of them the per-packet `compensate_gain` log line**. rx2, which hears only one sender: **0.7% loss**. | a466 tool output [OBS] |
| 01:17–01:22 | 867eea3 adds the chunked reader and demotes the gain log, flashed from the Pi. **6 links**: rx1 35.5–37, rx3 36.5/36.5, rx2←01 37 pkt/s. Loss **4–8%** per link; 72 lines/s per receiver; dashboard 7–11% CPU. | 867eea3; a466 [OBS] |
| 01:21 | rx1 drops off USB again. After four reboots it is still not enumerated, and the assistant blames the cable. Simon: "the cable doesnt change outof nowhere". At 02:41 rx1 enumerates at once **on rx2's cable**. | a466 [OBS] |
| **01:41** | Simon: "**i think having the 5th node causes too much problems**… go off the firmware we used for the 4 receivers bc that was the last that worked". rx3's board becomes the wireless **gateway**. | a466; 165ce75 |
| 02:19 | Gateway forwarding at MCS2 lost over half its unicasts; at MCS0 about 36/s. | 68c2b5f [OBS] |
| 02:37 | rx2 at −59 dBm after being handled or moved. The −94 dBm problem was placement or orientation, not the board. | a466 [OBS] |
| 02:44 | Wireless 4-link rig: 35.5–39.5 pkt/s per link. | a466 [OBS] |
| **03:20** | Simon: "**keep in mind we never were successfuly able to get past 4 boards**". The assistant answered: "we did briefly run 5 boards with 6 links at full rate tonight, around 01:20". | a466 |
| 03:22 | Simon: "i dont really wanna reflash firmware". The hardware agent studying a **third sender** (6 links) was stopped, and the 6-link option dropped. | a466 |
| 04:18–05:32 | Accuracy run on the wireless rig, clean. Loss per link (Pi's raw tsv): rx1←tx0 5–7%, rx1←tx1 2%, rx2←tx0 18–20%, rx2←tx1 5–12%. | a466 noise_scan [OBS] |
| 06:50–08:38 | **Battery failures.** tx0 at 06:50; rx2 at 06:56, 07:48 and 07:57; tx1 at 08:14; rx1 at 08:20. Boards were run from power banks *while the banks charged*. During charging, rx2's noise floor rose from −97 to −91/−93 dBm and its loss reached **91%** on rx2←tx0. | a466 [OBS] |
| 08:52 | Diagnosis of the day's loss: (1) tx1 moved or turned, −25 dB, 44–78% loss; (2) the charging noise above; (3) **venue AP "BASE-X Guest" on channel 6**, taking rx1←tx0 from 5–7% loss to 9–16%. | a466 [OBS] |
| 09:06 | tx0 dies mid-run. LED "on and then it turns off", and it never enumerates on USB. Board dead. | a466 [OBS] |
| 10:16–10:28 | rx1 goes silent. The replacement tx0 is a **Seeed XIAO ESP32-C5** (fw_tx0/flash.log: "ESP32-C5HR8", COM13), flashed with **no antenna**, so neither receiver hears it. | a466; fw_tx0/flash.log [OBS] |
| 10:44–11:01 | The replacement rx1 is a new XIAO, also without an antenna at first. With its antenna on, it sat "a few centimetres from the gateway" and its forwards failed. Moved ≥1 m away: **all 4 links 37–40 pkt/s**. | a466; ADR 0019 [OBS] |
| 11:32 | PR #34 merged. Final rig: **4 sensing boards** plus a gateway (5 ESP32s, 4 links). rx3 "was tried as a fifth node on 2026-11-08 and taken out again" (docs/hardware.md). | c0ad162 |

---

## 3. Attempts table

| # | Attempt | Config | Observed numbers | Outcome and stated reason | Source |
|---|---|---|---|---|---|
| A1 | 2 senders at 100 Hz | 1–2 receivers, stock esp-csi receiver (prints CSI plus a `compensate_gain` log line per packet, inside the Wi-Fi callback) | "lost about 65% of packets"; 40 Hz gives "5 to 10%" | Rate cut to 40 Hz. **Attributed to collisions.** No raw data. | fb3198a [DOC] |
| A2 | S3 rx3 (5 boards, 6 links, split roles) | rx1/rx2 C5, rx3 S3, 2 senders at 40 Hz, laptop host | Other receiver 35 → 22–26 pkt/s; S3 links 35–65% false MOTION | Disabled at 15:51: "S3 receivers were much noisier" | c3334f4 → 5c705c9; ADR 0002 [DOC] |
| A3 | C5 rx3 (5 boards, 6 links) | 3 C5 receivers, 2 senders at 40 Hz, laptop | "all links stayed at 34 to 38 packets/s"; rx3 on the top wall was the best sensor | **Disabled at 16:14 with no reason given** ("back to the… rig that was working") | 6b0bab3, 5fc978c, c3220bc, 60067fa [DOC] |
| A4 | TDMA + multi-channel design | Paper only | Airtime about 3.5% at 2×100 Hz, which "doesn't fully explain 65%" | Never built; issues #16–#21 closed 11-08 | d556bea [HYP] |
| A5 | Siv's sender turn-taking | csi_send only | None: compile-only | Folded into A6; never run on split-role senders | f5f299a; 49e8 18:16 |
| A6 | All-transceiver, 2 nodes | 2 C5, 40 Hz, turn-taking + CSI capture | NO_MEM floods; 4.8 pkt/s collapse; after fixes, **~35 pkt/s each way, 3/3 runs** | Worked, **with FORCE_GAIN disabled** (amplitude stability lost, never re-validated) | 98d0e89, 3064795 [OBS] |
| A7 | All-transceiver, 4 nodes | 3 C5 + 1 S3, `NUM_SENDERS=4`, 3 on USB, on a desk | **3.5–22 pkt/s per link** (S3 node2: 3.5–10.5) | Called "working"; not treated as a failure at the time [INF: already over budget] | 49e8 20:04 [OBS] |
| A8 | **All-transceiver, 5 nodes** | 3 C5 + 2 S3, `NUM_SENDERS=5`, 2 wired, 3 on battery, real room | 0–27.5 pkt/s; 60–64% loss; pairs going one-way deaf; blinking boards on battery | Jitter patch gave 1.5–18.5 pkt/s on 4 nodes. **Abandoned: "biting off too much"** | 49e8 21:00–21:38, 35d5cb6 [OBS] |
| A9 | Serial-budget model | Python toy model | 5 nodes: 26–28% serial drop, 0% air loss | Motivated A10 | a466 22:01 [HYP] |
| A10 | transceiver-5plus | Rate 60/(N−1), NVS IDs, `flash_all.py`, new prefix | 2 boards: 40 Hz, `csi_dropped=0`; then a ghost old-rig board; then heard=0 after the prefix change | **Shelved at 23:00 (pivot to the Pi demo); the 5-board test was never run** | 404aa8d [OBS, partial] |
| A11 | `fix/scaling-and-accuracy` (ProgrammerGN) | 20 Hz senders, stride, prefix, chunked reader, RTI | Budget derived, not measured | **Only the Python reader and the gain-log demotion were ever deployed** (867eea3); the firmware was never flashed | fe257f2, f740336, 47cf162 |
| A12 | rx3 C5 on the Pi (5 boards, 6 links) | 3 C5 receivers wired to a Pi 5, 2 senders at 40 Hz | Before the fix: 22.5–30.5 pkt/s, 19–27% loss. **After 867eea3: 35.5–37 pkt/s, 4–8% loss.** rx2 was half-deaf at −94 dBm throughout | **Taken out at 01:41: "having the 5th node causes too much problems"**. Proximate cause: rx1 cable dropouts, which started at 00:26, before rx3 was added. | 28506bf, 867eea3; a466 [OBS] |
| A13 | Wireless receivers via a gateway | 2 forwarding receivers + 1 gateway on the Pi | 4 links at 35.5–40 pkt/s | **Shipped**. It removes the per-receiver serial limit, but **was never scaled beyond 2 receivers** | 165ce75, 68c2b5f; ADR 0019 [OBS] |
| A14 | Third sender (6 links, wireless rig) | Proposed | — | Dropped at 03:22: "dont really wanna reflash firmware" | a466 |

---

## 4. Non-software causes, as recorded

| Cause | Evidence | Effect on scaling attempts |
|---|---|---|
| **Batteries and power banks** | Blinking LEDs on battery boards in the 5-node test, fixed by USB power (Nov 7 21:07–21:10). Nov 8 06:50–08:38: four boards died one by one; "running them while recharging" made outputs cut in and out; charging raised the noise floor by 4–6 dB and loss reached 91%. | Directly confounded A8. Ate most of Nov 8 morning. |
| **Flaky or charge-only USB cables** | node1 "persistent cable issue" (Nov 7 20:02, 21:21–21:27). rx1 dropouts at 00:26 and 01:21, four failed reboots, then it enumerated at once on a different cable (02:41). | Ended A12 and slowed every reflash. |
| **Dead boards** | Original tx0 died at 09:06 Nov 8 (never enumerates; LED goes off). Original rx1 went silent at 10:16 and was replaced. | Not during a scaling test, but cost 2 of the 5 C5 boards. |
| **XIAO ESP32-C5 without antenna** | The new tx0 was not heard by either receiver until its antenna went on; the same for the new rx1; a half-seated antenna behaves like none. | Now in docs/agents/hardware.md. |
| **Placement and orientation** | rx2 −94 to −97 dBm (40 dB down) all night until moved at 02:37 (−59 dBm). tx1 moved or turned, −25 dB, 44–78% loss. A receiver within cm of the gateway lost all its forwards. | rx2 being half-deaf made the 6-link run look worse than it was. |
| **S3 boards** | S3 receivers 35–65% false MOTION against 4–16% for C5; S3 rx3 cut the other receiver's rate. Two of the five transceivers in A8 were S3. S3 serial budget estimated at 65–79 lines/s against 100–116 for a C5. | Confounds A2 and A8. |
| **Wrong board or ghost boards** | An old-rig board transmitting as node 0 at RSSI −3 (22:24). Plugging rx2 when rx1 was meant (02:35, 09:57). The user was "not sure which one is rx1". | Forced a MAC-prefix change mid-attempt (A10). |
| **Venue Wi-Fi** | "BASE-X Guest" AP on channel 6; rx1←tx0 loss rose from 5–7% to 9–16% as the venue filled. The guest network also isolated clients. | Small. Channel 6 "was picked, not surveyed" (ADR 0003). |
| **Time, tooling and process** | Claude session limit at 20:32 and an API rate limit at 21:18. A first ESP-IDF build per board takes minutes on Windows; parallel builds raced on a shared `sdkconfig`; the 260-character path limit broke builds. Every transceiver had to be reflashed whenever N changed. "keep it super simple", "dont overcomplicate", "put all that on the shelf", "dont really wanna reflash". | The stated reason for abandoning A8, A10 and A14. |

---

## 5. Beliefs at the time vs what was tested

| Belief (who, when) | Tested? | What the record shows |
|---|---|---|
| "Collisions grow with senders" / "65% loss at 100 Hz" (andrew, README 14:27) | **No.** One unrecorded run, and three senders were never tried ("Not measured: three senders at any rate", ADR 0003). | [INF, strong] At 2 × 90.9 pkt/s the stock receiver had to print about 182 CSI lines/s **plus 182 `compensate_gain` log lines** (an `ESP_LOGI` in the callback, in the vendored firmware at 6975e7e) through a 921600-baud UART. The Pi later measured that 2 × 38 pkt/s already produces 140 lines/s, about 65 KB/s, with 5–12% loss from the receiver alone. The airtime arithmetic (3.5%) and ProgrammerGN's "collisions negligible" agree. The 65% was most likely serial overflow mislabelled as collisions. |
| "S3 third receiver cut others' rates 35 → 22–26 because S3s are noisier" (andrew) | Partly: chip swaps were observed side by side. | [INF, weak] The same symptom (a third receiver drops the others to about 28 pkt/s) reappeared on the Pi with an all-C5 rx3, and was caused by the host's byte-at-a-time `readline()` reader (164% CPU). S3 lines are also longer. Host reading may explain part of A2; untested. |
| "Impossible to scale above 4" (unnamed teammate, relayed 17:25) | — | No written source. A3 (and later A12) contradict it. |
| "Need TDMA / sequencing to clear up bandwidth" (Simon, 17:34 and 21:57) | Only inside the all-transceiver firmware, where it was confounded. | The model said sequencing makes no difference at 40 Hz (air about 2% busy). Never A/B-tested on hardware. |
| "The ESPs can't handle that amount of data" (Simon, 21:57; 01:11 "just too much data") | **Yes, for split roles** (A/B, 01:13). | Correct, but the bigger share was the **host** reader (19–27% loss → 5–12%), and half the board output was a debug log line. |
| "One USB port per receiver is a hard limit" (README) | Never hit (at most 3–4 ports were used). | Removed by the gateway (A13). |
| "5th node causes too many problems" (Simon, 01:41) | No. | The rx1 dropouts started at 00:26, before rx3 was plugged in, and followed rx1's cable. With rx3 in, the measured rates were at full rate after the reader fix. |
| "Gain lock makes transceivers deaf" (agent, 19:14) | Yes, A/B 3/3 on 2 nodes. | Real on transceivers. Never re-checked whether disabling it hurts sensing. The same lock later broke a receiver within cm of the gateway. |

---

## 6. What actually stopped scaling, ranked by evidence

1. **Per-board serial output, plus the host's serial reader** (strong, measured).
   - Split roles: measured A/B with sender counters (01:13).
   - Transceivers: the design forces every node to print (N−1) × 40 lines/s. The degradation at 4 nodes (3.5–22 pkt/s) and 5 nodes is consistent with this, but was never instrumented with `csi_dropped`, because A10 never ran at 5.
   - The about 110 lines/s cap is arithmetic, not a saturation measurement.
2. **Physical unreliability** (strong, observed many times): power banks, cables, dead boards, missing antennas, placement, mixed S3 and C5 chips, ghost boards. This was the *proximate* reason A8 and A12 were stopped, and it contaminated every multi-node number.
3. **All-transceiver firmware fragility** (strong for existence, moderate for the 5-node failure): NO_MEM, the timer race, gain-lock deafness, and a turn-taking phase lock that was only patched with jitter. Concentrating send, capture, print and sync on one core made failures hard to attribute ("biting off too much").
4. **Time and scope pressure** (strong, explicit): the stated reason for shelving A8, A10 and A14 and for never testing the teammate firmware, the third sender or TDMA on split roles.
5. **RF collisions and airtime** (weak): the team's original belief, never isolated, and contradicted by the airtime math and the A/B test. The venue AP on channel 6 is a small, real contributor (+4–9 points of loss).

**Never tested, and still open:**
- 3 or more senders at any rate.
- Split-role senders with turn-taking.
- `CSI_PRINT_STRIDE` / 20 Hz.
- More than 2 receivers through the gateway.
- HT20 / multi-channel.
- Whether running with the gain lock disabled harms detection.
