#include "ota_core.h"

#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------- byte helpers (little-endian) */

static uint16_t get16(const uint8_t *p)
{
    return (uint16_t)(p[0] | p[1] << 8);
}

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static bool all_zero(const uint8_t *p, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        if (p[i]) {
            return false;
        }
    }
    return true;
}

/* ---------------------------------------------------------------- the maintenance command */

/* Header offsets as in round_broadcast.h; slot .. membership (bytes 4..11) are zero in a command. */
#define OFF_ROUND 2
#define OFF_RESERVED_HEADER 4
#define RESERVED_HEADER_BYTES 8
#define OFF_SENDER 12
#define OFF_TAG 18
#define OFF_COMMAND 26
#define OFF_TARGET_COUNT 27
#define OFF_PORT 28
#define OFF_CHANNEL 30
#define OFF_RESERVED 31
#define OFF_ISSUED 32
#define OFF_SIZE 36
#define OFF_SHA 40
#define OFF_TARGETS 72

bool ota_is_command(const uint8_t *bytes, size_t len)
{
    return len >= 2 && bytes[0] == OTA_FORMAT_VERSION && bytes[1] == OTA_FRAME_MAINTENANCE;
}

static ota_status_t check_fields(uint8_t command, uint16_t port, uint32_t size, uint8_t n_targets)
{
    if (n_targets == 0 || n_targets > OTA_MAX_TARGETS) {
        return OTA_TARGETS;
    }
    if (command != OTA_CMD_UPDATE && command != OTA_CMD_REPORT) {
        return OTA_COMMAND;
    }
    if (port == 0 || (command == OTA_CMD_UPDATE && size == 0)) {
        return OTA_FIELD;
    }
    return OTA_OK;
}

ota_status_t ota_command_decode(const uint8_t *bytes, size_t len, ota_command_t *out)
{
    if (len < OTA_HEADER_BYTES) {
        return OTA_SHORT;
    }
    if (bytes[0] != OTA_FORMAT_VERSION) {
        return OTA_VERSION;
    }
    if (bytes[1] != OTA_FRAME_MAINTENANCE) {
        return OTA_FRAME_TYPE;
    }
    if (len < OFF_TARGETS) {
        return OTA_LENGTH;
    }
    uint8_t n = bytes[OFF_TARGET_COUNT];
    if (n == 0 || n > OTA_MAX_TARGETS) {
        return OTA_TARGETS;
    }
    if (len != (size_t)OFF_TARGETS + (size_t)OTA_SERIAL_BYTES * n) {
        return OTA_LENGTH;
    }
    ota_status_t st = check_fields(bytes[OFF_COMMAND], get16(bytes + OFF_PORT), get32(bytes + OFF_SIZE), n);
    if (st != OTA_OK) {
        return st;
    }
    if (bytes[OFF_RESERVED] != 0 || !all_zero(bytes + OFF_RESERVED_HEADER, RESERVED_HEADER_BYTES)) {
        return OTA_FIELD;
    }
    memset(out, 0, sizeof *out);
    out->round = get16(bytes + OFF_ROUND);
    memcpy(out->sender, bytes + OFF_SENDER, OTA_SERIAL_BYTES);
    memcpy(out->tag, bytes + OFF_TAG, sizeof out->tag);
    out->command = bytes[OFF_COMMAND];
    out->port = get16(bytes + OFF_PORT);
    out->ap_channel = bytes[OFF_CHANNEL];
    out->issued = get32(bytes + OFF_ISSUED);
    out->image_size = get32(bytes + OFF_SIZE);
    memcpy(out->image_sha, bytes + OFF_SHA, OTA_SHA_BYTES);
    out->n_targets = n;
    memcpy(out->targets, bytes + OFF_TARGETS, (size_t)OTA_SERIAL_BYTES * n);
    return OTA_OK;
}

