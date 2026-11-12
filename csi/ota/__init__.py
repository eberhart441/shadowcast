"""Over-the-air updates of the node image, on the host (docs/ota.md, docs/adr/0031).

    image = check_image(data, trusted_key_digest)   # refuse an unsigned, corrupt or foreign-signed image
    frame = maintenance_command(OTA_CMD_UPDATE, targets, kit_key, image=image, port=8070, ap_channel=36)
    ssid, passphrase = maintenance_network(kit_key)  # the access point the host provides
    report = parse_report(line, kit_key)             # what a node POSTs after maintenance mode

The node takes an update only if ESP-IDF's signed-app check passes (Secure Boot V2 signature block, RSA-3072,
verified against the key of the image the node runs). ``check_image`` does the same check on the host, so a bad
image is refused before any node leaves the round for it: the ESP image format, its appended SHA-256, the
signature block (magic, CRC, image digest), the RSA-PSS signature, and the signer's public key digest (what
``espsecure digest-sbv2-public-key`` writes) against the kit's trusted one.

The maintenance command is frame type 8 of the round broadcast format (docs/round-broadcast.md
"Maintenance command"), signed with the kit key; firmware/core/ota_core.c is the node's copy and both are pinned to
tests/fixtures/ota_command.json.

One module per concern: ``image`` (check_image, load_signer), ``command`` (the maintenance command and
channels_clear), ``network`` (maintenance_network), ``report`` (the node's report), ``rollout`` (plan_stages,
Rollout, target_serials), ``server`` (ImageServer) and ``job`` (RolloutJob, the dashboard's rollout). Everything
public is importable from ``csi.ota`` itself.
"""
from .command import (COMMAND_REASONS, HOST_ID, MAX_TARGETS, OTA_CMD_REPORT, OTA_CMD_UPDATE, Command, CommandError,
                      channels_clear, decode_command, maintenance_command)
from .image import (APP_DESC_MAGIC, CHIP_ID_ESP32C5, IMAGE_REASONS, ImageError, ImageInfo, check_image,
                    load_signer)
from .job import JOB_LOG_KEPT, JOB_POLL_S, RolloutJob
from .network import NETWORK_LABEL, SSID_PREFIX, maintenance_network
from .report import GOOD_OUTCOMES, OUTCOMES, REPORT_TAG_LABEL, Report, ReportError, parse_report, report_tag
from .rollout import (COPIES, COPY_GAP_S, REJOIN_TIMEOUT_S, REPORT_TIMEOUT_S, STAGE_SIZES, Rollout, plan_stages,
                      target_serials)
from .server import MAX_REPORT_BYTES, ImageServer

__all__ = ["APP_DESC_MAGIC", "CHIP_ID_ESP32C5", "COMMAND_REASONS", "COPIES", "COPY_GAP_S", "GOOD_OUTCOMES", "HOST_ID",
           "IMAGE_REASONS", "JOB_LOG_KEPT", "JOB_POLL_S", "MAX_REPORT_BYTES", "MAX_TARGETS", "NETWORK_LABEL",
           "OTA_CMD_REPORT", "OTA_CMD_UPDATE", "OUTCOMES", "REJOIN_TIMEOUT_S", "REPORT_TAG_LABEL", "REPORT_TIMEOUT_S",
           "SSID_PREFIX", "STAGE_SIZES", "Command", "CommandError", "ImageError", "ImageInfo", "ImageServer",
           "Report", "ReportError", "Rollout", "RolloutJob", "channels_clear", "check_image", "decode_command",
           "load_signer", "maintenance_command", "maintenance_network", "parse_report", "plan_stages", "report_tag",
           "target_serials"]
