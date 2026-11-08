# Wireless CSI forwarding: proposal and findings

Status: **design proposal, not implemented**. Written from reading the vendored firmware
(`firmware/csi_recv/main/app_main.c`, and `firmware/csi_transceiver/main/app_main.c` on
`feat/all-transceiver`), the ESP-IDF v6.0 source and docs at `C:\esp\v6.0\esp-idf`
(`components/esp_wifi/include/esp_wifi.h`, `docs/en/api-reference/network/esp_now.rst`,
`docs/en/api-guides/wifi-driver/wifi-mac-protocols.rst`, `examples/wifi/espnow/README.md`),
and the Python receive path (`csi/parser.py`, `csi/multisource.py`, `scripts/dashboard.py`).
No hardware access from this session: the radio-coexistence claims below are grounded in
ESP-IDF's own API contracts and documented example behavior, not a measurement on this rig.
Treat anything marked "unverified" as "worth a bench test before committing," not "proven."

This addresses the README roadmap item "wireless receivers: receivers forward CSI over UDP
instead of USB (nothing started)," in the context of the newer `csi_transceiver` firmware
where every node both sends its own CSI probe and promiscuously captures everyone else's, on
one fixed channel, using raw ESP-NOW — no AP, no IP stack, today.

## The crux: can one ESP32 radio do AP-STA + promiscuous CSI + ESP-NOW at once?

**Short answer: only if the AP is on the same channel as the sensing traffic.** The ESP32 has
one radio and one channel at a time, full stop — every mode (STA association, promiscuous
capture, ESP-NOW, SoftAP) rides the same physical channel, there is no per-mode channel. This
is not an inference; it is the documented contract of the two APIs in question:

- `esp_wifi.h`'s `esp_wifi_set_channel()` sets *the* device channel — there is no
  promiscuous-specific or ESP-NOW-specific channel knob. Promiscuous mode and
  `esp_wifi_set_csi_rx_cb` simply report whatever is on the channel the radio is currently
  tuned to.
- `esp_now.rst`, "Add Paired Device": *"If the channel is set to 0, data will be sent on the
  current channel. Otherwise, the channel must be set as the channel that the local device is
  on."* ESP-NOW has no independent channel of its own; it is layered on top of whatever
  channel the device (STA or SoftAP interface) currently occupies.
- `examples/wifi/espnow/README.md`'s troubleshooting section explicitly covers the scenario
  *"if the receiving device is in station mode only and it connects to an AP"* — confirming
  ESP-NOW is designed to run **alongside** a normal AP-connected STA. It does not say ESP-NOW
  requires being disconnected from an AP.
- `wifi-mac-protocols.rst`, on STA+AP coexist bandwidth negotiation: *"the station always has
  higher priority than AP in {IDF_TARGET_NAME}"* — when multiple interfaces fight over the
  channel, STA (association to an upstream AP) wins. The corollary for a pure-STA node: if it
  associates to an AP on, say, channel 1, the whole radio — including promiscuous capture and
  any concurrent ESP-NOW broadcast — moves to channel 1 too. Channel-6 CSI sensing traffic
  from the other transceiver nodes becomes invisible the moment that happens.

