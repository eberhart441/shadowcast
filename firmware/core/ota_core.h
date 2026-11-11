/*
 * Over-the-air updates on the node: the decisions, without ESP-IDF (docs/adr/0031, docs/ota.md).
 *
 *   maintenance command   frame type 8 on the sensing channel (docs/round-broadcast.md "Maintenance command"):
 *                         the 26-byte header (signed with the kit key, round_auth.h), then what to do, the image's
 *                         size and SHA-256, where to fetch it, and up to OTA_MAX_TARGETS target serials.
 *                         csi/ota.py is the host's copy; both are pinned to tests/fixtures/ota_command.json.
 *   action                what a node does with a command that verified: nothing, update, or report.
 *   record                what the node keeps in NVS across the restarts of an update (a job to run in maintenance
 *                         mode, an attempt waiting for its health check, the outcome to report).
 *   boot                  at every boot: run normally, run the health check of a new image, or go into maintenance
 *                         mode (fetch an image, or report an outcome).
 *   health check          a new image passes when it is a member of a round and has heard and sent enough round
 *                         broadcasts before a deadline; otherwise it fails and the node rolls back.
 *   report                the text line a node POSTs to the host after maintenance mode (its tag is added by the
 *                         caller, which holds the kit key).
 *
 * Portable C11: no allocation, no I/O, no platform calls.
 */
#ifndef OTA_CORE_H
#define OTA_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define OTA_FRAME_MAINTENANCE 8      /* round broadcast frame type of a maintenance command */
#define OTA_FORMAT_VERSION 2         /* the round broadcast format version the command rides in */
#define OTA_HEADER_BYTES 26
#define OTA_BODY_BYTES 46            /* command .. image SHA-256, before the targets */
#define OTA_SERIAL_BYTES 6
#define OTA_SHA_BYTES 32
#define OTA_MAX_TARGETS 13           /* 26 + 46 + 6 x 13 = 150 bytes, the most a node signs or checks */
#define OTA_MAX_BYTES (OTA_HEADER_BYTES + OTA_BODY_BYTES + OTA_SERIAL_BYTES * OTA_MAX_TARGETS)

#define OTA_CMD_UPDATE 1             /* fetch, verify and boot the image the command names */
#define OTA_CMD_REPORT 2             /* only report: running image, last outcome */

#define OTA_HEALTH_DEADLINE_US 120000000   /* a new image has this long after boot to pass */
#define OTA_HEALTH_HEARD 20                /* round broadcasts heard from other members (gateway: frames heard) */
#define OTA_HEALTH_SENT 20                 /* own round broadcasts sent (transceiver only) */

typedef enum {
    OTA_OK = 0,
    OTA_SHORT,        /* shorter than the header */
    OTA_VERSION,      /* not round broadcast format version 2 */
    OTA_FRAME_TYPE,   /* not a maintenance command (a round broadcast, a join frame, ...) */
    OTA_LENGTH,       /* the length does not match the target count */
    OTA_TARGETS,      /* no targets, or more than OTA_MAX_TARGETS */
    OTA_COMMAND,      /* a command this firmware does not know */
    OTA_FIELD,        /* a reserved field set, port 0, or an update without an image size */
} ota_status_t;

typedef struct {
    uint16_t round;                          /* header: the host's round number (freshness) */
    uint8_t sender[OTA_SERIAL_BYTES];        /* header node id: the host's id (all zero) */
    uint8_t tag[8];                          /* header: authentication tag, carried as it is */
    uint8_t command;                         /* OTA_CMD_* */
    uint16_t port;                           /* HTTP port of the host's image server */
    uint8_t ap_channel;                      /* maintenance network channel; 0 = scan passively */
    uint32_t issued;                         /* host time stamp (Unix s); a node runs each command once */
    uint32_t image_size;                     /* bytes of the signed image file (update) */
    uint8_t image_sha[OTA_SHA_BYTES];        /* SHA-256 of the signed image file (update) */
    uint8_t n_targets;
    uint8_t targets[OTA_MAX_TARGETS][OTA_SERIAL_BYTES];
} ota_command_t;

/* True if bytes look like a maintenance command (version 2, frame type 8): hand them to ota_command_decode and
 * not to rb_decode. */
bool ota_is_command(const uint8_t *bytes, size_t len);

