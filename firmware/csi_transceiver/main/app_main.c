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
 * ALL-TRANSCEIVER VERSION
 *
 * Every node both sends and receives: it transmits a CSI probe in its own
 * turn-taking slot (same scheme as firmware/csi_send's turn-taking version)
 * AND captures CSI from every other node's probes, streaming CSI_DATA lines
 * over serial exactly like firmware/csi_recv.
 *
 * Why: with N dedicated senders and M dedicated receivers you only ever get
 * N*M links. With N transceivers you get every pair, N*(N-1) of them -- on
 * the current 4-corner rig that fills in the top and bottom room edges,
 * which today's split sender/receiver roles never measure at all (no two
 * receivers ever hear each other, no two senders ever hear each other).
 *
 * Packets are unchanged (same MAC scheme, same 4-byte counter payload), so
 * nothing downstream (csi.parser, csi.links, the dashboard) needs to change.
 * The only new rule: a node must ignore its own transmissions when it sees
 * them in its own CSI capture callback, which a pure sender or pure receiver
 * never had to do.
 *
 * SETUP: one build per chip, every board gets its ID and the node count at flash
 * time (written to NVS). From the repo root, in an ESP-IDF shell:
 *     python firmware/flash_all.py COM6 COM7 COM8:s3 COM9 COM10:s3
 * The order of the ports is the node ID (0, 1, 2, ...).
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
#include "esp_timer.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

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
#define CONFIG_FORCE_GAIN                   0  /* A/B test: was 1; disabled to isolate the RX-collapse theory */

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

/* Node ID and node count are read at boot from NVS (namespace "node", keys "id" and "num"),
 * written by firmware/flash_all.py, so one build per chip serves every board.
 * -DSENDER_ID / -DNUM_SENDERS are only the fallback when NVS has no entry. */
#ifndef NUM_SENDERS
#define NUM_SENDERS 2
#endif
#ifndef SENDER_ID
#define SENDER_ID 0x00
#endif

/* The bottleneck is each node's UART, not the air: it prints one ~0.85 KB line per frame it
 * hears, and 921600 baud carries ~110 such lines/s on a C5 (fewer on an S3). So the send rate
 * is LINE_BUDGET split across the N-1 peers, capped at 40 Hz: 5 nodes -> 15 Hz, 4 -> 20 Hz. */
#ifndef LINE_BUDGET
#define LINE_BUDGET 60
#endif
#define MAX_SEND_HZ 40

static uint8_t s_id = SENDER_ID, s_num = NUM_SENDERS;
static int s_rate;          /* Hz per node, set at boot */
static int64_t s_slot_us;   /* one turn; a full round is s_num turns */
/* Our own address prefix, so old-rig boards (1a:00:00:00:00:xx) on the same channel are ignored. */
static uint8_t s_mac[6] = {0x1a, 0x0c, 0x12, 0x00, 0x01, 0x00};  /* [5] = s_id at boot */
static const uint8_t BROADCAST_MAC[] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const char *TAG = "csi_transceiver";

static esp_timer_handle_t s_turn_timer;
static uint32_t s_count = 0;
static volatile uint32_t s_turn_fires = 0;   /* diagnostic: my_turn_cb calls/sec */
static volatile uint32_t s_heard_count = 0;  /* diagnostic: on_packet_heard calls/sec */
static volatile uint32_t s_tx_skipped = 0;   /* diagnostic: turns skipped, previous send still in flight */
/* Known ESP-IDF issue (espressif/esp-idf #18682, #19238, #19239): unpaced esp_now_send()
 * with no registered send callback can lose TX-buffer-pool bookkeeping under a race,
 * permanently wedging a fixed number of buffers (seen here as a one-time free_heap drop
 * that then plateaus) and leaving ESP_ERR_ESPNOW_NO_MEM for the rest of the run. Pacing
 * to at most one send in flight, gated by a registered send callback, is the documented
 * mitigation.
 *
 * But the send-done callback itself turned out to be the next failure mode: on real
 * hardware, one node's callback stopped firing reliably after the first send (confirmed
 * via a diagnostic skip counter -- 38-39 of every 40 turns were being skipped because
 * s_tx_in_flight never cleared), which under strict gating throttles that node down to
 * roughly 1 real send/sec instead of 40 -- its peer then correctly reports a collapsed
 * reception rate, which looks identical to a receive-side bug but is actually a
 * send-side one. A lost completion event is itself a known failure mode in the same
 * ESP-IDF issues (#18682 et al). Fix: treat "in flight" as stale after one full
 * scheduling round and send anyway, rather than waiting forever for a callback that may
 * never come -- keeps the NO_MEM-avoiding pacing in the common case, but bounds the cost
 * of a lost callback to occasionally having two sends briefly in flight instead of
 * stalling indefinitely. */
