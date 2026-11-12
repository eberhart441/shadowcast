/*
 * The node image: one firmware for every board of the transceiver rig (spec #2, ticket #13).
 *
 * What a board does is set at runtime, from its settings in NVS (firmware/core/node_settings.h, written over USB by
 * scripts/node.py), never by a build flag. Its identity is its board serial (base MAC).
 *
 *   transceiver  The round scheduler (firmware/core/round_scheduler.h) gives it a node index and a slot: it
 *                listens, joins the round it hears (or founds one), and in its slot sends a round broadcast
 *                (docs/round-broadcast.md): an unacknowledged ESP-NOW broadcast at HT20 MCS0 carrying one link
 *                report per other member, signed with the kit key (node_auth.h). It measures CSI on every other
 *                member's round broadcast whose tag and round number check out, and scores each link with the C
 *                scorer. It needs no settings (the defaults join any round on channel 6 at 40 Hz), only the kit key:
 *                without it (scripts/node.py PORT key FILE) it only listens.
 *   gateway      Listens on the channel. Every ESP-NOW frame it hears goes to USB as a binary heard record with
 *                its receive time (docs/gateway-record.md). The only thing it sends is a frame the host hands it
 *                (`send <hex>`: a raw mode request the host signed), in the join slot of a round the scheduler
 *                numbers even (firmware/core/gateway_tx.h).
 *
 * Raw mode (ticket #17): a transceiver that takes a raw mode request for itself (firmware/core/raw_mode.h) flags
 * those links raw in its link reports and appends the last CSI heard on them (int8 I/Q) to its round broadcasts,
 * as much as fits its slot (rs_frame_cap). Those frames are up to 416 bytes: ESP-NOW v2, which the CSI callback
 * hands over in several vendor elements (espnow_gather).
 *
 * The USB Serial/JTAG port carries only gateway records: status once a second, text (log lines and command
 * replies), and on a gateway the heard records. Nothing is printed on the UART and no CSI is ever formatted as text.
 * The Wi-Fi callbacks only copy what they need into a queue; all work happens in the node task. Gain handling is
 * the hackathon receiver's (record 100 frames, then force the median gain) until ticket #15 replaces it.
 *
 * Files: this one (USB, settings, radio, app_main), node_transceiver.c and node_gateway.c (one per role), sharing
 * node_common.h; node_auth.c (the kit key) and node_ota.c (maintenance mode).
 *
 * Logic that does not need ESP-IDF lives in firmware/core and is tested on the PC (firmware/core/test.sh).
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "driver/usb_serial_jtag.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "csi_layout.h"
#include "espnow_frame.h"
#include "gateway_record.h"
#include "gateway_tx.h"
#include "link_table.h"
#include "node_auth.h"
#include "node_ota.h"
#include "node_settings.h"
#include "raw_mode.h"
#include "round_broadcast.h"
#include "round_scheduler.h"

#include "node_common.h"

#if !CONFIG_IDF_TARGET_ESP32C5
#error "the node image is built for esp32c5 (idf.py set-target esp32c5)"
#endif

static const char *TAG = "node";
const uint8_t NODE_BROADCAST_MAC[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

node_settings_t g_node_settings;
static bool s_configured;           /* settings were stored by scripts/node.py, not the defaults (which work too) */
bool g_node_keyed = true;         /* false: a transceiver without the kit key, which only listens */
uint8_t g_node_serial[6];         /* base MAC = USB serial = node id */
static char s_serial_text[18];
uint8_t g_node_sta_mac[6];
static volatile uint32_t s_count[GR_STATUS_COUNTERS];

void node_count(int which)
{
    __atomic_fetch_add(&s_count[which], 1, __ATOMIC_RELAXED);
}

/* ---------------------------------------------------------------- USB: records out, command lines in */

static SemaphoreHandle_t s_usb_lock;    /* one record at a time into the USB driver */
static SemaphoreHandle_t s_text_lock;   /* usb_text's buffers */

/* Write one whole record (the driver's ring buffer takes all of it or none). Counts a dropped record. */
void node_usb_write(const uint8_t *record, size_t n, TickType_t wait)
{
    if (!n) {
        return;
    }
    if (xSemaphoreTake(s_usb_lock, wait) != pdTRUE) {
        node_count(GR_COUNT_USB_DROPPED);
        return;
    }
    int written = usb_serial_jtag_write_bytes(record, n, wait);
    xSemaphoreGive(s_usb_lock);
    if (written != (int)n) {
        node_count(GR_COUNT_USB_DROPPED);
    }
}

