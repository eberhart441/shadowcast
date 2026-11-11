# Glossary

Team 12 senses motion in a room from WiFi channel state information (CSI) measured between fixed ESP32 boards. This is the project's vocabulary: use these words, in these meanings, in code, issues and docs. Where a term has a module, _See_ points to it; the decisions behind the terms are in [`docs/adr/`](docs/adr/README.md).

## The rig

**Rig**:
The physical installation: the boards, their positions and the room they sit in, as described by `config/room.json`. Today two receivers and two senders in a 2.77 m × 1.84 m room, so four links.
_See_: `config/room.json`, [ADR-0014](docs/adr/0014-room-model-as-config.md)

**Room**:
The rectangle the rig covers, in metres, with the origin at the top-left corner, x to the right and y down.
_See_: `csi/room.py` (`Room`, `load_room`)

**Node**:
A board at a fixed position in the room, with a role (receiver, sender or transceiver) and a name such as `rx1`, `tx0` or `n3`. A node with no role is a transceiver. A node with `"enabled": false` is ignored everywhere: it has no links and its port is never opened.
_Avoid_: device, station, puck
_See_: `csi/room.py` (`Node`)

**Receiver**:
A node that measures CSI on every sender's packets and prints one CSI_DATA line per packet over USB serial. Always an ESP32-C5.
_Avoid_: RX board, sniffer, gateway
_See_: `firmware/csi_recv/`, [ADR-0002](docs/adr/0002-esp32-c5-receivers-only.md)

