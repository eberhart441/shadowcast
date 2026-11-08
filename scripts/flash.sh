#!/usr/bin/env bash
# Build one CSI rig board, check the port holds the chip named on the command line, then flash it.
#   scripts/flash.sh <receiver|sender> <esp32c5|esp32s3> <port> [sender_id] [--force] [--dry-run]
#
#   receiver  firmware/csi_recv, build dir build_<chip>
#   sender    firmware/csi_send, build dir build_<chip>_id<NN>; sender_id (e.g. 0x01) is required
#   --force   flash a receiver although a dashboard answers on $FLASH_DASHBOARD_URL
#   --dry-run print every command and run none of them (the dashboard probe is read-only and still runs)
#
# Needs IDF_PATH set and idf.py on PATH: run `. ~/esp/esp-idf/export.sh` first. The script sources nothing.
# Exit status: 0 done, 1 refused or failed, 2 usage error. See firmware/README.md and docs/agents/hardware.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DASHBOARD_URL="${FLASH_DASHBOARD_URL:-http://127.0.0.1:8000}"

usage() {
  cat <<'EOF'
usage: scripts/flash.sh <receiver|sender> <esp32c5|esp32s3> <port> [sender_id] [--force] [--dry-run]
  sender_id  required for a sender, e.g. 0x01; not allowed for a receiver
  --force    flash a receiver while a dashboard answers on http://127.0.0.1:8000
  --dry-run  print the commands without building or flashing
EOF
}

usage_error() {
  echo "flash.sh: $1" >&2
  usage >&2
  exit 2
}

die() {
  echo "flash.sh: $*" >&2
  exit 1
}

# Print a command the way it would be typed.
show() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
}

# Print a command, and run it unless --dry-run.
run_cmd() {
  show "$@"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

FORCE=0
DRY_RUN=0
POS=()
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) usage_error "unknown option: $arg" ;;
    *) POS+=("$arg") ;;
  esac
done
# ${POS[@]+...} keeps an empty array from tripping `set -u` on bash 3.2.
set -- ${POS[@]+"${POS[@]}"}

[ $# -ge 3 ] || usage_error "need <role> <chip> <port>"
[ $# -le 4 ] || usage_error "too many arguments"
ROLE="$1"
CHIP="$2"
PORT="$3"

case "$ROLE" in
  receiver) SRC="$ROOT/firmware/csi_recv" ;;
  sender) SRC="$ROOT/firmware/csi_send" ;;
  *) usage_error "role must be receiver or sender, not '$ROLE'" ;;
esac

case "$CHIP" in
  esp32c5) CHIP_NAME="ESP32-C5" ;;
  esp32s3) CHIP_NAME="ESP32-S3" ;;
  *) usage_error "chip must be esp32c5 or esp32s3, not '$CHIP'" ;;
esac

[ -n "$PORT" ] || usage_error "port is empty"

BUILD_NAME="build_$CHIP"
SENDER_FLAG=()
if [ "$ROLE" = sender ]; then
  [ $# -eq 4 ] || usage_error "a sender needs a sender_id, e.g. 0x01"
  SENDER_ID="$4"
  case "$SENDER_ID" in
    0x[0-9a-fA-F][0-9a-fA-F]) ;;
    *) usage_error "sender_id must look like 0x01, not '$SENDER_ID'" ;;
  esac
  ID_NN="$(printf '%s' "${SENDER_ID#0x}" | tr '[:upper:]' '[:lower:]')"
  BUILD_NAME="build_${CHIP}_id${ID_NN}"
  SENDER_FLAG=("-DSENDER_ID=$SENDER_ID")
else
  [ $# -eq 3 ] || usage_error "a receiver takes no sender_id"
fi

BUILD="$SRC/$BUILD_NAME"

if [ -z "${IDF_PATH:-}" ]; then
  die "IDF_PATH is not set. Run '. ~/esp/esp-idf/export.sh' first so idf.py is on PATH."
fi
if ! command -v idf.py >/dev/null 2>&1; then
  die "idf.py is not on PATH (IDF_PATH=$IDF_PATH). Run '. \$IDF_PATH/export.sh' first."
fi

# A receiver takes its gain baseline from the first 100 packets after boot, and any esptool
# command or flash resets the board. Refuse while a dashboard holds the ports.
if [ "$ROLE" = receiver ]; then
  PROBE=(curl -s -o /dev/null --max-time 2 "$DASHBOARD_URL")
  show "${PROBE[@]}"
  if "${PROBE[@]}"; then
    if [ "$FORCE" -eq 0 ]; then
      cat >&2 <<EOF
flash.sh: refusing to flash a receiver: a dashboard answers on $DASHBOARD_URL.
  Flashing resets the board. csi_recv takes its gain baseline from the first 100 packets after
  boot, so every saved threshold on this receiver's links goes stale. Any esptool command on a
  receiver while the dashboard runs does the same (docs/agents/hardware.md).
  Stop the dashboard first, or pass --force if you accept the re-baseline.
EOF
      exit 1
    fi
    echo "flash.sh: warning: dashboard answers on $DASHBOARD_URL; continuing because of --force" >&2
  fi
fi

IDF_ARGS=(-C "$SRC" -B "$BUILD" "-DSDKCONFIG=$BUILD/sdkconfig" ${SENDER_FLAG[@]+"${SENDER_FLAG[@]}"})
run_cmd idf.py "${IDF_ARGS[@]}" set-target "$CHIP"
run_cmd idf.py "${IDF_ARGS[@]}" build

if [ "$DRY_RUN" -eq 0 ] && [ ! -f "$BUILD/flash_args" ]; then
  die "no flash_args in $BUILD after the build; nothing was flashed"
fi

PROBE_CMD=(python -m esptool --port "$PORT" chip_id)
show "${PROBE_CMD[@]}"
if [ "$DRY_RUN" -eq 0 ]; then
  if ! OUT="$("${PROBE_CMD[@]}" 2>&1)"; then
    printf '%s\n' "$OUT" >&2
    die "esptool could not read a chip on $PORT; nothing was flashed"
  fi
  DETECTED="$(printf '%s\n' "$OUT" | sed -n \
    -e 's/^Chip type: *\(ESP32[-A-Za-z0-9]*\).*/\1/p' \
    -e 's/.*Detecting chip type\.\.\. *\(ESP32[-A-Za-z0-9]*\).*/\1/p')"
  DETECTED="${DETECTED%%$'\n'*}"
  if [ -z "$DETECTED" ]; then
    printf '%s\n' "$OUT" >&2
    die "could not read the chip type from esptool on $PORT; nothing was flashed"
  fi
  if [ "$DETECTED" != "$CHIP_NAME" ]; then
    die "$PORT holds a $DETECTED, not $CHIP_NAME ($CHIP); nothing was flashed"
  fi
fi

FLASH_CMD=(python -m esptool --port "$PORT" write_flash @flash_args)
show cd "$BUILD"
show "${FLASH_CMD[@]}"
if [ "$DRY_RUN" -eq 0 ]; then
  (cd "$BUILD" && "${FLASH_CMD[@]}")
  echo "flash.sh: flashed $ROLE on $PORT as $CHIP_NAME from $BUILD"
else
  echo "flash.sh: dry run; nothing was built, probed or flashed"
fi
