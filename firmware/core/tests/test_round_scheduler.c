/*
 * The round scheduler, tested at its seam: heard frames and the device clock in; the next transmission, the
 * membership and the schedule out. Most tests run several nodes over a simulated radio (sim_* below): each node has
 * its own drifting clock, frames take airtime, a node does not hear while it sends, overlapping frames are lost,
 * and links can be cut. Nothing here looks inside rs_t except the read-only fields the glue reads too (state,
 * my_index, membership, version).
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../link_table.h"
#include "../round_scheduler.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

#define HZ 40
#define P_US (1000000 / HZ)

static void make_id(uint8_t id[RB_NODE_ID_BYTES], int k)
{
    const uint8_t base[RB_NODE_ID_BYTES] = {0x10, 0xBD, 0xA3, 0x00, 0x00, 0x00};
    memcpy(id, base, RB_NODE_ID_BYTES);
    id[4] = (uint8_t)(k >> 8);
    id[5] = (uint8_t)k;
}

static unsigned popcount(uint32_t m)
{
    unsigned n = 0;
    for (; m; m &= m - 1) {
        n++;
    }
    return n;
}

/* ---------------------------------------------------------------- one node on its own */

static void a_lone_node_listens_then_founds_its_own_schedule(void)
{
    rs_t s;
    uint8_t id[RB_NODE_ID_BYTES];
    make_id(id, 1);
    CHECK(rs_init(&s, id, HZ, RS_GUARD_DEFAULT_US, 1234, 0), "40 Hz with a 250 us guard makes a round");
    CHECK(s.state == RS_LISTENING, "a node starts by listening");
    rs_tx_t tx = rs_next(&s, 0);
    CHECK(tx.kind == RS_TX_ROUND && tx.at_us >= 3 * P_US && tx.at_us <= 7 * P_US,
          "it hears nothing and founds its schedule after 3 to 7 rounds (at %lld us)", (long long)tx.at_us);
    rb_broadcast_t b;
    CHECK(rs_take(&s, &tx, tx.at_us, &b), "its first round broadcast goes out");
    CHECK(s.state == RS_MEMBER && s.my_index == 0 && s.membership == 1u, "alone, it is node index 0");
    CHECK(b.frame_type == RB_FRAME_ROUND && b.slot == 0 && b.slots_per_round == 2 && b.node_index == 0
              && b.membership == 1u && memcmp(b.node_id, id, RB_NODE_ID_BYTES) == 0 && b.n_reports == 0,
          "slot 0 of 2 (its slot and the join slot), its own serial");
    rs_tx_t again = rs_next(&s, tx.at_us + 200);
    CHECK(again.kind == RS_TX_ROUND && again.at_us == tx.at_us + P_US && again.round == (uint16_t)(tx.round + 1),
          "then once per round");
    CHECK(!rs_take(&s, &tx, tx.at_us + 10, &b), "a round already sent is never sent twice");
    rs_tx_t late = again;
    CHECK(!rs_take(&s, &late, again.at_us + RS_GUARD_DEFAULT_US + 1, &b) && s.count.late_slots == 1,
          "a slot more than the guard late is skipped and counted");
    CHECK(!rs_init(&s, id, 400, RS_GUARD_DEFAULT_US, 1, 0), "400 rounds/s cannot fit two members and a join slot");
}

/* ---------------------------------------------------------------- the simulated radio
 *
 * Real time T in us. Node clock c = T x (1 + ppm 1e-6) + offset. A node does what node_main.c does: it arms one
 * timer for rs_next, calls rs_take when it fires, fills the link reports from its link table, and tells the
 * scheduler each frame it hears and each send callback. A frame starts on air a send latency after rs_take and
 * lasts its airtime; a receiver gets it a receive latency after it ends, unless the receiver was sending meanwhile
 * or another frame it can hear overlapped it.
 */

#define SIM_MAX_NODES 34
#define SIM_FRAMES 1024
#define SIM_EVENTS 8192
#define SIM_SENT_KEPT 1024

typedef struct {
    bool on;
    int gen;                    /* power cycles: a frame for an earlier life is not delivered */
    rs_t rs;
    link_table_t links;
    double ppm;
    int64_t offset;
    int64_t tx_latency_us, tx_jitter_us;
    rs_tx_t armed;
    bool was_member, left_member;   /* left_member: it stopped being a member while powered (a "restart") */
    uint32_t sent_keys[SIM_SENT_KEPT];
    int n_sent;
    bool duplicate_round;       /* it sent two round broadcasts with one (schedule version, round) */
    uint32_t delivered, lost;   /* round broadcasts from members it heard / lost to overlap (as receiver) */
} sim_node_t;

