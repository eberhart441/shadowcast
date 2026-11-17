/*
 * Raw mode on the node, at its seam: requests and heard CSI in; the raw set, the flags and the raw CSI blocks of the
 * next round broadcast out. The blocks are checked by encoding and decoding a real round broadcast with them.
 */
#include <stdio.h>
#include <string.h>

#include "../raw_mode.h"
#include "../round_broadcast.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static const uint8_t ME[6] = {0x02, 0, 0, 0, 0, 0x02};
static const uint8_t N1[6] = {0x02, 0, 0, 0, 0, 0x01};
static const uint8_t N3[6] = {0x02, 0, 0, 0, 0, 0x03};
static const uint8_t N4[6] = {0x02, 0, 0, 0, 0, 0x04};

static rb_request_t request(const uint8_t *target, uint16_t target_round, int n, const uint8_t *a, const uint8_t *b)
{
    rb_request_t q;
    memset(&q, 0, sizeof q);
    memcpy(q.target, target, 6);
    q.target_round = target_round;
    q.n = (uint8_t)n;
    if (n > 0) {
        memcpy(q.transmitters[0], a, 6);
    }
    if (n > 1) {
        memcpy(q.transmitters[1], b, 6);
    }
    return q;
}

static int8_t csi[200];

static void requests_set_the_raw_set_for_their_target_only_while_fresh(void)
{
    raw_mode_t r;
    raw_mode_init(&r);
    rb_request_t q = request(ME, 1000, 1, N3, NULL);
    CHECK(raw_mode_apply(&r, &q, ME, 1000) == RAW_APPLIED && r.n == 1, "a request for this node, this round");
    q = request(N1, 1000, 0, NULL, NULL);
    CHECK(raw_mode_apply(&r, &q, ME, 1001) == RAW_NOT_MINE && r.n == 1, "a request for another node changes nothing");
    q = request(ME, 1000, 2, N3, N4);
    CHECK(raw_mode_apply(&r, &q, ME, 1000 + RB_RAW_REQUEST_MAX_AGE) == RAW_APPLIED && r.n == 2,
          "a request made %d rounds ago still counts", RB_RAW_REQUEST_MAX_AGE);
    q = request(ME, 1000, 0, NULL, NULL);
    CHECK(raw_mode_apply(&r, &q, ME, 1001 + RB_RAW_REQUEST_MAX_AGE) == RAW_STALE && r.n == 2,
          "an older one is stale (a replay of a recorded request)");
    CHECK(raw_mode_apply(&r, &q, ME, 999) == RAW_STALE, "a target round ahead of the node's own is stale");
    q = request(ME, 65530, 0, NULL, NULL);
    CHECK(raw_mode_apply(&r, &q, ME, 4) == RAW_APPLIED && r.n == 0, "round numbers wrap; no transmitters: off");
    CHECK(r.count.applied == 3 && r.count.ignored == 3, "counted: %u applied, %u ignored", (unsigned)r.count.applied,
          (unsigned)r.count.ignored);
}

