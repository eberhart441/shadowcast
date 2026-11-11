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

#if !CONFIG_IDF_TARGET_ESP32C5
#error "the node image is built for esp32c5 (idf.py set-target esp32c5)"
#endif

#define QUEUE_DEPTH 32
#define PAYLOAD_COPY (8 + RB_MAX_FRAME_BYTES + 7 * 2 + 16)   /* our longest frame in two ESP-NOW v2 elements */
#define HEARD_MAX RB_MAX_FRAME_BYTES   /* a round broadcast with two raw CSI blocks; anything longer is not ours */
#define RAW_LOG_PERIOD_US 10000000  /* while raw mode is on, a log line this often */
#define GAIN_RECORD_FRAMES 100      /* as csi_recv: record 100 frames, then force their median gain */
#define REARM_TOLERANCE_US 100
#define STATUS_PERIOD_US 1000000
#define NODE_TASK_PRIORITY 6
#define USB_TASK_PRIORITY 3

static const char *TAG = "node";
static const uint8_t BROADCAST_MAC[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

static node_settings_t s_settings;
static bool s_configured;           /* settings were stored by scripts/node.py, not the defaults (which work too) */
static bool s_keyed = true;         /* false: a transceiver without the kit key, which only listens */
static uint8_t s_serial[6];         /* base MAC = USB serial = node id */
static char s_serial_text[18];
static uint8_t s_sta_mac[6];
static volatile uint32_t s_count[GR_STATUS_COUNTERS];

static void count(int which)
{
    __atomic_fetch_add(&s_count[which], 1, __ATOMIC_RELAXED);
}

/* ---------------------------------------------------------------- USB: records out, command lines in */

static SemaphoreHandle_t s_usb_lock;    /* one record at a time into the USB driver */
static SemaphoreHandle_t s_text_lock;   /* usb_text's buffers */

/* Write one whole record (the driver's ring buffer takes all of it or none). Counts a dropped record. */
static void usb_write(const uint8_t *record, size_t n, TickType_t wait)
{
    if (!n) {
        return;
    }
    if (xSemaphoreTake(s_usb_lock, wait) != pdTRUE) {
        count(GR_COUNT_USB_DROPPED);
        return;
    }
    int written = usb_serial_jtag_write_bytes(record, n, wait);
    xSemaphoreGive(s_usb_lock);
    if (written != (int)n) {
        count(GR_COUNT_USB_DROPPED);
    }
}

static void usb_text(const char *fmt, ...)
{
    static char text[200];
    static uint8_t record[sizeof text + GR_FRAME_BYTES];
    if (xSemaphoreTake(s_text_lock, pdMS_TO_TICKS(20)) != pdTRUE) {
        count(GR_COUNT_USB_DROPPED);
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
    usb_write(record, gr_encode_text(record, sizeof record, text, (size_t)(n > 0 ? n : 0)), pdMS_TO_TICKS(20));
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
        usb_write(record, gr_encode_text(record, sizeof record, line, (size_t)len), 0);
    }
    busy = false;
    return n;
}

static void send_status(void)
{
    gr_node_status_t st = {0};
    uint8_t record[GR_STATUS_HEAD + 4 * GR_STATUS_COUNTERS + GR_FRAME_BYTES];
    memcpy(st.serial, s_serial, 6);
    st.uptime_us = (uint64_t)esp_timer_get_time();
    st.reset_reason = (uint8_t)esp_reset_reason();
    st.role = s_settings.role == NODE_ROLE_GATEWAY ? GR_ROLE_GATEWAY : GR_ROLE_TRANSCEIVER;
    st.channel = s_settings.channel;
    for (int i = 0; i < GR_STATUS_COUNTERS; i++) {
        st.counters[i] = s_count[i];
    }
    usb_write(record, gr_encode_status(record, sizeof record, &st), 0);
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

static bool gateway_send_command(const char *line, char *reply, size_t n);

static void run_command(const char *line)
{
    char reply[200];
    if (s_settings.role == NODE_ROLE_GATEWAY && gateway_send_command(line, reply, sizeof reply)) {
        usb_text("%s", reply);
        return;
    }
    node_settings_t next = s_settings;
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

static bool round_member(void);

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
        node_ota_tick(round_member(), s_count[GR_COUNT_HEARD], s_count[GR_COUNT_SENT]);
        if (now >= next_status) {
            send_status();
            next_status = now + STATUS_PERIOD_US;
        }
        if (!s_keyed && now >= next_nag) {
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
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));
    wifi_protocols_t protocols = {.ghz_2g = WIFI_PROTOCOL_11N, .ghz_5g = WIFI_PROTOCOL_11N};
    ESP_ERROR_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protocols));
    wifi_bandwidths_t bandwidths = {.ghz_2g = WIFI_BW20, .ghz_5g = WIFI_BW20};   /* HT20, not the hackathon's HT40 */
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, s_sta_mac));
}

