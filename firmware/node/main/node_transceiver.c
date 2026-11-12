/*
 * The transceiver role of the node image (node_main.c has the overview): the round scheduler's slots, the round
 * broadcast it sends in its slot, CSI on every member's round broadcast, the link table and raw mode.
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

static const char *TAG = "node";

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
            || info->payload[3] != 0x34 || memcmp(info->mac, g_node_sta_mac, 6) == 0) {
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
        node_count(GR_COUNT_QUEUE_FULL);
    }
}

/* esp_timer task: this node's slot is due. Jump the queue. */
static void on_slot(void *arg)
{
    static node_item_t item;
    item.kind = ITEM_SLOT;
    item.tx = s_armed_tx;
    if (xQueueSendToFront(s_queue, &item, 0) != pdTRUE) {
        node_count(GR_COUNT_SEND_ERRORS);   /* a missed slot; try again next round so the node never falls silent */
        esp_timer_start_once(s_slot_timer, (uint64_t)s_sched.period_us);
    }
}

/* Wi-Fi task: the frame left. Its time goes to the scheduler (this node's send latency). */
static void on_sent(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    static node_item_t item;
    if (status != ESP_NOW_SEND_SUCCESS) {
        node_count(GR_COUNT_SEND_ERRORS);
        return;
    }
    item.kind = ITEM_SENT;
    item.t_us = esp_timer_get_time();
    xQueueSend(s_queue, &item, 0);   /* a lost one only costs one latency sample */
}

/* For the OTA health check (node_ota_tick, from the USB task): this node is a member of a round. */
bool node_round_member(void)
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
    if (!s_sched_ok || !g_node_keyed) {
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
    if (n && node_auth_sign(bytes, n) && esp_now_send(NODE_BROADCAST_MAC, bytes, n) == ESP_OK) {
        node_count(GR_COUNT_SENT);
    } else {
        node_count(GR_COUNT_SEND_ERRORS);
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
        node_count(GR_COUNT_REJECTED);   /* not ESP-NOW, or longer than any frame of ours */
        return;
    }
    if (node_ota_heard(body, body_len, item->t_us)) {
        return;   /* a maintenance command (node_ota.h) */
    }
    if (rb_decode(body, body_len, &b) != RB_OK || !node_auth_accept(body, body_len, item->t_us)) {
        node_count(GR_COUNT_REJECTED);   /* not a v2 frame of ours, forged or stale */
        return;
    }
    if (b.frame_type == RB_FRAME_RAW_REQUEST) {
        log_raw(raw_mode_apply(&s_raw, &b.request, g_node_serial, s_sched.last_sent_round) == RAW_APPLIED);
        return;
    }
    if (!s_sched_ok || rs_heard(&s_sched, item->t_us, &b) != RS_HEARD_MEMBER) {
        return;   /* a join frame, or a round broadcast from outside this node's schedule: no link to score */
    }
    node_count(GR_COUNT_HEARD);
    raw_mode_heard(&s_raw, b.node_id, b.node_index, b.round, (int8_t)item->rx_ctrl.rssi, item->csi, item->csi_len);
    float gain = gain_compensation(&item->rx_ctrl);
    int n = item->csi_len ? csi_layout_amplitudes(&s_layout, item->csi, item->csi_len, gain, amp) : 0;
    if (n == 0) {
        node_count(GR_COUNT_CSI_SKIPPED);
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

void node_transceiver_start(void)
{
    link_table_init(&s_links);
    csi_layout_init(&s_layout);
    raw_mode_init(&s_raw);
    uint32_t seed = esp_random();
    s_sched_ok = rs_init(&s_sched, g_node_serial, g_node_settings.round_hz, g_node_settings.guard_us, seed, esp_timer_get_time());
    if (!s_sched_ok) {
        ESP_LOGE(TAG, "round_hz %u with guard_us %u makes no round; this node does nothing", g_node_settings.round_hz,
                 g_node_settings.guard_us);
    }
    s_queue = xQueueCreate(QUEUE_DEPTH, sizeof(node_item_t));
    configASSERT(s_queue);

    node_add_broadcast_peer();
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
