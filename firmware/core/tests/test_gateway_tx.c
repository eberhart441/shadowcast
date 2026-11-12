/*
 * The gateway's sending side (raw mode, ticket #17), at its seam: heard round broadcasts, queued frames and the
 * device clock in; when to send and what out. The frames must go out in the join slot of a round the scheduler
 * numbers even (where joiners ask, never a member's slot, never a grant's round), at most one per round.
 */
#include <stdio.h>
#include <string.h>

#include "../gateway_tx.h"
#include "../round_scheduler.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

#define PERIOD 50000             /* 20 Hz */
#define MEMBERS 8
#define SLOTS (MEMBERS + 1)
#define SLOT (PERIOD / SLOTS)
#define LATENCY 200              /* the scheduler's send latency: on air this long after its slot starts */

static const uint8_t SCHED[6] = {0x02, 0, 0, 0, 0, 0x01};
static const uint8_t OTHER[6] = {0x02, 0, 0, 0, 0, 0x07};
static const uint8_t FRAME[47] = {2, 4, 42};

/* Slot 0's round broadcast of round `round`, which starts at `start`: heard when it ends on air. */
static void hear_slot0(gt_t *g, int64_t start, uint16_t round, const uint8_t *id, uint8_t members)
{
    rb_broadcast_t b;
    memset(&b, 0, sizeof b);
    b.version = RB_VERSION;
    b.frame_type = RB_FRAME_ROUND;
    b.round = round;
    b.slot = 0;
    b.slots_per_round = (uint8_t)(members + 1);
    b.node_index = 0;
    b.membership = (1u << members) - 1;
    memcpy(b.node_id, id, 6);
    b.len = (uint16_t)rb_size(b.membership);
    gt_heard(g, start + LATENCY + rs_airtime_us(b.len) + 30, &b);
}

static void it_waits_for_the_round_period_then_sends_in_an_even_rounds_join_slot(void)
{
    gt_t g;
    uint8_t out[GT_FRAME_MAX];
    gt_init(&g);
    CHECK(gt_queue(&g, FRAME, sizeof FRAME, 0), "a frame is queued");
    CHECK(gt_next(&g, 0) < 0, "no schedule heard: nothing planned");
    hear_slot0(&g, 1000000, 100, SCHED, MEMBERS);
    CHECK(gt_next(&g, 1001000) < 0, "one slot 0 heard: the round period is not known yet");
    hear_slot0(&g, 1000000 + PERIOD, 101, SCHED, MEMBERS);
    CHECK(gt_next(&g, 1000000 + PERIOD + 1000) < 0, "round 101 is odd: its join slot is the grant's");
    int64_t start = 1000000 + 2 * PERIOD;
    hear_slot0(&g, start, 102, SCHED, MEMBERS);
    int64_t at = gt_next(&g, start + 1000);
    int64_t join = start + LATENCY + (int64_t)(SLOTS - 1) * PERIOD / SLOTS;
    CHECK(at >= join && at <= join + 50, "round 102: the join slot (%lld, expected %lld)", (long long)at,
          (long long)join);
    CHECK(at - (start + LATENCY) >= (int64_t)MEMBERS * SLOT - 20, "after every member's slot");
    CHECK(at + rs_airtime_us(sizeof FRAME) + RS_GUARD_DEFAULT_US <= start + PERIOD,
          "and the request is off the air before the next round");
    CHECK(gt_take(&g, at - 500, out, sizeof out) == 0, "not before its time");
    CHECK(gt_take(&g, at + 30, out, sizeof out) == sizeof FRAME && memcmp(out, FRAME, sizeof FRAME) == 0,
          "sent as queued");
    CHECK(gt_next(&g, at + 100) < 0 && g.count.sent == 1, "the queue is empty");
}

