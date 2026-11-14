#!/usr/bin/env bash
# Compile the real sketch against the stub headers and run its logic on this
# computer (no board, no arduino-cli). Needs any C++17 compiler.
#
#   tools/hosttest/run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SKETCH="$ROOT/1-t_at/sketch/sketch.ino"
BUILD="$HERE/build"
CXX="${CXX:-c++}"

mkdir -p "$BUILD"
cp "$SKETCH" "$BUILD/sketch.ino"
# The tilt scenario needs the sketch built with the tilt switch enabled.
sed 's/^#define USE_TILT 0/#define USE_TILT 1/' "$SKETCH" > "$BUILD/sketch_tilt.ino"
grep -q '^#define USE_TILT 1' "$BUILD/sketch_tilt.ino"

FLAGS=(-std=c++17 -O1 -Wall -Wextra -Wno-unused-parameter -I "$HERE/stubs")
"$CXX" "${FLAGS[@]}" -DSKETCH_FILE="\"$BUILD/sketch.ino\"" "$HERE/host_main.cpp" -o "$BUILD/hosttest"
"$CXX" "${FLAGS[@]}" -DSKETCH_FILE="\"$BUILD/sketch_tilt.ino\"" "$HERE/host_main.cpp" -o "$BUILD/hosttest_tilt"

status=0
for scenario in virtual hx711 bare; do
  "$BUILD/hosttest" "$scenario" || status=1
done
"$BUILD/hosttest_tilt" tilt || status=1

if [ "$status" -eq 0 ]; then
  echo "sketch host test: all good"
else
  echo "sketch host test: FAILED"
fi
exit "$status"
