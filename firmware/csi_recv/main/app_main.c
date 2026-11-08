/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* Get Start Example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "nvs_flash.h"

#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_csi_gain_ctrl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_timer.h"

/* Wireless receivers. Both default to 0, which is the stock wired receiver.
 *   CSI_FORWARD=1  a receiver sends each CSI line over ESP-NOW to the gateway instead of printing it. The
 *                  line starts "RX,<this board's USB serial>," so the Pi can tell receivers apart.
 *   CSI_GATEWAY=1  the one board plugged into the Pi: no CSI of its own, it writes every forwarded line to USB.
 * Both stay on the sensing channel with the same radio settings, so the CSI itself does not change. */
#ifndef CSI_FORWARD
#define CSI_FORWARD 0
#endif
#ifndef CSI_GATEWAY
#define CSI_GATEWAY 0
#endif
#if CSI_GATEWAY
#include "driver/usb_serial_jtag.h"
#endif
#if CSI_FORWARD || CSI_GATEWAY
static const uint8_t GATEWAY_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x02, 0x00};
#endif
#define FWD_LINE_MAX 1400   /* one CSI line; under ESP-NOW v2's 1470-byte limit */
typedef struct {
    uint16_t len;
    char buf[FWD_LINE_MAX];
} fwd_item_t;
static uint8_t s_base_mac[6];   /* factory MAC, which is also the USB serial */

#define CONFIG_LESS_INTERFERENCE_CHANNEL   6
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE               WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT40
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN                   1

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
#define CSI_FORCE_LLTF                      0
#endif

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define CONFIG_GAIN_CONTROL                 1
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#define WIFI_BW_HT20 WIFI_BW20   /* enums renamed in IDF 6.0 */
#define WIFI_BW_HT40 WIFI_BW40
#endif

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const char *TAG = "csi_recv";

static void wifi_init()
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

#if CONFIG_IDF_TARGET_ESP32C5
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
        .ghz_5g = CONFIG_WIFI_5G_PROTOCOL
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
        .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
#if CONFIG_IDF_TARGET_ESP32C5
    if ((CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20)
            || (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY && CONFIG_WIFI_5G_BANDWIDTHS == WIFI_BW_HT20)) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    if (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#else
    if (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#endif

#if CSI_GATEWAY
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, GATEWAY_MAC));
#elif CSI_FORWARD
    uint8_t own[6] = {0x1a, 0x00, 0x00, 0x00, 0x01, s_base_mac[5]};
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, own));
#else
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
#endif
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,//  WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));

}

#if CSI_FORWARD
static char s_line[FWD_LINE_MAX];
static int s_len;
#define OUT(...) do { if (s_len < FWD_LINE_MAX) s_len += snprintf(s_line + s_len, FWD_LINE_MAX - s_len, __VA_ARGS__); } while (0)
static QueueHandle_t s_fwd_queue;
static SemaphoreHandle_t s_fwd_sent;
/* once-a-second counters, logged by fwd_diag (on the receiver's own USB, if anyone listens) */
static volatile uint32_t s_n_built, s_n_qfull, s_n_trunc, s_n_send_ok, s_n_send_err, s_n_cb_ok, s_n_cb_fail;
static volatile esp_err_t s_last_err;

static void fwd_diag(void *arg)
{
    ESP_LOGW(TAG, "FWD 1s: built %lu qfull %lu trunc %lu sent %lu send_err %lu (last %s) cb_ok %lu cb_fail %lu",
             (unsigned long)s_n_built, (unsigned long)s_n_qfull, (unsigned long)s_n_trunc, (unsigned long)s_n_send_ok,
             (unsigned long)s_n_send_err, esp_err_to_name(s_last_err), (unsigned long)s_n_cb_ok,
             (unsigned long)s_n_cb_fail);
    s_n_built = s_n_qfull = s_n_trunc = s_n_send_ok = s_n_send_err = s_n_cb_ok = s_n_cb_fail = 0;
}

