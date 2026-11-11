/*
 * The node's round broadcast authentication reproduces the golden fixtures the host is pinned to
 * (tests/fixtures/round_broadcast_v2.json, flattened by scripts/gen_wire_vectors.py into tests/vectors/round_auth.txt):
 * every valid case verifies and signs back to its bytes, every forged case is refused, the key check value matches,
 * and the freshness sequence gives the same fresh/replay verdicts as the host.
 *
 * The MAC here is the portable reference HMAC-SHA256 (tests/hmac_sha256.c, itself checked against RFC 4231); the
 * node supplies the same function from mbedTLS (firmware/node/main/node_auth.c).
 *
 *   test_round_auth <vectors file>
 */
#define _CRT_SECURE_NO_WARNINGS /* the MSVC runtime (clang on Windows) deprecates fopen */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../round_auth.h"
#include "hmac_sha256.h"
#include "vectors.h"

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static uint8_t s_key[RB_AUTH_KEY_BYTES];

static bool reference_mac(void *ctx, const uint8_t *msg, size_t len, uint8_t mac[RB_AUTH_MAC_BYTES])
{
    hmac_sha256((const uint8_t *)ctx, RB_AUTH_KEY_BYTES, msg, len, mac);
    return true;
}

static void check_rfc4231(void)
{
    /* RFC 4231 test case 2: key "Jefe", data "what do ya want for nothing?" */
    static const uint8_t want[32] = {
        0x5b, 0xdc, 0xc1, 0x46, 0xbf, 0x60, 0x75, 0x4e, 0x6a, 0x04, 0x24, 0x26, 0x08, 0x95, 0x75, 0xc7,
        0x5a, 0x00, 0x3f, 0x08, 0x9d, 0x27, 0x39, 0x83, 0x9d, 0xec, 0x58, 0xb9, 0x64, 0xec, 0x38, 0x43};
    uint8_t mac[32];
    const char *data = "what do ya want for nothing?";
    hmac_sha256((const uint8_t *)"Jefe", 4, (const uint8_t *)data, strlen(data), mac);
    CHECK(memcmp(mac, want, 32) == 0, "reference HMAC-SHA256 fails RFC 4231 test case 2");
    /* test case 6: a 131-byte key (longer than the block) */
    static const uint8_t want6[32] = {
        0x60, 0xe4, 0x31, 0x59, 0x1e, 0xe0, 0xb6, 0x7f, 0x0d, 0x8a, 0x26, 0xaa, 0xcb, 0xf5, 0xb7, 0x7f,
        0x8e, 0x0b, 0xc6, 0x21, 0x37, 0x28, 0xc5, 0x14, 0x05, 0x46, 0x04, 0x0f, 0x0e, 0xe3, 0x7f, 0x54};
    uint8_t key6[131];
    memset(key6, 0xaa, sizeof key6);
    const char *data6 = "Test Using Larger Than Block-Size Key - Hash Key First";
    hmac_sha256(key6, sizeof key6, (const uint8_t *)data6, strlen(data6), mac);
    CHECK(memcmp(mac, want6, 32) == 0, "reference HMAC-SHA256 fails RFC 4231 test case 6");
}

static void check_signed(rb_auth_t *a, const char *name, const char *hex)
{
    uint8_t bytes[RB_MAX_BYTES + 8], copy[RB_MAX_BYTES + 8];
    size_t n = vec_hex(hex, bytes, sizeof bytes);
    CHECK(rb_auth_tag_ok(a, bytes, n), "%s: a golden broadcast does not verify", name);
    memcpy(copy, bytes, n);
    memset(copy + RB_TAG_OFFSET, 0xA5, RB_TAG_BYTES);
    CHECK(rb_auth_sign(a, copy, n) && memcmp(copy, bytes, n) == 0, "%s: signing does not give the golden tag", name);
}

static void check_forged(rb_auth_t *a, const char *name, const char *hex)
{
    uint8_t bytes[RB_MAX_BYTES + 8];
    size_t n = vec_hex(hex, bytes, sizeof bytes);
    CHECK(!rb_auth_tag_ok(a, bytes, n), "%s: a forged broadcast verifies", name);
    CHECK(rb_auth_check(a, bytes, n, 0) == RB_AUTH_FORGED, "%s: check does not call it forged", name);
}

static void check_fresh(rb_auth_t *a, char **tok)
{
    uint8_t id[RB_NODE_ID_BYTES];
    vec_hex(tok[2], id, sizeof id);
    int64_t t = strtoll(tok[1], NULL, 10);
    rb_auth_status_t st = rb_auth_fresh(a, id, (uint16_t)strtoul(tok[3], NULL, 10), t);
    CHECK(strcmp(rb_auth_status_name(st), strcmp(tok[0], "fresh") == 0 ? "ok" : "replay") == 0,
          "freshness at t=%s us, node %s, round %s: got %s, expected %s", tok[1], tok[2], tok[3],
          rb_auth_status_name(st), tok[0]);
}