typedef struct {
    int sender, gen;
    int64_t start, end;
    uint8_t bytes[RB_MAX_BYTES];
    size_t len;
    bool round;
} sim_frame_t;

typedef struct {
    int64_t t;
    int kind;                   /* 1 delivery, 2 send callback */
    int node, frame, gen;
    int64_t slot_at;            /* send callback: the planned device time */
} sim_event_t;

typedef struct {
    int n;
    sim_node_t node[SIM_MAX_NODES];
    bool cut[SIM_MAX_NODES][SIM_MAX_NODES];   /* cut[a][b]: b does not hear a */
    sim_frame_t frame[SIM_FRAMES];
    int n_frames;
    sim_event_t ev[SIM_EVENTS];
    int n_ev;
    int64_t now;
    uint32_t hz, rng;
    int64_t rx_latency_us;
    uint32_t collisions;        /* round broadcasts lost because another frame overlapped them */
} sim_t;

static sim_t g_sim;

static uint32_t sim_rand(sim_t *sim)
{
    uint32_t x = sim->rng;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    sim->rng = x;
    return x;
}

static int64_t clock_of(const sim_node_t *n, int64_t T)
{
    return (int64_t)((double)T * (1.0 + n->ppm * 1e-6)) + n->offset;
}

static int64_t real_of(const sim_node_t *n, int64_t c)
{
    return (int64_t)ceil((double)(c - n->offset) / (1.0 + n->ppm * 1e-6));
}

static void sim_setup(sim_t *sim, int n, uint32_t hz, uint32_t seed)
{
    memset(sim, 0, sizeof *sim);
    sim->n = n;
    sim->hz = hz;
    sim->rng = seed ? seed : 1;
    sim->rx_latency_us = 30;
    for (int i = 0; i < n; i++) {
        sim_node_t *nd = &sim->node[i];
        nd->ppm = (double)((int)(sim_rand(sim) % 41) - 20);          /* +-20 ppm crystals */
        nd->offset = (int64_t)(sim_rand(sim) % 100000000u);          /* boot at different device times */
        nd->tx_latency_us = 150;
        nd->tx_jitter_us = 100;
    }
}

static void sim_rearm(sim_node_t *nd, int64_t now, bool force)
{
    rs_tx_t tx = rs_next(&nd->rs, now + 200);
    if (force || tx.kind != nd->armed.kind || tx.round != nd->armed.round || llabs(tx.at_us - nd->armed.at_us) >= 100) {
        nd->armed = tx;   /* node_main re-arms only when the time moved by 100 us or more */
    }
}

static void sim_after_call(sim_node_t *nd)
{
    uint32_t restarts = rs_take_restarts(&nd->rs);
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if (restarts >> i & 1u) {
            link_table_restart(&nd->links, i);
        }
    }
    bool member = nd->rs.state == RS_MEMBER;
    if (nd->was_member && !member) {
        nd->left_member = true;
    }
    nd->was_member = member;
}

static void sim_power_on(sim_t *sim, int i)
{
    sim_node_t *nd = &sim->node[i];
    uint8_t id[RB_NODE_ID_BYTES];
    make_id(id, i + 1);
    nd->on = true;
    nd->gen++;
    nd->was_member = false;
    link_table_init(&nd->links);
    int64_t now = clock_of(nd, sim->now);
    CHECK(rs_init(&nd->rs, id, sim->hz, RS_GUARD_DEFAULT_US, sim_rand(sim), now), "rs_init");
    sim_rearm(nd, now, true);
}

static void sim_power_off(sim_t *sim, int i)
{
    sim->node[i].on = false;
}

static void sim_push(sim_t *sim, sim_event_t e)
{
    if (sim->n_ev < SIM_EVENTS) {
        sim->ev[sim->n_ev++] = e;
    }
}