static void usb_text(const char *fmt, ...)
{
    static char text[200];
    static uint8_t record[sizeof text + GR_FRAME_BYTES];
    if (xSemaphoreTake(s_text_lock, pdMS_TO_TICKS(20)) != pdTRUE) {
        node_count(GR_COUNT_USB_DROPPED);
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof text - 1) {
        n = (int)sizeof text - 1;
    }
    while (n > 0 && (text[n - 1] == '\n' || text[n - 1] == '\r')) {
        n--;
    }
    node_usb_write(record, gr_encode_text(record, sizeof record, text, (size_t)(n > 0 ? n : 0)), pdMS_TO_TICKS(20));
    xSemaphoreGive(s_text_lock);
}

/* ESP_LOG output goes to USB as text records instead of the UART console. Never blocks. */
static int log_to_usb(const char *fmt, va_list ap)
{
    static char line[200];
    static uint8_t record[sizeof line + GR_FRAME_BYTES];
    static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
    static volatile bool busy;
    bool mine = false;
    portENTER_CRITICAL(&mux);
    if (!busy) {
        busy = mine = true;
    }
    portEXIT_CRITICAL(&mux);
    if (!mine) {
        return 0;
    }
    int n = vsnprintf(line, sizeof line, fmt, ap);
    int len = n > (int)sizeof line - 1 ? (int)sizeof line - 1 : n;
    while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) {
        len--;
    }
    if (len > 0) {
        node_usb_write(record, gr_encode_text(record, sizeof record, line, (size_t)len), 0);
    }
    busy = false;
    return n;
}

static void send_status(void)
{
    gr_node_status_t st = {0};
    uint8_t record[GR_STATUS_HEAD + 4 * GR_STATUS_COUNTERS + GR_FRAME_BYTES];
    memcpy(st.serial, g_node_serial, 6);
    st.uptime_us = (uint64_t)esp_timer_get_time();
    st.reset_reason = (uint8_t)esp_reset_reason();
    st.role = g_node_settings.role == NODE_ROLE_GATEWAY ? GR_ROLE_GATEWAY : GR_ROLE_TRANSCEIVER;
    st.channel = g_node_settings.channel;
    for (int i = 0; i < GR_STATUS_COUNTERS; i++) {
        st.counters[i] = s_count[i];
    }
    node_usb_write(record, gr_encode_status(record, sizeof record, &st), 0);
}

static bool load_settings(node_settings_t *s)
{
    nvs_handle_t h;
    uint8_t blob[16];
    size_t size = sizeof blob;
    bool ok = false;
    if (nvs_open("node", NVS_READONLY, &h) == ESP_OK) {
        ok = nvs_get_blob(h, "settings", blob, &size) == ESP_OK;
        nvs_close(h);
    }
    /* version 1 (the fixed round's index and nodes) keeps its role, channel and round rate */
    return node_settings_load(blob, ok ? size : 0, s);
}

static esp_err_t save_settings(const node_settings_t *s)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open("node", NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_blob(h, "settings", s, sizeof *s);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    return err;
}

static void run_command(const char *line)
{
    char reply[200];
    if (g_node_settings.role == NODE_ROLE_GATEWAY && node_gateway_command(line, reply, sizeof reply)) {
        usb_text("%s", reply);
        return;
    }
    node_settings_t next = g_node_settings;
    node_cmd_t action;
    if (!node_auth_command(line, reply, sizeof reply, &action) && !node_ota_command(line, reply, sizeof reply, &action)) {
        action = node_settings_command(&next, s_serial_text, line, reply, sizeof reply);
    }
    if (action == NODE_CMD_SAVE_AND_RESTART) {
        esp_err_t err = save_settings(&next);
        if (err != ESP_OK) {
            usb_text("error could not save the settings: %s", esp_err_to_name(err));
            return;
        }
    }
    if (reply[0]) {
        usb_text("%s%s", reply, (action == NODE_CMD_NONE && !s_configured && strncmp(line, "show", 4) == 0)
                                    ? " configured=no" : "");
    }
    if (action == NODE_CMD_SAVE_AND_RESTART || action == NODE_CMD_RESTART) {
        usb_serial_jtag_wait_tx_done(pdMS_TO_TICKS(500));
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    }
}

