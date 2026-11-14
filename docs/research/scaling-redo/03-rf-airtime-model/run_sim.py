import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from airtime import *

def row(label, **kw):
    acc = None
    seeds = (1, 2, 3)
    res = [simulate(**kw, seed=s) for s in seeds]
    avg = {k: sum(r[k] for r in res) / len(res) for k in res[0]}
    fg = max(1, avg["fwd_gen"])
    print(f"| {label} | {100*avg['util']:.0f} | {avg['pkt_per_link_s']:.1f} | "
          f"{100*avg['sense_coll']/max(1,avg['sense_tx']):.1f} | {100*avg['fwd_coll']/max(1,avg['fwd_att']):.1f} | "
          f"{100*avg['fwd_drop_q']/fg:.1f} | {100*avg['fwd_drop_retry']/fg:.1f} | {avg['e2e_per_link_s']:.1f} |")

print("| scenario (R=40 Hz unless noted) | sim busy % | sensing pkt/s per link (air) | sensing frames collided % | fwd attempts collided % | fwd dropped: queue full % | fwd dropped: retry limit % | end-to-end pkt/s per link at gateway |")
print("|---|---|---|---|---|---|---|---|")
A = dict(rate=40, mode_s="ht40_mcs0", mode_f="ht40_mcs0", payload=PAYLOADS["ascii_871"], secs=8.0)
row("split 2 tx x 2 rx, ASCII fwd (demo rig)", n_send=2, n_recv=2, **A)
row("split 2 tx x 2 rx, + 30% guest Wi-Fi", n_send=2, n_recv=2, bg_util=0.3, **A)
row("split 2 tx x 3 rx, ASCII fwd", n_send=2, n_recv=3, **A)
row("split 2 tx x 3 rx, + 30% guest", n_send=2, n_recv=3, bg_util=0.3, **A)
row("split 4 tx x 4 rx, ASCII fwd", n_send=4, n_recv=4, **A)
row("split 4 tx x 4 rx, + 30% guest", n_send=4, n_recv=4, bg_util=0.3, **A)
row("split 2 tx x 2 rx, no fwd (wired), + 30% guest", n_send=2, n_recv=2, bg_util=0.3, fwd=False, **A)
for N in (4, 6, 8):
    row(f"all-transceiver N={N}, ASCII fwd", n_send=N, n_recv=N, transceiver=True, **A)
for N in (4, 6, 8):
    row(f"all-transceiver N={N}, ASCII fwd, + 30% guest", n_send=N, n_recv=N, transceiver=True, bg_util=0.3, **A)
for N in (8, 12, 16):
    row(f"all-transceiver N={N}, no fwd (wired)", n_send=N, n_recv=N, transceiver=True, fwd=False, **A)
B = dict(rate=40, mode_s="ht20_mcs0", mode_f="ht20_mcs7", payload=PAYLOADS["bin_amp52_i8"], secs=8.0)
for N in (6, 8, 12):
    row(f"all-transceiver N={N}, amp52 int8 fwd @HT20 MCS7", n_send=N, n_recv=N, transceiver=True, **B)
C = dict(rate=20, mode_s="ht40_mcs0", mode_f="ht40_mcs0", payload=PAYLOADS["ascii_871"], secs=8.0)
for N in (4, 6, 8):
    row(f"all-transceiver N={N}, ASCII fwd, R=20 Hz", n_send=N, n_recv=N, transceiver=True, **C)