static void sim_fire(sim_t *sim, int i)
{
    sim_node_t *nd = &sim->node[i];
    int64_t now = clock_of(nd, sim->now);
    rs_tx_t tx = nd->armed;
    rb_broadcast_t b;
    if (rs_take(&nd->rs, &tx, now, &b)) {
        if (b.frame_type == RB_FRAME_ROUND) {
            b.n_reports = link_table_close_round(&nd->links, b.membership, b.node_index, b.reports);
            for (uint8_t k = 0; k < b.n_reports; k++) {
                b.reports[k].lateness = rs_take_lateness(&nd->rs, b.reports[k].transmitter_index);
            }
            uint32_t key = (uint32_t)b.schedule_version << 16 | b.round;
            int kept = nd->n_sent < SIM_SENT_KEPT ? nd->n_sent : SIM_SENT_KEPT;
            for (int k = 0; k < kept; k++) {
                if (nd->sent_keys[k] == key) {
                    nd->duplicate_round = true;
                }
            }
            nd->sent_keys[nd->n_sent++ % SIM_SENT_KEPT] = key;
        }
        sim_frame_t *f = &sim->frame[sim->n_frames++ % SIM_FRAMES];
        f->len = rb_encode(&b, f->bytes, sizeof f->bytes);
        CHECK(f->len > 0, "node %d built a frame the encoder refuses (type %u)", i, b.frame_type);
        f->sender = i;
        f->gen = nd->gen;
        f->round = b.frame_type == RB_FRAME_ROUND;
        f->start = sim->now + nd->tx_latency_us + (nd->tx_jitter_us ? (int64_t)(sim_rand(sim) % (uint32_t)nd->tx_jitter_us) : 0);
        f->end = f->start + rs_airtime_us(f->len);
        int fi = (sim->n_frames - 1) % SIM_FRAMES;
        for (int j = 0; j < sim->n; j++) {
            if (j != i) {
                sim_push(sim, (sim_event_t){f->end + sim->rx_latency_us, 1, j, fi, 0, 0});
            }
        }
        sim_push(sim, (sim_event_t){f->end + 20, 2, i, fi, nd->gen, tx.at_us});
    }
    sim_after_call(nd);
    sim_rearm(nd, now, true);
}

static bool overlaps(const sim_frame_t *a, const sim_frame_t *b)
{
    return a->start < b->end && b->start < a->end;
}

static void sim_deliver(sim_t *sim, const sim_event_t *e)
{
    sim_node_t *nd = &sim->node[e->node];
    const sim_frame_t *f = &sim->frame[e->frame];
    if (!nd->on || sim->cut[f->sender][e->node] || !sim->node[f->sender].on || sim->node[f->sender].gen != f->gen) {
        return;
    }
    int kept = sim->n_frames < SIM_FRAMES ? sim->n_frames : SIM_FRAMES;
    for (int k = 0; k < kept; k++) {
        const sim_frame_t *g = &sim->frame[k];
        if (g == f || !overlaps(f, g)) {
            continue;
        }
        if (g->sender == e->node || !sim->cut[g->sender][e->node]) {
            if (f->round && nd->rs.state == RS_MEMBER) {
                nd->lost++;
                if (g->sender != e->node) {
                    sim->collisions++;
                }
            }
            return;
        }
    }
    rb_broadcast_t b;
    CHECK(rb_decode(f->bytes, f->len, &b) == RB_OK, "a frame on air does not decode");
    int64_t now = clock_of(nd, sim->now);
    if (rs_heard(&nd->rs, now, &b) == RS_HEARD_MEMBER) {
        link_table_heard(&nd->links, b.node_index, now, -50, NULL, 0);
        nd->delivered++;
    }
    sim_after_call(nd);
    sim_rearm(nd, now, false);
}

/* Run until real time T_end. */
static void sim_run(sim_t *sim, int64_t T_end)
{
    for (;;) {
        int64_t best = T_end + 1;
        int who = -1, ev = -1;
        for (int i = 0; i < sim->n; i++) {
            if (sim->node[i].on) {
                int64_t T = real_of(&sim->node[i], sim->node[i].armed.at_us);
                if (T < best) {
                    best = T;
                    who = i;
                    ev = -1;
                }
            }
        }
        for (int k = 0; k < sim->n_ev; k++) {
            if (sim->ev[k].t < best) {
                best = sim->ev[k].t;
                ev = k;
                who = -1;
            }
        }
        if (best > T_end) {
            sim->now = T_end;
            return;
        }
        sim->now = best < sim->now ? sim->now : best;
        if (who >= 0) {
            sim_fire(sim, who);
            continue;
        }
        sim_event_t e = sim->ev[ev];
        sim->ev[ev] = sim->ev[--sim->n_ev];
        if (e.kind == 1) {
            sim_deliver(sim, &e);
        } else if (sim->node[e.node].on && sim->node[e.node].gen == e.gen) {
            sim_node_t *nd = &sim->node[e.node];
            rs_sent(&nd->rs, e.slot_at, clock_of(nd, e.t), sim->frame[e.frame].len);
        }
    }
}

/* Every powered node is a member of one schedule (same version and membership), with its own index, and the
 * membership is exactly the powered nodes. */