/* Unacknowledged ESP-NOW broadcasts at HT20 MCS0, as round broadcasts go. */
static void add_broadcast_peer(void)
{
    esp_now_peer_info_t peer = {.channel = 0, .ifidx = WIFI_IF_STA, .encrypt = false};
    memcpy(peer.peer_addr, BROADCAST_MAC, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate = {.phymode = WIFI_PHY_MODE_HT20, .rate = WIFI_PHY_RATE_MCS0_LGI, .ersu = false,
                                  .dcm = false};
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(BROADCAST_MAC, &rate));
}

/* ---------------------------------------------------------------- transceiver */

enum { ITEM_HEARD = 1, ITEM_SLOT = 2, ITEM_SENT = 3 };

typedef struct {
    uint8_t kind;
    rs_tx_t tx;                         /* ITEM_SLOT: what the slot timer was armed for */
    int64_t t_us;                       /* ITEM_HEARD: device time at the CSI callback; ITEM_SENT: at the send callback */
    wifi_pkt_rx_ctrl_t rx_ctrl;
    uint16_t csi_len;                   /* 0 when the CSI did not fit */
    uint16_t payload_len;
    int8_t csi[CSI_LAYOUT_MAX_RAW];
    uint8_t payload[PAYLOAD_COPY];
} node_item_t;

static QueueHandle_t s_queue;
static esp_timer_handle_t s_slot_timer;
static rs_t s_sched;
static bool s_sched_ok;
static link_table_t s_links;
static csi_layout_t s_layout;
static rs_tx_t s_armed_tx;              /* written by the node task before the timer starts, read by on_slot */
static bool s_armed;
static int64_t s_sent_slot_at;          /* the last frame sent: its planned time and length, for rs_sent */
static size_t s_sent_len;
static uint32_t s_gain_frames;
static raw_mode_t s_raw;                /* raw mode: the links that stream CSI, and what was kept for them */

/* Wi-Fi task: copy and queue, nothing else. */
static void on_csi(void *ctx, wifi_csi_info_t *info)
{
    static node_item_t item;
    if (!info || !info->buf || !info->payload || info->payload_len < ESPNOW_BODY_OFFSET
            || info->payload[0] != 127 || info->payload[1] != 0x18 || info->payload[2] != 0xfe
            || info->payload[3] != 0x34 || memcmp(info->mac, s_sta_mac, 6) == 0) {
        return;   /* not an ESP-NOW frame (other Wi-Fi on the channel), or this node's own broadcast */
    }
    item.kind = ITEM_HEARD;
    item.t_us = esp_timer_get_time();
    item.rx_ctrl = info->rx_ctrl;
    item.csi_len = info->len <= CSI_LAYOUT_MAX_RAW ? info->len : 0;
    memcpy(item.csi, info->buf, item.csi_len);
    item.payload_len = info->payload_len <= PAYLOAD_COPY ? info->payload_len : PAYLOAD_COPY;
    memcpy(item.payload, info->payload, item.payload_len);
    if (xQueueSend(s_queue, &item, 0) != pdTRUE) {
        count(GR_COUNT_QUEUE_FULL);
    }
}

/* esp_timer task: this node's slot is due. Jump the queue. */
static void on_slot(void *arg)
{
    static node_item_t item;
    item.kind = ITEM_SLOT;
    item.tx = s_armed_tx;
    if (xQueueSendToFront(s_queue, &item, 0) != pdTRUE) {
        count(GR_COUNT_SEND_ERRORS);   /* a missed slot; try again next round so the node never falls silent */
        esp_timer_start_once(s_slot_timer, (uint64_t)s_sched.period_us);
    }
}

