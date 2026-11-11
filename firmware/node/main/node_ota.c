#include "node_ota.h"

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs.h"
#include "psa/crypto.h"

#include "node_auth.h"
#include "ota_core.h"

#define RECORD_NVS_NAMESPACE "node"
#define RECORD_NVS_NAME "ota"
#define SCAN_RECORDS 8
#define SCAN_PASSIVE_MS 220           /* per channel; access points beacon about every 102 ms */
#define CONNECT_TIMEOUT_MS 20000
#define HTTP_TIMEOUT_MS 10000
#define MAINTENANCE_LIMIT_US (240LL * 1000000)   /* maintenance mode never keeps a node off the round longer */
#define NET_UP BIT0

static const char *TAG = "ota";
static const uint8_t BROADCAST_MAC[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const char KEY_CHECK_MESSAGE[] = "shadowcast kit key check";   /* as round_auth.c */

static uint8_t s_serial[OTA_SERIAL_BYTES];
static char s_serial_text[18];
static uint8_t s_sensing_channel;
static bool s_gateway;
static ota_record_t s_record;
static bool s_health_on;
static ota_health_t s_health;
static bool s_peer_added;
static EventGroupHandle_t s_net;

/* ---------------------------------------------------------------- the record in NVS */

static void load_record(void)
{
    uint8_t blob[OTA_RECORD_BYTES];
    size_t size = sizeof blob;
    nvs_handle_t h;
    bool ok = false;
    if (nvs_open(RECORD_NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
        ok = nvs_get_blob(h, RECORD_NVS_NAME, blob, &size) == ESP_OK;
        nvs_close(h);
    }
    ota_record_decode(blob, ok ? size : 0, &s_record);   /* none, or another version: a fresh record */
}

static esp_err_t save_record(const ota_record_t *r)
{
    uint8_t blob[OTA_RECORD_BYTES];
    size_t n = ota_record_encode(r, blob, sizeof blob);
    nvs_handle_t h;
    esp_err_t err = nvs_open(RECORD_NVS_NAMESPACE, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_blob(h, RECORD_NVS_NAME, blob, n);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "could not store the OTA record: %s", esp_err_to_name(err));
    }
    return err;
}

static uint8_t running_slot(void)
{
    const esp_partition_t *p = esp_ota_get_running_partition();
    return p && p->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1 ? 1 : 0;
}

static esp_ota_img_states_t running_state(void)
{
    esp_ota_img_states_t st = ESP_OTA_IMG_UNDEFINED;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) != ESP_OK) {
        st = ESP_OTA_IMG_UNDEFINED;
    }
    return st;
}

static void restart_soon(void)
{
    vTaskDelay(pdMS_TO_TICKS(200));   /* let the last log line reach USB */
    esp_restart();
}

static void hex(const uint8_t *b, size_t n, char *out)
{
    for (size_t i = 0; i < n; i++) {
        snprintf(out + 2 * i, 3, "%02x", b[i]);
    }
}

/* ---------------------------------------------------------------- maintenance mode */

static void on_net_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_net, NET_UP);
    }
}

/* SSID "shadowcast-<kit key check>", passphrase: 24 hex digits of HMAC(kit key, OTA_NETWORK_LABEL). */
static bool network_credentials(char ssid[33], char pass[65])
{
    uint8_t mac[32];
    if (!node_auth_mac((const uint8_t *)KEY_CHECK_MESSAGE, sizeof KEY_CHECK_MESSAGE - 1, mac)) {
        return false;
    }
    char check[9];
    hex(mac, 4, check);
    snprintf(ssid, 33, "%s%s", OTA_NETWORK_SSID_PREFIX, check);
    if (!node_auth_mac((const uint8_t *)OTA_NETWORK_LABEL, strlen(OTA_NETWORK_LABEL), mac)) {
        return false;
    }
    hex(mac, 12, pass);
    return true;
}

/* Find the maintenance network by a passive scan (no frame is sent on any channel), refuse it on a channel that
 * overlaps the sensing channel, join it. OTA_OUT_NONE with the access point's address in gw. */
