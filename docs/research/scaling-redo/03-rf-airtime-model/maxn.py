import sys, math
sys.path.insert(0, __import__("os").path.dirname(__file__))
from airtime import *
NDBPS["ht20_mcs3"] = 104
def per_sample_unicast(N, R, ms, mf, pay):
    b,_ = load(ms, mf, N*R, N*(N-1)*R, pay); return b
def batched(N, R, ms, mf, rep):
    # each node per round sends ceil((N-1)*rep/1470) unicast frames to gateway
    tot = (N-1)*rep; nfr = math.ceil(tot/1470); last = tot-(nfr-1)*1470
    t = (nfr-1)*(ppdu_us(mf, espnow_frame(1470))+SIFS+ack_for(mf)) + ppdu_us(mf, espnow_frame(last))+SIFS+ack_for(mf)
    return N*R*(ppdu_us(ms, SENSE_L)+t)/1e6
def piggy(N, R, m, rep):
    L = espnow_frame(min(1470, 4+(N-1)*rep)); return N*R*ppdu_us(m, L)/1e6 if 4+(N-1)*rep<=1470 else 9.9
def sense_only(N, R, m): return N*R*ppdu_us(m, SENSE_L)/1e6
def tdma_piggy(N, R, m, rep, guard=150):
    L = espnow_frame(min(1470, 4+(N-1)*rep)); return N*R*(ppdu_us(m, L)+guard)/1e6 if 4+(N-1)*rep<=1470 else 9.9
archs = [
 ("A today: per-sample ASCII unicast, HT40 MCS0", lambda N,R: per_sample_unicast(N,R,"ht40_mcs0","ht40_mcs0",871)),
 ("B per-sample ASCII unicast, HT20 MCS0", lambda N,R: per_sample_unicast(N,R,"ht20_mcs0","ht20_mcs0",871)),
 ("C per-sample binary amp52 int8 unicast, HT20 MCS7", lambda N,R: per_sample_unicast(N,R,"ht20_mcs0","ht20_mcs7",68)),
 ("D per-round batch amp52 (N-1 x 56 B) unicast, HT20 MCS7", lambda N,R: batched(N,R,"ht20_mcs0","ht20_mcs7",56)),
 ("E piggyback amp52 in own sensing broadcast, HT20 MCS3", lambda N,R: piggy(N,R,"ht20_mcs3",56)),
 ("F piggyback 8 B feature in sensing broadcast, HT20 MCS0", lambda N,R: piggy(N,R,"ht20_mcs0",8)),
 ("G TDMA + piggyback amp52, HT20 MCS3, 150 us guard", lambda N,R: tdma_piggy(N,R,"ht20_mcs3",56)),
 ("H sensing only (out-of-band backhaul), HT20 MCS0", lambda N,R: sense_only(N,R,"ht20_mcs0")),
 ("H' sensing only, HT40 MCS0", lambda N,R: sense_only(N,R,"ht40_mcs0")),
]
print("| architecture (all-transceiver, N(N-1) links) | max N, R=40, rig busy <=30% | max N, R=40, <=50% | max N, R=20, <=30% | max N, R=20, <=50% |")
print("|---|---|---|---|---|")
for name, f in archs:
    cells=[]
    for R in (40,20):
        for cap in (0.30,0.50):
            n=1
            while n<200 and f(n+1,R)<=cap: n+=1
            cells.append(str(n) if n<200 else ">=200")
    print(f"| {name} | " + " | ".join([cells[0],cells[1],cells[2],cells[3]]) + " |")
# split S=M topology with today's forwarding
print()
for R in (40,20):
  for cap in (0.3,0.5):
    best=0
    for S in range(1,40):
      for M in range(1,40):
        b,_=load("ht40_mcs0","ht40_mcs0",S*R,S*M*R,871)
        if b<=cap and S*M>best: best=S*M; bs=(S,M)
    print(f"split today R={R} cap={cap}: max links {best} at S,M={bs} (boards {sum(bs)}+gateway)")