static bool sim_one_schedule(const sim_t *sim, int *members_out)
{
    const rs_t *ref = NULL;
    uint32_t indices = 0;
    int on = 0;
    for (int i = 0; i < sim->n; i++) {
        const sim_node_t *nd = &sim->node[i];
        if (!nd->on) {
            continue;
        }
        on++;
        if (nd->rs.state != RS_MEMBER) {
            return false;
        }
        if (!ref) {
            ref = &nd->rs;
        } else if (nd->rs.version != ref->version || nd->rs.membership != ref->membership) {
            return false;
        }
        if (indices >> nd->rs.my_index & 1u) {
            return false;
        }
        indices |= 1u << nd->rs.my_index;
    }
    if (members_out) {
        *members_out = on;
    }
    return ref && indices == ref->membership && (int)popcount(indices) == on;
}

/* Run in steps of one round until one schedule holds; the real time it took, or -1 within `limit_us`. */
static int64_t sim_until_one_schedule(sim_t *sim, int64_t limit_us)
{
    int64_t t0 = sim->now, period = 1000000 / sim->hz;
    while (sim->now - t0 <= limit_us) {
        if (sim_one_schedule(sim, NULL)) {
            return sim->now - t0;
        }
        sim_run(sim, sim->now + period);
    }
    return -1;
}

static void sim_reset_counts(sim_t *sim)
{
    sim->collisions = 0;
    for (int i = 0; i < sim->n; i++) {
        sim->node[i].delivered = sim->node[i].lost = 0;
    }
}

static bool any_left_member(const sim_t *sim)
{
    for (int i = 0; i < sim->n; i++) {
        if (sim->node[i].left_member) {
            return true;
        }
    }
    return false;
}

static bool any_duplicate_round(const sim_t *sim)
{
    for (int i = 0; i < sim->n; i++) {
        if (sim->node[i].duplicate_round) {
            return true;
        }
    }
    return false;
}

/* ---------------------------------------------------------------- joining */

static void two_nodes_powered_together_end_in_one_schedule(void)
{
    sim_t *sim = &g_sim;
    sim_setup(sim, 2, HZ, 7);
    sim_power_on(sim, 0);
    sim_power_on(sim, 1);
    int64_t took = sim_until_one_schedule(sim, 2000000);
    CHECK(took >= 0, "two nodes powered together share one schedule within 2 s");
    sim_run(sim, sim->now + 200000);
    sim_reset_counts(sim);
    sim_run(sim, sim->now + 1000000);
    CHECK(sim->node[0].delivered >= HZ - 1 && sim->node[1].delivered >= HZ - 1 && sim->collisions == 0,
          "then each hears the other every round (%u and %u in 1 s, %u collisions)", sim->node[0].delivered,
          sim->node[1].delivered, sim->collisions);
    CHECK(!any_duplicate_round(sim), "no node sends one round number twice in a schedule version");
}

/* A settled rig of n nodes, powered on over `spread_us`. */
static void settled_rig(sim_t *sim, int n, uint32_t hz, uint32_t seed, int64_t spread_us, int64_t limit_us)
{
    sim_setup(sim, n, hz, seed);
    for (int i = 0; i < n; i++) {
        sim_run(sim, sim->now + (spread_us ? (int64_t)(sim_rand(sim) % (uint32_t)(spread_us / n + 1)) : 0));
        sim_power_on(sim, i);
    }
    int64_t took = sim_until_one_schedule(sim, limit_us);
    CHECK(took >= 0, "%d nodes settle into one schedule within %lld s", n, (long long)(limit_us / 1000000));
    sim_run(sim, sim->now + 5 * 1000000 / hz);
    for (int i = 0; i < n; i++) {
        sim->node[i].left_member = false;
    }
}

static uint8_t index_of(const sim_t *sim, int node)
{
    return sim->node[node].rs.my_index;
}

