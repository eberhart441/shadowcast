#include "node_auth.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "psa/crypto.h"

#include "round_auth.h"

#define KEY_NVS_NAMESPACE "node"
#define KEY_NVS_NAME "kit_key"
#define BENCH_ROUNDS 100
#define BENCH_BYTES 142             /* a 30-node round broadcast: 26 + 4 x 29 */
#define HMAC_ALG PSA_ALG_HMAC(PSA_ALG_SHA_256)

static const char *TAG = "auth";

/* lone_node_no_reports of tests/fixtures/round_broadcast_v2.json, signed with the published test key */
static const uint8_t GOLDEN[RB_HEADER_BYTES] = {
    0x02, 0x01, 0xff, 0xff, 0x05, 0x09, 0xff, 0x05, 0x20, 0x00, 0x00, 0x00, 0x10, 0xbd, 0xa3, 0xcc, 0x85, 0xbc,
    0xed, 0xf6, 0x86, 0x66, 0x73, 0x4a, 0x0f, 0xe9};

static psa_key_id_t s_key_id;
static bool s_ready;
static rb_auth_t s_auth;
static uint8_t s_check[RB_AUTH_CHECK_BYTES];
static uint32_t s_sign_us, s_verify_us;   /* measured at start, per 142-byte broadcast */
static volatile uint32_t s_forged, s_replayed;

static bool psa_hmac(void *ctx, const uint8_t *msg, size_t len, uint8_t mac[RB_AUTH_MAC_BYTES])
{
    size_t out_len = 0;
    return psa_mac_compute(*(const psa_key_id_t *)ctx, HMAC_ALG, msg, len, mac, RB_AUTH_MAC_BYTES, &out_len)
               == PSA_SUCCESS && out_len == RB_AUTH_MAC_BYTES;
}

static bool import_key(const uint8_t key[RB_AUTH_KEY_BYTES], psa_key_id_t *id)
{
    psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
    psa_set_key_usage_flags(&attr, PSA_KEY_USAGE_SIGN_MESSAGE);
    psa_set_key_lifetime(&attr, PSA_KEY_LIFETIME_VOLATILE);
    psa_set_key_algorithm(&attr, HMAC_ALG);
    psa_set_key_type(&attr, PSA_KEY_TYPE_HMAC);
    psa_set_key_bits(&attr, 8 * RB_AUTH_KEY_BYTES);
    psa_status_t st = psa_import_key(&attr, key, RB_AUTH_KEY_BYTES, id);
    psa_reset_key_attributes(&attr);
    return st == PSA_SUCCESS;
}

/* The key check value of a key that is not loaded (the one a key command brings). */
static bool check_of(const uint8_t key[RB_AUTH_KEY_BYTES], uint8_t check[RB_AUTH_CHECK_BYTES])
{
    psa_key_id_t id;
    if (!import_key(key, &id)) {
        return false;
    }
    rb_auth_t a;
    rb_auth_init(&a, psa_hmac, &id);
    bool ok = rb_auth_key_check(&a, check);
    psa_destroy_key(id);
    return ok;
}

/* mbedTLS reproduces a fixture tag; and the time to sign and to verify a 30-node broadcast. */
static bool self_test(void)
{
    uint8_t test_key[RB_AUTH_KEY_BYTES];
    for (int i = 0; i < RB_AUTH_KEY_BYTES; i++) {
        test_key[i] = (uint8_t)i;
    }
    psa_key_id_t id;
    if (!import_key(test_key, &id)) {
        return false;
    }
    rb_auth_t a;
    rb_auth_init(&a, psa_hmac, &id);
    uint8_t b[BENCH_BYTES] = {0};
    memcpy(b, GOLDEN, sizeof GOLDEN);
    bool ok = rb_auth_tag_ok(&a, b, sizeof GOLDEN);
    int64_t t0 = esp_timer_get_time();
    for (int i = 0; i < BENCH_ROUNDS; i++) {
        b[2] = (uint8_t)i;
        ok = rb_auth_sign(&a, b, sizeof b) && ok;
    }
    int64_t t1 = esp_timer_get_time();
    for (int i = 0; i < BENCH_ROUNDS; i++) {
        ok = rb_auth_tag_ok(&a, b, sizeof b) && ok;
    }
    int64_t t2 = esp_timer_get_time();
    psa_destroy_key(id);
    s_sign_us = (uint32_t)((t1 - t0) / BENCH_ROUNDS);
    s_verify_us = (uint32_t)((t2 - t1) / BENCH_ROUNDS);
    return ok;
}

