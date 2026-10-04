#!/usr/bin/env bash
# Runs the host unit tests and checks that the headers shared by the flight
# sketch and the ground bridge are byte-identical.
set -euo pipefail
cd "$(dirname "$0")/.."
for h in airone_frame.h airone_compact.h airone_radio.h airone_spibus.h; do
  cmp -s "airone_cansat/$h" "airone_ground_bridge/$h" || { echo "MISMATCH: $h differs between sketches"; exit 1; }
done
echo "shared headers identical"
out="${TMPDIR:-/tmp}/airone_host_test"
g++ -std=c++17 -O1 -Wall -Werror -I airone_cansat tests/host_test.cpp -o "$out"
"$out"
