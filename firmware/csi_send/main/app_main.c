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

/*
 * TURN-TAKING VERSION
 *
 * Before: every sender broadcast on its own timer, so senders talked over
 * each other and packets were lost.
 *
 * Now: senders take turns, like passing a talking stick.
 *   - Senders listen to each other's packets.
 *   - When sender N hears sender N-1, it waits one short "slot" and then sends.
 *   - If a sender is missing or a packet gets lost, the next one in line
 *     takes its turn anyway after waiting a bit longer (a backup timer).
 *     So one dead board never freezes the others.
 *
 * Packets are unchanged (same MAC scheme, same 4-byte counter payload),
 * so the receiver firmware and the Python code do not need to change.
 *
 * SETUP:
 *   1. Set NUM_SENDERS below to how many sender boards you have.
 *   2. Give each sender a different SENDER_ID from 0 up to NUM_SENDERS-1:
 *        idf.py -DSENDER_ID=0x00 build flash   (first sender)
 *        idf.py -DSENDER_ID=0x01 build flash   (second sender)
 *        idf.py -DSENDER_ID=0x02 build flash   (third sender) ...
 *      Every sender must be flashed with this same file.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

#include "nvs_flash.h"

#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL   6

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE   WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT40
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_MCS0_LGI

/* Packets per second from EACH sender (same as before). */
#define CONFIG_SEND_FREQUENCY               40

/* How many sender boards are in the rotation. Change this when you add one. */
#ifndef NUM_SENDERS
#define NUM_SENDERS 3
#endif

/* Length of one turn, in microseconds. One full round = NUM_SENDERS turns,
 * and each sender still sends CONFIG_SEND_FREQUENCY times per second.
 * Example: 3 senders at 40/s -> 120 turns/s -> about 8.3 ms per turn. */
#define SLOT_US  (1000000LL / ((int64_t)CONFIG_SEND_FREQUENCY * NUM_SENDERS))

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#define WIFI_BW_HT20 WIFI_BW20   /* enums renamed in IDF 6.0 */
#define WIFI_BW_HT40 WIFI_BW40
#endif

/* Unique per sender; receivers accept 1a:00:00:00:00:xx. Set at build time: idf.py -DSENDER_ID=0x01 (see CMakeLists.txt). */
#ifndef SENDER_ID
#define SENDER_ID 0x00
#endif

#if SENDER_ID >= NUM_SENDERS
#error "SENDER_ID must be between 0 and NUM_SENDERS-1. Raise NUM_SENDERS or pick a smaller SENDER_ID."
#endif

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, SENDER_ID};
static const uint8_t BROADCAST_MAC[] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const char *TAG = "csi_send";

static esp_timer_handle_t s_turn_timer;
static uint32_t s_count = 0;

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
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

/* (Re)arm the turn timer to fire after delay_us. */
static void schedule_my_turn(int64_t delay_us)
{
    esp_timer_stop(s_turn_timer);   /* returns an error if not running; that's fine */
    esp_timer_start_once(s_turn_timer, (uint64_t)delay_us);
}

/* How many turns after sender `last_id` it is my turn (1..NUM_SENDERS).
 * 1 = I'm next in line. NUM_SENDERS = that was me, so wait a full round. */
static int turns_until_me(int last_id)
{
    int d = (SENDER_ID - last_id + NUM_SENDERS) % NUM_SENDERS;
    return d == 0 ? NUM_SENDERS : d;
}

/* Fires when it's my turn: send one packet, then wait a full round.
 * If I hear the others in the meantime, the timer gets re-set from their packets. */
static void my_turn_cb(void *arg)
{
    esp_err_t ret = esp_now_send(BROADCAST_MAC, (const uint8_t *)&s_count, sizeof(s_count));
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "free_heap: %ld <%s> ESP-NOW send error", esp_get_free_heap_size(), esp_err_to_name(ret));
    }
    s_count++;
    schedule_my_turn(NUM_SENDERS * SLOT_US);
}

/* Called whenever this board hears another ESP-NOW packet. */
static void on_packet_heard(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    const uint8_t *src = info->src_addr;

    /* Only care about other senders in our rotation: 1a:00:00:00:00:xx */
    if (src[0] != 0x1a || src[1] || src[2] || src[3] || src[4]) {
        return;
    }
    int their_id = src[5];
    if (their_id >= NUM_SENDERS || their_id == SENDER_ID) {
        return;
    }

    /* If I'm next after them: wait 1 slot. If someone between us is missing,
     * wait proportionally longer, so the line keeps moving without them. */
    schedule_my_turn(turns_until_me(their_id) * SLOT_US);
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

    /* Listen to the other senders so we know whose turn it is. */
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_packet_heard));

    const esp_timer_create_args_t timer_args = {
        .callback = my_turn_cb,
        .name = "turn",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_turn_timer));

    ESP_LOGI(TAG, "================ CSI SEND (turn-taking) ================");
    ESP_LOGI(TAG, "wifi_channel: %d, send_frequency: %d, mac: " MACSTR,
             CONFIG_LESS_INTERFERENCE_CHANNEL, CONFIG_SEND_FREQUENCY, MAC2STR(CONFIG_CSI_SEND_MAC));
    ESP_LOGI(TAG, "sender %d of %d, slot: %lld us", SENDER_ID, NUM_SENDERS, (long long)SLOT_US);

    /* Start as if the last sender in line just spoke: sender 0 goes first,
     * sender 1 a slot later, and so on. Boards sync up once they hear each other. */
    schedule_my_turn(turns_until_me(NUM_SENDERS - 1) * SLOT_US);

    /* Everything else happens in the timer and receive callbacks. */
}
