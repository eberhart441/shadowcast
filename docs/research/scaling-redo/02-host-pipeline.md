# 02 - Host pipeline: does the Python side cap the rig at about 4 nodes?

Scope: the host software on the laptop / Raspberry Pi 5 (`Team-12-tracker`, branch `feat/wireless-receivers`, read-only), plus the
serial links it reads. Firmware, RF airtime, history and hardware are covered by the other reports. Code cites are
`Team-12-tracker/<path>:<line>` unless another checkout is named.

**Short answer.** Yes, in part. The whole pipeline runs in **one Python process under one GIL**: per-receiver reader
threads, the pump, and the HTTP/SSE threads. The measured end-to-end cost is about **0.45-0.57 ms of CPU per packet** on
a fast x86 laptop. On a Pi 5 that is roughly 1-1.5 ms. At 40 Hz per link the Pi runs out of its single effective core
at about **16-25 links, which is N = 4-5 nodes**. Before that, two other limits hit:

- **The per-receiver 921600-baud UART (wired receivers).** It fits only about 108 CSI lines/s, so 2.7 senders at 40 Hz.
  In an all-transceiver rig each node hears N-1 senders, so this binds at **N = 4**.
- **The `readline()` serial reader on the branch that tried 5+ boards.** It cost 165% CPU on the Pi with three
  receivers.

The detection math itself has one super-linear term: a room decision that scans every link on every packet, which is
O(links²) per second. It matters past about 8 nodes. In the experimental auto mode an O(window) Python loop per packet
makes things about 3x worse.

---

## 1. Method

**Machine.** Intel Core Ultra 7 366H (16 cores), Windows 11. Python 3.10.11 in a fresh venv
`research/scaling-redo/_venv` with the repo's `requirements.txt` (numpy 2.2.6, pyserial 3.5). The checkout was used
through `PYTHONPATH` only: no install into it, no edits, no `git switch`. No real serial port was opened and no server
was left running (each `DashboardServer` was started on port 0 and stopped).

**Pi 5 estimate.** The Pi 5 (Cortex-A76 at 2.4 GHz) is assumed to be **2-3x slower** single-threaded for this
CPython+numpy workload; the figures below use **x2.5**. This factor is an assumption and was not measured. The one real
Pi number in the history (commit `867eea3`, see §4.1) agrees with it in order of magnitude.

**Synthetic data.**

- Lines are ESP32-C5 `CSI_DATA` lines in the firmware's exact format (`firmware/csi_recv/main/app_main.c:289-321`),
  with 234 raw ints (117 subcarriers, `csi/parser.py` `KNOWN_NULL_SUBCARRIERS`).
- The value spread was tuned so the mean line length is **850 B**. This matches the team's own figure ("about 0.85 KB on
  a C5", commit `f740336` README) and the raw-log rate in `csi/rawlog.py:6` (4 links ≈ 130 KB/s ≈ 855 B/line).
- Topology: **N receivers × N senders = N² links**, all at 40 Hz, no loss, nodes spread around a 4 × 3 m perimeter. An
  all-transceiver rig of N boards has N(N-1) links; read the tables by link count for that case.

**Scripts and outputs** (all in `research/scaling-redo/`):

| File | What it holds |
|---|---|
| `bench_host_pipeline.py` | Stages: `line`, `parser`, `reader`, `gateway`, `pipeline`, `server`, `profile` |
| `bench_server_decompose.py` | Splits the end-to-end server cost into parts; paced real-time runs |
| `bench_results_*.json`, `run_*.log` | Raw numbers |

Rerun with:

```
PYTHONPATH=../../Team-12-tracker _venv/Scripts/python bench_host_pipeline.py [stage ...]
PYTHONPATH=../../Team-12-tracker _venv/Scripts/python bench_server_decompose.py
```

**What each stage measures:**

- **pipeline.** `DashboardState.push` per packet, the same call the server makes (`csi/dashboard.py:52-57`), plus a
  frame (`snapshot` + `json.dumps`) every 0.2 s of packet time, as the server does under its lock
  (`csi/server.py:586-597`, `:623-631`). It is timed in steady state, after a 62-80 s warm-up, so the heatmap's 60 s
  baseline and auto mode's 30 s/75 s windows are full. Fixed mode with thresholds, and the experimental `--auto-threshold`
  mode.
