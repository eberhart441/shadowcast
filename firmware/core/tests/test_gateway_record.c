/*
 * The node's gateway record encoder reproduces the golden records the host reader is pinned to
 * (tests/fixtures/gateway_record_v1.json, flattened by scripts/gen_wire_vectors.py into
 * tests/vectors/gateway_record.txt): each record decodes to its fields and encodes back to the same bytes, and each
 * bad one is rejected for the fixture's reason.
 *
 *   test_gateway_record <vectors file>
 */
#define _CRT_SECURE_NO_WARNINGS /* the MSVC runtime (clang on Windows) deprecates fopen */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../gateway_record.h"
#include "vectors.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static size_t decode_hex(const char *name, const char *hex, uint8_t *bytes, gr_record_t *r, uint8_t type)
{
    size_t n = vec_hex(hex, bytes, GR_MAX_RECORD);
    gr_status_t st = gr_decode(bytes, n, r);
    CHECK(st == GR_OK, "%s: decode gave %s", name, gr_status_name(st));
    CHECK(st != GR_OK || (r->type == type && r->record_len == n), "%s: type %u, %zu of %zu bytes", name, r->type,
          r->record_len, n);
    return st == GR_OK ? n : 0;
}

static void same_bytes(const char *name, const uint8_t *a, size_t na, const uint8_t *b, size_t nb)
{
    CHECK(na == nb && memcmp(a, b, na) == 0, "%s: re-encoded %zu bytes differ from the fixture's %zu", name, na, nb);
}

static void check_heard(char **tok)
{
    uint8_t bytes[GR_MAX_RECORD], expected_payload[GR_MAX_BODY], out[GR_MAX_RECORD];
    gr_record_t r;
    size_t n = decode_hex(tok[1], tok[2], bytes, &r, GR_TYPE_HEARD);
    if (!n) {
        return;
    }
    uint64_t t;
    int8_t rssi;
    const uint8_t *payload;
    size_t payload_len;
    gr_heard_fields(&r, &t, &rssi, &payload, &payload_len);
    size_t expected_len = vec_hex(tok[5], expected_payload, sizeof expected_payload);
    CHECK(t == strtoull(tok[3], NULL, 10), "%s: receive time %llu", tok[1], (unsigned long long)t);
    CHECK(rssi == atoi(tok[4]), "%s: rssi %d", tok[1], rssi);
    CHECK(payload_len == expected_len && memcmp(payload, expected_payload, expected_len) == 0, "%s: payload", tok[1]);
    same_bytes(tok[1], out, gr_encode_heard(out, sizeof out, t, rssi, payload, payload_len), bytes, n);
    CHECK(gr_encode_heard(out, n - 1, t, rssi, payload, payload_len) == 0, "%s: a buffer one byte short must fail",
          tok[1]);
}

static void check_status(char **tok, int n_tok)
{
    uint8_t bytes[GR_MAX_RECORD], serial[6], out[GR_MAX_RECORD];
    gr_record_t r;
    size_t n = decode_hex(tok[1], tok[2], bytes, &r, GR_TYPE_STATUS);
    if (!n) {
        return;
    }
    gr_node_status_t s;
    gr_status_fields(&r, &s);
    vec_hex(tok[3], serial, sizeof serial);
    CHECK(memcmp(s.serial, serial, 6) == 0, "%s: serial", tok[1]);
    CHECK(s.uptime_us == strtoull(tok[4], NULL, 10), "%s: uptime", tok[1]);
    CHECK(s.reset_reason == atoi(tok[5]) && s.role == atoi(tok[6]) && s.channel == atoi(tok[7]),
          "%s: reset reason %u role %u channel %u", tok[1], s.reset_reason, s.role, s.channel);
    CHECK(n_tok == 8 + GR_STATUS_COUNTERS, "%s: %d counters in the vector", tok[1], n_tok - 8);
    for (int i = 0; i < GR_STATUS_COUNTERS && 8 + i < n_tok; i++) {
        CHECK(s.counters[i] == strtoul(tok[8 + i], NULL, 10), "%s: counter %d is %lu", tok[1], i,
              (unsigned long)s.counters[i]);
    }
    same_bytes(tok[1], out, gr_encode_status(out, sizeof out, &s), bytes, n);
}

static void check_text(char **tok)
{
    uint8_t bytes[GR_MAX_RECORD], out[GR_MAX_RECORD];
    char text[GR_MAX_BODY];
    gr_record_t r;
    size_t n = decode_hex(tok[1], tok[2], bytes, &r, GR_TYPE_TEXT);
    if (!n) {
        return;
    }
    size_t len = vec_hex(tok[3], (uint8_t *)text, sizeof text);
    CHECK(r.body_len == len && memcmp(r.body, text, len) == 0, "%s: text", tok[1]);
    same_bytes(tok[1], out, gr_encode_text(out, sizeof out, text, len), bytes, n);
}

static void check_bad(char **tok)
{
    uint8_t bytes[GR_MAX_RECORD];
    gr_record_t r;
    size_t n = vec_hex(tok[2], bytes, sizeof bytes);
    gr_status_t st = gr_decode(bytes, n, &r);
    CHECK(strcmp(gr_status_name(st), tok[3]) == 0, "%s: rejected as %s, expected %s", tok[1], gr_status_name(st),
          tok[3]);
}

static void check_limits(void)
{
    static uint8_t out[GR_MAX_RECORD + 16], payload[GR_MAX_BODY];
    CHECK(gr_crc16((const uint8_t *)"123456789", 9) == 0x29B1, "CRC-16/CCITT-FALSE check value");
    CHECK(gr_encode_heard(out, sizeof out, 0, 0, payload, GR_MAX_BODY - GR_HEARD_HEAD) == GR_MAX_RECORD,
          "the longest heard record encodes");
    CHECK(gr_encode_heard(out, sizeof out, 0, 0, payload, GR_MAX_BODY - GR_HEARD_HEAD + 1) == 0,
          "a body over the limit is refused");
    uint8_t junk[] = {'b', 'o', 'o', 't'};
    gr_record_t r;
    CHECK(gr_decode(junk, sizeof junk, &r) == GR_SYNC, "boot text is not a record");
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <gateway_record.txt>\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(argv[1], "r");
    if (!f) {
        perror(argv[1]);
        return 2;
    }
    static char line[16384];
    char *tok[VEC_MAX_TOKENS];
    int good = 0, bad = 0;
    while (fgets(line, sizeof line, f)) {
        int n = vec_split(line, tok, VEC_MAX_TOKENS);
        if (n == 0 || tok[0][0] == '#') {
            continue;
        }
        if (strcmp(tok[0], "heard") == 0 && n == 6) {
            check_heard(tok);
            good++;
        } else if (strcmp(tok[0], "status") == 0 && n >= 8) {
            check_status(tok, n);
            good++;
        } else if (strcmp(tok[0], "text") == 0 && n == 4) {
            check_text(tok);
            good++;
        } else if (strcmp(tok[0], "bad") == 0 && n == 4) {
            check_bad(tok);
            bad++;
        } else {
            CHECK(0, "unknown vector line: %s", tok[0]);
        }
    }
    fclose(f);
    CHECK(good > 0 && bad > 0, "no vectors read");
    check_limits();
    printf("gateway_record: %d records and %d bad cases, %d failures\n", good, bad, failures);
    return failures ? 1 : 0;
}
