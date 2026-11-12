import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from airtime import *

R = 40
print("\n## Analytic load: split rig (S senders, M receivers, forwards ASCII 871 B, HT40 MCS0)\n")
print("| S | M | rate | sense frames/s | fwd frames/s | busy % sensing only | busy % with fwd | occupancy % (incl. DIFS+mean backoff) |")
print("|---|---|---|---|---|---|---|---|")
for S, M, r in [(2, 2, 40), (2, 3, 40), (3, 3, 40), (4, 4, 40), (4, 4, 20), (8, 8, 20), (8, 8, 40)]:
    b0, o0 = load("ht40_mcs0", "ht40_mcs0", S * r, 0, 0)
    b1, o1 = load("ht40_mcs0", "ht40_mcs0", S * r, S * M * r, PAYLOADS["ascii_871"])
    print(f"| {S} | {M} | {r} | {S*r} | {S*M*r} | {100*b0:.1f} | {100*b1:.1f} | {100*o1:.1f} |")

print("\n## Analytic load: all-transceiver N nodes, every node forwards N-1 lines per round to one gateway\n")
for r in (40, 20):
    print(f"\n### R = {r} Hz\n")
    hdr = "| N | links N(N-1) | sense only HT40 MCS0 | +ASCII fwd HT40 MCS0 | +ASCII fwd 11b 1M (ESP-NOW default) | +ASCII HT20 MCS0 | +ASCII HT20 MCS7 | +bin IQ 250 B HT20 MCS0 | +amp117 u8 HT20 MCS0 | +amp52 i8 HT20 MCS7 | +feature 16 B HT20 MCS0 |"
    print(hdr); print("|---" * hdr.count("|") + "|"[:0])
    for N in (4, 6, 8, 12, 16):
        nf = N * (N - 1) * r
        cells = [str(N), str(N * (N - 1))]
        cells.append(f"{100*load('ht40_mcs0','ht40_mcs0',N*r,0,0)[0]:.1f}")
        for ms, mf, p in [("ht40_mcs0", "ht40_mcs0", "ascii_871"), ("ht40_mcs0", "11b_1M", "ascii_871"),
                          ("ht20_mcs0", "ht20_mcs0", "ascii_871"), ("ht20_mcs0", "ht20_mcs7", "ascii_871"),
                          ("ht20_mcs0", "ht20_mcs0", "bin_iq_234"), ("ht20_mcs0", "ht20_mcs0", "bin_amp117_u8"),
                          ("ht20_mcs0", "ht20_mcs7", "bin_amp52_i8"), ("ht20_mcs0", "ht20_mcs0", "feature_8")]:
            b, o = load(ms, mf, N * r, nf, PAYLOADS[p])
            cells.append(f"{100*b:.0f} / {100*o:.0f}")
        print("| " + " | ".join(cells) + " |")

print("\n## Piggyback: each node's own sensing broadcast carries its N-1 reports from the last round (no unicast, no ACK)\n")
print("| N | report | frame B | HT20 MCS0 busy % | HT20 MCS3 busy % (approx NDBPS 104) | HT40 MCS0 busy % |")
print("|---|---|---|---|---|---|")
NDBPS["ht20_mcs3"] = 104
for N in (4, 8, 12, 16, 24):
    for rep, size in [("amp52 i8 + 4 B hdr", 56), ("feature 8 B", 8)]:
        L = espnow_frame(min(1470, 4 + (N - 1) * size))
        cells = [str(N), rep, str(L)]
        for m in ("ht20_mcs0", "ht20_mcs3", "ht40_mcs0"):
            b = N * 40 * (ppdu_us(m, L)) / 1e6
            cells.append(f"{100*b:.1f}")
        print("| " + " | ".join(cells) + " |")
