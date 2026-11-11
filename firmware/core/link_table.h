/*
 * The links a transceiver receives on, one per transmitting node index, and the link reports it sends about them.
 *
 * link_table_heard records one round broadcast heard from a transmitter (device time, RSSI, the amplitudes of its
 * CSI) and feeds the link's C scorer. link_table_close_round is called once per round, when the node builds its own
 * round broadcast: it ends the round for every link (the heard history moves on by one round) and writes one link
 * report per other member, calling motion_score_get once per link. The reports describe the round broadcasts heard
 * since the previous call, as docs/round-broadcast.md says.
 *
 * Indexed by node index (0..31), so it holds RB_MAX_NODES scorers: about 64 KiB at 64 subcarriers.
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef LINK_TABLE_H
#define LINK_TABLE_H

#include <stdbool.h>
#include <stdint.h>

#include "motion_score.h"
#include "round_broadcast.h"

typedef struct {
    motion_score_link_t score;
    uint16_t history;     /* bit 0: heard in the current round; bit k: heard k rounds ago (15 rounds kept) */
    int8_t rssi;          /* of the last round broadcast heard; RB_RSSI_NONE if never */
    bool ever_heard;
} link_state_t;

typedef struct {
    link_state_t link[RB_MAX_NODES];
} link_table_t;

void link_table_init(link_table_t *table);

/* One round broadcast heard from `transmitter` at device time t_us. amp may be NULL (n_sub 0) when the frame gave
 * no amplitudes (CSI layout warm-up): it still counts as heard. Out-of-range indices are ignored. */
void link_table_heard(link_table_t *table, uint8_t transmitter, int64_t t_us, int8_t rssi, const float *amp,
                      uint16_t n_sub);

/* Re-baseline one link: its scorer starts over (its reports are warming until the window is full again). */
void link_table_restart(link_table_t *table, uint8_t transmitter);

/* End the round: write one report per member of `membership` other than `me`, ascending, and return how many. */
uint8_t link_table_close_round(link_table_t *table, uint32_t membership, uint8_t me, rb_report_t *reports);

#endif /* LINK_TABLE_H */
