/*
 * The fixed round of ticket #13: a set number of members, node index = slot, one round broadcast per member per round
 * at a fixed round rate, and no join slot. It is the placeholder for the round scheduler (#14), behind the same
 * calls: the node tells it every round broadcast it hears (device time, round, slot) and asks it when to send next.
 *
 * Timing comes from any heard round broadcast; there is no master. The first one heard sets this node's round phase
 * and round number outright; after that each one moves the phase halfway towards what it implies, so two nodes
 * settle one slot apart instead of chasing each other. Round numbers follow the last broadcast heard. A node that
 * hears nobody keeps sending on its own clock (the scheduler of #14 will listen first instead).
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef FIXED_ROUND_H
#define FIXED_ROUND_H

#include <stdbool.h>
#include <stdint.h>

#define FIXED_ROUND_MIN_SLOT_US 1000   /* the shortest slot accepted (a 150-byte broadcast is ~0.4 ms on air) */

typedef struct {
    uint8_t index;          /* this node's index and slot */
    uint8_t nodes;          /* members 0..nodes-1, and slots per round */
    int64_t period_us;
    int64_t slot_us;
    int64_t base_us;        /* device time at which round base_round starts */
    uint16_t base_round;
    bool synced;            /* false until the first round broadcast is heard */
    uint32_t corrections;   /* round broadcasts that moved the phase */
    uint32_t ignored;       /* round broadcasts not used for timing (another slot count, a bad slot) */
} fixed_round_t;

/* Start a round of `nodes` members at `round_hz` rounds per second, with this node at `index`, on its own clock
 * from now_us. Returns false (and leaves r unusable) if the settings cannot make a round. */
bool fixed_round_init(fixed_round_t *r, uint8_t index, uint8_t nodes, uint32_t round_hz, int64_t now_us);

/* A round broadcast heard at device time t_rx_us, sent in `slot` of `round` with `slots_per_round` slots. */
void fixed_round_heard(fixed_round_t *r, int64_t t_rx_us, uint16_t round, uint8_t slot, uint8_t slots_per_round);

/* The device time of this node's next slot strictly after after_us, and the round it belongs to. */
int64_t fixed_round_next_tx(const fixed_round_t *r, int64_t after_us, uint16_t *round);

/* The membership bitmap of the round. */
uint32_t fixed_round_membership(const fixed_round_t *r);

#endif /* FIXED_ROUND_H */