static volatile bool s_tx_in_flight = false;
static volatile int64_t s_tx_sent_at_us = 0;
static volatile uint32_t s_tx_stale = 0;   /* diagnostic: times we gave up waiting on a lost callback */
static volatile uint32_t s_csi_dropped = 0; /* diagnostic: CSI lines dropped, print queue full (UART behind) */

static void on_send_done(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    s_tx_in_flight = false;
}

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

    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, s_mac));
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

/* schedule_my_turn can be called concurrently from three contexts (my_turn_cb's own
 * self-rearm, on_packet_heard, and wifi_csi_rx_cb via note_peer_heard) on the same
 * s_turn_timer handle. ESP-IDF's esp_timer docs only guarantee the single-caller
 * stop-then-start sequence, not concurrent callers racing on one handle (see
 * espressif/esp-idf #11215, #19393) -- a torn update here could rearm the timer far
 * sooner than intended (runaway sends) or silently drop a reschedule (stale timer,
 * peer goes "unheard"). A spinlock makes the pair atomic across all three contexts. */
static portMUX_TYPE s_turn_timer_mux = portMUX_INITIALIZER_UNLOCKED;

/* (Re)arm the turn timer to fire after delay_us. */
static void schedule_my_turn(int64_t delay_us)
{
    portENTER_CRITICAL(&s_turn_timer_mux);
    esp_timer_stop(s_turn_timer);   /* returns an error if not running; that's fine */
    esp_timer_start_once(s_turn_timer, (uint64_t)delay_us);
    portEXIT_CRITICAL(&s_turn_timer_mux);
}

/* How many turns after sender `last_id` it is my turn (1..s_num).
 * 1 = I'm next in line. s_num = that was me, so wait a full round. */
static int turns_until_me(int last_id)
{
    int d = (s_id - last_id + s_num) % s_num;
    return d == 0 ? s_num : d;
}

/* Fires when it's my turn: send one packet, then wait a full round.
 * If I hear the others in the meantime, the timer gets re-set from their packets. */
static void my_turn_cb(void *arg)
{
    s_turn_fires++;
    /* A send-done callback can get lost; after a full round, stop waiting for it and send anyway. */
    bool stale = s_tx_in_flight && (esp_timer_get_time() - s_tx_sent_at_us) > s_num * s_slot_us;
    if (stale) {
        s_tx_stale++;
    }
    if (s_tx_in_flight && !stale) {
        /* Previous send hasn't completed yet (its callback hasn't fired): skip this
         * turn rather than risk the buffer-pool race above. Rare in healthy operation. */
        s_tx_skipped++;
    } else {
        s_tx_in_flight = true;
        s_tx_sent_at_us = esp_timer_get_time();
        esp_err_t ret = esp_now_send(BROADCAST_MAC, (const uint8_t *)&s_count, sizeof(s_count));
        if (ret != ESP_OK) {
            s_tx_in_flight = false;
            ESP_LOGW(TAG, "free_heap: %ld <%s> ESP-NOW send error", esp_get_free_heap_size(), esp_err_to_name(ret));
        }
    }
    s_count++;
    /* Bounded random jitter on the fallback (unheard) self-rearm only -- not on the precise
     * note_peer_heard resync path. Found on real 5-node hardware: a node with asymmetric/bad
     * RX can land in a stable bad phase relative to specific peers and never self-correct,
     * since nothing ever perturbs it (diagnosed via a background research pass, not guessed).
     * A small random nudge each time we fall back to free-running lets a stuck node's phase
     * drift until it lands somewhere it's actually heard again, at which point the precise
     * path above takes back over. Half a slot width either way is enough to shift phase
     * without meaningfully disrupting an already-working schedule. */
    int64_t jitter_us = (int64_t)(esp_random() % (s_slot_us + 1)) - (s_slot_us / 2);
    schedule_my_turn(s_num * s_slot_us + jitter_us);
}