static void usb_task(void *arg)
{
    char line[384];                     /* "ota send <hex>" carries up to 150 bytes */
    size_t len = 0;
    uint8_t buf[64];
    int64_t next_status = esp_timer_get_time() + STATUS_PERIOD_US;
    int64_t next_nag = esp_timer_get_time();
    for (;;) {
        int n = usb_serial_jtag_read_bytes(buf, sizeof buf, pdMS_TO_TICKS(50));
        for (int i = 0; i < n; i++) {
            char c = (char)buf[i];
            if (c == '\n' || c == '\r') {
                if (len) {
                    line[len] = '\0';
                    run_command(line);
                    len = 0;
                }
            } else if (len < sizeof line - 1) {
                line[len++] = c;
            }
        }
        int64_t now = esp_timer_get_time();
        node_ota_tick(node_round_member(), s_count[GR_COUNT_HEARD], s_count[GR_COUNT_SENT]);
        if (now >= next_status) {
            send_status();
            next_status = now + STATUS_PERIOD_US;
        }
        if (!g_node_keyed && now >= next_nag) {
            usb_text("no kit key: this node only listens until scripts/node.py gives it one (PORT key FILE; serial %s)",
                     s_serial_text);
            next_nag = now + 10 * STATUS_PERIOD_US;
        }
    }
}

/* ---------------------------------------------------------------- radio */

static void wifi_start(uint8_t channel)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* the band follows from the kit's channel (node_settings.h): 2.4 GHz by default, 5 GHz when configured */
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(node_channel_is_5ghz(channel) ? WIFI_BAND_MODE_5G_ONLY
                                                                         : WIFI_BAND_MODE_2G_ONLY));
    wifi_protocols_t protocols = {.ghz_2g = WIFI_PROTOCOL_11N, .ghz_5g = WIFI_PROTOCOL_11N};
    ESP_ERROR_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protocols));
    wifi_bandwidths_t bandwidths = {.ghz_2g = WIFI_BW20, .ghz_5g = WIFI_BW20};   /* HT20, not the hackathon's HT40 */
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, g_node_sta_mac));
}

/* Unacknowledged ESP-NOW broadcasts at HT20 MCS0, as round broadcasts go. */
void node_add_broadcast_peer(void)
{
    esp_now_peer_info_t peer = {.channel = 0, .ifidx = WIFI_IF_STA, .encrypt = false};
    memcpy(peer.peer_addr, NODE_BROADCAST_MAC, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate = {.phymode = WIFI_PHY_MODE_HT20, .rate = WIFI_PHY_RATE_MCS0_LGI, .ersu = false,
                                  .dcm = false};
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(NODE_BROADCAST_MAC, &rate));
}

/* ---------------------------------------------------------------- start */

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_ERROR_CHECK(esp_read_mac(g_node_serial, ESP_MAC_BASE));
    snprintf(s_serial_text, sizeof s_serial_text, "%02X:%02X:%02X:%02X:%02X:%02X", g_node_serial[0], g_node_serial[1],
             g_node_serial[2], g_node_serial[3], g_node_serial[4], g_node_serial[5]);

    usb_serial_jtag_driver_config_t usb = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb.tx_buffer_size = 8192;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));
    s_usb_lock = xSemaphoreCreateMutex();
    s_text_lock = xSemaphoreCreateMutex();
    configASSERT(s_usb_lock && s_text_lock);
    esp_log_set_vprintf(log_to_usb);

    s_configured = load_settings(&g_node_settings);
    if (!node_auth_start() && g_node_settings.role != NODE_ROLE_GATEWAY) {
        g_node_keyed = false;   /* a transceiver without the kit key only listens: it cannot sign or verify */
    }
    node_ota_boot(g_node_serial, g_node_settings.channel, g_node_settings.role == NODE_ROLE_GATEWAY);   /* maintenance mode: no return */
    wifi_start(g_node_settings.channel);
    ESP_ERROR_CHECK(esp_now_init());
    if (g_node_settings.role == NODE_ROLE_GATEWAY) {
        node_gateway_start();
    } else {
        node_transceiver_start();
    }
    xTaskCreate(usb_task, "usb", 4096, NULL, USB_TASK_PRIORITY, NULL);

    uint32_t espnow_version = 0;
    esp_now_get_version(&espnow_version);
    if (espnow_version < 2) {
        ESP_LOGW(TAG, "ESP-NOW v%lu: round broadcasts with raw CSI over 250 bytes cannot be sent or heard",
                 (unsigned long)espnow_version);
    }
    char text[96];
    node_settings_format(&g_node_settings, text, sizeof text);
    ESP_LOGI(TAG, "node %s: %s%s%s (HT20 MCS0)", s_serial_text, text, s_configured ? "" : " configured=no",
             g_node_keyed ? "" : " no kit key: listening only");
}