static ota_outcome_t join_network(uint8_t ap_channel, char gw[16])
{
    char ssid[33], pass[65];
    if (!network_credentials(ssid, pass)) {
        ESP_LOGE(TAG, "no kit key: no maintenance network");
        return OTA_OUT_NO_NETWORK;
    }
    s_net = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_net_event, NULL));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO));   /* 5 GHz is the preferred maintenance band */

    wifi_scan_config_t scan = {.ssid = (uint8_t *)ssid, .channel = ap_channel, .scan_type = WIFI_SCAN_TYPE_PASSIVE};
    scan.scan_time.passive = SCAN_PASSIVE_MS;
    static wifi_ap_record_t found[SCAN_RECORDS];
    uint16_t n = SCAN_RECORDS;
    if (esp_wifi_scan_start(&scan, true) != ESP_OK || esp_wifi_scan_get_ap_records(&n, found) != ESP_OK) {
        return OTA_OUT_NO_NETWORK;
    }
    int best = -1;
    for (int i = 0; i < n; i++) {
        if (strcmp((const char *)found[i].ssid, ssid) == 0 && (best < 0 || found[i].rssi > found[best].rssi)) {
            best = i;
        }
    }
    if (best < 0) {
        ESP_LOGW(TAG, "maintenance network %s not found (channel %u, 0 = all)", ssid, ap_channel);
        return OTA_OUT_NO_NETWORK;
    }
    uint8_t channel = found[best].primary;
    if (channel == 0 || !ota_channels_clear(s_sensing_channel, channel)) {
        ESP_LOGW(TAG, "maintenance network on channel %u overlaps sensing channel %u: not joined", channel,
                 s_sensing_channel);
        return OTA_OUT_OVERLAP;
    }
    wifi_config_t wc = {0};
    memcpy(wc.sta.ssid, ssid, strlen(ssid));
    memcpy(wc.sta.password, pass, strlen(pass));
    wc.sta.channel = channel;
    wc.sta.bssid_set = true;
    memcpy(wc.sta.bssid, found[best].bssid, 6);
    wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wc.sta.scan_method = WIFI_FAST_SCAN;
    if (esp_wifi_set_config(WIFI_IF_STA, &wc) != ESP_OK || esp_wifi_connect() != ESP_OK) {
        return OTA_OUT_NO_NETWORK;
    }
    EventBits_t bits = xEventGroupWaitBits(s_net, NET_UP, pdFALSE, pdFALSE, pdMS_TO_TICKS(CONNECT_TIMEOUT_MS));
    esp_netif_ip_info_t ip;
    if (!(bits & NET_UP) || esp_netif_get_ip_info(netif, &ip) != ESP_OK) {
        ESP_LOGW(TAG, "could not join %s on channel %u", ssid, channel);
        return OTA_OUT_NO_NETWORK;
    }
    snprintf(gw, 16, IPSTR, IP2STR(&ip.gw));
    ESP_LOGI(TAG, "joined %s on channel %u; image server %s", ssid, channel, gw);
    return OTA_OUT_NONE;
}

/* Fetch the image into the other app slot, check its size and SHA-256 against the command, let ESP-IDF verify it
 * (format, digest, signature by the running image's key) and select it for the next boot. */
