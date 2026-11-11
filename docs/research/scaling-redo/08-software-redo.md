# 08 - Software redo: what to keep, what to rewrite, what stack

Scope: design quality of `Team-12-tracker` (branch feat/wireless-receivers, read-only) and the forward stack. No profiling (other agents). Code cites are `Team-12-tracker/<path>:<line>`.

Size: ~9.7k lines incl. docs; `csi/` 25 modules, `web/index.html` 1026 lines (single file), firmware = two stock esp-csi examples with edits (`csi_recv/main/app_main.c` 505 lines, `csi_send` 174). Tests: 57 files in `tests/`, 449 `def test_` functions; `scripts/check.sh` = ruff (only `F,E9` rules, pyproject.toml) + pytest-xdist.

**Important gap:** `data/` is empty and `tests/fixtures/` holds only three room-geometry JSONs. There is no committed real CSI recording and no real-data golden test; every detection test runs on the simulator (`csi/simulate.py`). The hard-won knowledge lives in constants and ADR prose, not in data.

## 1. Inventory and verdict

| Module | What it is | Verdict |
|---|---|---|
| `csi/detect.py` `MotionScorer` (:111-170) | Per-subcarrier 3-stage exponential high-pass in device time (tau 0.16 s), variance over 1 s window, mean across subcarriers. Breathing attenuated ~21 dB at 0.5 Hz. | **Keep the idea, port the math.** Core domain knowledge (ADR 0007). Weak point: uniform subcarrier averaging (:169). Move onto node. |
| `detect.py` `_Debouncer`, `classify` (:173-243) | MOTION immediate, leaving debounced 0.5 s. | Keep (asymmetry is right for ADR 0001). |
| `detect.py` calibration (:272-424) | Chunk-wise contamination rejection (median > 3x quiet baseline or p90 > 2.5x median), threshold = 1.25 x p99; noisy mode p97; refuses on loss/sparsity. | **Rewrite as continuous self-baseline.** The contamination logic is valuable; the "operator records empty room per link after the last reset" workflow is what ADR 0020 says to avoid. Keep the quality gates as link-health checks. |
| `csi/clock.py` `DeviceClock` | Unwrap 32-bit us counter; tell wrap from reboot by host-clock agreement. | **Drop** in redo: `boot_id` + 64-bit timestamp in the protocol removes the heuristic. Keep tests as spec. |
| `csi/parser.py` | Parse esp-csi CSV text lines; null-subcarrier table (C5 234 raw -> 57-59 null). | **Drop** parser (text over serial is scaffolding); keep the null-subcarrier data. |
| `csi/links.py` | Link = (rx, last octet of tx MAC) (:49 `mac[-2:]`); per-link scorer/health/state; calibration orchestration. | **Rewrite.** Last-octet label collides; key by (rx_mac, tx_mac). Keep NO_SIGNAL / WARMUP / UNCALIBRATED vocabulary (visible-not-silent is right). |
| `csi/adaptive.py` | Rolling 75 s median + 1.4826*MAD; enter = max(b+5.5s, 2b); exit lower; 0.6 s sustain; baseline rebuild when 80% of last 30 s high/low (:161-171, copes with receiver re-baseline); `RoomDecision` needs >=2 elevated links or one "strong" (:55, :220). | **Keep as basis of the redo detector** (right per-link normaliser) but fix stillness absorption, fixed `ROOM_MIN_LINKS`, and gain-step handling (section 2). |
| `csi/heatmap.py` | Per-link log-ratio evidence vs own 60 s median (1.2 -> 0, 3 -> 1; ADR 0011), Gaussian footprint around link segment, per-cell `0.5*sum + 0.5*sqrt(top1*top2)`, smoothing. | **Keep the evidence idea** (ADR 0011 is a real finding: threshold-normalised evidence gave ~zero heat). **Rewrite the spatial part** as regularised RTI. |
| `csi/tracker.py` | Kalman on heat-blob centroid. | Keep as a toy only. Per standing decision (memory: locator rejected), no trained localisation; zone-level output is the honest product. |
| `csi/room.py` | `config/room.json`, Gaussian link response for simulation, Walk/Stand. | Keep geometry + `point_segment_distance`; replace config with MAC-keyed fleet registry. |
| `csi/breathing.py` | Off by default; false-fires on empty rooms. | **Drop** from v1 (stillness unreliable). |
| `csi/server.py` (872 lines), `dashboard.py`, `replay.py`, `viewer.py` | stdlib `ThreadingHTTPServer` + SSE, loopback-only commands, `_Reorder` buffer (:172), frame-log replay. | **Rewrite** on asyncio/WebSocket. Keep ideas: loopback-only control (ADR 0015), calibration off the server lock (0016), packet time not wall-clock (0013), replay of dashboard frames (UI regression for free). |
| `csi/multisource.py`, `gateway.py`, `ports.py` | N serial ports -> streams; `RX,<serial>,<line>` demux; boards by USB serial. | **Drop** (port juggling is a symptom of wired text transport). Silent drop of unknown serials (ADR 0019) becomes a "pending node" in UI. |
| `csi/recording.py`, `evaluate.py`, `accuracy*.py`, `scripts/record.py` | Labelled CSV sessions, confusion-matrix evaluation (ADR 0018). | **Keep concept, upgrade format and metrics.** Evaluate-by-recording is the best process decision in the repo. |
| `csi/simulate.py` | Synthetic per-link streams, Gaussian response by person-to-segment distance. | Keep as base; extend (section 3). |
| `csi/health.py` | pkt rate, drops. | Keep. |
| `web/index.html` | Single page: link table, heatmap, dot, calibration controls. | Treat as UX spec; rewrite. |
| `firmware/*` | Vendored esp-csi; `SENDER_ID` build flag; `CSI_FORWARD`/`CSI_GATEWAY` flags; `CONFIG_FORCE_GAIN 1` (`app_main.c:71`), gain baseline from first 100 packets (:258-270). | **Rewrite as one image, role at runtime.** Keep channel 6 / 40 Hz (ADR 0003), forced gain concept, ESP-NOW forward experience, the `FWD 1s` / `GW 1s` self-telemetry idea. |
| `docs/adr/*` (20), `GLOSSARY.md`, `docs/agents/*` | Decision records with Evidence and "Not measured" sections. | **Keep wholesale.** ADR 0001/0007/0009/0011/0020 are the redo's requirements. |