size_t ota_command_encode(const ota_command_t *c, uint8_t *out, size_t cap)
{
    size_t len = (size_t)OFF_TARGETS + (size_t)OTA_SERIAL_BYTES * c->n_targets;
    if (check_fields(c->command, c->port, c->image_size, c->n_targets) != OTA_OK || cap < len) {
        return 0;
    }
    memset(out, 0, len);
    out[0] = OTA_FORMAT_VERSION;
    out[1] = OTA_FRAME_MAINTENANCE;
    put16(out + OFF_ROUND, c->round);
    memcpy(out + OFF_SENDER, c->sender, OTA_SERIAL_BYTES);
    memcpy(out + OFF_TAG, c->tag, sizeof c->tag);
    out[OFF_COMMAND] = c->command;
    out[OFF_TARGET_COUNT] = c->n_targets;
    put16(out + OFF_PORT, c->port);
    out[OFF_CHANNEL] = c->ap_channel;
    put32(out + OFF_ISSUED, c->issued);
    put32(out + OFF_SIZE, c->image_size);
    memcpy(out + OFF_SHA, c->image_sha, OTA_SHA_BYTES);
    memcpy(out + OFF_TARGETS, c->targets, (size_t)OTA_SERIAL_BYTES * c->n_targets);
    return len;
}

const char *ota_status_name(ota_status_t st)
{
    static const char *const names[] = {"ok", "short", "version", "frame_type", "length", "targets", "command",
                                        "field"};
    return (unsigned)st < sizeof names / sizeof names[0] ? names[st] : "unknown";
}

/* ---------------------------------------------------------------- what a node does with a command */

/* The 80 MHz block a 5 GHz channel lies in (36-48, 52-64, 100-112, ...; 149-161, 165-177): an access point may use
 * all of it. */
static int block_80mhz(uint8_t channel)
{
    return channel >= 149 ? 100 + (channel - 149) / 16 : (channel - 36) / 16;
}

bool ota_channels_clear(uint8_t sensing_channel, uint8_t ap_channel)
{
    bool ap_5ghz = ap_channel >= 36 && ap_channel <= 177;
    if (ap_channel == 0) {
        return true;   /* scan (checked again on the channel found) */
    }
    if (sensing_channel >= 36) {   /* sensing on 5 GHz: 2.4 GHz, or another 80 MHz block */
        return (ap_channel >= 1 && ap_channel <= 14)
               || (ap_5ghz && block_80mhz(ap_channel) != block_80mhz(sensing_channel));
    }
    if (ap_5ghz) {
        return true;
    }
    if (ap_channel > 14) {
        return false;
    }
    int apart = (int)ap_channel - (int)sensing_channel;
    return apart >= 5 || apart <= -5;   /* 20 MHz channels 5 apart (25 MHz) do not overlap */
}

static bool targets(const ota_command_t *c, const uint8_t serial[OTA_SERIAL_BYTES])
{
    for (uint8_t i = 0; i < c->n_targets && i < OTA_MAX_TARGETS; i++) {
        if (memcmp(c->targets[i], serial, OTA_SERIAL_BYTES) == 0) {
            return true;
        }
    }
    return false;
}

ota_action_t ota_command_action(const ota_command_t *c, const uint8_t serial[OTA_SERIAL_BYTES], uint32_t last_issued,
                                uint8_t sensing_channel, const uint8_t *running_sha, bool *already)
{
    if (already) {
        *already = false;
    }
    if (!targets(c, serial) || c->issued <= last_issued || !ota_channels_clear(sensing_channel, c->ap_channel)) {
        return OTA_ACT_NONE;
    }
    if (c->command == OTA_CMD_REPORT) {
        return OTA_ACT_REPORT;
    }
    if (c->command != OTA_CMD_UPDATE) {
        return OTA_ACT_NONE;
    }
    if (running_sha && !all_zero(running_sha, OTA_SHA_BYTES) && memcmp(running_sha, c->image_sha, OTA_SHA_BYTES) == 0) {
        if (already) {
            *already = true;
        }
        return OTA_ACT_REPORT;
    }
    return OTA_ACT_UPDATE;
}

/* ---------------------------------------------------------------- the record kept in NVS */

void ota_record_init(ota_record_t *r)
{
    memset(r, 0, sizeof *r);
}