static ota_outcome_t download(const ota_record_t *job, const char *gw, uint8_t *written_slot)
{
    char url[96];
    snprintf(url, sizeof url, "http://%s:%u/node.bin?serial=%s", gw, job->port, s_serial_text);
    esp_http_client_config_t hc = {.url = url, .timeout_ms = HTTP_TIMEOUT_MS};
    esp_http_client_handle_t client = esp_http_client_init(&hc);
    if (!client) {
        return OTA_OUT_NO_SERVER;
    }
    ota_outcome_t out = OTA_OUT_NONE;
    if (esp_http_client_open(client, 0) != ESP_OK) {
        esp_http_client_cleanup(client);
        return OTA_OUT_NO_SERVER;
    }
    int64_t length = esp_http_client_fetch_headers(client);
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    esp_ota_handle_t handle = 0;
    if (esp_http_client_get_status_code(client) != 200) {
        out = OTA_OUT_NO_SERVER;
    } else if (length > 0 && length != (int64_t)job->image_size) {
        out = OTA_OUT_REFUSED_SIZE;
    } else if (!part || job->image_size > part->size
               || esp_ota_begin(part, OTA_WITH_SEQUENTIAL_WRITES, &handle) != ESP_OK) {
        out = OTA_OUT_WRITE_FAILED;
    }
    if (out != OTA_OUT_NONE) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return out;
    }
    psa_hash_operation_t sha = PSA_HASH_OPERATION_INIT;
    psa_hash_setup(&sha, PSA_ALG_SHA_256);
    static uint8_t buf[4096];
    uint32_t got = 0;
    for (;;) {
        int n = esp_http_client_read(client, (char *)buf, sizeof buf);
        if (n < 0) {
            out = OTA_OUT_NO_SERVER;
            break;
        }
        if (n == 0) {
            break;
        }
        if (got + (uint32_t)n > job->image_size) {
            out = OTA_OUT_REFUSED_SIZE;
            break;
        }
        if (esp_ota_write(handle, buf, (size_t)n) != ESP_OK) {
            out = OTA_OUT_WRITE_FAILED;
            break;
        }
        psa_hash_update(&sha, buf, (size_t)n);
        got += (uint32_t)n;
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    uint8_t digest[OTA_SHA_BYTES] = {0};
    size_t digest_len = 0;
    if (psa_hash_finish(&sha, digest, sizeof digest, &digest_len) != PSA_SUCCESS && out == OTA_OUT_NONE) {
        out = OTA_OUT_WRITE_FAILED;
    }
    if (out == OTA_OUT_NONE) {
        out = ota_download_check(job, got, digest);   /* a corrupt or different image is refused here */
    }
    if (out != OTA_OUT_NONE) {
        esp_ota_abort(handle);
        return out;
    }
    esp_err_t err = esp_ota_end(handle);   /* verifies the image, and its signature (signed apps on update) */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "the image was refused: %s", esp_err_to_name(err));
        return OTA_OUT_REFUSED_IMAGE;
    }
    if (esp_ota_set_boot_partition(part) != ESP_OK) {
        return OTA_OUT_WRITE_FAILED;
    }
    *written_slot = part->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_1 ? 1 : 0;
    return OTA_OUT_NONE;
}

/* POST one report line with its tag; the host's rollout waits for it. Best effort. */
static void post_report(const ota_record_t *job, ota_outcome_t outcome, const char *gw)
{
    static char line[256], tagged[300];
    ota_report_t rep = {.issued = job->issued, .outcome = (uint8_t)outcome,
                        .version = esp_app_get_description()->version, .slot = running_slot(),
                        .running_sha = job->running_sha};
    memcpy(rep.serial, s_serial, OTA_SERIAL_BYTES);
    size_t label = strlen(OTA_REPORT_TAG_LABEL);
    memcpy(line, OTA_REPORT_TAG_LABEL, label);
    size_t n = ota_report_format(&rep, line + label, sizeof line - label);
    uint8_t mac[32];
    if (!n || !node_auth_mac((const uint8_t *)line, label + n, mac)) {
        return;
    }
    char tag[17];
    hex(mac, 8, tag);
    int len = snprintf(tagged, sizeof tagged, "%s tag=%s\n", line + label, tag);
    char url[64];
    snprintf(url, sizeof url, "http://%s:%u/report", gw, job->port);
    esp_http_client_config_t hc = {.url = url, .method = HTTP_METHOD_POST, .timeout_ms = HTTP_TIMEOUT_MS};
    esp_http_client_handle_t client = esp_http_client_init(&hc);
    if (!client) {
        return;
    }
    esp_http_client_set_post_field(client, tagged, len);
    esp_err_t err = esp_http_client_perform(client);
    ESP_LOGI(TAG, "report %s: %s (HTTP %d)", ota_outcome_name(outcome), esp_err_to_name(err),
             esp_http_client_get_status_code(client));
    esp_http_client_cleanup(client);
}

static void on_maintenance_limit(void *arg)
{
    esp_restart();
}

/* Run the stored job once, then restart. The job is cleared in NVS before anything else, so a crash or a hang
 * leaves the node on the image it had, with nothing to retry. */