**Sender**:
A node that broadcasts ESP-NOW packets at the send rate from its sender address. An ESP32-C5 or ESP32-S3.
_Avoid_: transmitter, beacon (reserved for the proposed TDMA sync beacon, issue #16)
_See_: `firmware/csi_send/`

**Link**:
One (receiver, sender) pair, labelled `<receiver><-<last byte of the sender address>`, for example `rx1<-00`. Every enabled receiver pairs with every enabled sender. Between transceivers a link is one ordered pair (receiving node, transmitting node) by full node identity, labelled `<receiver><-<transmitter>` with both node names, for example `n1<-n5`; N transceivers give N(N-1) links. The link is the unit that is scored, thresholded, calibrated and drawn.
_Avoid_: channel, pair, path
_See_: `csi/links.py` (`LinkId`, `NodeLinkId`), `csi/room.py` (`Room.links`, `Room.transceiver_links`)

**Transceiver**:
A node that both transmits and receives: in its slot it sends a round broadcast, and it measures CSI on every other node's round broadcast. Every node of the rig in spec #2 is one, running the same firmware; it is known by its board serial.
_Avoid_: sender, receiver (for these nodes), station
_See_: `csi/room.py` (`Room.transceivers`), [`docs/round-broadcast.md`](docs/round-broadcast.md)

**Round**:
One turn of the transceiver schedule: each member node broadcasts once, in its own slot, and a join slot ends it. The round rate is the rate at which every link is measured (20 Hz in the simulator). Rounds are numbered (u16, wrapping).
_See_: `csi/simulate.py` (`simulate_rounds`, `ROUND_HZ`)

**Slot**:
The part of a round in which one node, and only that node, transmits its round broadcast.
_Avoid_: turn, window (reserved for the score window)

**Round broadcast**:
The ESP-NOW broadcast a transceiver sends in its slot: a round header (version, round number, slot, membership, the sender's board serial, an authentication tag) and one link report per other member. The join frames of the join slot share its header. It is both the frame the others measure CSI on and the data for the host. Format version 2 is defined in [`docs/round-broadcast.md`](docs/round-broadcast.md).
_Avoid_: packet (that is a parsed CSI_DATA line), frame (that is the dashboard's event)
_See_: `csi/broadcast.py`, [ADR-0022](docs/adr/0022-host-time-round-broadcast-seam.md)

**Authentication tag**:
The 8 bytes at offset 18 of a round broadcast: the first 8 bytes of HMAC-SHA256 under the kit key, over the whole round broadcast with the tag zeroed. A round broadcast whose tag does not verify is rejected as `auth` and never used, on the host or on a node.
_Avoid_: signature (it is a MAC, not a public-key signature), checksum, CRC
_See_: `csi/round_auth.py`, `firmware/core/round_auth.h`, [ADR-0028](docs/adr/0028-hmac-sha256-tag-and-round-number-freshness.md)

**Kit key**:
The 16-byte secret shared by every node of a kit and the dashboard host, that authentication tags are made with. Made once by `scripts/node.py keygen` (into `data/kit.key`), given to each node with `scripts/node.py PORT key`, and named by its 8-hex-digit check value, never shown itself. The published test kit key (`00 01 .. 0f`) is for the fixtures and the simulator only.
_Avoid_: password, secret (alone)
_See_: `csi/round_auth.py` (`load_kit_key`, `kit_key_check`), `firmware/node/main/node_auth.h`

**Replay**:
A round broadcast with a valid tag whose round number is stale for its sender: not 1 to 16 + 1 per millisecond ahead of the last fresh one. It is rejected as `replay` and not used. A sender silent for more than 2 s is taken at any round number again.
_Avoid_: duplicate (that is a second gateway's copy, within 2 s)
_See_: `csi/round_auth.py` (`Freshness`), `firmware/core/round_auth.h` (`rb_auth_fresh`)

**Link report**:
The 4 bytes a receiving node sends about one of its links in its round broadcast: the quantised score, the RSSI, how many round broadcasts it heard from the transmitter in the last 15 rounds and how many rounds since the last one, flags (baseline warming, possible move, raw mode) and the transmitter's slot lateness. A heard count of 0 makes the link NO_SIGNAL.
_See_: `csi/broadcast.py` (`LinkReport`), `csi/links.py` (`push_report`)

**Fixed round**:
The round of the first transceiver image (ticket #13): a set number of members, node index = slot, a set round rate, no join slot, and slot timing taken from any heard round broadcast. Replaced by the round scheduler (#14); the code is gone.

**Round scheduler**:
The node's state machine that decides who is in the round and when this node sends: heard round broadcasts and the device clock in; this node's node index, slot, the membership and the schedule version out. Nodes join, drop out and merge on their own; there is no timing master and no per-board setting.
_See_: `firmware/core/round_scheduler.h`, [ADR-0027](docs/adr/0027-round-scheduler.md)

**Schedule**:
One membership of the round with its schedule version. The member in slot 0 is its **scheduler**: only it admits and drops members, each time bumping the schedule version. Its board serial is the **scheduler ID**; when two schedules meet, the one with the lower scheduler ID wins and the other's nodes join it.
_Avoid_: master, coordinator (no node is a timing master)

**Join slot**:
The last slot of every round, kept free for joining: a node that is not a member sends a **join request** there in even rounds, and the scheduler answers with a **join grant** (naming the joiner's serial and its node index) there in odd rounds. Both are join frames, the round header alone (frame types 2 and 3).
_See_: [`docs/round-broadcast.md`](docs/round-broadcast.md) (join frames)

**Slot guard**:
The margin a slot keeps beyond the round broadcast's airtime, a kit-wide setting (`guard_us`, 250 µs until the slot-jitter bench test sets it). The scheduler admits no more members than slots of airtime plus guard fit in a round, and a node whose slot timer fires more than the guard late skips that slot.
_Avoid_: gap, margin (alone)

**Slot lateness**:
How far into its slot a node's round broadcast starts on air, as another node's schedule places that slot. Each receiving node sends it per link in the link report (32 µs steps); the dashboard shows each node's median over the receiving nodes.
_See_: `csi/coverage.py` (`SlotLateness`), `node_health[].slot_lateness`

**Gateway**:
A node of the transceiver rig on the dashboard host's USB that only listens: it sends nothing on the channel except the maintenance commands the host hands it, and writes every round broadcast it hears to USB as a heard record with its receive time. The same image as a transceiver, with the role setting `gateway`. (The hackathon's wireless receivers had a different gateway board, `CSI_GATEWAY=1`, that wrote text lines.)
_See_: `firmware/node/`, `csi/gateway_records.py` (`GatewaySource`), `dashboard.py --node-gateway`

**Gateway record**:
One framed binary record on a node's USB port: heard (receive time, RSSI, a round broadcast), status (counters since boot) or text (a log line or a command reply). Format version 1 is defined in [`docs/gateway-record.md`](docs/gateway-record.md).
_See_: `csi/gateway_records.py`, `firmware/core/gateway_record.h`, [ADR-0026](docs/adr/0026-one-node-image-runtime-settings-gateway-records.md)

**Node settings**:
What differs between boards running the one node image: role (transceiver or gateway), channel, round rate and slot guard. Kept in the node's flash and set over USB with `scripts/node.py`, never by a build flag. A transceiver needs none of them (the defaults join any round on channel 6); the node index is not a setting but given out by the round scheduler.
_Avoid_: build flags, SENDER_ID (for these nodes)
_See_: `firmware/core/node_settings.h`, `scripts/node.py`

**Maintenance command**:
The signed frame (frame type 8) that tells up to 13 named nodes to update their firmware to one image (named by its size and SHA-256) or to report what they run. The host builds it, the gateway sends it on the sensing channel; a node runs each one at most once.
_Avoid_: OTA packet, update frame
_See_: [`docs/round-broadcast.md`](docs/round-broadcast.md) (maintenance command), `csi/ota.py`, `firmware/core/ota_core.h`

**Maintenance mode**:
What a node runs instead of the transceiver for one job after a maintenance command: off the round and off the sensing channel, it joins the maintenance network, fetches and checks an image (or only reports), then restarts. At most 240 s.
_See_: `firmware/node/main/node_ota.c`, [`docs/ota.md`](docs/ota.md)

**Maintenance network**:
The WPA2 access point the dashboard host provides during a firmware update, on 5 GHz or a 2.4 GHz channel clear of the sensing channel. Its SSID and passphrase are derived from the kit key. Nodes fetch the image from the host over it and report there.
_Avoid_: hotspot (that is one way to provide it), backhaul

**Signing key**:
The RSA-3072 key pair whose private half signs node images (`espsecure sign-data`) and stays on the user's machine; a node takes a new image only if it is signed by the same key as the image it runs. Not the kit key, which every node holds.
_Avoid_: kit key (a different, symmetric key), secure boot key (hardware secure boot is not enabled)
_See_: [ADR-0030](docs/adr/0030-ota-maintenance-network-signed-images.md), `csi/ota.py` (`check_image`)

**Health check**:
The test a newly updated image must pass to stay: within 120 s of its first boot, a member of a round that has heard and sent 20 round broadcasts. Failing it, or crashing before it passes, rolls the node back to its old app slot.
_See_: `firmware/core/ota_core.h` (`ota_health_step`)

**Staged rollout**:
Updating the rig a few nodes at a time (2, then 6, then 13 per command), each stage only after every node of the last one reported the new image committed and was heard on the round again; the first failure stops it.
_See_: `csi/ota.py` (`Rollout`), `scripts/ota.py rollout`

**Sender address**:
The MAC a sender transmits from, `1a:00:00:00:00:<SENDER_ID>`. A receiver accepts every packet whose address starts `1a:00:00:00:00`, so a forgotten board with such an address shows up as a ghost sender.
_Avoid_: MAC (alone), serial
_See_: `mac` in `config/room.json`, `firmware/csi_recv/main/app_main.c`

**SENDER_ID**:
The last byte of a sender address, set at build time (`idf.py -DSENDER_ID=0x01`). It must be unique: two senders with one ID merge into one noisy link.
_See_: `firmware/csi_send/CMakeLists.txt`, `scripts/flash.sh`

**Board serial**:
A board's USB serial number, which equals its base MAC (for example `38:44:BE:A5:18:B0`); the `serial` field of a node. It is what identifies a physical board.
_Avoid_: port, MAC (alone)
_See_: `csi/ports.py`, [ADR-0006](docs/adr/0006-identify-boards-by-usb-serial.md)

**Port**:
The operating-system path of a board's USB serial connection (`/dev/cu.usbmodem101`, `COM3`). It changes on replug and reset, so it is never an identity: `--auto-ports` finds each receiver's port from its board serial.
_See_: `csi/multisource.py` (`parse_port_specs`), `csi/ports.py`, `scripts/ports.py`

## Radio and firmware

**CSI (channel state information)**:
A receiver's per-packet estimate of how the radio channel affected each subcarrier, reported as signed 8-bit I/Q pairs. A person moving between the boards changes it.
_See_: `csi/parser.py`

**CSI_DATA line**:
The text line a receiver prints for each packet at 921600 baud: metadata columns, then the I/Q list. A header line printed at boot names the columns; the parser infers the ESP32-C5 layout if the header was missed.
_See_: `csi/parser.py`, `firmware/README.md` ("Serial output")

**ESP-NOW**:
Espressif's connectionless WiFi frame protocol. Senders broadcast with it, so the rig needs no access point and no association.
_See_: `firmware/csi_send/main/app_main.c`

**Channel**:
The single 2.4 GHz WiFi channel every board uses: channel 6, 40 MHz wide (HT40, secondary channel below), hard-coded in both firmwares. Never a synonym for link or stream.
_See_: `CONFIG_LESS_INTERFERENCE_CHANNEL` in `firmware/*/main/app_main.c`, [ADR-0003](docs/adr/0003-one-shared-channel-at-40-hz.md)

**Send rate**:
How often each sender transmits: 40 Hz (`CONFIG_SEND_FREQUENCY`). The simulator defaults to 100 Hz.
_Avoid_: packet rate (that is what arrives)
_See_: `firmware/csi_send/main/app_main.c`, `csi/common.py` (`SIM_RATE_HZ`)

**Packet rate**:
The packets per second a link actually delivers, counted over the last 2 s of host time. A healthy link on the current rig delivers 36 to 38.
_See_: `csi/health.py` (`LinkHealth.packet_rate`)

**Drops**:
The packets a link is estimated to have lost, inferred from gaps in device time against the send period. **Loss** is the missing fraction. A receiver reboot is not counted as drops.
_See_: `csi/health.py`, `csi/detect.py` (`packet_loss`)

**Gain baseline**:
The receiver's radio gain, recorded over its first 100 packets after boot and then locked (`CONFIG_FORCE_GAIN`), so amplitudes stay comparable for the rest of the run.
_Avoid_: baseline (alone)
_See_: `firmware/csi_recv/main/app_main.c`

**Re-baseline**:
What a receiver reset does: flashing, any `esptool` call, replugging, and sometimes opening its port record a new gain baseline. Every score on that receiver's links shifts, so every saved threshold for them is stale.
_See_: `docs/agents/hardware.md` ("Receivers re-baseline on reset")

## Signal

**Packet**:
One parsed CSI_DATA line: host time, device time, RSSI, channel, sender address and the amplitude vector.
_See_: `csi/parser.py` (`CSIPacket`)

**Subcarrier**:
One of the OFDM frequency bins that CSI is reported on. An ESP32-C5 packet carries 117; the three that are always null (57 to 59) are dropped.
_See_: `csi/parser.py` (`KNOWN_NULL_SUBCARRIERS`)

**Amplitude**:
The magnitude of one subcarrier's I/Q pair, `hypot(I, Q)`. The only CSI feature the system uses: phase is discarded when the line is parsed.
_See_: `csi/parser.py`, [ADR-0007](docs/adr/0007-amplitude-only-motion-score.md)

**Score**:
A link's motion measure. Per subcarrier, the amplitude has its components below about 1 Hz removed (three cascaded high-pass stages); the score is the variance of what is left over the last 1 s of device time, averaged across subcarriers. Each link has its own scale.
_Avoid_: energy, variance (alone), level
_See_: `csi/detect.py` (`MotionScorer`)

**Window**:
The 1 s of device time a score is computed over. It needs at least 5 packets spanning 0.9 s, or there is no score yet.
_See_: `csi/detect.py` (`WINDOW_S`, `MIN_WINDOW_PACKETS`)

**Score code**:
The score as one byte, as a link report carries it. Code 0 means no score. Code c from 1 to 255 stands for 2^((c − 1)/10 − 10), on a log scale of 10 codes per octave, from about 0.001 to about 43 000.
_See_: `csi/score_code.py`, `firmware/core/motion_score.h`, [ADR-0021](docs/adr/0021-on-node-score-streaming-window-and-score-code.md)

## Link states

A link is in exactly one state. The dashboard uses the first five; STILL_PRESENT exists only in the single-link tools, with `--breathing`.

**EMPTY**:
The link's score is at or below its threshold: no gross movement on this link. It does not mean nobody is there.
_Avoid_: clear, vacant, nobody present
_See_: `csi/detect.py` (`classify`)

**MOTION**:
The link's score is above its threshold. It takes effect on the first window above it.
_See_: `csi/detect.py` (`classify`, `_Debouncer`)

**WARMUP**:
The link has a threshold but no full window yet; in auto mode, its first 10 s. On a transceiver link, also a link report with the warming flag or no score.
_See_: `csi/links.py`

**UNCALIBRATED**:
The link has no threshold, so it is scored but not judged.
_See_: `csi/links.py`

**NO_SIGNAL**:
No packet (or link report) on the link for more than 2 s of host time. On a transceiver link, also a link report whose heard count is 0.
_See_: `csi/links.py` (`NO_SIGNAL_S`)

**STILL_PRESENT**:
Experimental and opt-in (`--breathing`): no motion, but a breathing rate of 6 to 30 breaths per minute found with confidence of at least 0.85 over a 15 s window. It reads 43% of empty windows on hw5 as STILL_PRESENT.
_See_: `csi/breathing.py`, `csi/detect.py`, [ADR-0008](docs/adr/0008-motion-only-by-default.md)

**Room state**:
The room-level decision in the payload's `status.room`. It is not any one link's state. With transceivers: MOTION while any zone is MOTION; a zone is MOTION while, at one of its probe points, the elevated links covering the point hold enough independent links, and for 1 s after. Enough is a third of the independent links that could cover the point, and at least two (three where three exist). For the hackathon links: MOTION when at least two links are elevated together, or one link is strongly elevated (auto mode only), held for 1 s.
_See_: `csi/room_state.py` (`RoomState`), `csi/adaptive.py` (`RoomDecision`), [ADR-0029](docs/adr/0029-adaptive-baseline-only-and-a-room-state-from-zones.md)

**Zone**:
A square of the room, about 1 m on a side, that the room state reports on: `A1` is the top-left zone, letters run along x and numbers down y. Each zone holds a grid of probe points 0.25 m apart; a link covers a probe point when its segment passes within 0.3 m of it.
_See_: `csi/room_state.py` (`ZoneMap`)

**Independent links**:
Links that share no node. One node's fault (a front-end glitch, a gain step) raises all of its links at once, so links that share a node count as one witness, and both directions of a node pair are one link here. Counted as the largest set of links in which no two share a node.
_Avoid_: independent witnesses (in code and docs)
_See_: `csi/room_state.py` (`independent_links`)

## Detection and thresholds

**Threshold**:
The score above which a link reads MOTION. One per link, from a calibration, a Set, or a thresholds file.
_Avoid_: limit, cutoff
_See_: `csi/links.py`, [ADR-0009](docs/adr/0009-fixed-thresholds-by-default.md)

**Thresholds file**:
A JSON object `{link label: threshold}`, conventionally `links.json`, loaded with `--thresholds` and written by Save.
_See_: `csi/links.py` (`save_thresholds`, `load_thresholds`)

**Set / Save**:
Set fixes one link's threshold on the running dashboard (`POST /api/threshold`). Save writes every current threshold to the thresholds file (`POST /api/thresholds/save`).
_See_: `csi/server.py`

**Level**:
A link's score divided by its threshold (in auto mode, by its enter level); 1 or more means at or above it. The payload's `level` and `normalised` fields.
_See_: `csi/links.py`, `csi/dashboard.py`

**Debounce**:
The hold that stops a state flickering. MOTION takes effect at once; leaving MOTION, or switching between EMPTY and STILL_PRESENT, needs the new state to persist for 0.5 s of host time.
_See_: `csi/detect.py` (`_Debouncer`, `DEBOUNCE_S`)

**Fixed mode**:
The default: each link is judged against its threshold.
_See_: `csi/links.py` (`LinkMonitor`)

**Auto mode**:
Experimental (`--auto-threshold`): each link is judged against its own adaptive baseline and needs no calibration. A transceiver link is always judged this way, in either mode, and takes no threshold.
_See_: `csi/adaptive.py`, [ADR-0009](docs/adr/0009-fixed-thresholds-by-default.md)

**Adaptive baseline**:
In auto mode, a link's median score over the last 75 s of packet time, with a robust spread of 1.4826 × the median absolute deviation.
_Avoid_: baseline (alone)
_See_: `csi/adaptive.py` (`AdaptiveDetector`)

**Enter level / exit level**:
In auto mode, a link becomes elevated once its score has stayed at or above the enter level, max(baseline + 5.5 × spread, 2 × baseline), for 0.6 s; it stops being elevated below the lower exit level.
_See_: `csi/adaptive.py`

**Elevated**:
A link in MOTION; in auto mode, a link above its enter level. The payload's `elevated` field.
_Avoid_: raised, triggered
_See_: `csi/links.py`, `csi/adaptive.py`

## Calibration

**Calibration**:
Setting each link's threshold from a stretch of its scores, recorded while the room is as quiet as it gets.
_See_: `csi/detect.py` (`calibrate_clips`), `csi/links.py` (`calibrate_links_report`), [ADR-0010](docs/adr/0010-two-noisy-room-calibration-methods.md)

**Clip**:
One continuous calibration recording. Each clip is scored on its own and the results are pooled. The dashboard records 3 clips of 10 s, 3 s apart, after a 5 s countdown (the lead-in).
_See_: `csi/live_calibration.py`

**Chunk**:
A 1 s slice of a clip, judged clean or contaminated. A chunk is contaminated when its median score is more than 3 × the median of the quieter half of the chunks, or its 90th percentile is more than 2.5 × its own median (someone passed through).
_See_: `csi/detect.py` (`REJECT_RATIO`, `BURST_RATIO`)

**Clean fraction**:
The share of clip time kept as clean chunks. The quiet-room rules need at least half, and at least 8 s, or the link gets no threshold.
_See_: `csi/detect.py` (`MIN_CLEAN_FRACTION`, `MIN_CLEAN_S`)

**Margin**:
The factor on the calibration percentile: threshold = margin × p99 under the quiet-room rules, or margin × p97 in noisy-room mode. Default 1.25. Higher is quieter but may miss weak motion. The noisy-room formula does not use it.
_See_: `csi/detect.py` (`DEFAULT_MARGIN`)

**Quiet-room rules**:
Calibration that drops contaminated chunks, refuses a link with too little clean data, and sets margin × p99 of the kept windows. The terminal tools always use them; the dashboard uses them with "noisy room" unticked.
_See_: `csi/detect.py`

**Noisy-room mode**:
The default of the dashboard's Calibrate button (`noisy: true`): every window counts, the threshold is margin × p97, movement never causes a refusal, and the result warns how many seconds looked like movement.
_See_: `csi/detect.py`, `csi/live_calibration.py`

**Noisy-room formula**:
The method in `scripts/calibrate.py`: poll the live scores about 4 times a second for 45 s, then set max(median + 6 × 1.4826 × lower-half spread, 3 × median) per link.
_See_: `csi/noisy_calibration.py`

**Lower-half spread**:
The median of (median − score) over the scores at or below the median. The noisy-room formula's noise estimate; movement in the upper tail cannot inflate it.
_See_: `csi/noisy_calibration.py`

**Skipped link**:
A link that calibration refused: no packets, under 15 packets/s, over 60% loss, or too little clean data. It keeps its previous threshold, and the reason is reported.
_See_: `csi/links.py`, `csi/live_calibration.py`

## Heatmap

**Heatmap**:
A 28 × 18 grid over the room of motion likelihood from 0 to 1, built by spreading each link's evidence along its sender-receiver line. It shows where change is, not where a person is.
_Avoid_: position, track, location
_See_: `csi/heatmap.py`, [ADR-0011](docs/adr/0011-heatmap-evidence-from-quiet-baseline.md)

**Radio tomography**:
How a room with transceivers builds its heatmap: every link's evidence is imaged over the grid at once with one matrix, computed from the room's geometry when the heatmap is built (an ellipse around each link, a smooth prior over the cells). More links make the map sharper. Rooms of receivers and senders keep the per-link footprint and agreement.
_Avoid_: RTI (in prose), localisation
_See_: `csi/rti.py`, [ADR-0025](docs/adr/0025-radio-tomography-heatmap.md)

**Quiet baseline**:
The heatmap's reference for one link: the median of its score over the last 60 s of host time, used once 10 s of history exist. A person who stays still for about 30 s is absorbed into it.
_Avoid_: baseline (alone)
_See_: `csi/heatmap.py` (`QuietBaseline`)

**Evidence**:
How far a link's score sits above its quiet baseline, mapped to 0..1 on a log scale: a ratio of 1.2 or less gives 0, a ratio of 3 gives 1.
_See_: `csi/heatmap.py` (`link_evidence`)

**Link weight**:
A per-link multiplier on evidence, from `link_weights` in `config/room.json` (default 1.0). `rx1<-01` is at 0.3, set when `rx1` was an ESP32-S3 and not re-measured since.
_See_: `csi/heatmap.py`, `config/room.json`

**Contributing link**:
A link that puts heat on the grid: in EMPTY or MOTION, with a warmed-up quiet baseline and evidence above 0. NO_SIGNAL, UNCALIBRATED and WARMUP links never contribute.
_See_: `csi/heatmap.py` (`CONTRIBUTING_STATES`)

**Falloff**:
The width (one standard deviation, 0.35 m) of the Gaussian band a link's evidence is spread over around its sender-receiver segment.
_See_: `csi/heatmap.py` (`FALLOFF_M`)

**Agreement**:
The share (0.5) of each cell's value taken from the geometric mean of its two strongest links, so cells where links agree are hotter than one link alone.
_See_: `csi/heatmap.py` (`AGREEMENT`)

**Sensitivity, rise, decay**:
The live-tunable heatmap parameters: the soft-saturation gain (1.3) and the time constants for a cell heating up and cooling down (0.3 s each). Changed with `POST /api/params` or the page's sliders.
_See_: `csi/heatmap.py`, `csi/server.py` (`PARAM_RANGES`)

## Sources, time and data

**Stream**:
The input from one receiver: a sequence of `(host_ts, line)` pairs, from a serial port, the simulator or a replay. Every source plugs into the pipeline through it. In the transceiver pipeline a stream is a sequence of `(host time, round broadcast bytes)` pairs, from a gateway, the simulator or a replay.
_Avoid_: feed, channel
_See_: `csi/parser.py` (`Stream`), `csi/rounds.py` (`BroadcastStream`), [ADR-0012](docs/adr/0012-host-ts-line-stream-seam.md), [ADR-0022](docs/adr/0022-host-time-round-broadcast-seam.md)

**Source**:
A named stream (receiver name to stream) given to the dashboard. Sources are read concurrently, each on its own thread with its own parser.
_See_: `csi/multisource.py` (`MultiSource`)

**Heartbeat**:
`(host_ts, None)`, yielded by a serial port that read nothing for 1 s, so a reader can check its stop flag.
_See_: `csi/parser.py` (`serial_stream`)

**Host time**:
The laptop timestamp a line is stamped with once, when it is read (`host_ts`, seconds), and that its packet carries from then on. The clock for states, debounce, NO_SIGNAL, the heatmap and calibration clips.
_Avoid_: timestamp (alone), wall clock
_See_: [ADR-0013](docs/adr/0013-packet-time-not-wall-clock.md)

**Device time**:
The receiver's 32-bit microsecond counter (`local_timestamp`), unwrapped across its 71.6-minute wrap and restarted on a reboot. The clock for score windows, send-rate estimates and drops.
_See_: `csi/clock.py` (`DeviceClock`)

**Wall clock**:
The laptop's clock at the moment of processing. Used to pace the event stream, run the calibration countdown and notice a stalled receiver, and on live hardware to move time on between packets so a silent link turns NO_SIGNAL. Never used to score or classify.
_See_: `csi/server.py` (`_clock`, `_Reorder`)

**Simulator**:
Generates CSI_DATA text for a simulated room: empty, motion and still-with-breathing segments, or a person walking a loop (`--simulate-room`). For a room of transceivers it generates round broadcasts instead, with a person walking, dead nodes and lossy links (`--simulate`). It is seeded, so a run repeats exactly.
_See_: `csi/simulate.py`

**Recording**:
One CSV file of packets that all carry one label (`empty`, `walking`, `still_standing` or `still_sitting_or_lying`), named by the label and its UTC start time.
_See_: `csi/recording.py`, `scripts/record.py`

**Session**:
A folder of recordings plus `session.json` (placement notes, channel, send rate, subcarrier layout). `hw3` and `hw5` are the two real sessions, both single-link, on the branch `data/two-board-sessions`.
_See_: `csi/recording.py` (`load_session`)

**Evaluation**:
Replaying a session's recordings through the live detector and comparing each decision with the label of its file. The first empty recording calibrates; the split is by recording, never by random window.
_See_: `csi/evaluate.py`, [ADR-0018](docs/adr/0018-evaluate-by-recording.md)

**Round recording**:
One binary file of a transceiver run: every round broadcast as its source delivered it, with its host time and source name, labels that each hold from a host time on, and the room config. Written by `dashboard.py --record`, replayed by `--replay-rounds`.
_Avoid_: recording (alone; that is a CSV file of packets)
_See_: `csi/round_recording.py`, [docs/round-recording.md](docs/round-recording.md), [ADR-0023](docs/adr/0023-round-recordings.md)

**Run log**:
A `--log` file: one header line, then one dashboard payload per line.
_See_: `csi/server.py` (`LOG_FORMAT`)

**Replay**:
Serving a run log (`--replay`), or feeding a session (`--replay-session`) or a round recording (`--replay-rounds`) through the live pipeline, at 1×, N× or maximum speed. A replay is read-only: commands return 409.
_See_: `csi/replay.py`

## Dashboard

**Dashboard**:
The long-running server and its page. The server reads every receiver, keeps the per-link monitor and the heatmap, and serves them over HTTP.
_See_: `csi/server.py`, `scripts/dashboard.py`, `web/index.html`

**Payload**:
The single JSON state the page renders from: room, nodes, links, heatmap and status. `GET /api/state` returns one; each event of `GET /api/events` carries one.
_See_: `csi/dashboard.py`, `csi/server.py`

**Frame**:
One payload in the event stream, produced every 0.2 s of packet time. A browser receives at most about 5 per second. In a room with more than 64 links a frame carries no per-link rows: the page gets one link's detail on demand from `GET /api/link`.
_See_: `csi/server.py` (`FRAME_INTERVAL_S`, `MIN_EVENT_GAP_S`, `FRAME_LINKS_MAX`), [ADR-0024](docs/adr/0024-link-detail-on-demand-frames-off-the-lock.md)

**Node health**:
Per transceiver, in the payload's `node_health`: by how many others it is heard, how many it hears, when its last round broadcast reached the host, the mean packet rate of its links and its slot lateness (`"unknown"` until the nodes report it). A node is **missing** when none of its round broadcasts reached the host for more than 2 s.
_See_: `csi/coverage.py` (`node_health`)

**Coverage degraded**:
The rig cannot see the whole room: a node is missing, a board not in the room config is broadcasting, or more than 5 % of the links between live nodes are NO_SIGNAL. The page then shows a COVERAGE DEGRADED banner, because a quiet reading from such a rig does not mean nobody is there.
_Avoid_: offline, fault
_See_: `csi/coverage.py` (`coverage`, `SILENT_SHARE`)

**Link matrix**:
Every transceiver link's state as one character, row the receiving node and column the transmitting node (`E`, `M`, `W`, `U`, `N`, and `-` on the diagonal). The page draws it as a grid of cells instead of one card per link.
_See_: `csi/coverage.py` (`link_matrix`)


**Commands**:
The five POST endpoints that calibrate, set or save thresholds, and tune the heatmap. They run only when the server is bound to a loopback address or started with `--allow-control`.
_Avoid_: API (alone)
_See_: `csi/server.py` (`COMMAND_PATHS`), [ADR-0015](docs/adr/0015-stdlib-http-server-loopback-commands.md)

## Words with more than one meaning

- **Baseline** has three meanings: say **gain baseline** (firmware), **quiet baseline** (heatmap) or **adaptive baseline** (auto mode). Calibration's contamination test also compares chunks with "the quiet baseline" of that one run (the median of the quieter half of its chunk medians); it lives only inside one calibration.
- **MAC** can mean a sender address (`1a:00:00:00:00:01`) or a board's base MAC, which is its board serial (`38:44:BE:A5:33:9C`). They are different numbers for the same sender board. Say which.
- **Noisy-room calibration** covers two methods that give different thresholds. Say **noisy-room mode** (the Calibrate button) or **noisy-room formula** (`scripts/calibrate.py`).
- **Empty** is a link state (`EMPTY`: no movement on that link), a recording label (`empty`: the room was cleared) and, in `scripts/calibrate.py`, an "empty reading" (a poll with no score). Only the label says the room had nobody in it.
- **Heatmap** on the dashboard is the room map above. `scripts/live.py` also draws an "amplitude heatmap": subcarriers against time for one port.
- **Threshold** in the payload is the fixed threshold in fixed mode and the enter level in auto mode.
- **Channel** is the radio channel only, never a link or a stream.