So: **joining an arbitrary AP breaks capture of the fixed sensing channel**, exactly as the
task description suspected. But the fix is not "don't use infrastructure Wi-Fi" — it's
**"make sure the infrastructure is on the sensing channel."** If the AP itself is pinned to
channel 6 (2.4 GHz, HT20/HT40 as needed), then STA-association-for-UDP, promiscuous CSI
capture, and ESP-NOW broadcast are all, simultaneously, "the current channel" — there is no
conflict, by the same logic that lets ESP-NOW coexist with an AP-connected STA today. This is
the one load-bearing fact this document rests on, and it resolves option (a) from the task
affirmatively, **conditional on controlling the AP's channel**, which an off-the-shelf router
or an ESP32 SoftAP both allow (`esp_wifi_set_channel`, or a router's manual-channel setting).

What is **not** confirmed: nobody in the ESP-IDF tree ships an example that combines
STA-association-for-IP, promiscuous CSI RX, and ESP-NOW broadcast on one node simultaneously
under sustained bidirectional WiFi traffic. Each pairwise combination is documented as
supported; the three-way combination, with meaningful UDP throughput competing for the same
airtime as 40 Hz CSI probes, is **unverified** and should be bench-tested on a spare board
before being relied on.

## Options evaluated

### (a) True UDP over a channel-6-pinned AP — the "wireless receivers" roadmap item, literally

Every transceiver node also runs as a WiFi STA, associated to an AP that is fixed to channel
6, and sends its own CSI_DATA (or a binary equivalent, see below) to the laptop over UDP. The
laptop joins the same channel-6 network (most laptops have a WiFi NIC; this does not need
internet routing, just L2/L3 reachability to the AP).

- **Needs new infrastructure**: an AP pinned to channel 6, 20 MHz or matching the sensing
  bandwidth. A cheap travel router with a manual-channel setting works; alternatively, one
  spare ESP32 running SoftAP mode on channel 6 (no CSI duties of its own) can be the AP — it
  does not need to be one of the sensing transceivers.
- **Zero USB nodes** once set up — the actual roadmap goal.
- **No frame-size constraint**: UDP payload (up to ~65 KB, practically limited by path MTU,
  typically ~1472 bytes before IP fragmentation, which the OS/IP stack handles transparently)
  comfortably fits one full ASCII CSI_DATA line (1500-2000 bytes) in one or two IP fragments,
  with zero firmware-side repacking needed — this is the big advantage over ESP-NOW (below).
- **Unverified three-way radio coexistence** (STA-for-UDP + promiscuous CSI + ESP-NOW, all
  busy at once, same channel) — plausible per the docs above, but not bench-confirmed here.
- **Still shares airtime with sensing traffic.** Putting UDP control/data frames on channel 6
  does not make them free — they contend with the same 40 Hz CSI probes the multichannel-TDMA
  design doc (if present on this branch) is already worried about saturating. A busy UDP
  uplink could itself become a new source of collisions for the sensing traffic it's supposed
  to carry off of USB.
- **New failure surface**: DHCP/static-IP assignment per node, discovery (how does the laptop
  learn each node's IP:port), and no security on an open sensing network (see "not solved").

### (b) ESP-NOW relay to one USB-connected gateway node

Every transceiver keeps today's behavior (promiscuous capture + ESP-NOW broadcast on channel
6, no IP stack, no AP) and additionally re-encodes its own captured CSI samples as an *extra*
ESP-NOW payload addressed (or broadcast) to one designated gateway node, which is the only one
still on a USB cable and simply forwards what it relays to `scripts/dashboard.py` over serial,
interleaved with its own captures.

- **Zero new infrastructure** — no AP, no new physical device, reuses the exact transport
  (`esp_now_send`/`esp_now_register_recv_cb`) already proven in `csi_transceiver`.
- **Needs a new wire format.** ESP-NOW's frame cap is 250 bytes (v1) or 1470 bytes (v2) per
  the frame-format table in `esp_now.rst`. The current ASCII CSI_DATA line (1500-2000 bytes)
  does **not** fit either, and was never going to — but that ASCII bloat is the firmware's own
  debug-print format, not the underlying data: `csi_transceiver`'s own `wifi_csi_rx_cb` already
  caps the raw CSI buffer at `CSI_MAX_LEN = 512` bytes (`int8_t buf[CSI_MAX_LEN]`). A binary
  frame carrying that raw buffer plus ~16-24 bytes of metadata (rssi, channel, rx_id/mac,
  timestamp, len) is roughly 530-540 bytes worst case — **one ESP-NOW v2 frame (1470 B cap)**,
  or 3 ESP-NOW v1 frames (250 B cap) with a trivial sequence/reassembly header. This is a much
  better fit than the naive "just forward the printed ASCII line" framing would suggest.
- **Still a real congestion risk at scale.** See the rate math below — relay traffic is
  additive with sensing traffic on the one shared channel, and ESP-NOW's effective throughput
  is capped by its default ~1 Mbps bit rate and per-frame overhead. Fine at the current 4-node
  rig; will need the multichannel/TDMA work (or a scheduled relay slot, not a free-running one)
  to stay sane as N grows.
- **Keeps exactly one USB cable** — doesn't fully deliver "wireless receivers," but removes
  the N-minus-one others, which is most of the actual scaling pain today (one USB port per
  node → one USB port, period).
- **Single point of failure**: the gateway node's death takes the whole dashboard with it
  (true today too, but now for everyone, not just itself).

### (c) Time-shared channel hopping (dwell on an AP's channel, then back to channel 6)

Considered per the task's prompt and **not recommended**. The turn-taking scheme in
`csi_transceiver` is already timing-sensitive (`SLOT_US`, `schedule_my_turn`, the documented
ESP-IDF timer races in its own comments) at a fixed single channel. Layering a periodic
channel-hop dwell on top adds: (1) guaranteed dropped CSI samples during every dwell window,
(2) a second clock/scheduling domain to keep synchronized against the existing turn-taking
timer, (3) worse latency than (a) or (b) for no benefit (a) doesn't already give you more
simply (a) **is** a form of "put the AP where the sensing channel already is" that avoids
hopping entirely). This option only makes sense if the AP's channel is unmovable and channel 6
specifically is unavailable to it — not the case for either a manual-channel router or a
SotfAP-mode ESP32 gateway.