/* version, job, outcome, ap_channel, attempt, attempt_slot, port, issued, last_issued, image_size, three SHA-256s */
#define REC_PORT 6
#define REC_ISSUED 8
#define REC_LAST 12
#define REC_SIZE 16
#define REC_IMAGE 20
#define REC_ATTEMPT_SHA (REC_IMAGE + OTA_SHA_BYTES)
#define REC_RUNNING_SHA (REC_ATTEMPT_SHA + OTA_SHA_BYTES)
_Static_assert(REC_RUNNING_SHA + OTA_SHA_BYTES == OTA_RECORD_BYTES, "record layout");

size_t ota_record_encode(const ota_record_t *r, uint8_t *out, size_t cap)
{
    if (cap < OTA_RECORD_BYTES) {
        return 0;
    }
    out[0] = OTA_RECORD_VERSION;
    out[1] = r->job;
    out[2] = r->outcome;
    out[3] = r->ap_channel;
    out[4] = r->attempt;
    out[5] = r->attempt_slot;
    put16(out + REC_PORT, r->port);
    put32(out + REC_ISSUED, r->issued);
    put32(out + REC_LAST, r->last_issued);
    put32(out + REC_SIZE, r->image_size);
    memcpy(out + REC_IMAGE, r->image_sha, OTA_SHA_BYTES);
    memcpy(out + REC_ATTEMPT_SHA, r->attempt_sha, OTA_SHA_BYTES);
    memcpy(out + REC_RUNNING_SHA, r->running_sha, OTA_SHA_BYTES);
    return OTA_RECORD_BYTES;
}

bool ota_record_decode(const uint8_t *blob, size_t len, ota_record_t *r)
{
    ota_record_init(r);
    if (len != OTA_RECORD_BYTES || blob[0] != OTA_RECORD_VERSION) {
        return false;
    }
    r->job = blob[1];
    r->outcome = blob[2];
    r->ap_channel = blob[3];
    r->attempt = blob[4];
    r->attempt_slot = blob[5];
    r->port = get16(blob + REC_PORT);
    r->issued = get32(blob + REC_ISSUED);
    r->last_issued = get32(blob + REC_LAST);
    r->image_size = get32(blob + REC_SIZE);
    memcpy(r->image_sha, blob + REC_IMAGE, OTA_SHA_BYTES);
    memcpy(r->attempt_sha, blob + REC_ATTEMPT_SHA, OTA_SHA_BYTES);
    memcpy(r->running_sha, blob + REC_RUNNING_SHA, OTA_SHA_BYTES);
    return true;
}

void ota_record_take(ota_record_t *r, const ota_command_t *c, ota_action_t action, bool already)
{
    if (action == OTA_ACT_NONE) {
        return;
    }
    r->job = action == OTA_ACT_UPDATE ? OTA_JOB_UPDATE : OTA_JOB_REPORT;
    r->outcome = action == OTA_ACT_UPDATE ? OTA_OUT_NONE : already ? OTA_OUT_ALREADY : OTA_OUT_CHECKED_IN;
    r->port = c->port;
    r->ap_channel = c->ap_channel;
    r->issued = c->issued;
    r->last_issued = c->issued;
    r->image_size = c->image_size;
    memcpy(r->image_sha, c->image_sha, OTA_SHA_BYTES);
}

/* ---------------------------------------------------------------- at boot */

static void judged(ota_record_t *r, ota_outcome_t outcome)
{
    if (outcome == OTA_OUT_COMMITTED) {
        memcpy(r->running_sha, r->attempt_sha, OTA_SHA_BYTES);
    }
    r->attempt = 0;
    r->outcome = (uint8_t)outcome;
    r->job = OTA_JOB_REPORT;
}

ota_boot_t ota_boot(ota_record_t *r, bool pending, uint8_t running_slot)
{
    if (pending) {
        return OTA_BOOT_HEALTH_CHECK;   /* a new image: it stays only if it proves itself */
    }
    if (r->attempt) {
        /* judged without the verdict stored: valid in its slot means committed; any other slot means the
         * bootloader rolled back (the new image crashed, hung or browned out while pending) */
        judged(r, running_slot == r->attempt_slot ? OTA_OUT_COMMITTED : OTA_OUT_ROLLED_BACK);
    }
    return r->job == OTA_JOB_NONE ? OTA_BOOT_RUN : OTA_BOOT_MAINTENANCE;
}