- **server.** The real `DashboardServer` (MultiSource reader threads, `CSIParser`, `_Reorder`, lock, frames) fed with
  pre-rendered lines. Three variants:
  - unpaced: as fast as it can go
  - decomposed into its parts
  - paced at real time with wall-clock stamps, like a live port (20 s runs)

---

## 2. Measured results

### 2.1 Per-line costs (laptop)

| Item | Cost | Source |
|---|---|---|
| `CSIParser.feed`, one 850 B C5 line | **31 µs** (32k lines/s on one core) | `parser` stage |
| of which `np.array(s.replace(",", " ").split(), int64)` (`csi/parser.py:154`) | 20 µs | `np.fromstring(sep=",")` does the same in 6.5 µs |
| Chunked serial reader (current, `csi/parser.py:290-300`), Python side, 4 KB chunks | **0.8 µs/line** (2.2 µs with 512 B chunks, 11.6 µs with 64 B) | `reader` stage, fake in-memory port |
| pyserial `readline()` (= `SerialBase.read_until`, one `read(1)` per byte), Python side only | **321 µs/line**, 400x the chunked reader, before ~850 syscalls/line on a real port | `reader` stage |
| Gateway demux `split_gateway` pump (`csi/gateway.py:35-52`), one thread for every receiver | **3.5-6 µs/line** | `gateway` stage |
| `MotionScorer.push` alone | 55 µs/packet | micro-benchmark |
| `LinkHealth.observe` with estimated rate (`np.median` of 200 intervals each packet, `csi/health.py:63,75`) | 17 µs/packet | micro-benchmark |

### 2.2 `DashboardState` per packet vs N (laptop, steady state)

"laptop CPU-s/s" is CPU seconds needed per second of live traffic. Parsing is not included here; add 31 µs per packet.

| mode | N | links | pkt/s offered | push µs/pkt | total µs/pkt (incl. frames) | frame ms (under lock) | SSE payload KB | laptop CPU-s/s | Pi 5 est. (x2.5, + parse) |
|---|---|---|---|---|---|---|---|---|---|
| fixed | 2 | 4 | 160 | 174 | 204 | 1.0 | 4.9 | 0.03 | 0.09 |
| fixed | 3 | 9 | 360 | 153 | 176 | 1.7 | 6.8 | 0.06 | 0.19 |
| fixed | 4 | 16 | 640 | 154 | 174 | 2.6 | 9.3 | 0.11 | 0.33 |
| fixed | 5 | 25 | 1000 | 144 | 160 | 3.3 | 12.7 | 0.16 | 0.48 |
| fixed | 6 | 36 | 1440 | 150 | 165 | 4.5 | 16.6 | 0.24 | 0.71 |
| fixed | 8 | 64 | 2560 | 218 | 241 | 11.7 | 26.5 | 0.62 | 1.74 |
| fixed | 10 | 100 | 4000 | 224 | 244 | 16.1 | 39.4 | 0.98 | 2.75 |
| fixed | 12 | 144 | 5760 | 282 | 307 | 28.6 | 54.9 | 1.77 | 4.87 |
| auto | 2 | 4 | 160 | 300 | 321 | 0.7 | 4.9 | 0.05 | 0.14 |
| auto | 3 | 9 | 360 | 401 | 427 | 1.8 | 6.8 | 0.15 | 0.41 |
| auto | 4 | 16 | 640 | 501 | 524 | 3.1 | 9.4 | 0.34 | 0.89 |
| auto | 5 | 25 | 1000 | 572 | 592 | 3.9 | 12.7 | 0.59 | 1.56 |
| auto | 6 | 36 | 1440 | 715 | 740 | 7.3 | 16.7 | 1.07 | 2.78 |
| auto | 8 | 64 | 2560 | 789 | 813 | 12.3 | 26.6 | 2.08 | 5.40 |
| auto | 10 | 100 | 4000 | 1609 | 1659 | 39.9 | 39.6 | 6.64 | 16.9 |
| auto | 12 | 144 | 5760 | 1374 | 1413 | 44.8 | 55.2 | 8.14 | 20.8 |

### 2.3 End-to-end `DashboardServer` (laptop, the most realistic number)

**Unpaced** (max throughput, 20 s of traffic):

