#!/usr/bin/env bash
# Build and run the node-core C tests on this machine (portable C11; CI uses gcc on Ubuntu).
#   firmware/core/test.sh            compiler from $CC, else the first of cc, gcc, clang on PATH
#   CC=gcc firmware/core/test.sh
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
# shellcheck disable=SC2086
"$CC" "${CFLAGS[@]}" -o "build/test_motion_score$EXE" motion_score.c tests/test_motion_score.c $LIBM
"./build/test_motion_score$EXE" tests/vectors/motion_score.txt