static void one_frame_per_round_and_never_late(void)
{
    gt_t g;
    uint8_t out[GT_FRAME_MAX];
    gt_init(&g);
    for (int k = 0; k < 3; k++) {
        CHECK(gt_queue(&g, FRAME, sizeof FRAME, 0), "queued %d", k);
    }
    int64_t start = 1000000;   /* frames are queued at 0: well within GT_STALE_US */
    hear_slot0(&g, start - 2 * PERIOD, 200, SCHED, MEMBERS);
    hear_slot0(&g, start, 202, SCHED, MEMBERS);
    int64_t at = gt_next(&g, start + 1000);
    CHECK(gt_take(&g, at + 10, out, sizeof out) == sizeof FRAME, "the first goes out");
    CHECK(gt_next(&g, at + 50) < 0 && gt_take(&g, at + 60, out, sizeof out) == 0, "one per round");
    hear_slot0(&g, start + 2 * PERIOD, 204, SCHED, MEMBERS);
    at = gt_next(&g, start + 2 * PERIOD + 1000);
    CHECK(gt_take(&g, at + GT_LATE_US + 1, out, sizeof out) == 0, "a timer that fired more than %d us late sends "
          "nothing (the join slot may be over)", GT_LATE_US);
    hear_slot0(&g, start + 4 * PERIOD, 206, SCHED, MEMBERS);
    at = gt_next(&g, start + 4 * PERIOD + 1000);
    CHECK(gt_take(&g, at, out, sizeof out) == sizeof FRAME && g.count.sent == 2, "it waits for the next even round");
    CHECK(!gt_queue(&g, FRAME, GT_FRAME_MAX + 1, 0), "a frame longer than a raw mode request is refused");
}

static void frames_nobody_could_send_expire(void)
{
    gt_t g;
    gt_init(&g);
    for (int k = 0; k < GT_QUEUE; k++) {
        gt_queue(&g, FRAME, sizeof FRAME, 0);
    }
    CHECK(!gt_queue(&g, FRAME, sizeof FRAME, 0) && g.count.dropped == 1, "a full queue refuses and counts");
    hear_slot0(&g, GT_STALE_US + 10000, 300, SCHED, MEMBERS);
    hear_slot0(&g, GT_STALE_US + 10000 + 2 * PERIOD, 302, SCHED, MEMBERS);
    CHECK(gt_next(&g, GT_STALE_US + 10000 + 2 * PERIOD + 1000) < 0 && g.count.stale == GT_QUEUE,
          "frames queued more than %d us ago are dropped (the target would ignore them anyway)", GT_STALE_US);
}

static void it_follows_the_schedule_with_the_lowest_scheduler_id(void)
{
    gt_t g;
    gt_init(&g);
    gt_queue(&g, FRAME, sizeof FRAME, 0);
    hear_slot0(&g, 0, 10, OTHER, 3);
    hear_slot0(&g, PERIOD, 11, OTHER, 3);
    hear_slot0(&g, 7000, 400, SCHED, MEMBERS);     /* a second schedule with a lower scheduler ID */
    hear_slot0(&g, 7000 + PERIOD, 401, SCHED, MEMBERS);
    hear_slot0(&g, 2 * PERIOD, 12, OTHER, 3);      /* the higher one's slot 0: ignored */
    hear_slot0(&g, 7000 + 2 * PERIOD, 402, SCHED, MEMBERS);
    int64_t at = gt_next(&g, 7000 + 2 * PERIOD + 1000);
    int64_t join = 7000 + 2 * PERIOD + LATENCY + (int64_t)MEMBERS * PERIOD / SLOTS;
    CHECK(at >= join && at <= join + 50, "it plans by the lower ID's schedule (%lld vs %lld)", (long long)at,
          (long long)join);
}

/* The host's command line, as csi/gateway_records.py writes it: "send " and the frame in hex. */
static void the_send_command_carries_the_frame_in_hex(void)
{
    uint8_t out[GT_FRAME_MAX];
    size_t n = 0;
    CHECK(gt_parse_send("send 02042a00ff", out, sizeof out, &n) && n == 5 && out[0] == 2 && out[4] == 0xff,
          "five bytes");
    CHECK(gt_parse_send("send 02042A00FF", out, sizeof out, &n) && n == 5, "either case");
    CHECK(!gt_parse_send("send 02042", out, sizeof out, &n), "an odd number of digits is refused");
    CHECK(!gt_parse_send("send 0204zz", out, sizeof out, &n), "a non-hex digit is refused");
    CHECK(!gt_parse_send("send ", out, sizeof out, &n), "an empty frame is refused");
    CHECK(!gt_parse_send("sendx 0204", out, sizeof out, &n) && !gt_parse_send("show", out, sizeof out, &n),
          "other commands are not send");
    CHECK(!gt_parse_send("send 0102030405", out, 4, &n), "a frame longer than the buffer is refused");
}

int main(void)
{
    the_send_command_carries_the_frame_in_hex();
    it_waits_for_the_round_period_then_sends_in_an_even_rounds_join_slot();
    one_frame_per_round_and_never_late();
    frames_nobody_could_send_expire();
    it_follows_the_schedule_with_the_lowest_scheduler_id();
    printf("gateway_tx: %d failures\n", failures);
    return failures ? 1 : 0;
}