| N | links | demand pkt/s | achieved pkt/s | realtime factor | CPU µs/pkt |
|---|---|---|---|---|---|
| 2 | 4 | 160 | 3517 | 22.0 | 278 |
| 4 | 16 | 640 | 2256 | 3.5 | 403 |
| 6 | 36 | 1440 | 1701 | **1.18** | 551 |
| 8 | 64 | 2560 | 1978 | **0.77** (falls behind) | 474 |
| 10 | 100 | 4000 | 1709 | **0.43** | 519 |

**Decomposition**, CPU µs/pkt, unpaced, 15 s of traffic:

| N | MultiSource + parse | + `_Reorder` | Full server |
|---|---|---|---|
| 2 | 65 | 65 | 260 |
| 4 | 72 | 73 | 369 |
| 6 | 132 | 152 | 543 |

The full server costs about 100-230 µs/pkt more than `push` (≈165) plus the reader side. That gap is GIL hand-offs
between N reader threads and the pump, and queue traffic. With more threads the reader side alone doubles, from 65 to
132 µs.

**Paced at real time** (live-like, wall-clock stamps; every run kept up, wall time 20.5 s for 20 s of traffic):

| N | links | pkt/s | laptop CPU (fraction of one core) | CPU µs/pkt | **Pi 5 est. x2-x3** |
|---|---|---|---|---|---|
| 4 | 16 | 640 | **0.36** | 571 | 0.71-1.07 |
| 5 | 25 | 1000 | **0.46** | 468 | 0.91-1.37 |
| 6 | 36 | 1440 | **0.69** | 493 | 1.38-2.08 |

(The paced sources wake once per line, a little more often than a chunked serial reader does, so this slightly
overstates the reader cost.)

**What this means.** The process is single-GIL, so for this workload one core is the ceiling, whatever the Pi's other
three cores are doing. On a Pi 5:

- Fixed mode reaches that ceiling at about **16-25 links**: N = 4-5 with N×N links, or N = 5 for an all-transceiver
  rig (20 links).
- The laptop reaches it at about 36-50 links (N ≈ 6-7).

### 2.4 Serial-link ceilings (computed from the measured 850 B line)

| Path | Budget | Demand at 40 Hz | Binds at |
|---|---|---|---|
| **Wired receiver, UART 921600 8N1 = 92.2 KB/s** (`firmware/csi_recv/sdkconfig.defaults:6`, console via `ets_printf`, `app_main.c:241`) | **108 lines/s per receiver = 2.7 senders at 40 Hz** | (senders heard) × 40 × 850 B. Utilisation with N senders: N=2 74%, N=3 111%, N=4 148%, N=6 221%, N=12 443% | **3 senders per receiver.** All-transceiver: N-1 ≥ 3, i.e. **N = 4**. The team's own budget note says ≈80 pkt/s per receiver (commit `f740336`). |
| Same, before `867eea3` (per-packet `compensate_gain` ESP_LOGI line, half of all lines) | about 2x the lines | n/a | ~2 senders |
| **Gateway board, one USB-Serial-JTAG port** (`app_main.c:370`, no baud limit) carrying `RX,<serial>,` + line = 871 B | USB full-speed; the practical ESP32 USB-Serial-JTAG throughput was **not measured here** (the firmware report owns it) | links × 40 × 871 B: N=4 (12 links all-transceiver) 418 KB/s; N=5 (20) 697 KB/s; N=6 (30) 1.05 MB/s; N=8 (56) 1.95 MB/s; N=12 (132) 4.6 MB/s | Host-side read is not the limit (0.8 µs/line chunked). If the port sustains ≈1 MB/s, it binds near N = 6 (inferred). |
| SSE to the browser | LAN | 55 KB × 5 Hz = 275 KB/s per client at N=12 | Not a limit |

---

## 3. Hot spots (cProfile, N = 8, 64 links, steady state)

**Fixed mode** (8.04 s profiled for 12,808 packets; cProfile inflates small calls):

