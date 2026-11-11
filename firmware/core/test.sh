#!/usr/bin/env bash
# Build and run the node-core C tests on this machine (portable C11; CI uses gcc on Ubuntu).
#   firmware/core/test.sh            compiler from $CC, else the first of cc, gcc, clang on PATH
#   CC=gcc firmware/core/test.sh
#   EXTRA_CFLAGS="-fsanitize=address,undefined" firmware/core/test.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -z "${CC:-}" ]; then
  for c in cc gcc clang; do
    if command -v "$c" >/dev/null 2>&1; then CC=$c; break; fi
  done
fi
if [ -z "${CC:-}" ]; then
  echo "test.sh: no C compiler found (set CC)" >&2
  exit 1
fi

LIBM=-lm
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) LIBM= ;; esac  # clang for Windows has no libm to link
EXE=
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) EXE=.exe ;; esac

mkdir -p build
CFLAGS=(-std=c11 -O2 -Wall -Wextra -Werror -pedantic -Wshadow -Wconversion -Wno-sign-conversion)

# build_and_run <test name> <sources...> -- <arguments>
build_and_run() {
  local name=$1; shift
  local sources=()
  while [ "$1" != "--" ]; do sources+=("$1"); shift; done
  shift
  # shellcheck disable=SC2086
  "$CC" "${CFLAGS[@]}" ${EXTRA_CFLAGS:-} -o "build/$name$EXE" "${sources[@]}" $LIBM
  "./build/$name$EXE" "$@"
}

build_and_run test_motion_score motion_score.c tests/test_motion_score.c -- tests/vectors/motion_score.txt
build_and_run test_round_broadcast round_broadcast.c tests/test_round_broadcast.c -- tests/vectors/round_broadcast.txt
build_and_run test_gateway_record gateway_record.c tests/test_gateway_record.c -- tests/vectors/gateway_record.txt
build_and_run test_espnow_frame espnow_frame.c tests/test_espnow_frame.c --
build_and_run test_csi_layout csi_layout.c tests/test_csi_layout.c --
build_and_run test_link_table link_table.c motion_score.c round_broadcast.c tests/test_link_table.c --
build_and_run test_fixed_round fixed_round.c tests/test_fixed_round.c --