static void a_node_powered_on_into_a_running_round_joins_within_a_few_rounds(void)
{
    sim_t *sim = &g_sim;
    settled_rig(sim, 6, HZ, 11, 0, 4000000);
    sim_power_off(sim, 5);
    sim_run(sim, sim->now + 1000000);   /* the rig runs without node 5 */
    CHECK(sim_one_schedule(sim, NULL) && popcount(sim->node[0].rs.membership) == 5, "five members without node 5");
    int64_t worst = 0;
    for (int trial = 0; trial < 10; trial++) {
        sim_power_on(sim, 5);
        int64_t t0 = sim->now;
        while (sim->node[5].rs.state != RS_MEMBER && sim->now - t0 < 2000000) {
            sim_run(sim, sim->now + 1000);
        }
        int64_t took = sim->now - t0;
        worst = took > worst ? took : worst;
        CHECK(sim_until_one_schedule(sim, 500000) >= 0, "after joining, all six share one schedule (trial %d)", trial);
        sim_run(sim, sim->now + 500000);
        sim_power_off(sim, 5);
        sim_run(sim, sim->now + 500000);
    }
    /* it listens up to 7 rounds before it may found; it hears the rig in its first round; then a request, a grant */
    CHECK(worst <= 12 * P_US, "a powered-on node is a member within 12 rounds (worst of 10: %.1f rounds)",
          (double)worst / P_US);
    CHECK(!any_left_member(sim), "no member of the running round restarted or left while a node joined");
    CHECK(!any_duplicate_round(sim), "no node sends one round number twice in a schedule version");
}

static void a_node_missing_for_three_rounds_is_dropped(void)
{
    sim_t *sim = &g_sim;
    settled_rig(sim, 5, HZ, 23, 0, 4000000);
    sim_power_off(sim, 3);
    uint8_t dropped_index = index_of(sim, 3);
    int64_t t0 = sim->now;
    while ((sim->node[0].rs.membership >> dropped_index & 1u) && sim->now - t0 < 1000000) {
        sim_run(sim, sim->now + 1000);
    }
    int64_t took = sim->now - t0;
    CHECK(took > 3 * P_US && took <= 5 * P_US, "dropped after 3 missed rounds, before the 5th (%.1f rounds)",
          (double)took / P_US);
    CHECK(sim_until_one_schedule(sim, 3 * P_US) >= 0, "every remaining node takes the new schedule");
    CHECK(!any_left_member(sim), "nobody else restarted");
    sim_reset_counts(sim);
    sim_run(sim, sim->now + 1000000);
    CHECK(sim->collisions == 0 && sim->node[0].delivered >= 3 * (HZ - 1), "the four remaining nodes run on cleanly");
}

static void when_the_scheduler_dies_the_next_member_takes_over(void)
{
    sim_t *sim = &g_sim;
    settled_rig(sim, 5, HZ, 31, 0, 4000000);
    int scheduler = -1;
    for (int i = 0; i < 5; i++) {
        if (rs_slot_of(sim->node[i].rs.membership, index_of(sim, i)) == 0) {
            scheduler = i;
        }
    }
    CHECK(scheduler >= 0, "one node is in slot 0");
    sim_power_off(sim, scheduler);
    CHECK(sim_until_one_schedule(sim, 8 * P_US) >= 0, "the other four agree on a schedule without it within 8 rounds");
    CHECK(!any_left_member(sim), "nobody restarted");
    /* the new scheduler still admits nodes */
    sim_power_on(sim, scheduler);
    CHECK(sim_until_one_schedule(sim, 20 * P_US) >= 0, "the old scheduler rejoins as an ordinary member");
}

static void unplugging_a_node_and_plugging_it_back_restarts_nobody_and_its_reports_resume(void)
{
    sim_t *sim = &g_sim;
    settled_rig(sim, 3, HZ, 41, 0, 3000000);
    sim_power_off(sim, 2);
    sim_run(sim, sim->now + 2000000);
    sim_power_on(sim, 2);
    CHECK(sim_until_one_schedule(sim, 1000000) >= 0, "node 2 is back in the round within 1 s");
    sim_run(sim, sim->now + 500000);
    CHECK(!any_left_member(sim), "nodes 0 and 1 never left the round");
    rb_report_t reports[RB_MAX_REPORTS];
    link_table_t copy = sim->node[0].links;
    uint8_t n = link_table_close_round(&copy, sim->node[0].rs.membership, index_of(sim, 0), reports);
    bool heard_2 = false;
    for (uint8_t k = 0; k < n; k++) {
        if (reports[k].transmitter_index == index_of(sim, 2)) {
            heard_2 = reports[k].heard >= 10 && reports[k].since == 0;
        }
    }
    CHECK(heard_2, "node 0's link reports about node 2 show it heard again");
}

int main(void)
{
    a_lone_node_listens_then_founds_its_own_schedule();
    two_nodes_powered_together_end_in_one_schedule();
    a_node_powered_on_into_a_running_round_joins_within_a_few_rounds();
    a_node_missing_for_three_rounds_is_dropped();
    when_the_scheduler_dies_the_next_member_takes_over();
    unplugging_a_node_and_plugging_it_back_restarts_nobody_and_its_reports_resume();
    printf("round_scheduler: %d failures\n", failures);
    return failures ? 1 : 0;
}