| Rank | Where | Share | Scaling |
|---|---|---|---|
| 1 | `MotionScorer.push`, `csi/detect.py:149-170`. Each packet re-stacks the whole 1 s window (`np.stack`, :168) and recomputes `var(axis=0).mean()` (:169) over ~40 × 114 floats. | **36%** (2.86 s) | O(window × subcarriers) per packet. Constant per packet, so linear in links. A running sum and sum-of-squares would make it O(subcarriers). |
| 2 | `LinkMonitor._update_room`, `csi/links.py:179-191`. Every packet rebuilds a dict over **all** links, calling `LinkId.label` (an f-string property, `:46-49`) twice per link (828k calls), then `RoomDecision.update` (`csi/adaptive.py:212-222`) re-sorts the elevated labels. | **15-16%** (0.91 s dictcomp + 0.32 s label) | **O(links) per packet, so O(links²) = O(N⁴) per second.** This is the term that pushes `push` from ~150 µs (N ≤ 6) to 282 µs (N = 12). |
| 3 | `QuietBaseline.value`, `csi/heatmap.py:91-95`. A Python list of 60 s of samples plus `np.percentile`, per link, per heatmap advance (10 Hz from `push` via `due()`, `csi/dashboard.py:55`, plus 5 Hz from frames). | **14%** (0.45 s listcomp + 0.71 s quantile) | O(links × window) per advance; linear in links per second. |
| 4 | `LinkHealth.rate_hz` → `np.median(deque of 200)` per packet (`csi/health.py:63,75`), because the dashboard never passes a send rate. | **9%** (0.76 s) | Constant per packet. |
| 5 | Frame build plus `json.dumps` under the server lock (`csi/server.py:623-637`, `csi/dashboard.py:92-147`): heatmap 504 cells rounded one by one in Python, one dict per link. | 2.6 ms at N=4 → 28.6 ms at N=12 (laptop); est. ~70 ms on a Pi at N=12, which blocks `_feed` | O(links + cells) at 5 Hz |

The rest is small:

- `Heatmap._combine` (`csi/heatmap.py:206-219`) does `np.vstack` plus `np.sort` over links × 504 cells: ~1 ms at 144
  links. Grid size is fixed (28 × 18) and does not grow with N.
- The tracker and `_Reorder` (`csi/server.py:190-216`) are negligible: +0-20 µs/pkt.

**Auto mode** (`--auto-threshold`, experimental; 28.8 s profiled):

- **58%** goes to `AdaptiveDetector._should_rebuild`, `csi/adaptive.py:161-171`. Each packet runs two Python generator
  sums over the link's whole 30 s `_hist` deque (~1,200 tuples): 15.4M generator steps for 12,800 packets.
- `_refresh` (`:150-159`) also rebuilds a Python list of up to 3,000 samples every 0.25 s per link.
- Together these make auto mode **~3x the CPU of fixed mode**: 0.34 vs 0.11 laptop CPU-s/s at N = 4. On a Pi it
  saturates at about **N = 4** on the pipeline alone. Running counters would make it O(1).

---

## 4. Other host-side findings

### 4.1 The 5+ attempt ran on the old per-byte serial reader

This is measured history plus inference, and probably the biggest host-side factor in the "never past 4" story.

- **What the reader cost on the Pi.** Commit `867eea3` ("Cut packet loss: chunked serial reads", 2026-11-08 01:17)
  records a Pi measurement. With three receivers, `main`'s `readline()` reader **used 165% CPU and lost 19-27%** on
  receivers that hear both senders. The chunked reader **used 6% and lost 8-10%**, which is the boards' own floor.
- **The 5+ branch never got the fix.** `Team-12-5plus` (branch `feat/transceiver-5plus`, last commit 2026-11-07 23:00)
  does not contain `867eea3` or `560917e`. Its `csi/parser.py:271-275` still does `ser.readline()` per line.
- **Why it was so expensive.** pyserial's `readline()` is `read_until`, which calls `read(1)` per byte, so about 850
  `select`+`read` syscalls and Python iterations per line. The Python part alone measured 321 µs/line on this laptop.
- **What that implies for the 5+ test (inferred).** The Pi measurement works out to ≈ 3.9 ms of CPU per line
  (165% / (3 × 140 lines/s)). A 5-node all-transceiver test means 5 receivers × 4 senders × 40 Hz = 800 CSI lines/s,
  plus the per-packet gain log, which is still `ESP_LOGI` in the 5plus `csi_recv/main/app_main.c:169`. At that cost the
  reader alone would need about 3+ cores. The excess shows up as kernel buffer overruns, then cut or malformed lines,
  then "packet loss".

### 4.2 Single gateway port, one demux thread

- `split_gateway` (`csi/gateway.py:30-63`) costs 3.5-6 µs/line, which is 1-2% of a laptop core even at N = 8, so the
  thread is not a CPU bottleneck.