Domain knowledge to port deliberately (it sits in constants and comments, easy to lose): amplitude-only score (ADR 0007); high-pass tau 0.16 s x 3 stages; 1 s window with >=5 packets; REJECT_RATIO 3 / BURST_RATIO 2.5; ADR 0011 log-ratio evidence with deadzone 1.2 and full at 3; 75 s adaptive window, 0.6 s sustain, hysteresis; "re-baseline shifts scores 3-5x" (`adaptive.py:3`); wire lessons (null subcarriers, gain locks ~1 s after boot, receiver within 1 m of gateway floods ESP-NOW, `docs/agents/hardware.md:24`).
Hackathon scaffolding: stdlib server, serial/port logic, text parsing, per-chip build dirs, `port_hint`, the `scripts/` zoo, doc drift (project-context.md "Doc drift").

## 2. Algorithm quality and scaling to N links

N nodes in a full mesh give N(N-1) directed links (8 nodes: 56; 16: 240). The code is built for 4. What breaks or worsens:

1. **Per-link thresholds = N^2 calibrations.** Fixed mode needs an empty-room recording per link after the last receiver reset (hardware.md:5-11), unworkable at 56 links and contrary to ADR 0020. `adaptive.py` already removes it; ADR 0009's revert was about walking sensitivity (enter >= 2x baseline, 0.6 s sustain, two-link agreement), not a flaw of the idea, and no sensitivity figures were recorded. Redo: baseline-relative z-score as the only mode, tuned on recordings.
2. **Fixed `min_links = 2` fusion (`adaptive.py:55,220`) ignores N.** If any link false-fires with probability p per window, "any 2 of N" fires ~C(N,2)p^2: 6 pairs at 4 links, 1540 at 56 (~250x). Raising k hurts misses (ADR 0001). Use spatially aware fusion: sum weighted per-link evidence over links that cover the same area; do not count links sharing a node as independent votes (one node's front-end glitch or gain step hits all its links). This is where more links help most: independent geometric views suppress noise.
3. **Stillness absorption is a miss-cost problem.** The 75 s median plus rebuild when 80% of last 30 s is high (`adaptive.py:161-171`) absorbs a person who stops for ~30 s (ADR 0009/0011 consequences); it repeats on every link at scale. Proposal: a **zone-level occupancy latch**: after motion in a zone, report "possibly occupied (last motion N min ago)" rather than EMPTY unless an exit pattern (motion crossing toward outside) followed. Pure logic, no extra sensing; consistent with "missed person costs more" and with stillness being unreliable. It must be labelled as inference.
4. **Receiver reset / gain lock.** Reset shifts scores 3-5x; the adaptive rebuild recovers in ~30 s; fixed thresholds go silently stale. Redo: (a) nodes send `boot_id`, gain values and a gain-locked flag; (b) host marks only the rebooted node's links WARMUP, not the room; (c) persist the locked gain in NVS and reapply at boot instead of re-deriving it from the first 100 packets, so scale does not change at all. (c) is unverified; ask the RF/chip agents (temperature, battery sag).
5. **Subcarrier averaging.** `window.var(axis=0).mean()` (`detect.py:169`) weights all subcarriers equally, including faded ones. CARM-style work uses PCA/sensitivity-weighted subcarriers. A per-link self-supervised weight (inverse quiet variance or top principal components) is cheap, needs no site training, and improves with subcarrier count. Do it on-node.
6. **Heatmap spatial model.** Gaussian-around-segment with `sqrt(top1*top2)` (`heatmap.py:206-219`) uses at most two links per cell, so extra links barely add, and every elevated link paints its whole line (ghost lines in a mesh). Replace with Wilson-Patwari VRTI: elliptical excess-path footprint per link, Tikhonov-regularised inversion as a **single precomputed matrix** times the evidence vector (cheap at 240 links), accuracy improves with link count, geometry-only (ADR 0020 compliant). ADR 0011's ratio-to-own-baseline evidence is the right input.
7. **Host O(N^2)-ish hazards.** `_Reorder` (`server.py:172-216`) waits on the slowest connected receiver (`stall_s`), so one slow node delays all links. On-node scoring with timestamps lets the host merge by time bucket.
8. **Time base.** Windows are per link so unsynchronised clocks are fine, but cross-link fusion uses host arrival time (`pkt.host_ts`), which adds jitter over wireless forwarding. Nodes should carry timestamps with an estimated host offset so fusion is on event time.

**Literature (multi-link, no per-site training):**
- Wilson & Patwari, "See-Through Walls: Motion Tracking Using Variance-Based Radio Tomography Networks" (VRTI, IEEE TMC 2011): https://arxiv.org/abs/0909.5417 . Motion = per-link RSS variance in a window, imaged by regularised inversion, 34 nodes through walls, no empty-room training. Essentially the current design plus the inversion.
- Wilson & Patwari, "Radio Tomographic Imaging with Wireless Networks", IEEE TMC 2010 (attenuation RTI; needs empty-room calibration, hence VRTI for rooms that are never empty). From memory, URL not verified.
- Wang et al., "Understanding and Modeling of WiFi Signal Based Human Activity Recognition" (CARM), MobiCom 2015: PCA over subcarriers, CSI-speed model. From memory, URL not verified.
- Zheng et al., "Zero-Effort Cross-Domain Gesture Recognition with Wi-Fi" (Widar3.0), MobiSys 2019: domain-independent velocity profile from multiple receivers. From memory, URL not verified.
- Found by search: ML-Track (multi-link round-trip CSI + particle filter) https://discovery.ucl.ac.uk/id/eprint/10203112/1/ML_Track%20Paper_accepted.pdf ; lightweight passive tracking https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8781929/ . My search did not surface a definitive result on site-survey-free static presence. Honest state: self-normalised variance motion detection is robust; static presence is not, which matches the user's decisions. Treat cross-room accuracy claims as lab results until reproduced on our recordings.

## 3. Redo stack proposal

**On node: ESP-IDF (C), one image for all roles, plus a portable C core.**
- C5 CSI via esp-csi on IDF 5.5.6 is the verified path (`parser.py` C5 header comment). Rust: esp-radio lists esp32c5 but its Wi-Fi/sniffer features are marked unstable (https://docs.espressif.com/projects/rust/esp-radio/1.0.0-beta.2/esp32c5/esp_radio/index.html) and CSI comes from the third-party esp-csi-rs, which documents a C5 ISR wedge when forcing broadcast rate (https://docs.rs/crate/esp-csi-rs/0.10.1). Too risky for a safety product; revisit Rust for host tools. Zephyr: I know of no first-class CSI API for ESP32 radios, so skip.
- Pin IDF in a container (e.g. `espressif/idf` image) to remove the IDF 6.0 `WIFI_BW_HT40` rename break and Windows 260-char path issue (hardware.md:34).
- Move to the node: amplitude extraction, null-subcarrier mask, high-pass + windowed variance per link (MotionScorer), subcarrier weighting, RSSI/noise/gain stats. Emit **feature frames ~10 Hz per link** (tens of bytes) instead of 40 Hz x ~230-byte raw CSI: roughly an order of magnitude less airtime competing with sensing on channel 6 (ADR 0003/0019) and host load independent of packet rate. Keep a runtime-switchable **raw mode** and a ring buffer dumpable on demand for recording and R&D. Drift risk (two scorers): write the scorer once in portable C, compile it natively into a Python extension for host/simulator, golden-vector test against today's numpy output.
- Role (sender/receiver/gateway) is runtime config in NVS; sender address derives from factory MAC. Kills `SENDER_ID`, `CSI_FORWARD`, `CSI_GATEWAY`, fresh-build-dir-per-ID.
- OTA: `esp_https_ota`, A/B partitions with rollback, signed images (secure boot v2), manifest from host, staged rollout with health check before commit. ESP-NOW is not a practical OTA path; use the infrastructure link (network agent decides).
- Self-telemetry (`FWD 1s`, `GW 1s` idea) becomes a first-class frame type.

**Wire protocol (binary, versioned):** magic, proto_version, frame_type, node_id (6-byte MAC), `boot_id` (random u32 per boot), `seq` (u32 per node/stream), 64-bit monotonic node time (us), link key, payload, CRC32. Types: LINK_FEATURE, RAW_CSI (debug), NODE_STATUS (gain locked, channel, fw version, heap, tx/rx error counters), EVENT (reset reason, brownout), CONFIG_ACK. One schema (nanopb/protobuf or generated codec) for C and Python; no CSV/JSON. Transport-agnostic (UDP, COBS over serial, MQTT). `seq` gives exact loss per link (replaces `estimate_rate_hz`, `detect.py:92`); `boot_id` replaces `DeviceClock` inference.

**Host: Python + numpy for algorithms; asyncio Python for ingest.** After on-node scoring 240 links x 10 Hz is ~2.4k small frames/s, easy for asyncio. Rust/Go ingest only pays if raw CSI stays on the wire at hundreds of links. Split: `ingest` (decode, de-dup by (node, boot_id, seq), time-align, record) -> `core` (pure functions over (t, link, feature): z-score, fusion, VRTI, zone latch; no I/O, identical live and replay) -> `api` (FastAPI/WebSocket, loopback or authenticated commands). Preserve the existing property that detectors take `(time, score)` (`replay_series`, `adaptive.py:225`).

**UI:** host-served web dashboard, offline, no cloud. For firefighters, a PWA on rugged tablet/phone over the local network: glove-sized targets, high contrast, map-first, loud binary alerts, "possibly occupied" as a first-class state (never a bare EMPTY). Native mobile only if background alerts/haptics prove needed. Keep the explicit UNCALIBRATED/NO_SIGNAL/WARMUP states and the "map shows change, not presence" caption.

**Recording / replay / evaluation:** MCAP or Parquet (raw + features + node status + decisions) with JSON sidecar (rig geometry, firmware hashes, timestamped operator labels, environment notes). Commit a small real-rig corpus (Git LFS): receiver reboot, gain step, packet loss, person standing still, walking, crawling, empty-but-noisy room. Evaluate at event level (ADR 0001): per-event miss rate, time-to-detect, false alarms per empty hour, per-zone confusion with CIs; hold out a *site*, not just time. Extend ADR 0018's evaluate-by-recording.

**Simulation:** extend `simulate.py` to N-link layouts with per-node gain steps on reboot (3-5x), per-node common-mode noise (correlated links), fading, loss bursts, stationary person (should vanish), multiple walkers. Use it for property tests of false-alarm rate and detection versus N, the check for "more links help" before hardware exists. It cannot validate absolute thresholds.

**CI / HIL:** (1) host: full ruff rules, pytest, type check, recording-based regression gate (miss rate may not rise); today there is only local `check.sh`. (2) firmware: build in the pinned container, native unit tests of the C core against golden vectors. (3) HIL: a Pi/laptop with 3-4 boards on USB hubs: flash via OTA path, boot, protocol conformance, seq continuity, reboot -> WARMUP behaviour, OTA rollback. RF-in-loop detection stays recorded/manual (CSI cannot be injected).

**Fleet config:** identity = factory MAC. Host registry `{mac: name, role, position, group}`; unknown MACs show as "pending" (fixes silent drop, ADR 0019); roles/positions pushed at runtime and ACKed.

## 4. Developer-experience pain to design out

| Hackathon pain (source) | Design-out |
|---|---|
| Build dir per chip and role; shared `sdkconfig` carries last target; `dependencies.lock` dirties git (firmware/README.md) | One image per chip target built in CI container; roles at runtime |
| `SENDER_ID` build flag; changing it does not rebuild | Runtime config keyed by MAC |
| IDF 5.5.6 vs 6.0 rename breaks `csi_send`; Windows long paths (hardware.md:34) | Pinned container toolchain |
| Receiver re-baselines on reset; esptool, replug, even opening a native-USB port resets (hardware.md:9) so thresholds go stale | NVS-persisted gain + self-normalising detector + `boot_id`; reset only degrades that node's links briefly |
| Port juggling; names change on replug; `--auto-ports`, `ports.py`; cannot mix wired and wireless (ADR 0004/0006/0019) | One transport to host; USB only for first flash/debug |
| Boards identified by USB serial; unknown serial dropped silently; board swap = config edit | MAC registry + pending-node enrolment |
| Calibration ritual (Calibrate button, `calibrate.py`, two methods ADR 0010) | Continuous baselines; at most an optional "confirm empty" |
| Receiver within 1 m of gateway floods (hardware.md:24) | Surface ESP-NOW send errors in NODE_STATUS and the UI |
| Pi without GitHub, deploy by git bundle (memory) | Artefact registry + OTA; host as a container image |
| Flash ritual with pre-checks; refusal while dashboard runs (`flash.sh`) | Flash once by USB, then OTA; never touches a running rig |
| Doc drift, dead deps (scipy, scikit-learn), stale `port_hint` | Keep and extend `test_docs.py`; dependency check in CI |
| No real-data fixtures | Commit recordings; share golden vectors between C core and Python |

## 5. Top risks

1. Two-implementation drift (node C scorer vs numpy research code): single C core plus golden vectors.
2. On-node processing discards raw data needed for later ideas: switchable raw mode, always record raw during evaluation campaigns.
3. Simulator-only confidence: first task is to capture and commit a labelled real corpus before new detection code.
4. Stillness cannot be fixed by amplitude-only features; the occupancy latch is mitigation, not detection, and must not be oversold.
5. N-link false alarms from correlated node-level faults if fusion treats links as independent.
6. C5 Wi-Fi/CSI stack maturity (IDF quirks, ESP-NOW NO_MEM): pin versions, HIL-test.
7. NVS gain persistence unverified across temperature and battery sag.