bool node_auth_start(void)
{
    if (psa_crypto_init() != PSA_SUCCESS) {
        ESP_LOGE(TAG, "psa_crypto_init failed: round broadcasts cannot be signed");
        return false;
    }
    bool tested = self_test();
    ESP_LOGI(TAG, "HMAC-SHA256 self-test %s; per 142-byte broadcast sign %lu us, verify %lu us",
             tested ? "ok" : "FAILED", (unsigned long)s_sign_us, (unsigned long)s_verify_us);
    uint8_t key[RB_AUTH_KEY_BYTES];
    size_t size = sizeof key;
    nvs_handle_t h;
    bool have = false;
    if (nvs_open(KEY_NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
        have = nvs_get_blob(h, KEY_NVS_NAME, key, &size) == ESP_OK && size == sizeof key;
        nvs_close(h);
    }
    if (!have) {
        ESP_LOGW(TAG, "no kit key: a transceiver only listens until scripts/node.py PORT key FILE gives it one");
        return false;
    }
    have = import_key(key, &s_key_id);
    memset(key, 0, sizeof key);
    rb_auth_init(&s_auth, psa_hmac, &s_key_id);
    s_ready = have && tested && rb_auth_key_check(&s_auth, s_check);
    if (s_ready) {
        ESP_LOGI(TAG, "kit key check %02x%02x%02x%02x", s_check[0], s_check[1], s_check[2], s_check[3]);
    } else {
        ESP_LOGE(TAG, "the kit key could not be loaded into mbedTLS; this node only listens");
    }
    return s_ready;
}

bool node_auth_ready(void)
{
    return s_ready;
}

bool node_auth_sign(uint8_t *bytes, size_t len)
{
    return s_ready && rb_auth_sign(&s_auth, bytes, len);
}

bool node_auth_accept(const uint8_t *bytes, size_t len, int64_t t_us)
{
    if (!s_ready) {
        return false;
    }
    rb_auth_status_t st = rb_auth_check(&s_auth, bytes, len, t_us);
    if (st == RB_AUTH_FORGED) {
        s_forged++;
    } else if (st == RB_AUTH_REPLAY) {
        s_replayed++;
    }
    return st == RB_AUTH_OK;
}

bool node_auth_mac(const uint8_t *msg, size_t len, uint8_t mac[32])
{
    return s_ready && psa_hmac(&s_key_id, msg, len, mac);
}

static esp_err_t save_key(const uint8_t key[RB_AUTH_KEY_BYTES])
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(KEY_NVS_NAMESPACE, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_blob(h, KEY_NVS_NAME, key, RB_AUTH_KEY_BYTES);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    return err;
}

bool node_auth_command(const char *line, char *reply, size_t n, node_cmd_t *action)
{
    while (*line == ' ' || *line == '\t') {
        line++;
    }
    if (strncmp(line, "key", 3) != 0 || (line[3] != '\0' && line[3] != ' ' && line[3] != '\t')) {
        return false;
    }
    char hex[2 * RB_AUTH_KEY_BYTES + 2] = {0};
    const char *p = line + 3;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    size_t len = strcspn(p, " \t\r\n");
    *action = NODE_CMD_NONE;
    if (len == 0) {
        if (s_ready) {
            snprintf(reply, n, "ok key check=%02x%02x%02x%02x sign_us=%lu verify_us=%lu forged=%lu replayed=%lu",
                     s_check[0], s_check[1], s_check[2], s_check[3], (unsigned long)s_sign_us,
                     (unsigned long)s_verify_us, (unsigned long)s_forged, (unsigned long)s_replayed);
        } else {
            snprintf(reply, n, "ok key none (this node only listens; scripts/node.py PORT key FILE) sign_us=%lu "
                     "verify_us=%lu", (unsigned long)s_sign_us, (unsigned long)s_verify_us);
        }
        return true;
    }
    uint8_t key[RB_AUTH_KEY_BYTES], check[RB_AUTH_CHECK_BYTES];
    memcpy(hex, p, len < sizeof hex - 1 ? len : sizeof hex - 1);
    *action = NODE_CMD_ERROR;
    if (p[len + strspn(p + len, " \t\r\n")] != '\0' || !rb_auth_parse_key(hex, key)) {
        snprintf(reply, n, "error key takes one kit key of 32 hex digits");
    } else if (rb_auth_is_test_key(key)) {
        snprintf(reply, n, "error that is the published test key; make a kit key with scripts/node.py keygen");
    } else if (!check_of(key, check)) {
        snprintf(reply, n, "error mbedTLS could not use the key");
    } else if (save_key(key) != ESP_OK) {
        snprintf(reply, n, "error could not save the kit key");
    } else {
        snprintf(reply, n, "ok key check=%02x%02x%02x%02x (saved; restarting)", check[0], check[1], check[2],
                 check[3]);
        *action = NODE_CMD_RESTART;
    }
    memset(key, 0, sizeof key);
    memset(hex, 0, sizeof hex);
    return true;
}