/* Wi-Fi task: the frame left. Its time goes to the scheduler (this node's send latency). */
static void on_sent(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    static node_item_t item;
    if (status != ESP_NOW_SEND_SUCCESS) {
        count(GR_COUNT_SEND_ERRORS);
        return;
    }
    item.kind = ITEM_SENT;
    item.t_us = esp_timer_get_time();
    xQueueSend(s_queue, &item, 0);   /* a lost one only costs one latency sample */
}

/* For the OTA health check (node_ota_tick, from the USB task): this node is a member of a round. */
static bool round_member(void)
{
    return s_sched_ok && s_sched.state == RS_MEMBER;
}

/* Links whose node index now names another node start over. */
static void apply_restarts(void)
{
    uint32_t restarts = rs_take_restarts(&s_sched);
    for (uint8_t i = 0; i < RB_MAX_NODES; i++) {
        if (restarts >> i & 1u) {
            link_table_restart(&s_links, i);
        }
    }
}

/* One log line (a text record on USB) whenever this node's place in the round changes. */
static void log_schedule(void)
{
    static rs_state_t state = RS_LISTENING;
    static uint8_t version, index = RS_NO_INDEX;
    static uint32_t membership;
    const rs_t *s = &s_sched;
    if (s->state == state && s->version == version && s->my_index == index && s->membership == membership) {
        return;
    }
    state = s->state;
    version = s->version;
    index = s->my_index;
    membership = s->membership;
    static const char *const names[] = {"listening", "joining", "member"};
    ESP_LOGI(TAG, "round: %s, schedule %u, index %d, membership %08lx (joins %lu dropped %lu merges %lu late %lu)",
             names[state], version, index == RS_NO_INDEX ? -1 : index, (unsigned long)membership,
             (unsigned long)s->count.joins, (unsigned long)s->count.dropped, (unsigned long)s->count.merges,
             (unsigned long)s->count.late_slots);
}

/* Arm the slot timer for the scheduler's next transmission. Without `force` it stays armed when the plan moved
 * by less than REARM_TOLERANCE_US (rs_take accepts a slot within the guard). */
static void arm_next_slot(bool force)
{
    if (!s_sched_ok || !s_keyed) {
        return;
    }
    int64_t now = esp_timer_get_time();
    rs_tx_t tx = rs_next(&s_sched, now + 200);
    if (!force && s_armed && tx.kind == s_armed_tx.kind && tx.round == s_armed_tx.round
            && llabs(tx.at_us - s_armed_tx.at_us) < REARM_TOLERANCE_US) {
        return;
    }
    esp_timer_stop(s_slot_timer);   /* an error when it is not running is fine */
    s_armed_tx = tx;
    int64_t wait = tx.at_us - now;
    s_armed = esp_timer_start_once(s_slot_timer, (uint64_t)(wait > 0 ? wait : 1)) == ESP_OK;
}

/* The slot timer fired: send what the scheduler still wants sent (a round broadcast or a join frame). */
static void send_slot(const rs_tx_t *tx)
{
    static rb_broadcast_t b;
    static uint8_t bytes[RB_MAX_FRAME_BYTES];
    if (!rs_take(&s_sched, tx, esp_timer_get_time(), &b)) {
        return;   /* the plan changed, or the slot came more than the guard late (rs_t count.late_slots) */
    }
    if (b.frame_type == RB_FRAME_ROUND) {
        b.n_reports = link_table_close_round(&s_links, b.membership, b.node_index, b.reports);
        uint32_t raw = raw_mode_flags(&s_raw, s_sched.member_id, s_sched.id_known);
        for (uint8_t k = 0; k < b.n_reports; k++) {
            b.reports[k].lateness = rs_take_lateness(&s_sched, b.reports[k].transmitter_index);
            if (raw >> b.reports[k].transmitter_index & 1u) {
                b.reports[k].flags |= RB_FLAG_RAW_MODE;   /* the host's acknowledgement */
            }
        }
        size_t base = rb_size(b.membership), cap = rs_frame_cap(&s_sched);
        b.n_raw = raw_mode_take(&s_raw, b.membership, b.node_index, cap > base ? cap - base : 0, b.raw);
        if (b.n_raw) {
            b.frame_type = RB_FRAME_ROUND_RAW;
        }
    }
    size_t n = rb_encode(&b, bytes, sizeof bytes);
    s_sent_slot_at = tx->at_us;
    s_sent_len = n;
    if (n && node_auth_sign(bytes, n) && esp_now_send(BROADCAST_MAC, bytes, n) == ESP_OK) {
        count(GR_COUNT_SENT);
    } else {
        count(GR_COUNT_SEND_ERRORS);
    }
}