/* Decode a maintenance command. Anything but OTA_OK leaves *out unspecified. The tag is not checked here. */
ota_status_t ota_command_decode(const uint8_t *bytes, size_t len, ota_command_t *out);

/* Encode (tag bytes as in c->tag). Returns the length, or 0 if c is invalid or out is too small. */
size_t ota_command_encode(const ota_command_t *c, uint8_t *out, size_t cap);

/* "ok", "short", "version", "frame_type", "length", "targets", "command" or "field" (the fixture's reasons). */
const char *ota_status_name(ota_status_t st);

/* ---------------------------------------------------------------- what a node does with a command */

typedef enum {
    OTA_ACT_NONE = 0,     /* not for this node, already run, or the network would overlap the sensing channel */
    OTA_ACT_UPDATE,       /* store an update job and restart into maintenance mode */
    OTA_ACT_REPORT,       /* store a report job and restart into maintenance mode */
} ota_action_t;

/* True when a maintenance network on ap_channel cannot overlap a 20 MHz sensing channel: 5 GHz (36..177), or a
 * 2.4 GHz channel at least 5 away. 0 (scan) is allowed here; the channel found must pass again. */
bool ota_channels_clear(uint8_t sensing_channel, uint8_t ap_channel);

/* What a node does with a command that verified. `last_issued`: the newest command it ran (0: none);
 * `running_sha`: the SHA-256 of the image it runs, if known (NULL otherwise). An update naming the running image
 * becomes a report ("already"). Sets *already accordingly when not NULL. */
ota_action_t ota_command_action(const ota_command_t *c, const uint8_t serial[OTA_SERIAL_BYTES], uint32_t last_issued,
                                uint8_t sensing_channel, const uint8_t *running_sha, bool *already);

/* ---------------------------------------------------------------- the record kept in NVS */

#define OTA_RECORD_VERSION 1
#define OTA_RECORD_BYTES 116

typedef enum { OTA_JOB_NONE = 0, OTA_JOB_UPDATE = 1, OTA_JOB_REPORT = 2 } ota_job_t;

/* Outcomes reported to the host. The names (ota_outcome_name) are the report's words; csi/ota.py has them too. */
typedef enum {
    OTA_OUT_NONE = 0,
    OTA_OUT_COMMITTED,          /* the new image passed its health check and runs */
    OTA_OUT_ROLLED_BACK,        /* the new image failed its health check or crashed; the old one runs */
    OTA_OUT_ALREADY,            /* the commanded image already runs */
    OTA_OUT_CHECKED_IN,         /* a report command: nothing changed */
    OTA_OUT_NO_NETWORK,         /* the maintenance network was not found or refused the passphrase */
    OTA_OUT_OVERLAP,            /* the maintenance network is on a channel that overlaps the sensing channel */
    OTA_OUT_NO_SERVER,          /* the image server did not answer or answered with an error */
    OTA_OUT_REFUSED_SIZE,       /* the download's size is not the command's */
    OTA_OUT_REFUSED_DIGEST,     /* the download's SHA-256 is not the command's: corrupt or another image */
    OTA_OUT_REFUSED_IMAGE,      /* ESP-IDF refused the image: not signed by the running image's key, or corrupt */
    OTA_OUT_WRITE_FAILED,       /* the other app slot could not be written or selected */
    OTA_OUT_COUNT
} ota_outcome_t;

typedef struct {
    uint8_t job;                         /* ota_job_t to run at the next boot, in maintenance mode */
    uint8_t outcome;                     /* ota_outcome_t to report next */
    uint8_t ap_channel;
    uint8_t attempt;                     /* 1: a new image was written and selected and has not been judged */
    uint8_t attempt_slot;                /* the app slot (0 or 1) the attempt was written to */
    uint16_t port;
    uint32_t issued;                     /* the job's command */
    uint32_t last_issued;                /* the newest command run (a replay or an older command is ignored) */
    uint32_t image_size;
    uint8_t image_sha[OTA_SHA_BYTES];    /* the job's image */
    uint8_t attempt_sha[OTA_SHA_BYTES];  /* the image of the attempt */
    uint8_t running_sha[OTA_SHA_BYTES];  /* the image that passed last (all zero: not known) */
} ota_record_t;