/* Common to both ways we can hear a peer (see note_peer_heard_from_csi below): reschedule
 * our turn relative to theirs. */
static void note_peer_heard(int their_id)
{
    if (their_id < 0 || their_id >= s_num || their_id == s_id) {
        return;
    }
    s_heard_count++;
    /* If I'm next after them: wait 1 slot. If someone between us is missing,
     * wait proportionally longer, so the line keeps moving without them. */
    schedule_my_turn(turns_until_me(their_id) * s_slot_us);
}

/* Called whenever this node hears another node's ESP-NOW packet at the application
 * layer. On real hardware this stopped firing reliably a couple of seconds after
 * promiscuous CSI capture was enabled (confirmed with a diagnostic counter: `heard`
 * dropped to 0/s while CSI capture kept working), even though the physical frames
 * were still arriving -- so this can no longer be the only way we stay synchronized.
 * Kept as a cheap extra signal when it does fire; wifi_csi_rx_cb below is now the
 * reliable path (see note_peer_heard_from_csi). */
static void on_packet_heard(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    const uint8_t *src = info->src_addr;
    if (memcmp(src, s_mac, 5)) {  /* not one of ours */
        return;
    }
    note_peer_heard(src[5]);
}

static void diag_timer_cb(void *arg)
{
    ESP_LOGW(TAG, "DIAG 1s: my_turn fires=%lu heard=%lu skipped=%lu stale=%lu csi_dropped=%lu free_heap=%lu",
             (unsigned long)s_turn_fires, (unsigned long)s_heard_count,
             (unsigned long)s_tx_skipped, (unsigned long)s_tx_stale, (unsigned long)s_csi_dropped,
             (unsigned long)esp_get_free_heap_size());
    s_turn_fires = 0;
    s_heard_count = 0;
    s_tx_skipped = 0;
    s_tx_stale = 0;
    s_csi_dropped = 0;
}

/* Printing one CSI_DATA line over a 921600-baud UART (ets_printf, blocking) takes on
 * the order of 15-20 ms for a ~234-subcarrier line. Doing that inline in the CSI RX
 * callback stole enough CPU time from the WiFi/ESP-NOW stack to starve this node's own
 * sends (ESP_ERR_ESPNOW_NO_MEM, seen on real hardware) -- a cost a pure receiver never
 * paid, because it never also had to transmit. So the callback only copies what it
 * needs into a queue and returns immediately; a dedicated low-priority task does the
 * actual formatting and printing off the WiFi stack's critical path. A full queue drops
 * the newest sample rather than blocking the callback. */
#define CSI_PRINT_QUEUE_LEN 16
#define CSI_MAX_LEN 512

typedef struct {
    uint32_t rx_id;
    uint8_t mac[6];
    wifi_pkt_rx_ctrl_t rx_ctrl;
    float compensate_gain;
    int8_t fft_gain;
    uint8_t agc_gain;
    uint16_t len;
    uint8_t first_word_invalid;
    int8_t buf[CSI_MAX_LEN];
} csi_print_item_t;

static QueueHandle_t s_csi_print_queue;