/* Queue the finished line for forward_task; the Wi-Fi callback must not wait on the radio. */
static void forward_line(void)
{
    static fwd_item_t item;   /* the CSI callback runs on one task, so one buffer is enough */
    s_n_built++;
    if (s_len >= FWD_LINE_MAX) {
        s_n_trunc++;
        return;               /* truncated: drop it rather than send half a line */
    }
    item.len = s_len;
    memcpy(item.buf, s_line, s_len);
    if (xQueueSend(s_fwd_queue, &item, 0) != pdTRUE) {   /* full queue: drop; the Pi sees it as loss */
        s_n_qfull++;
    }
}

static void on_fwd_sent(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    if (status == ESP_NOW_SEND_SUCCESS) {
        s_n_cb_ok++;
    } else {
        s_n_cb_fail++;
    }
    xSemaphoreGive(s_fwd_sent);
}

/* One send in flight at a time, as the ESP-NOW docs advise, but wait at most one tick (10 ms) for the
 * send-done callback: with CSI capture on it often never fires, and a 50 ms wait capped forwarding at about
 * 30 lines/s (measured on the rig), half of what a receiver hears. */
static void forward_task(void *arg)
{
    static fwd_item_t item;
    for (;;) {
        if (xQueueReceive(s_fwd_queue, &item, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        esp_err_t err = esp_now_send(GATEWAY_MAC, (const uint8_t *)item.buf, item.len);
        if (err == ESP_OK) {
            s_n_send_ok++;
            xSemaphoreTake(s_fwd_sent, 1);
        } else {
            s_n_send_err++;
            s_last_err = err;
        }
    }
}
#else
#define OUT(...) ets_printf(__VA_ARGS__)
#endif

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        ESP_LOGW(TAG, "<%s> wifi_csi_cb", esp_err_to_name(ESP_ERR_INVALID_ARG));
        return;
    }

    if (memcmp(info->mac, CONFIG_CSI_SEND_MAC, 5)) {  /* accept any sender 1a:00:00:00:00:xx */
        return;
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;
    static int s_count = 0;
    float compensate_gain = 1.0f;
    static uint8_t agc_gain = 0;
    static int8_t fft_gain = 0;
#if CONFIG_GAIN_CONTROL
    static uint8_t agc_gain_baseline = 0;
    static int8_t fft_gain_baseline = 0;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);
    if (s_count < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else if (s_count == 100) {
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_gain_baseline, &fft_gain_baseline);
#if CONFIG_FORCE_GAIN
        esp_csi_gain_ctrl_set_rx_force_gain(agc_gain_baseline, fft_gain_baseline);
        ESP_LOGD(TAG, "fft_force %d, agc_force %d", fft_gain_baseline, agc_gain_baseline);
#endif
    }
    esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain);
    /* Debug, not info: printed per packet it doubled the lines each receiver sends over its serial budget. */
    ESP_LOGD(TAG, "compensate_gain %f, agc_gain %d, fft_gain %d", compensate_gain, agc_gain, fft_gain);
#endif

    uint32_t rx_id = *(uint32_t *)(info->payload + 15);
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
    if (!s_count) {
        ESP_LOGI(TAG, "================ CSI RECV ================");
        ets_printf("type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,data\n");
    }

#if CSI_FORWARD
    s_len = 0;
    OUT("RX," MACSTR ",", MAC2STR(s_base_mac));
#endif
    OUT("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d",
               (int)rx_id, MAC2STR(info->mac), rx_ctrl->rssi, rx_ctrl->rate,
               rx_ctrl->noise_floor, fft_gain, agc_gain,  rx_ctrl->channel,
               rx_ctrl->timestamp, rx_ctrl->sig_len, rx_ctrl->cur_bb_format);
#else
    if (!s_count) {
        ESP_LOGI(TAG, "================ CSI RECV ================");
        ets_printf("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word,data\n");
    }