## Payload/rate budget

Per the task's figures: one CSI_DATA ASCII line is 1500-2000 bytes at up to 40 Hz per captured
link-direction. On the current 4-transceiver rig, each node captures from the other 3
(N-1 = 3 link-directions into that node), so each node needs to move roughly:

```
3 link-directions x 40 Hz x ~1750 bytes/line (ASCII)  ~= 210 KB/s  ~= 1.7 Mbps  (ASCII, per node)
3 link-directions x 40 Hz x ~540 bytes (binary, see above)  ~= 65 KB/s  ~= 0.52 Mbps  (binary, per node)
```

At N nodes generally, total raw CSI volume across the whole network scales as
`N * (N-1) * 40 Hz * line-size` — quadratic in N. Concretely:

| N | link-directions (N·(N-1)) | ASCII total | binary total |
|---|---|---|---|
| 4 (today) | 12 | ~2.5 MB/s (~20 Mbps) | ~0.78 MB/s (~6.2 Mbps) |
| 8 | 56 | ~3.9 MB/s (~31 Mbps) | ~1.2 MB/s (~9.7 Mbps) |

This total is spread across all the nodes' own USB/relay/UDP links, not concentrated on one
wire, so the per-node numbers above are the ones that matter for "does one relay hop fit." For
**ESP-NOW relay (option b)**, each binary-packed sample is ~540 bytes: 1 ESP-NOW v2 frame, or 3
v1 frames with reassembly. At 3 incoming links x 40 Hz = 120 samples/s to relay per node, that
is 120 extra ESP-NOW v2 frames/s (or 360 v1 frames/s) layered onto the existing turn-taking
traffic (today: `NUM_SENDERS x 40 Hz` = 160 ESP-NOW sends/s total across a 4-node rig) — **the
relay traffic alone roughly matches or exceeds the current sensing traffic's frame count**,
which is the real risk with this option as N grows past the current rig size: it is not a
"free" use of spare airtime, it competes directly with the thing it's there to support.

For **UDP over a channel-6 AP (option a)**, the ASCII line fits in one UDP datagram with no
repacking, and WiFi/802.11n link rates (tens of Mbps) comfortably clear even the N=8 ASCII
total above — so payload size stops being the constraint for UDP once the radio-coexistence
question is resolved; the constraint becomes shared airtime contention with the CSI probes
themselves, same as for ESP-NOW relay, just with far more headroom before it bites.

**Binary-packing the data is worth doing regardless of which transport wins** — it cuts
volume roughly 3-4x versus the ASCII debug format, which only exists because the firmware's
serial-print path reused `ets_printf` for human-readable debugging, not because the data is
inherently that large.

## Recommendation

**Short term (this hackathon): option (b), ESP-NOW relay to one USB gateway, with a new
binary wire format (not the ASCII CSI_DATA line).** It needs no new infrastructure, reuses a
transport already proven in `csi_transceiver`, and is a bounded, verifiable change — the
binary-packing math above shows it comfortably fits ESP-NOW's frame limits at the current
4-node scale. It does not fully satisfy the roadmap item (one USB cable remains), and it will
need pairing with scheduled (not free-running) relay sends — likely folded into the same
turn-taking/TDMA scheme already being designed for the sensing traffic itself — before it's
safe to scale past roughly the current node count.

**Medium term / the actual roadmap answer: option (a), true UDP over an AP pinned to channel
6.** This is what "wireless receivers forward CSI over UDP" in the README means, and the
coexistence research above says it is technically sound *specifically because* the constraint
turned out to be "same channel," not "no AP at all" — a fact not obvious from the API surface
without reading the ESP-NOW doc's own troubleshooting note. Recommend prototyping this first
on a single spare board (SoftAP-mode gateway, one transceiver node as a UDP-forwarding STA
client) to confirm the three-way radio coexistence claim before committing the whole rig to
it, since no ESP-IDF example demonstrates that exact combination today.

**Not recommended: option (c),** channel hopping — strictly worse than (a) on latency and
complexity, and it does not avoid anything (a) doesn't already avoid by picking the AP's
channel to match the sensing channel.

## Sketch of the actual changes

### Firmware (not implemented here; sketch only)