/* Raw CSI capture (promiscuous layer, separate from on_packet_heard above): fires for
 * every matching frame on the channel, including our own transmissions, so we must
 * explicitly skip those -- a pure sender or pure receiver never had to. Kept fast and
 * non-blocking: no printing happens here (see csi_print_task). */
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        ESP_LOGW(TAG, "<%s> wifi_csi_cb", esp_err_to_name(ESP_ERR_INVALID_ARG));
        return;
    }

    if (memcmp(info->mac, s_mac, 5)) {  /* not in our group (1a:0c:12:00:01:xx) */
        return;
    }
    if (info->mac[5] == s_id) {  /* that's our own transmission; nothing to capture */
        return;
    }
    /* This fires reliably even when on_packet_heard (ESP-NOW app layer) doesn't under
     * promiscuous mode -- see its comment -- so this is the scheduling signal we trust. */
    note_peer_heard(info->mac[5]);
    if (info->len > CSI_MAX_LEN) {
        ESP_LOGW(TAG, "CSI frame too long (%d > %d), dropped", info->len, CSI_MAX_LEN);
        return;
    }

    static int s_csi_count = 0;
    float compensate_gain = 1.0f;
    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
#if CONFIG_GAIN_CONTROL
    static uint8_t agc_gain_baseline = 0;
    static int8_t fft_gain_baseline = 0;
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc_gain, &fft_gain);
    if (s_csi_count < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else if (s_csi_count == 100) {
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_gain_baseline, &fft_gain_baseline);
#if CONFIG_FORCE_GAIN
        esp_csi_gain_ctrl_set_rx_force_gain(agc_gain_baseline, fft_gain_baseline);
        ESP_LOGD(TAG, "fft_force %d, agc_force %d", fft_gain_baseline, agc_gain_baseline);
#endif
    }
    esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain);
#endif
    s_csi_count++;

    csi_print_item_t item = {
        .rx_id = *(uint32_t *)(info->payload + 15),
        .rx_ctrl = info->rx_ctrl,
        .compensate_gain = compensate_gain,
        .fft_gain = fft_gain,
        .agc_gain = agc_gain,
        .len = info->len,
        .first_word_invalid = info->first_word_invalid,
    };
    memcpy(item.mac, info->mac, 6);
    memcpy(item.buf, info->buf, info->len);

    /* Never log from here: the UART is the bottleneck, and blocking on it stalls the WiFi task. */
    if (xQueueSend(s_csi_print_queue, &item, 0) != pdTRUE) {
        s_csi_dropped++;
    }
}

/* Formats and prints one queued CSI sample. Runs in csi_print_task, off the WiFi
 * callback's critical path -- this is where all the ets_printf time goes now. */
static void print_csi_item(const csi_print_item_t *it)
{
    static bool header_printed = false;
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &it->rx_ctrl;

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
    if (!header_printed) {
        ESP_LOGI(TAG, "================ CSI TRANSCEIVER ================");
        ets_printf("type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,data\n");
        header_printed = true;
    }

    ets_printf("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d",
               it->rx_id, MAC2STR(it->mac), rx_ctrl->rssi, rx_ctrl->rate,
               rx_ctrl->noise_floor, it->fft_gain, it->agc_gain, rx_ctrl->channel,
               rx_ctrl->timestamp, rx_ctrl->sig_len, rx_ctrl->cur_bb_format);
#else
    if (!header_printed) {
        ESP_LOGI(TAG, "================ CSI TRANSCEIVER ================");
        ets_printf("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word,data\n");
        header_printed = true;
    }

    ets_printf("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
               it->rx_id, MAC2STR(it->mac), rx_ctrl->rssi, rx_ctrl->rate, rx_ctrl->sig_mode,
               rx_ctrl->mcs, rx_ctrl->cwb, rx_ctrl->smoothing, rx_ctrl->not_sounding,
               rx_ctrl->aggregation, rx_ctrl->stbc, rx_ctrl->fec_coding, rx_ctrl->sgi,
               rx_ctrl->noise_floor, rx_ctrl->ampdu_cnt, rx_ctrl->channel, rx_ctrl->secondary_channel,
               rx_ctrl->timestamp, rx_ctrl->ant, rx_ctrl->sig_len, rx_ctrl->sig_mode);
#endif
#if (CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61) && CSI_FORCE_LLTF
    int16_t csi = ((int16_t)(((((uint16_t)it->buf[1]) << 8) | it->buf[0]) << 4) >> 4);
    ets_printf(",%d,%d,\"[%d", (it->len - 2) / 2, it->first_word_invalid, (int16_t)(it->compensate_gain * csi));
    for (int i = 2; i < (it->len - 2); i += 2) {
        csi = ((int16_t)(((((uint16_t)it->buf[i + 1]) << 8) | it->buf[i]) << 4) >> 4);
        ets_printf(",%d", (int16_t)(it->compensate_gain * csi));
    }
#else
    ets_printf(",%d,%d,\"[%d", it->len, it->first_word_invalid, (int16_t)(it->compensate_gain * it->buf[0]));
    for (int i = 1; i < it->len; i++) {
        ets_printf(",%d", (int16_t)(it->compensate_gain * it->buf[i]));
    }
#endif
    ets_printf("]\"\n");
}