#if CSI_FORWARD
    s_len = 0;
    OUT("RX," MACSTR ",", MAC2STR(s_base_mac));
#endif
    OUT("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
               (int)rx_id, MAC2STR(info->mac), rx_ctrl->rssi, rx_ctrl->rate, rx_ctrl->sig_mode,
               rx_ctrl->mcs, rx_ctrl->cwb, rx_ctrl->smoothing, rx_ctrl->not_sounding,
               rx_ctrl->aggregation, rx_ctrl->stbc, rx_ctrl->fec_coding, rx_ctrl->sgi,
               rx_ctrl->noise_floor, rx_ctrl->ampdu_cnt, rx_ctrl->channel, rx_ctrl->secondary_channel,
               rx_ctrl->timestamp, rx_ctrl->ant, rx_ctrl->sig_len, rx_ctrl->sig_mode);

#endif
#if (CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61) && CSI_FORCE_LLTF
    int16_t csi = ((int16_t)(((((uint16_t)info->buf[1]) << 8) | info->buf[0]) << 4) >> 4);
    OUT(",%d,%d,\"[%d", (info->len - 2) / 2, info->first_word_invalid, (int16_t)(compensate_gain * csi));
    for (int i = 2; i < (info->len - 2); i += 2) {
        csi = ((int16_t)(((((uint16_t)info->buf[i + 1]) << 8) | info->buf[i]) << 4) >> 4);
        OUT(",%d", (int16_t)(compensate_gain * csi));
    }
#else
    OUT(",%d,%d,\"[%d", info->len, info->first_word_invalid, (int16_t)(compensate_gain * info->buf[0]));
    for (int i = 1; i < info->len; i++) {
        OUT(",%d", (int16_t)(compensate_gain * info->buf[i]));
    }
#endif
    OUT("]\"\n");
#if CSI_FORWARD
    forward_line();
#endif
    s_count++;
}

#if CSI_GATEWAY
static QueueHandle_t s_gw_queue;
static volatile uint32_t s_g_rx, s_g_qfull, s_g_short;   /* once-a-second counters, see gw_diag */

static void gw_diag(void *arg)
{
    char line[96];
    int n = snprintf(line, sizeof line, "GW 1s: received %lu qfull %lu short_writes %lu\n",
                     (unsigned long)s_g_rx, (unsigned long)s_g_qfull, (unsigned long)s_g_short);
    s_g_rx = s_g_qfull = s_g_short = 0;
    fwd_item_t *item = malloc(sizeof(fwd_item_t));
    if (item) {
        item->len = n;
        memcpy(item->buf, line, n);
        xQueueSend(s_gw_queue, item, 0);
        free(item);
    }
}

static void on_gateway_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    static const uint8_t receiver_prefix[] = {0x1a, 0x00, 0x00, 0x00, 0x01};
    static fwd_item_t item;
    if (memcmp(info->src_addr, receiver_prefix, 5) || len <= 3 || len > FWD_LINE_MAX || memcmp(data, "RX,", 3)) {
        return;   /* only forwarded CSI lines from our receivers */
    }
    s_g_rx++;
    item.len = len;
    memcpy(item.buf, data, len);
    if (xQueueSend(s_gw_queue, &item, 0) != pdTRUE) {
        s_g_qfull++;
    }
}

static void gateway_task(void *arg)
{
    static fwd_item_t item;
    for (;;) {
        if (xQueueReceive(s_gw_queue, &item, portMAX_DELAY) == pdTRUE) {
            if (usb_serial_jtag_write_bytes(item.buf, item.len, pdMS_TO_TICKS(100)) < item.len) {
                s_g_short++;
            }
        }
    }
}
#endif