static void the_last_csi_of_each_raw_link_rides_in_the_next_round_broadcast(void)
{
    raw_mode_t r;
    raw_mode_init(&r);
    rb_request_t q = request(ME, 7, 2, N4, N1);
    raw_mode_apply(&r, &q, ME, 7);
    for (int i = 0; i < 200; i++) {
        csi[i] = (int8_t)(i - 100);
    }
    uint32_t membership = 0x1F;   /* indices 0..4; this node is index 2 */
    raw_mode_heard(&r, N1, 1, 500, -40, csi, 114);
    raw_mode_heard(&r, N3, 3, 501, -41, csi, 114);           /* not in the raw set */
    raw_mode_heard(&r, N4, 4, 502, -42, csi + 1, 200);       /* longer than one HT20 field: the first 128 bytes */
    raw_mode_heard(&r, N4, 4, 503, -43, csi + 2, 200);       /* a later one replaces it */
    rb_raw_t blocks[RB_RAW_MAX_LINKS];
    uint8_t n = raw_mode_take(&r, membership, 2, 1000, blocks);
    CHECK(n == 2, "two raw links heard: two blocks (%u)", n);
    rb_broadcast_t b;
    memset(&b, 0, sizeof b);
    b.version = RB_VERSION;
    b.frame_type = RB_FRAME_ROUND_RAW;
    b.node_index = 2;
    b.membership = membership;
    b.n_reports = 4;
    for (uint8_t k = 0, i = 0; i < 5; i++) {
        if (i != 2) {
            b.reports[k++].transmitter_index = i;
        }
    }
    b.n_raw = n;
    memcpy(b.raw, blocks, sizeof blocks);
    static uint8_t bytes[RB_MAX_FRAME_BYTES];
    size_t len = rb_encode(&b, bytes, sizeof bytes);
    rb_broadcast_t d;
    CHECK(len && rb_decode(bytes, len, &d) == RB_OK && d.n_raw == 2, "the blocks make a valid round broadcast");
    bool saw1 = false, saw4 = false;
    for (int k = 0; k < d.n_raw; k++) {
        const rb_raw_t *c = &d.raw[k];
        if (c->transmitter_index == 1) {
            saw1 = c->round == 500 && c->rssi == -40 && c->n_iq == 114 && memcmp(c->iq, csi, 114) == 0;
        }
        if (c->transmitter_index == 4) {
            saw4 = c->round == 503 && c->rssi == -43 && c->n_iq == RB_RAW_IQ_MAX && memcmp(c->iq, csi + 2, 128) == 0;
        }
    }
    CHECK(saw1 && saw4, "each block holds its transmitter's last CSI, round and rssi");
    CHECK(raw_mode_take(&r, membership, 2, 1000, blocks) == 0, "sent once: nothing kept for the round after");
    CHECK(r.count.sent == 2, "two blocks counted sent");
}

static void what_does_not_fit_the_slot_is_dropped_not_sent_late(void)
{
    raw_mode_t r;
    raw_mode_init(&r);
    rb_request_t q = request(ME, 7, 2, N1, N4);
    raw_mode_apply(&r, &q, ME, 7);
    raw_mode_heard(&r, N1, 1, 500, -40, csi, 128);
    raw_mode_heard(&r, N4, 4, 500, -40, csi, 128);
    rb_raw_t blocks[RB_RAW_MAX_LINKS];
    CHECK(raw_mode_take(&r, 0x1F, 2, RB_RAW_BLOCK_MAX + 10, blocks) == 1 && r.count.skipped == 1,
          "room for one block: one sent, one dropped");
    raw_mode_heard(&r, N1, 1, 501, -40, csi, 128);
    CHECK(raw_mode_take(&r, 0x1D, 2, 1000, blocks) == 0 && r.count.skipped == 2,
          "a transmitter that left the round (index 1 not in the membership) is dropped");
    raw_mode_heard(&r, N1, 1, 502, -40, csi, 1);
    CHECK(raw_mode_take(&r, 0x1F, 2, 1000, blocks) == 0, "CSI too short to be I/Q pairs is not kept");
}

static void the_raw_set_flags_its_links_by_member_index(void)
{
    raw_mode_t r;
    raw_mode_init(&r);
    rb_request_t q = request(ME, 7, 2, N3, N4);
    raw_mode_apply(&r, &q, ME, 7);
    uint8_t member_id[RB_MAX_NODES][RB_NODE_ID_BYTES];
    memset(member_id, 0, sizeof member_id);
    memcpy(member_id[0], N1, 6);
    memcpy(member_id[5], N3, 6);
    memcpy(member_id[9], N4, 6);
    /* ISO C before C23 does not convert T (*)[N] to const T (*)[N] implicitly. */
    const uint8_t (*members)[RB_NODE_ID_BYTES] = (const uint8_t (*)[RB_NODE_ID_BYTES])member_id;
    CHECK(raw_mode_flags(&r, members, (1u << 0) | (1u << 5) | (1u << 9)) == ((1u << 5) | (1u << 9)),
          "the members whose serials are in the raw set");
    CHECK(raw_mode_flags(&r, members, (1u << 0) | (1u << 5)) == (1u << 5), "only members whose serial is known");
}

int main(void)
{
    requests_set_the_raw_set_for_their_target_only_while_fresh();
    the_last_csi_of_each_raw_link_rides_in_the_next_round_broadcast();
    what_does_not_fit_the_slot_is_dropped_not_sent_late();
    the_raw_set_flags_its_links_by_member_index();
    printf("raw_mode: %d failures\n", failures);
    return failures ? 1 : 0;
}