static float gain_compensation(const wifi_pkt_rx_ctrl_t *rx_ctrl)
{
    uint8_t agc = 0, agc_base = 0;
    int8_t fft = 0, fft_base = 0;
    float compensate = 1.0f;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc, &fft);
    if (s_gain_frames < GAIN_RECORD_FRAMES) {
        esp_csi_gain_ctrl_record_rx_gain(agc, fft);
    } else if (s_gain_frames == GAIN_RECORD_FRAMES) {
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_base, &fft_base);
        esp_csi_gain_ctrl_set_rx_force_gain(agc_base, fft_base);
        ESP_LOGI(TAG, "gain forced: agc %u fft %d", agc_base, fft_base);
    }
    s_gain_frames++;
    esp_csi_gain_ctrl_get_gain_compensation(&compensate, agc, fft);
    return compensate;
}

/* One log line whenever this node's raw mode changes, and every RAW_LOG_PERIOD_US while it is on. */
static void log_raw(bool changed)
{
    static int64_t next;
    int64_t now = esp_timer_get_time();
    if (!changed && (s_raw.n == 0 || now < next)) {
        return;
    }
    next = now + RAW_LOG_PERIOD_US;
    ESP_LOGI(TAG, "raw mode: %u link(s) (requests %lu applied %lu ignored; blocks %lu sent %lu skipped)",
             s_raw.n, (unsigned long)s_raw.count.applied, (unsigned long)s_raw.count.ignored,
             (unsigned long)s_raw.count.sent, (unsigned long)s_raw.count.skipped);
}

static void on_heard(const node_item_t *item)
{
    static rb_broadcast_t b;
    static float amp[CSI_LAYOUT_MAX_RAW / 2];
    static uint8_t body[RB_MAX_FRAME_BYTES];
    size_t body_len;
    if (!espnow_gather(item->payload, item->payload_len, body, sizeof body, &body_len)) {
        count(GR_COUNT_REJECTED);   /* not ESP-NOW, or longer than any frame of ours */
        return;
    }
    if (node_ota_heard(body, body_len, item->t_us)) {
        return;   /* a maintenance command (node_ota.h) */
    }
    if (rb_decode(body, body_len, &b) != RB_OK || !node_auth_accept(body, body_len, item->t_us)) {
        count(GR_COUNT_REJECTED);   /* not a v2 frame of ours, forged or replayed */
        return;
    }
    if (b.frame_type == RB_FRAME_RAW_REQUEST) {
        log_raw(raw_mode_apply(&s_raw, &b.request, s_serial, s_sched.last_sent_round) == RAW_APPLIED);
        return;
    }
    if (!s_sched_ok || rs_heard(&s_sched, item->t_us, &b) != RS_HEARD_MEMBER) {
        return;   /* a join frame, or a round broadcast from outside this node's schedule: no link to score */
    }
    count(GR_COUNT_HEARD);
    raw_mode_heard(&s_raw, b.node_id, b.node_index, b.round, (int8_t)item->rx_ctrl.rssi, item->csi, item->csi_len);
    float gain = gain_compensation(&item->rx_ctrl);
    int n = item->csi_len ? csi_layout_amplitudes(&s_layout, item->csi, item->csi_len, gain, amp) : 0;
    if (n == 0) {
        count(GR_COUNT_CSI_SKIPPED);
    }
    link_table_heard(&s_links, b.node_index, item->t_us, (int8_t)item->rx_ctrl.rssi, n ? amp : NULL, (uint16_t)n);
}

