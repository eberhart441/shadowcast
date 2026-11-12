/*
 * The gateway role of the node image (node_main.c has the overview): every ESP-NOW frame heard goes to USB as a
 * heard record, and the frames the host hands it (raw mode requests) go out in the join slot.
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
bool node_gateway_command(const char *line, char *reply, size_t n)
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
    node_count(status == ESP_NOW_SEND_SUCCESS ? GR_COUNT_SENT : GR_COUNT_SEND_ERRORS);
}

/* Wi-Fi task: copy and queue, nothing else. */
static void on_gateway_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    static heard_item_t item;
    if (len < 0 || len > HEARD_MAX) {
        node_count(GR_COUNT_REJECTED);
        return;
    }
    item.kind = GW_HEARD;
    item.t_us = esp_timer_get_time();
    item.rssi = info && info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : RB_RSSI_NONE;
    item.len = (uint16_t)len;
    memcpy(item.data, data, (size_t)len);
    node_count(GR_COUNT_HEARD);
    if (xQueueSend(s_heard_queue, &item, 0) != pdTRUE) {
        node_count(GR_COUNT_QUEUE_FULL);
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
            node_usb_write(record, gr_encode_heard(record, sizeof record, (uint64_t)item.t_us, item.rssi, item.data,
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
            if (n && esp_now_send(NODE_BROADCAST_MAC, frame, n) != ESP_OK) {
                node_count(GR_COUNT_SEND_ERRORS);
            }
        }
        int64_t at = gt_next(&s_gt, now + 200);
        if (at > 0 && at != s_gt_armed) {
            esp_timer_stop(s_gt_timer);
            s_gt_armed = esp_timer_start_once(s_gt_timer, (uint64_t)(at - now)) == ESP_OK ? at : -1;
        }
    }
}

void node_gateway_start(void)
{
    gt_init(&s_gt);
    s_heard_queue = xQueueCreate(64, sizeof(heard_item_t));
    configASSERT(s_heard_queue);
    node_add_broadcast_peer();
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_gateway_sent));
    const esp_timer_create_args_t gt_args = {.callback = on_gt_timer, .name = "gw_send"};
    ESP_ERROR_CHECK(esp_timer_create(&gt_args, &s_gt_timer));
    xTaskCreate(gateway_task, "gateway", 4096, NULL, NODE_TASK_PRIORITY, NULL);
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_gateway_recv));
}