static void maintenance(void)
{
    ota_record_t job = s_record, cleared = s_record;
    ota_record_job_done(&cleared, OTA_OUT_NO_NETWORK, 0);   /* no attempt yet: just clears the job */
    if (save_record(&cleared) != ESP_OK) {
        restart_soon();   /* never run a job that could repeat forever */
    }
    esp_timer_handle_t limit;
    const esp_timer_create_args_t args = {.callback = on_maintenance_limit, .name = "ota limit"};
    if (esp_timer_create(&args, &limit) == ESP_OK) {
        esp_timer_start_once(limit, MAINTENANCE_LIMIT_US);
    }
    ESP_LOGW(TAG, "maintenance mode: %s for command %lu", job.job == OTA_JOB_UPDATE ? "update" : "report",
             (unsigned long)job.issued);
    char gw[16] = "";
    ota_outcome_t out = join_network(job.ap_channel, gw);
    if (out == OTA_OUT_NONE && job.job == OTA_JOB_UPDATE) {
        uint8_t slot = 0;
        out = download(&job, gw, &slot);
        if (out == OTA_OUT_NONE) {
            ota_record_job_done(&job, OTA_OUT_NONE, slot);   /* the attempt: the new image proves itself next */
            save_record(&job);
            ESP_LOGW(TAG, "new image in ota_%u; restarting into it", slot);
            restart_soon();
        }
    } else if (out == OTA_OUT_NONE) {
        out = (ota_outcome_t)job.outcome;
    }
    if (gw[0]) {
        post_report(&job, out, gw);
    }
    ESP_LOGW(TAG, "maintenance mode done: %s; restarting", ota_outcome_name(out));
    restart_soon();
}

/* ---------------------------------------------------------------- the five calls */

void node_ota_boot(const uint8_t serial[6], uint8_t sensing_channel, bool gateway)
{
    memcpy(s_serial, serial, OTA_SERIAL_BYTES);
    snprintf(s_serial_text, sizeof s_serial_text, "%02X:%02X:%02X:%02X:%02X:%02X", serial[0], serial[1], serial[2],
             serial[3], serial[4], serial[5]);
    s_sensing_channel = sensing_channel;
    s_gateway = gateway;
    load_record();
    ota_record_t before = s_record;
    ota_boot_t boot = ota_boot(&s_record, running_state() == ESP_OTA_IMG_PENDING_VERIFY, running_slot());
    if (memcmp(&before, &s_record, sizeof before) != 0) {
        save_record(&s_record);
    }
    if (boot == OTA_BOOT_MAINTENANCE) {
        maintenance();   /* never returns */
    }
    if (boot == OTA_BOOT_HEALTH_CHECK) {
        ota_health_init(&s_health, esp_timer_get_time(), gateway);
        s_health_on = true;
        ESP_LOGW(TAG, "new image in ota_%u: health check (member, %d heard, %d sent within %d s) or roll back",
                 running_slot(), OTA_HEALTH_HEARD, OTA_HEALTH_SENT, (int)(OTA_HEALTH_DEADLINE_US / 1000000));
    }
}

bool node_ota_heard(const uint8_t *body, size_t len, int64_t t_us)
{
    if (!ota_is_command(body, len)) {
        return false;
    }
    ota_command_t c;
    if (s_gateway || s_health_on || ota_command_decode(body, len, &c) != OTA_OK || !node_auth_accept(body, len, t_us)) {
        return true;   /* a malformed, forged or replayed command, or none this node takes now */
    }
    bool already = false;
    ota_action_t action = ota_command_action(&c, s_serial, s_record.last_issued, s_sensing_channel,
                                             s_record.running_sha, &already);
    if (action == OTA_ACT_NONE) {
        return true;
    }
    ota_record_take(&s_record, &c, action, already);
    if (save_record(&s_record) == ESP_OK) {
        ESP_LOGW(TAG, "maintenance command %lu (%s): restarting into maintenance mode", (unsigned long)c.issued,
                 action == OTA_ACT_UPDATE ? "update" : "report");
        restart_soon();
    }
    return true;
}

void node_ota_tick(bool member, uint32_t heard, uint32_t sent)
{
    if (!s_health_on) {
        return;
    }
    ota_verdict_t v = ota_health_step(&s_health, esp_timer_get_time(), member, heard, sent);
    if (v == OTA_HEALTH_PASS) {
        s_health_on = false;
        esp_ota_mark_app_valid_cancel_rollback();
        ota_record_committed(&s_record);
        save_record(&s_record);
        ESP_LOGW(TAG, "health check passed: this image stays; restarting to report it");
        restart_soon();
    } else if (v == OTA_HEALTH_FAIL) {
        s_health_on = false;
        ota_record_rolled_back(&s_record);
        save_record(&s_record);
        ESP_LOGE(TAG, "health check failed (member %d, heard %lu, sent %lu): rolling back", member,
                 (unsigned long)heard, (unsigned long)sent);
        vTaskDelay(pdMS_TO_TICKS(200));
        esp_ota_mark_app_invalid_rollback_and_reboot();
        esp_restart();   /* only if there was nothing to roll back to; the next boot judges it again */
    }
}

