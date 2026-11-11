/*
 * The node's round broadcast codec reproduces the golden fixtures the host decoder is pinned to
 * (tests/fixtures/round_broadcast_v1.json, flattened by scripts/gen_wire_vectors.py into
 * tests/vectors/round_broadcast.txt): every valid case decodes to its fields and encodes back to the same bytes,
 * and every invalid case is rejected for the fixture's reason.
 *
 *   test_round_broadcast <vectors file>
 */
#define _CRT_SECURE_NO_WARNINGS /* the MSVC runtime (clang on Windows) deprecates fopen */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../round_broadcast.h"
#include "vectors.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static void check_valid(char **tok, int n_tok)
{
    const char *name = tok[1];
    uint8_t bytes[RB_MAX_BYTES + 8];
    size_t n = vec_hex(tok[2], bytes, sizeof bytes);
    rb_broadcast_t b;
    rb_status_t st = rb_decode(bytes, n, &b);
    CHECK(st == RB_OK, "%s: decode gave %s", name, rb_status_name(st));
    if (st != RB_OK) {
        return;
    }
    CHECK(b.version == atoi(tok[3]), "%s: version %u", name, b.version);
    CHECK(b.frame_type == atoi(tok[4]), "%s: frame type %u", name, b.frame_type);
    CHECK(b.round == strtoul(tok[5], NULL, 10), "%s: round %u", name, b.round);
    CHECK(b.slot == atoi(tok[6]), "%s: slot %u", name, b.slot);
    CHECK(b.slots_per_round == atoi(tok[7]), "%s: slots per round %u", name, b.slots_per_round);
    CHECK(b.schedule_version == atoi(tok[8]), "%s: schedule version %u", name, b.schedule_version);
    CHECK(b.node_index == atoi(tok[9]), "%s: node index %u", name, b.node_index);
    CHECK(b.membership == strtoul(tok[10], NULL, 10), "%s: membership %lu", name, (unsigned long)b.membership);
    uint8_t id[RB_NODE_ID_BYTES], tag[RB_TAG_BYTES];
    vec_hex(tok[11], id, sizeof id);
    vec_hex(tok[12], tag, sizeof tag);
    CHECK(memcmp(b.node_id, id, sizeof id) == 0, "%s: node id", name);
    CHECK(memcmp(b.tag, tag, sizeof tag) == 0, "%s: tag", name);
    int n_reports = atoi(tok[13]);
    CHECK(b.n_reports == n_reports, "%s: %u reports, expected %d", name, b.n_reports, n_reports);
    CHECK(n_tok == 14 + 6 * n_reports, "%s: vector line has %d fields", name, n_tok);
    for (int k = 0; k < n_reports && k < b.n_reports; k++) {
        char **f = tok + 14 + 6 * k;
        const rb_report_t *r = &b.reports[k];
        CHECK(r->transmitter_index == atoi(f[0]), "%s: report %d transmitter %u", name, k, r->transmitter_index);
        CHECK(r->score == atoi(f[1]), "%s: report %d score %u", name, k, r->score);
        CHECK(r->rssi == atoi(f[2]), "%s: report %d rssi %d", name, k, r->rssi);
        CHECK(r->heard == atoi(f[3]), "%s: report %d heard %u", name, k, r->heard);
        CHECK(r->since == atoi(f[4]), "%s: report %d since %u", name, k, r->since);
        CHECK((r->flags | r->lateness << RB_LATENESS_SHIFT) == atoi(f[5]), "%s: report %d flags %u lateness %u", name,
              k, r->flags, r->lateness);
    }
    uint8_t out[RB_MAX_BYTES];
    size_t m = rb_encode(&b, out, sizeof out);
    CHECK(m == n && memcmp(out, bytes, n) == 0, "%s: re-encoded %zu bytes differ from the fixture's %zu", name, m, n);
    CHECK(rb_encode(&b, out, n - 1) == 0, "%s: encode into a buffer one byte short must fail", name);
}

static void check_invalid(char **tok)
{
    uint8_t bytes[RB_MAX_BYTES + 8];
    size_t n = vec_hex(tok[2], bytes, sizeof bytes);
    rb_broadcast_t b;
    rb_status_t st = rb_decode(bytes, n, &b);
    CHECK(strcmp(rb_status_name(st), tok[3]) == 0, "%s: rejected as %s, expected %s", tok[1], rb_status_name(st), tok[3]);
}

static void check_encoder_refusals(void)
{
    rb_broadcast_t b = {0};
    uint8_t out[RB_MAX_BYTES];
    b.version = RB_VERSION;
    b.frame_type = RB_FRAME_ROUND;
    b.membership = 0x3;
    b.node_index = 0;
    b.n_reports = 1;
    b.reports[0].transmitter_index = 1;
    b.reports[0].heard = 15;
    CHECK(rb_encode(&b, out, sizeof out) == RB_HEADER_BYTES + RB_REPORT_BYTES, "two-member broadcast encodes");
    b.reports[0].heard = 16;
    CHECK(rb_encode(&b, out, sizeof out) == 0, "a heard count over 15 is refused");
    b.reports[0].heard = 3;
    b.reports[0].transmitter_index = 2;
    CHECK(rb_encode(&b, out, sizeof out) == 0, "a report for a non-member is refused");
    b.reports[0].transmitter_index = 1;
    b.node_index = 2;
    CHECK(rb_encode(&b, out, sizeof out) == 0, "a sender outside the membership is refused");
    b.node_index = 0;
    b.reports[0].lateness = 32;
    CHECK(rb_encode(&b, out, sizeof out) == 0, "a lateness code over 31 is refused");
    b.reports[0].lateness = 31;
    b.frame_type = RB_FRAME_JOIN_REQUEST;
    CHECK(rb_encode(&b, out, sizeof out) == 0, "a join frame with link reports is refused");
    b.n_reports = 0;
    b.node_index = 5;
    CHECK(rb_encode(&b, out, sizeof out) == RB_HEADER_BYTES, "a join request asks for an index outside the membership");
    b.frame_type = RB_FRAME_JOIN_GRANT;
    CHECK(rb_encode(&b, out, sizeof out) == 0, "a join grant must give an index in the membership");
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <round_broadcast.txt>\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(argv[1], "r");
    if (!f) {
        perror(argv[1]);
        return 2;
    }
    char line[4096];
    char *tok[VEC_MAX_TOKENS];
    int valid = 0, invalid = 0;
    while (fgets(line, sizeof line, f)) {
        int n = vec_split(line, tok, VEC_MAX_TOKENS);
        if (n == 0 || tok[0][0] == '#') {
            continue;
        }
        if (strcmp(tok[0], "valid") == 0 && n >= 14) {
            check_valid(tok, n);
            valid++;
        } else if (strcmp(tok[0], "invalid") == 0 && n == 4) {
            check_invalid(tok);
            invalid++;
        } else {
            CHECK(0, "unknown vector line: %s", tok[0]);
        }
    }
    fclose(f);
    CHECK(valid > 0 && invalid > 0, "no vectors read");
    check_encoder_refusals();
    printf("round_broadcast: %d valid and %d invalid cases, %d failures\n", valid, invalid, failures);
    return failures ? 1 : 0;
}