static void wifi_csi_init()
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /**< default config */
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
    wifi_csi_config_t csi_config = {
        .enable                   = true,
        .acquire_csi_legacy       = false,
        .acquire_csi_force_lltf   = CSI_FORCE_LLTF,
        .acquire_csi_ht20         = true,
        .acquire_csi_ht40         = true,
        .acquire_csi_vht          = false,
        .acquire_csi_su           = false,
        .acquire_csi_mu           = false,
        .acquire_csi_dcm          = false,
        .acquire_csi_beamformed   = false,
        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg            = 0,
        .dump_ack_en              = false,
        .reserved                 = false
    };
#elif CONFIG_IDF_TARGET_ESP32C6
    wifi_csi_config_t csi_config = {
        .enable                 = true,
        .acquire_csi_legacy     = false,
        .acquire_csi_ht20       = true,
        .acquire_csi_ht40       = true,
        .acquire_csi_su         = true,
        .acquire_csi_mu         = true,
        .acquire_csi_dcm        = true,
        .acquire_csi_beamformed = true,
        .acquire_csi_he_stbc    = 2,
        .val_scale_cfg          = false,
        .dump_ack_en            = false,
        .reserved               = false
    };
#else
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
#endif
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

void app_main()
{
    /**
     * @brief Initialize NVS
     */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_ERROR_CHECK(esp_read_mac(s_base_mac, ESP_MAC_BASE));   /* 6-byte base MAC: the USB serial */

    /**
     * @brief Initialize Wi-Fi
     */
    wifi_init();

    /**
     * @brief Initialize ESP-NOW
     *        ESP-NOW protocol see: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/network/esp_now.html
     */

    esp_now_peer_info_t peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };

    wifi_esp_now_init(peer);

#if CSI_GATEWAY
    usb_serial_jtag_driver_config_t usb_cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_cfg.tx_buffer_size = 16384;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb_cfg));
    s_gw_queue = xQueueCreate(16, sizeof(fwd_item_t));
    xTaskCreate(gateway_task, "gateway", 4096, NULL, tskIDLE_PRIORITY + 2, NULL);
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_gateway_recv));
    esp_timer_handle_t diag;
    const esp_timer_create_args_t diag_args = {.callback = gw_diag, .name = "gw_diag"};
    ESP_ERROR_CHECK(esp_timer_create(&diag_args, &diag));
    ESP_ERROR_CHECK(esp_timer_start_periodic(diag, 1000000));
    ESP_LOGW(TAG, "CSI gateway on channel %d, mac " MACSTR, CONFIG_LESS_INTERFERENCE_CHANNEL, MAC2STR(GATEWAY_MAC));
    esp_log_level_set("*", ESP_LOG_WARN);   /* keep the USB stream to forwarded lines */
    return;                                 /* no CSI of its own */
#endif
#if CSI_FORWARD
    esp_now_peer_info_t gateway = {
        .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx   = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(gateway.peer_addr, GATEWAY_MAC, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&gateway));
    esp_now_rate_config_t fwd_rate = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = WIFI_PHY_RATE_MCS0_LGI,   /* the senders' robust rate; about 0.5 ms on air per line */
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(gateway.peer_addr, &fwd_rate));
    s_fwd_queue = xQueueCreate(16, sizeof(fwd_item_t));
    s_fwd_sent = xSemaphoreCreateBinary();
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_fwd_sent));
    xTaskCreate(forward_task, "forward", 4096, NULL, tskIDLE_PRIORITY + 2, NULL);
    esp_timer_handle_t diag;
    const esp_timer_create_args_t diag_args = {.callback = fwd_diag, .name = "fwd_diag"};
    ESP_ERROR_CHECK(esp_timer_create(&diag_args, &diag));
    ESP_ERROR_CHECK(esp_timer_start_periodic(diag, 1000000));
    ESP_LOGW(TAG, "forwarding CSI to the gateway " MACSTR " as RX," MACSTR, MAC2STR(GATEWAY_MAC), MAC2STR(s_base_mac));
#endif

    wifi_csi_init();
}