- **Failure mode:** `put_nowait` into a 2,000-line queue per receiver drops silently when full (`csi/gateway.py:25,44-47`).
  In the unpaced test, where the consumers could not keep up with the pump, it dropped 7-44% of lines and nothing counted them. On a
  saturated Pi this would show up as packet loss that looks like RF loss.
- Further down the chain, the `MultiSource` queue is unbounded (`csi/multisource.py:63,90`). If the pump falls behind,
  latency and memory grow without limit instead of anything dropping (inferred from the code).

### 4.3 Locks

- One `threading.Lock` covers all packet ingestion, frame building, `/api/state` and commands (`csi/server.py:575-584`,
  `:623`, `:684-699`).
- `_feed` holds it for a whole drained batch. Frame building (up to ~29 ms laptop / ~70 ms Pi at N = 12) runs inside it.
- Calibration was moved off the lock (ADR 0016), but per-frame work was not.
- None of this matters below N ≈ 6. The real constraint is the GIL, not this lock.

### 4.4 Browser

`web/index.html:780-791` rebuilds every link card from scratch (`clearChildren` plus `appendChild`) on each 5 Hz frame.
That is O(links) DOM work per frame: fine on a laptop browser, likely slow in a Pi-hosted kiosk browser at 50+ links
(not measured).

---

## 5. Ranked host-side bottlenecks

| # | Bottleneck | Bites at | Evidence | Confidence |
|---|---|---|---|---|
| 1 | **Per-receiver UART budget**: 850 B text lines at 921600 baud = 108 lines/s = 2.7 senders at 40 Hz | **N = 4** all-transceiver (3 senders per receiver), wired receivers | Computed from the measured line length; matches the team's own ≈80-116 pkt/s budget (`f740336`) | High |
| 2 | **Per-byte `readline()` reader** on the 5+ branch | **N ≈ 3-4** on a Pi | Pi measurement in `867eea3` (165% CPU at 3 receivers); laptop 321 µs/line Python-only vs 0.8 µs chunked; `Team-12-5plus/csi/parser.py:271-275` | High that it existed on that branch; medium that it was the binding limit in the 5+ test |
| 3 | **Single-GIL Python pipeline**, end to end ≈0.45-0.57 ms CPU/packet on the laptop | Pi 5 (x2-3): **16-25 links = N 4-5** (fixed); laptop ≈ N 6-7 | Paced server runs (§2.3); unpaced server falls behind at N=8 on the laptop | Medium (the Pi factor is assumed, not measured) |
| 4 | **Auto mode `_should_rebuild` O(window) Python loop** | Pi: N ≈ 4 | 58% of profile; 3x fixed-mode CPU | High (measured), but auto is opt-in |
| 5 | **`_update_room` O(links) per packet → O(N⁴)** | Matters from N ≈ 8 (laptop push 150 → 282 µs) | Profile 15%; push cost growth N=6→12 | High |
| 6 | Silent gateway queue drops, unbounded MultiSource backlog | Whenever 3 or 4 saturate | Unpaced gateway test dropped 7-44% silently | Medium (how often it fires live is inferred) |
| 7 | Gateway USB-Serial-JTAG bandwidth (links × 35 KB/s) | Inferred around N ≈ 6 if ~1 MB/s | Arithmetic only | Low (throughput not measured) |
| 8 | Frame JSON under the lock, SSE size, DOM rebuild | N ≥ 10 | 29 ms/frame and 55 KB at N=12 | High (measured), low impact |

**Measured vs inferred.**

- **Measured** (all on the laptop): every µs, ms, KB and CPU fraction in §2-§3.
- **Inferred:**
  - the Pi 5 figures (x2-3 factor)
  - the USB-Serial-JTAG ceiling
  - how the 5+ test's loss broke down between the reader and the boards
  - live behaviour of the queues when saturated

**Cheap fixes**, in order of payoff (not implemented; repo untouched):

1. Keep the chunked reader everywhere.
2. Send binary or fewer subcarriers from the boards (cuts the UART load and also the 20 µs split).
3. Make `MotionScorer` incremental, with running sums.
4. Run the room decision on a timer, not per packet.
5. Pass the known send rate to `LinkHealth`.
6. Use running counters in `AdaptiveDetector`.
7. Parse in a `multiprocessing` worker per receiver, or use `np.fromstring`, to get off the single GIL.
