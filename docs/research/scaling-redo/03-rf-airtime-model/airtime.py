"""Airtime model + small DCF Monte-Carlo for the Team-12 CSI rig. Estimates only.

PHY timing (IEEE 802.11-2020 cl. 17/19, 802.11ax-2021 cl. 27; 2.4 GHz ERP/HT/HE add 6 us signal extension):
  OFDM legacy: 20 + 4*ceil((16+6+8L)/NDBPS) + 6
  HT-mixed 1SS LGI: 36 + 4*ceil((16+6+8L)/NDBPS) + 6
  HE SU 1SS 0.8us GI, 2x HE-LTF, PE=0: 43.2 + 13.6*ceil((16+6+8L)/NDBPS) + 6
  DSSS 1 Mbps long preamble: 192 + 8L
"""
import math, random, sys

SLOT, SIFS, DIFS = 9.0, 10.0, 28.0
SE = 6.0

NDBPS = {
    "11b_1M": None,
    "ofdm_6M": 24, "ofdm_24M": 96,
    "ht20_mcs0": 26, "ht20_mcs7": 260,
    "ht40_mcs0": 54, "ht40_mcs7": 540,
    "he20_mcs0": 117, "he20_mcs7": 1170,
}


def ppdu_us(mode, L):
    bits = 16 + 6 + 8 * L
    if mode == "11b_1M":
        return 192 + 8 * L
    n = NDBPS[mode]
    if mode.startswith("ofdm"):
        return 20 + 4 * math.ceil(bits / n) + SE
    if mode.startswith("ht"):
        return 36 + 4 * math.ceil(bits / n) + SE
    if mode.startswith("he"):
        return 43.2 + 13.6 * math.ceil(bits / n) + SE
    raise ValueError(mode)


ACK_OFDM = ppdu_us("ofdm_6M", 14)   # 50 us
ACK_11B = ppdu_us("11b_1M", 14)     # 304 us


def ack_for(mode):
    return ACK_11B if mode == "11b_1M" else ACK_OFDM


ESPNOW_OVH = 24 + 1 + 3 + 4 + 4   # MAC hdr, category, OUI, random, FCS = 36
def espnow_frame(payload):
    n_el = max(1, math.ceil(payload / 250))
    return ESPNOW_OVH + 7 * n_el + payload

SENSE_L = espnow_frame(4)           # 47 B (4-byte counter)
PAYLOADS = {                        # forwarded payload per CSI sample
    "ascii_871": 871,                 # RX,<serial>, + CSI_DATA line, mean of synthetic lines (sibling bench)
    "ascii_1400": 1400,               # FWD_LINE_MAX worst case
    "bin_iq_234": 234 + 16,           # raw int8 I/Q of 117 subcarriers + 16 B header
    "bin_amp117_u8": 117 + 16,        # amplitude per subcarrier, uint8
    "bin_amp64_i8": 64 + 16,
    "bin_amp52_i8": 52 + 16,
    "feature_8": 8 + 8,               # e.g. per-sample motion feature + seq/rssi
}


def access_overhead(cw=15):
    return DIFS + SLOT * cw / 2.0     # mean idle cost per access when deferring


def util_table():
    out = []
    out.append("## PPDU durations (us)\n")
    out.append("| PHY | sense 47 B | ack | " + " | ".join(PAYLOADS) + " |")
    out.append("|---" * (3 + len(PAYLOADS)) + "|")
    for m in NDBPS:
        row = [m, f"{ppdu_us(m, SENSE_L):.0f}", f"{ack_for(m):.0f}"]
        row += [f"{ppdu_us(m, espnow_frame(p)):.0f}" for p in PAYLOADS.values()]
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def load(mode_s, mode_f, n_sense, n_fwd, payload, retries=0.0, with_access=True):
    """Return (busy fraction, occupancy incl. DIFS+mean backoff) per second."""
    ts = ppdu_us(mode_s, SENSE_L)
    busy = n_sense * ts
    occ = n_sense * (ts + access_overhead())
    if n_fwd:
        tf = ppdu_us(mode_f, espnow_frame(payload)) + SIFS + ack_for(mode_f)
        busy += n_fwd * tf * (1 + retries)
        occ += n_fwd * (tf + access_overhead()) * (1 + retries)
    return busy / 1e6, occ / 1e6


# ------------------------------------------------------------------ Monte-Carlo DCF (all-hear-all, no capture)
class Sta:
    def __init__(self, name):
        self.name = name
        self.q = []          # list of (ready_time, kind, meta)
        self.bo = None
        self.cw = 15
        self.retry = 0


