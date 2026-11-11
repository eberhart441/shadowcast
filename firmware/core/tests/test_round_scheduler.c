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
    bool stale_round;           /* a frame carrying its serial broke the freshness rule of #16 */
    bool id_sent_any;
    uint16_t id_last_round;     /* the last frame carrying its serial (its own, or a grant naming it) */
    int64_t id_last_T;
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
    nd->id_sent_any = false;   /* a power cycle may restart the count (the 2 s loss of #16 is expected) */
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
        /* #16's freshness rule, per serial: within 2 s, 1 to 16 + 1/ms rounds ahead of the last one */
        int named = -1;
        for (int j = 0; j < sim->n; j++) {
            uint8_t id[RB_NODE_ID_BYTES];
            make_id(id, j + 1);
            if (memcmp(id, b.node_id, RB_NODE_ID_BYTES) == 0) {
                named = j;
            }
        }
        if (named >= 0) {
            sim_node_t *x = &sim->node[named];
            int64_t gap = sim->now - x->id_last_T;
            int16_t ahead = (int16_t)(uint16_t)(b.round - x->id_last_round);
            if (x->id_sent_any && gap <= 2000000 && (ahead < 1 || ahead > 16 + gap / 1000)) {
                x->stale_round = true;
            }
            x->id_sent_any = true;
            x->id_last_round = b.round;
            x->id_last_T = sim->now;
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

/* A node sent one round number twice in a schedule version, or a frame with its serial was not fresh (#16). */
static bool any_duplicate_round(const sim_t *sim)
{
    for (int i = 0; i < sim->n; i++) {
        if (sim->node[i].duplicate_round || sim->node[i].stale_round) {
            return true;
        }
    }
    return false;
}

static bool any_stale_round(const sim_t *sim)
{
    for (int i = 0; i < sim->n; i++) {
        if (sim->node[i].stale_round) {
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
    printf("round_scheduler: a node powered on into a running round of 5 joined within %.1f rounds (worst of 10)\n",
           (double)worst / P_US);
    CHECK(!any_left_member(sim), "no member of the running round restarted or left while a node joined");
    CHECK(!any_duplicate_round(sim), "no node sends one round number twice in a schedule version");
}

static void a_node_missing_for_three_rounds_is_dropped(void)
{
    sim_t *sim = &g_sim;
    settled_rig(sim, 5, HZ, 23, 0, 4000000);
    int sent = sim->node[3].n_sent;
    while (sim->node[3].n_sent == sent) {   /* switch it off just after its round broadcast has been heard */
        sim_run(sim, sim->now + 100);
    }
    sim_run(sim, sim->now + 2000);
    int64_t since_last = 2000;
    sim_power_off(sim, 3);
    uint8_t dropped_index = index_of(sim, 3);
    int64_t t0 = sim->now;
    while ((sim->node[0].rs.membership >> dropped_index & 1u) && sim->now - t0 < 1000000) {
        sim_run(sim, sim->now + 1000);
    }
    int64_t took = sim->now - t0 + since_last;
    CHECK(took > 3 * P_US && took <= 4 * P_US + P_US / 10,
          "dropped once 3 of its slots went unheard, at the scheduler's next slot (%.1f rounds after its last)",
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

/* ---------------------------------------------------------------- merging, drift, scale */

static void split_groups_merge_into_the_schedule_with_the_lower_scheduler_id(void)
{
    sim_t *sim = &g_sim;
    sim_setup(sim, 8, HZ, 53);
    for (int a = 0; a < 8; a++) {
        for (int b = 0; b < 8; b++) {
            sim->cut[a][b] = (a < 4) != (b < 4);   /* nodes 0-3 and 4-7 cannot hear each other */
        }
    }
    for (int i = 7; i >= 0; i--) {
        sim_power_on(sim, i);
        sim_run(sim, sim->now + 3000);
    }
    sim_run(sim, sim->now + 3000000);
    CHECK(sim->node[0].rs.state == RS_MEMBER && sim->node[4].rs.state == RS_MEMBER
              && popcount(sim->node[0].rs.membership) == 4 && popcount(sim->node[4].rs.membership) == 4,
          "while split, each half runs its own schedule of four");
    uint8_t low[RB_NODE_ID_BYTES], high[RB_NODE_ID_BYTES];
    const rs_t *g1 = &sim->node[0].rs, *g2 = &sim->node[4].rs;
    bool g1_lower = memcmp(g1->sched_id, g2->sched_id, RB_NODE_ID_BYTES) < 0;
    memcpy(low, g1_lower ? g1->sched_id : g2->sched_id, RB_NODE_ID_BYTES);
    memcpy(high, g1_lower ? g2->sched_id : g1->sched_id, RB_NODE_ID_BYTES);
    memset(sim->cut, 0, sizeof sim->cut);   /* the halves come into range */
    int64_t took = sim_until_one_schedule(sim, 5000000);
    CHECK(took >= 0, "the two halves merge into one schedule of eight within 5 s (%.1f rounds)",
          (double)took / P_US);
    bool low_wins = true;
    for (int i = 0; i < 8; i++) {
        low_wins = low_wins && memcmp(sim->node[i].rs.sched_id, low, RB_NODE_ID_BYTES) == 0;
    }
    CHECK(low_wins, "the merged schedule is the one with the lower scheduler ID");
    CHECK(!any_duplicate_round(sim), "no node sends one round number twice in a schedule version");
    sim_reset_counts(sim);
    sim_run(sim, sim->now + 1000000);
    CHECK(sim->collisions == 0, "after the merge no round broadcasts collide (%u)", sim->collisions);
    (void)high;
}

static void drifting_clocks_stay_in_their_slots(void)
{
    sim_t *sim = &g_sim;
    settled_rig(sim, 8, HZ, 67, 0, 4000000);
    sim->node[0].ppm = 40;    /* the worst crystals of two boards, far apart */
    sim->node[7].ppm = -40;
    sim_reset_counts(sim);
    sim_run(sim, sim->now + 120 * 1000000LL);   /* two minutes: 9.6 ms of relative drift if nothing corrected it */
    uint32_t delivered = 0, lost = 0;
    for (int i = 0; i < 8; i++) {
        delivered += sim->node[i].delivered;
        lost += sim->node[i].lost;
    }
    CHECK(sim->collisions == 0 && lost == 0, "no round broadcast is lost to overlap in two minutes (%u lost)", lost);
    CHECK(delivered >= (uint32_t)(8 * 7 * HZ * 120) * 999 / 1000, "every link is heard every round (%u)", delivered);
    CHECK(sim_one_schedule(sim, NULL) && !any_left_member(sim), "still one schedule, nobody restarted");
}

/* Report codes about each member, as the receiving nodes would send them (glue: one per round per link). */
static void lateness_reported_on_links(sim_t *sim, int receiver, uint8_t *lo, uint8_t *hi)
{
    *lo = RS_LATENESS_MAX_CODE;
    *hi = 0;
    rs_t copy = sim->node[receiver].rs;
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if ((copy.membership >> i & 1u) && i != copy.my_index) {
            uint8_t c = rs_take_lateness(&copy, i);
            if (c != RS_LATENESS_NONE) {
                *lo = c < *lo ? c : *lo;
                *hi = c > *hi ? c : *hi;
            }
        }
    }
}

static void slot_lateness_shows_how_late_each_node_starts_in_its_slot(void)
{
    CHECK(rs_lateness_code(0) == 1 && rs_lateness_code(31) == 1 && rs_lateness_code(32) == 2
              && rs_lateness_code(-50) == 1 && rs_lateness_code(959) == 30 && rs_lateness_code(5000) == 31,
          "lateness codes are 32 us steps from 1, saturating at 31");
    sim_t *sim = &g_sim;
    sim_setup(sim, 4, HZ, 71);
    for (int i = 0; i < 4; i++) {
        sim->node[i].tx_latency_us = 200;   /* every node starts on air 200 to 220 us after its slot */
        sim->node[i].tx_jitter_us = 20;
    }
    for (int i = 0; i < 4; i++) {
        sim_power_on(sim, i);
    }
    CHECK(sim_until_one_schedule(sim, 3000000) >= 0, "four nodes settle");
    sim_run(sim, sim->now + 2000000);
    uint8_t lo, hi;
    lateness_reported_on_links(sim, 0, &lo, &hi);
    /* each node compensates for its own send latency, so a node like the others reads about that latency */
    CHECK(lo >= rs_lateness_code(120) && hi <= rs_lateness_code(300),
          "with 200 us send latency everywhere, lateness reads 120 to 300 us (codes %u..%u)", lo, hi);
    sim->node[3].tx_latency_us = 600;   /* one node starts sending 400 us later than the others */
    sim_run(sim, sim->now + 2000000);
    rs_t copy = sim->node[0].rs;
    uint8_t late3 = rs_take_lateness(&copy, index_of(sim, 3)), late1 = rs_take_lateness(&copy, index_of(sim, 1));
    CHECK(late3 >= late1 + 5, "the late node reads later than the others (codes %u vs %u)", late3, late1);
}

static void thirty_nodes_powered_at_once_fill_a_full_round(void)
{
    sim_t *sim = &g_sim;
    uint32_t hz = 20;
    int64_t period = 1000000 / hz;
    sim_setup(sim, 30, hz, 89);
    CHECK(rs_max_members(hz, RS_GUARD_DEFAULT_US) >= 30, "20 Hz fits 30 members (%u)", rs_max_members(hz, 250));
    CHECK(rs_max_members(40, RS_GUARD_DEFAULT_US) >= 16, "40 Hz fits 16 members (%u)", rs_max_members(40, 250));
    for (int i = 0; i < 30; i++) {   /* all switched on within half a second */
        sim_run(sim, sim->now + (int64_t)(sim_rand(sim) % 33000u));
        sim_power_on(sim, i);
    }
    int64_t took = sim_until_one_schedule(sim, 60 * 1000000LL);
    CHECK(took >= 0, "30 nodes switched on together share one schedule within 60 s (%.1f s)", (double)took / 1e6);
    sim_run(sim, sim->now + 10 * period);
    sim_reset_counts(sim);
    int64_t seconds = 10;
    sim_run(sim, sim->now + seconds * 1000000);
    uint32_t delivered = 0, lost = 0;
    for (int i = 0; i < 30; i++) {
        delivered += sim->node[i].delivered;
        lost += sim->node[i].lost;
    }
    uint32_t expected = (uint32_t)(30 * 29 * hz * seconds);
    CHECK(delivered >= expected * 999 / 1000 && sim->collisions == 0,
          "in a full round every node hears all 29 others every round (%u of %u, %u lost)", delivered, expected, lost);
    CHECK(popcount(sim->node[0].rs.membership) == 30 && rs_slot_of(sim->node[0].rs.membership, 31) == 30,
          "30 members, slots 0 to 29 and the join slot");
    CHECK(!any_duplicate_round(sim), "no node sends one round number twice in a schedule version");
    printf("round_scheduler: 30 nodes settled in %.1f s\n", (double)took / 1e6);
}

static void the_scheduler_admits_no_more_members_than_the_slots_hold(void)
{
    sim_t *sim = &g_sim;
    uint8_t max = rs_max_members(HZ, RS_GUARD_DEFAULT_US);
    sim_setup(sim, max + 2, HZ, 97);
    for (int i = 0; i < sim->n; i++) {
        sim_power_on(sim, i);
        sim_run(sim, sim->now + 1000);
    }
    sim_run(sim, sim->now + 40 * 1000000LL);
    int members = 0, joining = 0;
    for (int i = 0; i < sim->n; i++) {
        members += sim->node[i].rs.state == RS_MEMBER;
        joining += sim->node[i].rs.state == RS_JOINING;
    }
    CHECK(members == max && joining == 2 && popcount(sim->node[0].rs.membership) == max,
          "at 40 Hz the round holds %u members; the other two keep asking (%d members, %d joining)", max, members,
          joining);
    sim_reset_counts(sim);
    sim_run(sim, sim->now + 1000000);
    CHECK(sim->collisions == 0, "the full round runs without collisions (%u)", sim->collisions);
}

static void round_numbers_only_count_forward_through_joins_merges_and_failover(void)
{
    /* #16 drops a broadcast whose round number is not 1 to 16 + 1/ms ahead of its sender's last one (within 2 s),
     * so a node's numbers must never step back or leap, whatever happens to its schedule */
    sim_t *sim = &g_sim;
    sim_setup(sim, 6, HZ, 101);
    for (int a = 0; a < 6; a++) {
        for (int b = 0; b < 6; b++) {
            sim->cut[a][b] = (a < 3) != (b < 3);
        }
    }
    for (int i = 0; i < 6; i++) {
        sim_power_on(sim, i);
        sim_run(sim, sim->now + 7000);
    }
    sim_run(sim, sim->now + 2000000);
    memset(sim->cut, 0, sizeof sim->cut);                 /* merge */
    CHECK(sim_until_one_schedule(sim, 5000000) >= 0, "the halves merge");
    sim_run(sim, sim->now + 500000);
    for (int i = 0; i < 6; i++) {                          /* fail the scheduler over */
        if (sim->node[i].rs.my_index == 0) {
            sim_power_off(sim, i);
        }
    }
    CHECK(sim_until_one_schedule(sim, 1000000) >= 0, "failover");
    sim_run(sim, sim->now + 1000000);
    CHECK(!any_stale_round(sim), "every frame carrying a node's serial counts 1 to 16 + 1/ms rounds on from the last");
}

int main(void)
{
    a_lone_node_listens_then_founds_its_own_schedule();
    two_nodes_powered_together_end_in_one_schedule();
    a_node_powered_on_into_a_running_round_joins_within_a_few_rounds();
    a_node_missing_for_three_rounds_is_dropped();
    when_the_scheduler_dies_the_next_member_takes_over();
    unplugging_a_node_and_plugging_it_back_restarts_nobody_and_its_reports_resume();
    split_groups_merge_into_the_schedule_with_the_lower_scheduler_id();
    round_numbers_only_count_forward_through_joins_merges_and_failover();
    drifting_clocks_stay_in_their_slots();
    slot_lateness_shows_how_late_each_node_starts_in_its_slot();
    thirty_nodes_powered_at_once_fill_a_full_round();
    the_scheduler_admits_no_more_members_than_the_slots_hold();
    printf("round_scheduler: %d failures\n", failures);
    return failures ? 1 : 0;
}