void ota_record_committed(ota_record_t *r)
{
    judged(r, OTA_OUT_COMMITTED);
}

void ota_record_rolled_back(ota_record_t *r)
{
    judged(r, OTA_OUT_ROLLED_BACK);
}

void ota_record_job_done(ota_record_t *r, ota_outcome_t outcome, uint8_t written_slot)
{
    bool attempt = r->job == OTA_JOB_UPDATE && outcome == OTA_OUT_NONE;
    if (attempt) {
        r->attempt = 1;
        r->attempt_slot = written_slot;
        memcpy(r->attempt_sha, r->image_sha, OTA_SHA_BYTES);
    }
    r->job = OTA_JOB_NONE;
    r->outcome = OTA_OUT_NONE;
}

/* ---------------------------------------------------------------- the download */

ota_outcome_t ota_download_check(const ota_record_t *r, uint32_t got_size, const uint8_t got_sha[OTA_SHA_BYTES])
{
    if (got_size != r->image_size) {
        return OTA_OUT_REFUSED_SIZE;
    }
    return memcmp(got_sha, r->image_sha, OTA_SHA_BYTES) == 0 ? OTA_OUT_NONE : OTA_OUT_REFUSED_DIGEST;
}

/* ---------------------------------------------------------------- the health check */

void ota_health_init(ota_health_t *h, int64_t now_us, bool gateway)
{
    h->start_us = now_us;
    h->gateway = gateway;
    h->verdict = OTA_HEALTH_WAIT;
}

ota_verdict_t ota_health_step(ota_health_t *h, int64_t now_us, bool member, uint32_t heard, uint32_t sent)
{
    if (h->verdict != OTA_HEALTH_WAIT) {
        return h->verdict;
    }
    bool healthy = h->gateway ? heard >= OTA_HEALTH_HEARD
                              : member && heard >= OTA_HEALTH_HEARD && sent >= OTA_HEALTH_SENT;
    if (healthy) {
        h->verdict = OTA_HEALTH_PASS;
    } else if (now_us - h->start_us >= OTA_HEALTH_DEADLINE_US) {
        h->verdict = OTA_HEALTH_FAIL;
    }
    return h->verdict;
}

/* ---------------------------------------------------------------- the report */

const char *ota_outcome_name(ota_outcome_t o)
{
    static const char *const names[OTA_OUT_COUNT] = {
        "none", "committed", "rolled_back", "already", "checked_in", "no_network", "overlap", "no_server",
        "refused_size", "refused_digest", "refused_image", "write_failed"};
    return (unsigned)o < OTA_OUT_COUNT ? names[o] : "unknown";
}

/* The version string with anything but letters, digits and . _ + - as '_', so the line stays one word per field. */
static void copy_word(char *out, size_t cap, const char *in)
{
    size_t i = 0;
    for (; in && in[i] && i + 1 < cap; i++) {
        char c = in[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' || c == '_'
                  || c == '+' || c == '-';
        out[i] = ok ? c : '_';
    }
    out[i] = '\0';
}

size_t ota_report_format(const ota_report_t *rep, char *out, size_t cap)
{
    char image[17] = "-", version[48];
    if (rep->running_sha && !all_zero(rep->running_sha, OTA_SHA_BYTES)) {
        for (int i = 0; i < 8; i++) {
            snprintf(image + 2 * i, 3, "%02x", rep->running_sha[i]);
        }
    }
    copy_word(version, sizeof version, rep->version);
    const uint8_t *s = rep->serial;
    int n = snprintf(out, cap,
                     "ota-report 1 serial=%02X:%02X:%02X:%02X:%02X:%02X issued=%lu outcome=%s slot=ota_%u image=%s "
                     "version=%s", s[0], s[1], s[2], s[3], s[4], s[5], (unsigned long)rep->issued,
                     ota_outcome_name((ota_outcome_t)rep->outcome), (unsigned)rep->slot, image, version);
    if (n < 0 || (size_t)n >= cap) {
        if (cap) {
            out[0] = '\0';
        }
        return 0;
    }
    return (size_t)n;
}
