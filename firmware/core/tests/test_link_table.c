/*
 * The link table turns what a transceiver heard in a round into its link reports (docs/round-broadcast.md): one per
 * other member in ascending node index, with the score code of the C scorer, the last RSSI, the heard count over the
 * last 15 rounds and the rounds since last heard, and the warming flag while there is no score.
 */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../link_table.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

#define N_SUB 52
#define ROUND_US 25000   /* 40 Hz */

static link_table_t table;
static motion_score_link_t reference;

static void amplitudes(float *amp, int round, float swing)
{
    for (int i = 0; i < N_SUB; i++) {
        amp[i] = 20.0f + (float)(i % 7) + swing * sinf(0.9f * (float)round + 0.3f * (float)i);
    }
}

static const rb_report_t *report_for(const rb_report_t *reports, uint8_t n, uint8_t transmitter)
{
    for (uint8_t k = 0; k < n; k++) {
        if (reports[k].transmitter_index == transmitter) {
            return &reports[k];
        }
    }
    return NULL;
}

int main(void)
{
    rb_report_t reports[RB_MAX_REPORTS];
    float amp[N_SUB];
    const uint32_t three = 0x7;   /* members 0, 1 and 2; this node is index 1 */
    link_table_init(&table);
    motion_score_init(&reference);

    /* Never heard anyone: every other member gets a report saying so. */
    uint8_t n = link_table_close_round(&table, three, 1, reports);
    CHECK(n == 2 && reports[0].transmitter_index == 0 && reports[1].transmitter_index == 2,
          "one report per other member, ascending");
    CHECK(reports[0].heard == 0 && reports[0].since == RB_COUNT_MAX && reports[0].rssi == RB_RSSI_NONE
          && reports[0].score == 0 && (reports[0].flags & RB_FLAG_WARMING), "never heard: no signal, no score");

    /* Node 0 heard every round for 2 s; node 2 never. */
    int64_t t = 1000000;
    for (int r = 0; r < 80; r++, t += ROUND_US) {
        amplitudes(amp, r, 4.0f);
        link_table_heard(&table, 0, t, -48, amp, N_SUB);
        motion_score_push(&reference, t, amp, N_SUB);
        n = link_table_close_round(&table, three, 1, reports);
        if (r == 10) {
            const rb_report_t *a = report_for(reports, n, 0);
            CHECK(a && a->heard == 11 && a->since == 0 && a->score == 0 && (a->flags & RB_FLAG_WARMING),
                  "after 11 rounds: heard 11, still warming (no full window)");
        }
    }
    const rb_report_t *a = report_for(reports, n, 0), *c = report_for(reports, n, 2);
    float expected;
    CHECK(motion_score_get(&reference, &expected), "the reference scorer has a score");
    CHECK(a && a->heard == 15 && a->since == 0 && a->rssi == -48 && a->flags == 0, "heard every round: 15, 0, rssi");
    CHECK(a && a->score == motion_score_encode(expected) && a->score > 1, "the score code is the scorer's: %u",
          a ? a->score : 0);
    CHECK(c && c->heard == 0 && c->since == RB_COUNT_MAX, "node 2 still never heard");

    /* Node 0 misses 3 rounds: since counts them, heard drops by 3. */
    for (int r = 0; r < 3; r++) {
        n = link_table_close_round(&table, three, 1, reports);
    }
    a = report_for(reports, n, 0);
    CHECK(a && a->since == 3 && a->heard == 12 && a->rssi == -48, "3 missed rounds: since 3, heard 12, last rssi kept");

    /* Heard twice in one round (a duplicate): still one round heard. A frame with no CSI still counts as heard. */
    link_table_heard(&table, 0, t, -50, amp, N_SUB);
    link_table_heard(&table, 0, t + 100, -51, NULL, 0);
    n = link_table_close_round(&table, three, 1, reports);
    a = report_for(reports, n, 0);
    CHECK(a && a->since == 0 && a->heard == 12 && a->rssi == -51, "one round heard however many frames");

    /* Gone for good: after 15 rounds the heard count is 0 and since saturates. */
    for (int r = 0; r < 20; r++) {
        n = link_table_close_round(&table, three, 1, reports);
    }
    a = report_for(reports, n, 0);
    CHECK(a && a->heard == 0 && a->since == RB_COUNT_MAX, "gone: heard 0, since 15");

    /* Reports follow the membership: a two-member round has one report; this node's own index never has one. */
    link_table_heard(&table, 1, t, -40, amp, N_SUB);
    n = link_table_close_round(&table, 0x3, 0, reports);
    CHECK(n == 1 && reports[0].transmitter_index == 1 && reports[0].heard == 1, "two members: one report");
    n = link_table_close_round(&table, 0x3, 1, reports);
    CHECK(n == 1 && reports[0].transmitter_index == 0, "this node's own index has no report");

    /* Built reports encode as a valid round broadcast. */
    rb_broadcast_t b = {0};
    uint8_t bytes[RB_MAX_BYTES];
    b.version = RB_VERSION;
    b.frame_type = RB_FRAME_ROUND;
    b.membership = three;
    b.node_index = 1;
    b.n_reports = link_table_close_round(&table, three, 1, b.reports);
    CHECK(rb_encode(&b, bytes, sizeof bytes) == RB_HEADER_BYTES + 2 * RB_REPORT_BYTES, "the reports encode");

    printf("link_table: %d failures\n", failures);
    return failures ? 1 : 0;
}