void ota_record_init(ota_record_t *r);

/* A fixed little-endian layout with a version byte; OTA_RECORD_BYTES long. */
size_t ota_record_encode(const ota_record_t *r, uint8_t *out, size_t cap);

/* False (and *r initialised) for a blob of another length or version. */
bool ota_record_decode(const uint8_t *blob, size_t len, ota_record_t *r);

/* A command the node will act on becomes its job (and its last_issued). */
void ota_record_take(ota_record_t *r, const ota_command_t *c, ota_action_t action, bool already);

/* ---------------------------------------------------------------- at boot */

typedef enum {
    OTA_BOOT_RUN = 0,       /* the normal transceiver or gateway */
    OTA_BOOT_HEALTH_CHECK,  /* the normal node, and ota_health decides whether this new image stays */
    OTA_BOOT_MAINTENANCE,   /* run r->job in maintenance mode, then restart */
} ota_boot_t;

/* Decide at boot. `pending`: the running image waits for verification (ESP-IDF's PENDING_VERIFY);
 * `running_slot`: 0 or 1. Updates *r (an attempt that was judged becomes an outcome and a report job); the caller
 * stores *r when it changed. */
ota_boot_t ota_boot(ota_record_t *r, bool pending, uint8_t running_slot);

/* The health check passed: the attempt is the running image now, and the outcome is reported. */
void ota_record_committed(ota_record_t *r);

/* The health check failed (the caller rolls back right after storing this). */
void ota_record_rolled_back(ota_record_t *r);

/* Maintenance mode finished its job with `outcome` (reported, or not: the job is done either way). For an update
 * that wrote and selected a new image (OTA_OUT_NONE here), the attempt is set for the next boot. */
void ota_record_job_done(ota_record_t *r, ota_outcome_t outcome, uint8_t written_slot);

/* ---------------------------------------------------------------- the health check of a new image */

typedef enum { OTA_HEALTH_WAIT = 0, OTA_HEALTH_PASS, OTA_HEALTH_FAIL } ota_verdict_t;

typedef struct {
    int64_t start_us;
    bool gateway;
    ota_verdict_t verdict;
} ota_health_t;

void ota_health_init(ota_health_t *h, int64_t now_us, bool gateway);

/* `member`: the round scheduler has this node as a member now; `heard`, `sent`: totals since boot. Once it says
 * PASS or FAIL it keeps saying so. */
ota_verdict_t ota_health_step(ota_health_t *h, int64_t now_us, bool member, uint32_t heard, uint32_t sent);

/* ---------------------------------------------------------------- the download and the report */

/* The download against the command: OTA_OUT_NONE when size and SHA-256 match, else REFUSED_SIZE / _DIGEST. */
ota_outcome_t ota_download_check(const ota_record_t *r, uint32_t got_size, const uint8_t got_sha[OTA_SHA_BYTES]);

/* "committed", "rolled_back", ..., "none" */
const char *ota_outcome_name(ota_outcome_t o);

typedef struct {
    uint8_t serial[OTA_SERIAL_BYTES];
    uint32_t issued;          /* the command this answers */
    uint8_t outcome;          /* ota_outcome_t */
    const char *version;      /* the running image's version string */
    uint8_t slot;             /* the running app slot */
    const uint8_t *running_sha;   /* NULL or all zero: not known */
} ota_report_t;

/* "ota-report 1 serial=02:00:00:00:01:5C issued=1794000000 outcome=committed slot=ota_1 image=<16 hex|-> version=..."
 * without a line end; the caller appends " tag=<16 hex>" (OTA_REPORT_TAG_LABEL). Returns the length (0: no room). */
size_t ota_report_format(const ota_report_t *rep, char *out, size_t cap);

/* The tag of a report line is the first 8 bytes of HMAC-SHA256(kit key, OTA_REPORT_TAG_LABEL + line). */
#define OTA_REPORT_TAG_LABEL "shadowcast ota report\n"
/* The maintenance network's passphrase is the first 12 bytes of HMAC-SHA256(kit key, this), as 24 hex digits. */
#define OTA_NETWORK_LABEL "shadowcast maintenance network"
#define OTA_NETWORK_SSID_PREFIX "shadowcast-"   /* then the kit key check, 8 hex digits */

#endif /* OTA_CORE_H */