- **Option (b):** in `firmware/csi_transceiver/main/app_main.c`, add a binary struct mirroring
  `csi_print_item_t` (already exists for the local print path) and a second ESP-NOW send path
  from `csi_print_task` (or a new low-priority task) that serializes it and calls
  `esp_now_send()` to the gateway's MAC instead of/alongside `ets_printf`. The gateway variant
  of the same firmware adds an `esp_now_register_recv_cb` handler that deserializes relayed
  samples and prints them as CSI_DATA lines (or a new binary line type) over its own UART,
  tagged with the originating node's id so the Python side can attribute them correctly.
- **Option (a):** a new firmware build mode (or Kconfig flag) that additionally calls
  `esp_wifi_set_mode(WIFI_MODE_STA)` + `esp_wifi_connect()` to the channel-6 AP, brings up
  `esp_netif`/lwIP, and opens a UDP socket sending the same binary-packed samples to the
  laptop's known IP:port — while `wifi_csi_init()` and the ESP-NOW broadcast path are left
  untouched, since they are channel-agnostic once the AP's channel matches.

### Python side (`csi/multisource.py`, `csi/parser.py`, `scripts/dashboard.py`)

This is a genuinely small diff, because the seam is already exactly right for it. `Stream` is
defined in `csi/parser.py` as `Iterable[tuple[float, str]]` (or `(host_ts, CSIPacket)` for
replay), and `MultiSource` (`csi/multisource.py`) only ever consumes that interface — it has no
knowledge of serial ports specifically. `serial_stream()` is one generator satisfying `Stream`;
a `udp_stream(bind_addr, port)` generator (or `espnow_relay_stream()` reading the gateway's new
binary line type from the existing serial connection) satisfying the same interface would slot
into `MultiSource` with no changes to `MultiSource` itself. The actual diff:

- `csi/parser.py`: add `udp_stream()` next to `serial_stream()` — binds a UDP socket, yields
  `(time.time(), line_or_bytes)` per datagram, `None` on a receive timeout (same heartbeat
  convention `serial_stream` already uses so `MultiSource`'s stop-check keeps working).
- `csi/parser.py` / a new module: a `CSIParser` variant (or a branch inside the existing one)
  that accepts the binary frame format instead of/alongside the ASCII `CSI_DATA` line, if
  option (b) or (a)'s binary packing is adopted — the amplitude/mac/rssi/channel extraction
  logic is unchanged, only the line-splitting front end differs.
- `scripts/dashboard.py`: a new `--udp` (or `--network`) argument alongside `--port`, parsed the
  same way `parse_port_specs` parses `--port` (`[NAME=]bind:port`), merged into the same
  `sources` dict passed to `MultiSource` at line ~175 today (`sources = {name: serial_stream(...)
  for name, path in parse_port_specs(args.port)}` becomes a union of the serial dict and a
  UDP-source dict). No changes needed in `MultiSource`, `csi/links.py`, or the dashboard's
  rendering path — they already operate on `(name, CSIPacket)` regardless of where it came from.

### Explicitly not solved by this document

- **Security/auth of the wireless link.** Both UDP-over-AP and ESP-NOW-relay are unauthenticated
  and (for UDP) likely on an open or shared-PSK network; anyone on the same channel/network can
  inject or sniff CSI data. ESP-NOW does support CCMP encryption via PMK/LMK (`esp_now_set_pmk`,
  already called in `csi_transceiver`, though currently with a hardcoded demo key) — worth
  tightening before any non-demo deployment, but out of scope here.
- **Congestion as N grows**, beyond the order-of-magnitude estimates above — this needs an
  actual bench test, ideally alongside whatever comes out of the multichannel/TDMA design work,
  since both documents are fighting for the same scarce channel-6 airtime budget.
- **IP assignment/discovery** for option (a): how each node gets an address and how the laptop
  learns which node is which (static IP table, mDNS, or a handshake over the existing ESP-NOW
  channel to announce "I am node 3 at 192.168.x.y:port") is unspecified here.
- **The three-way radio coexistence claim for option (a)** is argued from documentation, not
  measured. It should be the very first thing validated on hardware before further design work
  depends on it.

## Next implementation step

Bench-validate the crux claim for option (a) on one spare board: bring up SoftAP mode on a
second board pinned to channel 6, have one `csi_transceiver` node associate to it as STA, open
a UDP socket, and confirm with the existing diagnostic counters (`s_heard_count`,
`wifi_csi_rx_cb` firing rate) that promiscuous CSI capture and ESP-NOW broadcast keep working
at their normal rate while a UDP socket is simultaneously open and sending. If that holds, (a)
is the end state to build toward. If it does not hold even on the same channel (e.g. the
STA-association process itself destabilizes promiscuous mode, which the docs don't rule out),
fall back to (b) as the de facto answer and revisit the roadmap item's wording.