static bool send_command(const uint8_t *bytes, size_t n)
{
    if (!s_peer_added) {
        esp_now_peer_info_t peer = {.channel = 0, .ifidx = WIFI_IF_STA, .encrypt = false};
        memcpy(peer.peer_addr, BROADCAST_MAC, 6);
        esp_err_t err = esp_now_add_peer(&peer);
        if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
            return false;
        }
        /* HT20 MCS0 like round broadcasts: transceivers take CSI frames at HT20 only, and hear nothing else */
        esp_now_rate_config_t rate = {.phymode = WIFI_PHY_MODE_HT20, .rate = WIFI_PHY_RATE_MCS0_LGI};
        if (esp_now_set_peer_rate_config(BROADCAST_MAC, &rate) != ESP_OK) {
            return false;
        }
        s_peer_added = true;
    }
    return esp_now_send(BROADCAST_MAC, bytes, n) == ESP_OK;
}

static const char *state_name(esp_ota_img_states_t st)
{
    switch (st) {
    case ESP_OTA_IMG_NEW: return "new";
    case ESP_OTA_IMG_PENDING_VERIFY: return "pending_verify";
    case ESP_OTA_IMG_VALID: return "valid";
    case ESP_OTA_IMG_INVALID: return "invalid";
    case ESP_OTA_IMG_ABORTED: return "aborted";
    default: return "undefined";
    }
}

static int nibble(char c)
{
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                                                                                     : -1;
}

bool node_ota_command(const char *line, char *reply, size_t n, node_cmd_t *action)
{
    while (*line == ' ' || *line == '\t') {
        line++;
    }
    if (strncmp(line, "ota", 3) != 0 || (line[3] != '\0' && line[3] != ' ' && line[3] != '\t')) {
        return false;
    }
    const char *p = line + 3;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    *action = NODE_CMD_NONE;
    if (*p == '\0') {
        char image[17] = "-";
        static const uint8_t zero[OTA_SHA_BYTES] = {0};
        if (memcmp(s_record.running_sha, zero, sizeof zero) != 0) {
            hex(s_record.running_sha, 8, image);
        }
        snprintf(reply, n, "ok ota slot=ota_%u state=%s version=%s image=%s last_issued=%lu health_check=%s",
                 running_slot(), state_name(running_state()), esp_app_get_description()->version, image,
                 (unsigned long)s_record.last_issued, s_health_on ? "running" : "no");
        return true;
    }
    *action = NODE_CMD_ERROR;
    if (strncmp(p, "send ", 5) != 0) {
        snprintf(reply, n, "error ota takes nothing (status) or send <hex>");
        return true;
    }
    if (!s_gateway) {
        snprintf(reply, n, "error ota send is for a gateway (role=gateway): a transceiver sends only in its slot");
        return true;
    }
    p += 5;
    static uint8_t bytes[OTA_MAX_BYTES];
    size_t len = 0;
    while (p[0] && p[1] && nibble(p[0]) >= 0 && nibble(p[1]) >= 0 && len < sizeof bytes) {
        bytes[len++] = (uint8_t)(nibble(p[0]) << 4 | nibble(p[1]));
        p += 2;
    }
    ota_command_t c;
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
        p++;
    }
    if (*p != '\0' || ota_command_decode(bytes, len, &c) != OTA_OK) {
        snprintf(reply, n, "error ota send takes one maintenance command as hex (scripts/ota.py makes it)");
    } else if (!send_command(bytes, len)) {
        snprintf(reply, n, "error ota send: ESP-NOW refused the frame");
    } else {
        snprintf(reply, n, "ok ota sent %u bytes (command %lu)", (unsigned)len, (unsigned long)c.issued);
        *action = NODE_CMD_NONE;
    }
    return true;
}