static void node_task(void *arg)
{
    static node_item_t item;
    arm_next_slot(true);
    for (;;) {
        if (xQueueReceive(s_queue, &item, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (item.kind == ITEM_SLOT) {
            send_slot(&item.tx);
            arm_next_slot(true);
        } else if (item.kind == ITEM_SENT) {
            rs_sent(&s_sched, s_sent_slot_at, item.t_us, s_sent_len);
        } else {
            on_heard(&item);
            arm_next_slot(false);
        }
        apply_restarts();
        log_schedule();
        log_raw(false);
    }
}

static void start_transceiver(void)
{
    link_table_init(&s_links);
    csi_layout_init(&s_layout);
    raw_mode_init(&s_raw);
    uint32_t seed = esp_random();
    s_sched_ok = rs_init(&s_sched, s_serial, s_settings.round_hz, s_settings.guard_us, seed, esp_timer_get_time());
    if (!s_sched_ok) {
        ESP_LOGE(TAG, "round_hz %u with guard_us %u makes no round; this node does nothing", s_settings.round_hz,
                 s_settings.guard_us);
    }
    s_queue = xQueueCreate(QUEUE_DEPTH, sizeof(node_item_t));
    configASSERT(s_queue);

    add_broadcast_peer();
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_sent));

    const esp_timer_create_args_t slot_args = {.callback = on_slot, .name = "slot"};
    ESP_ERROR_CHECK(esp_timer_create(&slot_args, &s_slot_timer));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    wifi_csi_config_t csi = {
        .enable = true,
        .acquire_csi_legacy = false,
        .acquire_csi_force_lltf = false,
        .acquire_csi_ht20 = true,       /* round broadcasts are HT20 */
        .acquire_csi_ht40 = false,
        .acquire_csi_vht = false,
        .acquire_csi_su = false,
        .acquire_csi_mu = false,
        .acquire_csi_dcm = false,
        .acquire_csi_beamformed = false,
        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg = 0,
        .dump_ack_en = false,
        .reserved = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(on_csi, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
    xTaskCreate(node_task, "node", 6144, NULL, NODE_TASK_PRIORITY, NULL);
}

/* ---------------------------------------------------------------- gateway */

enum { GW_HEARD = 1, GW_SEND = 2, GW_TIMER = 3 };

typedef struct {
    uint8_t kind;               /* GW_HEARD: a frame heard; GW_SEND: one to send (from USB); GW_TIMER: send now */
    int64_t t_us;
    int8_t rssi;
    uint16_t len;
    uint8_t data[HEARD_MAX];
} heard_item_t;

static QueueHandle_t s_heard_queue;
static gt_t s_gt;                       /* the gateway's sending side; only gateway_task touches it */
static esp_timer_handle_t s_gt_timer;
static int64_t s_gt_armed = -1;

/* USB task: "send <hex>" queues a frame for the gateway task. */
static bool gateway_send_command(const char *line, char *reply, size_t n)
{
    static heard_item_t item;
    size_t len = 0;
    if (strncmp(line, "send", 4) != 0) {
        return false;
    }
    if (!gt_parse_send(line, item.data, GT_FRAME_MAX, &len)) {
        snprintf(reply, n, "error send takes one frame of 1 to %d bytes in hex", GT_FRAME_MAX);
        return true;
    }
    item.kind = GW_SEND;
    item.t_us = esp_timer_get_time();
    item.len = (uint16_t)len;
    bool queued = xQueueSend(s_heard_queue, &item, pdMS_TO_TICKS(20)) == pdTRUE;
    snprintf(reply, n, queued ? "ok send %u bytes queued" : "error send: the gateway is busy", (unsigned)len);
    return true;
}

/* esp_timer task: the planned join slot is due. */
static void on_gt_timer(void *arg)
{
    static heard_item_t item;
    item.kind = GW_TIMER;
    xQueueSendToFront(s_heard_queue, &item, 0);
}

static void on_gateway_sent(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    count(status == ESP_NOW_SEND_SUCCESS ? GR_COUNT_SENT : GR_COUNT_SEND_ERRORS);
}

/* Wi-Fi task: copy and queue, nothing else. */
static void on_gateway_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    static heard_item_t item;
    if (len < 0 || len > HEARD_MAX) {
        count(GR_COUNT_REJECTED);
        return;
    }
    item.kind = GW_HEARD;
    item.t_us = esp_timer_get_time();
    item.rssi = info && info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : RB_RSSI_NONE;
    item.len = (uint16_t)len;
    memcpy(item.data, data, (size_t)len);
    count(GR_COUNT_HEARD);
    if (xQueueSend(s_heard_queue, &item, 0) != pdTRUE) {
        count(GR_COUNT_QUEUE_FULL);
    }
}

static void gateway_task(void *arg)
{
    static heard_item_t item;
    static uint8_t record[GR_HEARD_HEAD + HEARD_MAX + GR_FRAME_BYTES];
    static rb_broadcast_t b;
    static uint8_t frame[GT_FRAME_MAX];
    for (;;) {
        if (xQueueReceive(s_heard_queue, &item, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        int64_t now = esp_timer_get_time();
        if (item.kind == GW_HEARD) {
            usb_write(record, gr_encode_heard(record, sizeof record, (uint64_t)item.t_us, item.rssi, item.data,
                                              item.len), pdMS_TO_TICKS(10));
            if (rb_decode(item.data, item.len, &b) == RB_OK) {
                gt_heard(&s_gt, item.t_us, &b);   /* slot 0 gives the schedule's timing (tag not checked) */
            }
        } else if (item.kind == GW_SEND) {
            if (!gt_queue(&s_gt, item.data, item.len, now)) {
                ESP_LOGW(TAG, "send: queue full, frame dropped (%lu dropped)", (unsigned long)s_gt.count.dropped);
            }
        } else {
            s_gt_armed = -1;
            size_t n = gt_take(&s_gt, now, frame, sizeof frame);
            if (n && esp_now_send(BROADCAST_MAC, frame, n) != ESP_OK) {
                count(GR_COUNT_SEND_ERRORS);
            }
        }
        int64_t at = gt_next(&s_gt, now + 200);
        if (at > 0 && at != s_gt_armed) {
            esp_timer_stop(s_gt_timer);
            s_gt_armed = esp_timer_start_once(s_gt_timer, (uint64_t)(at - now)) == ESP_OK ? at : -1;
        }
    }
}

static void start_gateway(void)
{
    gt_init(&s_gt);
    s_heard_queue = xQueueCreate(64, sizeof(heard_item_t));
    configASSERT(s_heard_queue);
    add_broadcast_peer();
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_gateway_sent));
    const esp_timer_create_args_t gt_args = {.callback = on_gt_timer, .name = "gw_send"};
    ESP_ERROR_CHECK(esp_timer_create(&gt_args, &s_gt_timer));
    xTaskCreate(gateway_task, "gateway", 4096, NULL, NODE_TASK_PRIORITY, NULL);
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_gateway_recv));
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
    ESP_ERROR_CHECK(esp_read_mac(s_serial, ESP_MAC_BASE));
    snprintf(s_serial_text, sizeof s_serial_text, "%02X:%02X:%02X:%02X:%02X:%02X", s_serial[0], s_serial[1],
             s_serial[2], s_serial[3], s_serial[4], s_serial[5]);

    usb_serial_jtag_driver_config_t usb = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb.tx_buffer_size = 8192;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));
    s_usb_lock = xSemaphoreCreateMutex();
    s_text_lock = xSemaphoreCreateMutex();
    configASSERT(s_usb_lock && s_text_lock);
    esp_log_set_vprintf(log_to_usb);

    s_configured = load_settings(&s_settings);
    if (!node_auth_start() && s_settings.role != NODE_ROLE_GATEWAY) {
        s_keyed = false;   /* a transceiver without the kit key only listens: it cannot sign or verify */
    }
    node_ota_boot(s_serial, s_settings.channel, s_settings.role == NODE_ROLE_GATEWAY);   /* maintenance mode: no return */
    wifi_start(s_settings.channel);
    ESP_ERROR_CHECK(esp_now_init());
    if (s_settings.role == NODE_ROLE_GATEWAY) {
        start_gateway();
    } else {
        start_transceiver();
    }
    xTaskCreate(usb_task, "usb", 4096, NULL, USB_TASK_PRIORITY, NULL);

    uint32_t espnow_version = 0;
    esp_now_get_version(&espnow_version);
    if (espnow_version < 2) {
        ESP_LOGW(TAG, "ESP-NOW v%lu: round broadcasts with raw CSI over 250 bytes cannot be sent or heard",
                 (unsigned long)espnow_version);
    }
    char text[96];
    node_settings_format(&s_settings, text, sizeof text);
    ESP_LOGI(TAG, "node %s: %s%s%s (HT20 MCS0)", s_serial_text, text, s_configured ? "" : " configured=no",
             s_keyed ? "" : " no kit key: listening only");
}