def simulate(n_send, n_recv, rate, mode_s, mode_f, payload, transceiver=False, secs=5.0,
             qlim=16, retry_limit=7, bg_util=0.0, bg_len_us=400.0, seed=1, fwd=True, delta=4.0):
    rnd = random.Random(seed)
    T = secs * 1e6
    ts = ppdu_us(mode_s, SENSE_L)
    tf = ppdu_us(mode_f, espnow_frame(payload)) if fwd else 0
    ack = ack_for(mode_f)
    if transceiver:
        nodes = [Sta(f"n{i}") for i in range(n_send)]
        senders = nodes; receivers = nodes
    else:
        senders = [Sta(f"s{i}") for i in range(n_send)]
        receivers = [Sta(f"r{i}") for i in range(n_recv)]
        nodes = senders + receivers
    bg = Sta("bg")
    allst = nodes + [bg]
    # sensing arrivals: free-running periodic with random phase + 0.2 ms jitter
    arrivals = []
    for s in senders:
        ph = rnd.uniform(0, 1e6 / rate)
        t = ph
        while t < T:
            arrivals.append((t, s, "sense"))
            t += 1e6 / rate + rnd.gauss(0, 200)
    if bg_util > 0:
        lam = bg_util / bg_len_us   # per us
        t = rnd.expovariate(lam)
        while t < T:
            arrivals.append((t, bg, "bg"))
            t += rnd.expovariate(lam)
    arrivals.sort(key=lambda a: a[0])
    ai = 0
    stats = dict(sense_tx=0, sense_ok=0, link_ok=0, link_exp=0, fwd_gen=0, fwd_ok=0, fwd_drop_q=0,
                 fwd_drop_retry=0, busy=0.0, coll=0, sense_drop_q=0, sense_coll=0, fwd_att=0, fwd_coll=0)
    pending_fwd = []   # (time, sta)
    t = 0.0
    import heapq
    while t < T:
        # admit arrivals up to now
        while ai < len(arrivals) and arrivals[ai][0] <= t:
            at, st, kind = arrivals[ai]; ai += 1
            if len(st.q) >= qlim and st is not bg:
                stats["sense_drop_q"] += 1
                continue
            st.q.append((at, kind, None))
        while pending_fwd and pending_fwd[0][0] <= t:
            at, st = heapq.heappop(pending_fwd)
            stats["fwd_gen"] += 1
            if len(st.q) >= qlim:
                stats["fwd_drop_q"] += 1
            else:
                st.q.append((at, "fwd", None))
        # candidates
        best = None; starts = []
        for st in allst:
            if not st.q:
                continue
            r = max(t, st.q[0][0]) + DIFS
            if st.bo is None:
                st.bo = rnd.randint(0, st.cw)
            s = r + st.bo * SLOT
            starts.append((s, st, r))
        nxt_arr = arrivals[ai][0] if ai < len(arrivals) else float("inf")
        nxt_fwd = pending_fwd[0][0] if pending_fwd else float("inf")
        if not starts:
            t = min(nxt_arr, nxt_fwd)
            continue
        smin = min(s for s, _, _ in starts)
        if min(nxt_arr, nxt_fwd) < smin:
            # an arrival happens before anyone transmits: advance, keeping backoff progress
            tn = min(nxt_arr, nxt_fwd)
            # freeze nothing: backoff continues; emulate by reducing bo for elapsed slots
            for s, st, r in starts:
                if tn > r:
                    k = int((tn - r) // SLOT)
                    st.bo = max(0, st.bo - k)
            # stations re-reference r from new t; approximate (DIFS re-applied => slight conservatism)
            t = tn
            continue
        txs = [(s, st) for s, st, r in starts if s < smin + delta]
        for s, st, r in starts:
            if all(st is not x for _, x in txs) and smin > r:
                st.bo = max(0, st.bo - int((smin - r) // SLOT))
        collided = len(txs) > 1
        if collided:
            stats["coll"] += 1
        durs = []
        for s, st in txs:
            kind = st.q[0][1]
            if kind == "sense":
                d = ts
            elif kind == "fwd":
                d = tf + SIFS + ack
            else:
                d = rnd.expovariate(1 / bg_len_us)
            durs.append(d)
        end = smin + max(durs)
        stats["busy"] += max(durs)
        for (s, st), d in zip(txs, durs):
            kind = st.q[0][1]
            if kind == "sense":
                stats["sense_tx"] += 1
                stats["sense_coll"] += collided
                st.q.pop(0); st.bo = None; st.cw = 15
                rx = [x for x in receivers if x is not st]
                stats["link_exp"] += len(rx)
                if not collided:
                    stats["sense_ok"] += 1
                    stats["link_ok"] += len(rx)
                    if fwd:
                        for x in rx:
                            heapq.heappush(pending_fwd, (end + rnd.uniform(300, 1500), x))
            elif kind == "fwd":
                stats["fwd_att"] += 1
                stats["fwd_coll"] += collided
                if not collided:
                    stats["fwd_ok"] += 1
                    st.q.pop(0); st.bo = None; st.cw = 15; st.retry = 0
                else:
                    st.retry += 1
                    if st.retry > retry_limit:
                        stats["fwd_drop_retry"] += 1
                        st.q.pop(0); st.bo = None; st.cw = 15; st.retry = 0
                    else:
                        st.cw = min(2 * (st.cw + 1) - 1, 1023); st.bo = None
            else:
                st.q.pop(0); st.bo = None
        t = end
    stats["util"] = stats["busy"] / T
    per_link = stats["link_ok"] / max(1, stats["link_exp"]) * rate
    stats["pkt_per_link_s"] = per_link
    # delivered end-to-end per link (sensing ok AND forward ok)
    if fwd:
        frac_fwd = stats["fwd_ok"] / max(1, stats["fwd_gen"])
        stats["e2e_per_link_s"] = per_link * frac_fwd
    else:
        stats["e2e_per_link_s"] = per_link
    return stats


if __name__ == "__main__":
    print(util_table())