/* rb_auth_check on whole broadcasts: a signed one is fresh once, then a replay; a forged one never counts. */
static void check_whole_broadcasts(void)
{
    rb_auth_t a;
    rb_auth_init(&a, reference_mac, s_key);
    uint8_t b[RB_HEADER_BYTES] = {RB_VERSION, RB_FRAME_ROUND, 7, 0, 0, 1, 1, 0, 1, 0, 0, 0, 2, 0, 0, 0, 0, 9};
    CHECK(rb_auth_sign(&a, b, sizeof b), "a header-only broadcast signs");
    CHECK(rb_auth_check(&a, b, sizeof b, 1000) == RB_AUTH_OK, "a signed broadcast is accepted");
    CHECK(rb_auth_check(&a, b, sizeof b, 30000) == RB_AUTH_REPLAY, "the same broadcast again is a replay");
    b[2] = 8;   /* round 8, tag left for round 7: forged; it must not move the freshness state */
    CHECK(rb_auth_check(&a, b, sizeof b, 40000) == RB_AUTH_FORGED, "a changed broadcast is forged");
    CHECK(rb_auth_sign(&a, b, sizeof b) && rb_auth_check(&a, b, sizeof b, 50000) == RB_AUTH_OK,
          "round 8, signed, is the next fresh one");
    CHECK(!rb_auth_sign(&a, b, RB_HEADER_BYTES - 1), "bytes shorter than the header are not signed");
    CHECK(!rb_auth_tag_ok(&a, b, RB_HEADER_BYTES - 1), "bytes shorter than the header never verify");
}

/* Many senders: the freshness table never forgets a node that is still sending, and takes a new one by dropping
 * the one heard longest ago. */
static void check_table_is_bounded(void)
{
    rb_auth_t a;
    rb_auth_init(&a, reference_mac, s_key);
    uint8_t id[RB_NODE_ID_BYTES] = {2, 0, 0, 0, 0, 0};
    for (int k = 0; k < RB_AUTH_FRESH_NODES + 8; k++) {
        id[5] = (uint8_t)k;
        CHECK(rb_auth_fresh(&a, id, 10, 1000 * k) == RB_AUTH_OK, "node %d first heard", k);
    }
    id[5] = (uint8_t)(RB_AUTH_FRESH_NODES + 7);
    CHECK(rb_auth_fresh(&a, id, 10, 1000 * (RB_AUTH_FRESH_NODES + 8)) == RB_AUTH_REPLAY,
          "the newest node is still remembered");
}

/* The kit key as scripts/node.py sends it: 32 hex digits, either case; anything else is refused. */
static void check_key_text(void)
{
    uint8_t key[RB_AUTH_KEY_BYTES];
    CHECK(rb_auth_parse_key("00112233445566778899AABBCCDDEEFF", key) && key[0] == 0x00 && key[9] == 0x99
              && key[15] == 0xff, "a key in hex parses");
    CHECK(!rb_auth_parse_key("00112233445566778899aabbccddeef", key), "31 digits are refused");
    CHECK(!rb_auth_parse_key("00112233445566778899aabbccddeeff00", key), "34 digits are refused");
    CHECK(!rb_auth_parse_key("00112233445566778899aabbccddeegg", key), "a non-hex digit is refused");
    CHECK(!rb_auth_parse_key("", key), "an empty key is refused");
    CHECK(rb_auth_is_test_key((const uint8_t *)"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"),
          "the published test key is recognised");
    CHECK(!rb_auth_is_test_key(key), "another key is not the test key");
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <round_auth.txt>\n", argv[0]);
        return 2;
    }
    check_rfc4231();
    FILE *f = fopen(argv[1], "r");
    if (!f) {
        perror(argv[1]);
        return 2;
    }
    char line[4096];
    char *tok[VEC_MAX_TOKENS];
    int have_key = 0, signed_n = 0, forged_n = 0, fresh_n = 0;
    rb_auth_t a;
    while (fgets(line, sizeof line, f)) {
        int n = vec_split(line, tok, VEC_MAX_TOKENS);
        if (n == 0 || tok[0][0] == '#') {
            continue;
        }
        if (strcmp(tok[0], "key") == 0 && n == 3) {
            CHECK(vec_hex(tok[1], s_key, sizeof s_key) == RB_AUTH_KEY_BYTES, "key is not 16 bytes");
            rb_auth_init(&a, reference_mac, s_key);
            uint8_t check[RB_AUTH_CHECK_BYTES], want[RB_AUTH_CHECK_BYTES];
            vec_hex(tok[2], want, sizeof want);
            CHECK(rb_auth_key_check(&a, check) && memcmp(check, want, sizeof want) == 0, "key check value differs");
            have_key = 1;
        } else if (!have_key) {
            CHECK(0, "vectors before the key line: %s", tok[0]);
        } else if (strcmp(tok[0], "signed") == 0 && n == 3) {
            check_signed(&a, tok[1], tok[2]);
            signed_n++;
        } else if (strcmp(tok[0], "forged") == 0 && n == 3) {
            check_forged(&a, tok[1], tok[2]);
            forged_n++;
        } else if ((strcmp(tok[0], "fresh") == 0 || strcmp(tok[0], "replay") == 0) && n == 4) {
            check_fresh(&a, tok);
            fresh_n++;
        } else {
            CHECK(0, "unknown vector line: %s", tok[0]);
        }
    }
    fclose(f);
    CHECK(signed_n > 0 && forged_n > 0 && fresh_n > 0, "no vectors read");
    check_whole_broadcasts();
    check_table_is_bounded();
    check_key_text();
    printf("round_auth: %d signed, %d forged and %d freshness cases, %d failures\n", signed_n, forged_n, fresh_n,
           failures);
    return failures ? 1 : 0;
}
