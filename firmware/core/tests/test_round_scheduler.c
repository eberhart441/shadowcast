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

static unsigned __attribute__((unused)) popcount(uint32_t m)
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

int main(void)
{
    a_lone_node_listens_then_founds_its_own_schedule();
    printf("round_scheduler: %d failures\n", failures);
    return failures ? 1 : 0;
}