static void csi_print_task(void *arg)
{
    csi_print_item_t item;
    for (;;) {
        if (xQueueReceive(s_csi_print_queue, &item, portMAX_DELAY) == pdTRUE) {
            print_csi_item(&item);
        }
    }
}

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

    /* Per-board ID and node count from NVS (firmware/flash_all.py); compile-time values otherwise. */
    nvs_handle_t nvs;
    if (nvs_open("node", NVS_READONLY, &nvs) == ESP_OK) {
        nvs_get_u8(nvs, "id", &s_id);
        nvs_get_u8(nvs, "num", &s_num);
        nvs_close(nvs);
    }
    if (s_num < 2 || s_id >= s_num) {
        ESP_LOGE(TAG, "bad node config: id %d of %d; using id 0 of 2", s_id, s_num);
        s_id = 0;
        s_num = 2;
    }
    s_mac[5] = s_id;
    s_rate = LINE_BUDGET / (s_num - 1);
    if (s_rate > MAX_SEND_HZ) {
        s_rate = MAX_SEND_HZ;
    }
    s_slot_us = 1000000LL / ((int64_t)s_rate * s_num);

    /* Printing is the slow part (UART at 921600 baud); do it on its own low-priority
     * task so the WiFi/ESP-NOW stack is never blocked behind it. */
    s_csi_print_queue = xQueueCreate(CSI_PRINT_QUEUE_LEN, sizeof(csi_print_item_t));
    configASSERT(s_csi_print_queue != NULL);
    /* 8192, not the original 4096: marginal at higher s_num per a diagnostic agent's
     * review (more peers heard -> more frequent csi_print_task wakeups -> more exposure to any
     * rare worst-case stack depth in the ets_printf call chain). Doubled as a cheap precaution;
     * not confirmed as the actual cause of a real crash -- read the reset reason on a misbehaving
     * board first if this recurs (stack overflow panics name the task explicitly). */
    xTaskCreate(csi_print_task, "csi_print", 8192, NULL, tskIDLE_PRIORITY + 1, NULL);

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
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_send_done));

    /* Receive side: raw CSI capture from every other node's probes. */
    wifi_csi_init();

    /* Send side: listen for other nodes' turns so we know when ours is, then take it. */
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_packet_heard));

    const esp_timer_create_args_t timer_args = {
        .callback = my_turn_cb,
        .name = "turn",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_turn_timer));

    ESP_LOGI(TAG, "================ CSI TRANSCEIVER (turn-taking) ================");
    ESP_LOGI(TAG, "wifi_channel: %d, send_frequency: %d, mac: " MACSTR,
             CONFIG_LESS_INTERFERENCE_CHANNEL, s_rate, MAC2STR(s_mac));
    ESP_LOGI(TAG, "node %d of %d, slot: %lld us", s_id, s_num, (long long)s_slot_us);

    /* Start as if the last node in line just spoke; boards sync up once they hear
     * each other for real. */
    schedule_my_turn(turns_until_me(s_num - 1) * s_slot_us);

    /* Diagnostic: how often are we actually trying to send, and hearing the peer? */
    esp_timer_handle_t diag_timer;
    const esp_timer_create_args_t diag_args = {
        .callback = diag_timer_cb,
        .name = "diag",
    };
    ESP_ERROR_CHECK(esp_timer_create(&diag_args, &diag_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(diag_timer, 1000000));

    /* Everything else happens in the timer and receive callbacks. */
}
