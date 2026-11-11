/*
 * The fixed round: N members, node index = slot, a fixed round rate, and slot timing taken from any heard round
 * broadcast. Tested on its own and with two simulated nodes whose clocks start out of phase and drift apart, with a
 * receive latency between them: they must settle into their own slots, agree on round numbers, and stay there.
 */
#include <math.h>
#include <stdio.h>

#include "../fixed_round.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static void lone_node(void)
{
    fixed_round_t r;
    CHECK(fixed_round_init(&r, 1, 2, 40, 1000000), "index 1 of 2 at 40 Hz is valid");
    CHECK(r.period_us == 25000 && r.slot_us == 12500, "40 Hz, 2 slots: 25 ms rounds, 12.5 ms slots");
    CHECK(fixed_round_membership(&r) == 0x3, "membership is the fixed members");
    uint16_t round, prev_round = 0;
    int64_t t = 1000000, prev = 0;
    for (int k = 0; k < 5; k++) {
        t = fixed_round_next_tx(&r, t, &round);
        if (k) {
            CHECK(t - prev == 25000 && (uint16_t)(round - prev_round) == 1, "alone: one broadcast per round");
        }
        prev = t;
        prev_round = round;
    }
    CHECK(!fixed_round_init(&r, 2, 2, 40, 0), "index outside the members is refused");
    CHECK(!fixed_round_init(&r, 0, 1, 40, 0), "a round needs two members");
    CHECK(!fixed_round_init(&r, 0, 33, 40, 0), "at most 32 members");
    CHECK(!fixed_round_init(&r, 0, 32, 40, 0), "slots shorter than the minimum are refused (32 x 40 Hz)");
}

static void first_heard_sets_the_phase(void)
{
    fixed_round_t r;
    fixed_round_init(&r, 1, 2, 40, 0);
    fixed_round_heard(&r, 5003000, 777, 0, 2);   /* node 0's broadcast of round 777 */
    uint16_t round;
    int64_t t = fixed_round_next_tx(&r, 5003000, &round);
    CHECK(t == 5003000 + 12500 && round == 777, "node 1 sends one slot after node 0, in the same round (got %lld, %u)",
          (long long)t, round);
    fixed_round_heard(&r, 5003000 + 3000, 778, 0, 4);
    CHECK(r.ignored == 1, "a broadcast from a round with another slot count is ignored for timing");
}

/* Two nodes in real time T (us). Clock of node i: c = T * (1 + ppm_i * 1e-6) + offset_i. */
typedef struct {
    fixed_round_t r;
    double ppm, offset;
    double next_T;        /* real time of its next broadcast */
    uint16_t next_round;
} sim_node_t;

static double clock_of(const sim_node_t *n, double T) { return T * (1 + n->ppm * 1e-6) + n->offset; }
static double real_of(const sim_node_t *n, double c) { return (c - n->offset) / (1 + n->ppm * 1e-6); }

static void plan(sim_node_t *n, double after_T)
{
    int64_t c = fixed_round_next_tx(&n->r, (int64_t)clock_of(n, after_T), &n->next_round);
    n->next_T = real_of(n, (double)c);
}

static void two_nodes(double ppm_b, double offset_b, double latency_us)
{
    sim_node_t n[2] = {{.ppm = 0, .offset = 1e6}, {.ppm = ppm_b, .offset = offset_b}};
    fixed_round_init(&n[0].r, 0, 2, 40, (int64_t)clock_of(&n[0], 0));
    fixed_round_init(&n[1].r, 1, 2, 40, (int64_t)clock_of(&n[1], 0));
    plan(&n[0], 0);
    plan(&n[1], 0);
    double last_T[2] = {-1, -1};
    uint16_t last_round[2] = {0, 0};
    int bad_gaps = 0, round_mismatch = 0, sent = 0;
    double first_T = -1, settled_T = -1;
    for (int step = 0; step < 4000; step++) {
        int i = n[0].next_T <= n[1].next_T ? 0 : 1, o = 1 - i;
        double T = n[i].next_T;
        uint16_t round = n[i].next_round;
        sent++;
        if (step >= 40) {            /* settled: a gap to the other node's last broadcast must be one slot */
            double gap = T - last_T[o];
            if (fabs(gap - 12500) > 2500) {
                bad_gaps++;
            }
            if (i == 1 && round != last_round[0]) {
                round_mismatch++;
            }
            if (settled_T < 0) {
                settled_T = T;
                first_T = step;
            }
        }
        last_T[i] = T;
        last_round[i] = round;
        /* the other node hears it latency_us later, by its own clock */
        fixed_round_heard(&n[o].r, (int64_t)clock_of(&n[o], T + latency_us), round, (uint8_t)i, 2);
        plan(&n[o], T + latency_us);
        plan(&n[i], T);
    }
    double rounds = (sent - first_T) / 2.0;
    double period = (last_T[0] - settled_T) / rounds;
    CHECK(bad_gaps == 0, "ppm %.0f offset %.0f latency %.0f: %d broadcasts outside their slot after settling", ppm_b,
          offset_b, latency_us, bad_gaps);
    CHECK(round_mismatch == 0, "ppm %.0f offset %.0f: %d rounds where the nodes disagree on the round number", ppm_b,
          offset_b, round_mismatch);
    CHECK(fabs(period - 25000) < 25000 * 0.03, "the round stays near 25 ms: %.0f us", period);
}

int main(void)
{
    lone_node();
    first_heard_sets_the_phase();
    two_nodes(0, 1e6 + 3000, 300);        /* node 1 boots 3 ms after node 0: it starts inside node 0's slot */
    two_nodes(0, 1e6 + 12000, 300);
    two_nodes(50, 7e6, 300);              /* 50 ppm apart, unrelated boot times */
    two_nodes(-80, 2.5e6, 800);           /* slow receive path */
    two_nodes(20, 1e6 + 12499, 0);
    printf("fixed_round: %d failures\n", failures);
    return failures ? 1 : 0;
}
