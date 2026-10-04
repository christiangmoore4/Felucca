#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Leo Kuroshita (@kurogedelic), Hügelton Instruments
# Host tests (no hardware). Run from the repo root after ./build.sh:
#   tests/run_tests.sh
# Without the JieLi toolchain and SDK (CI, a fresh clone): HOST_ONLY=1 tests/run_tests.sh generates
# build/gen itself (tools/build.py --gen-only) and runs every test that does not read
# build/felucca.{bin,fwsc,dis}. What it did not run is listed at the end, and the last line says
# HOST-ONLY, not ALL: a subset never reads as the full suite.
#
# Resource ledger (tools/ledger.py, tests/ledger_baseline.txt): RAM / POOL / NOINIT / constants from a
# 32-bit host compile of the whole firmware, +1 %. BUDGET_UPDATE=1 rewrites that baseline too.
#
# Regression suite (tests/regress.c, tests/target_budget.py; details at the top of regress.c):
#   golden renders  every engine x preset, the drum kit, voice modes, FX sends, a 4-track mix: one hash
#                   each in tests/golden.txt. A change of the sound fails with the list of renders.
#   health          clipping, DC, peak level, voices free after the release, silence at the end.
#   CPU             instructions / sample per preset and mix (tests/cpu_baseline.txt, +25 %), ns printed;
#                   target: loop instructions of the render functions in build/felucca.dis
#                   (tests/target_budget.txt, +10 %; exact, static).
#   voices          the budget of 8, steal fades, MONO / LEGATO / UNISON keep their note, the VOICE cap,
#                   no hanging notes on any MIDI / key routing.
# After an intended change of the sound: GOLDEN_UPDATE=1 sh tests/run_tests.sh, review the diff
# of tests/golden.txt, commit it with the change. After an intended change of the cost (or a new
# compiler): BUDGET_UPDATE=1 (rewrites cpu_baseline.txt and target_budget.txt). VERBOSE=1: every render.
set -e
export AC79_SDK="${AC79_SDK:-$HOME/fw-AC79_AIoT_SDK}"
cd "$(dirname "$0")/.."
OUT=build/host
mkdir -p "$OUT"
CC="${CC:-cc} -O1 -Wall -Wno-unused-function"
fail=0
HOST_ONLY="${HOST_ONLY:-0}"
skipped=""
run() { echo "== $1"; shift; "$@" || fail=1; }
skip() { echo "== skip: $1"; skipped="$skipped
  $1"; }

if [ "$HOST_ONLY" = 1 ]; then
    gen=$(python3 tools/build.py --gen-only 2>&1) || { echo "$gen"; echo "tools/build.py --gen-only failed (needs Pillow)"; exit 1; }
else
    [ -f build/felucca.fwsc ] || { echo "run ./build.sh first (HOST_ONLY=1 runs the tests that need no target build)"; exit 1; }
fi

$CC -o "$OUT/storage_test" tests/storage_test.c
run "flash storage (A/B, torn writes)" "$OUT/storage_test"

$CC -o "$OUT/upreset_test" tests/upreset_test.c
run "user presets (UP_PUT parser, bank round trip, versions)" "$OUT/upreset_test"

$CC -o "$OUT/midi_uart_test" tests/midi_uart_test.c
run "TRS MIDI parser" "$OUT/midi_uart_test"

if [ "$HOST_ONLY" = 1 ]; then
    skip "M-UPGRADE entry (ota_test): needs build/felucca.fwsc"
    skip "update loader (ldr_test): needs build/felucca.bin and build/loader/ota.bin"
else
    $CC -o "$OUT/ota_test" tests/ota_test.c
    run "M-UPGRADE entry" "$OUT/ota_test" build/felucca.fwsc

    head -c 200000 build/felucca.bin > "$OUT/old_app.bin"
    python3 tools/fm1pkg_make.py "$OUT/old_app.bin" build/loader/ota.bin "$OUT/old.fwsc" >/dev/null
    $CC -o "$OUT/ldr_test" tests/ldr_test.c
    run "update loader: other app -> this build" "$OUT/ldr_test" "$OUT/old.fwsc" build/felucca.fwsc
fi

$CC -O2 -w -Ibuild/gen -Ifirmware/src -o "$OUT/hostsim" tests/hostsim.c -lm
$CC -O2 -w -Ibuild/gen -Ifirmware/src -o "$OUT/scale_test" tests/scale_test.c -lm
run "scales: white-key mapping and note lifecycle" "$OUT/scale_test"
run "DSP render (ANALOG preset 0)" "$OUT/hostsim" 0 0 1 "$OUT/render.wav"
mkdir -p build/tracks_demo
run "TRACKS: 4-track pattern, live recording (lengths, swing), voice budget, engine switch, cost" env TRACKS=build/tracks_demo "$OUT/hostsim" 0 0 1 "$OUT/tracks.wav"
$CC -w -Ibuild/gen -Ifirmware/src -o "$OUT/project_test" tests/project_test.c -lm
run "project formats (FUN2 / FUN1 -> FUN3: the SLICER parameters)" "$OUT/project_test"
$CC -O2 -w -Ibuild/gen -Ifirmware/src -o "$OUT/slicer_test" tests/slicer_test.c -lm
mkdir -p build/slicer_demo
run "SLICER: no clicks, timing, sync with the sequencer, STUT, cost, demos" "$OUT/slicer_test" build/slicer_demo
$CC -O2 -w -Ibuild/gen -Ifirmware/src -o "$OUT/regress" tests/regress.c -lm
run "regression: golden renders, health, voices, CPU budget" "$OUT/regress" tests/golden.txt tests/cpu_baseline.txt
# SLICE (tests/slice_test.c) needs a FELUCCA_SLICE=1 build; the engine is not built by default

if [ "$HOST_ONLY" = 1 ]; then
    skip "target cost of the render loops (target_budget.py): needs build/felucca.dis"
else
    run "regression: target cost of the render loops" python3 tests/target_budget.py \
        build/felucca.dis tests/target_budget.txt
fi
echo "== resource ledger: RAM / POOL / NOINIT / constants vs tests/ledger_baseline.txt"
python3 tools/ledger.py --check || {
    rc=$?
    if [ $rc = 3 ] && [ "$HOST_ONLY" != 1 ]; then skip "resource ledger: no 32-bit capable cc (LEDGER_CC)"; else fail=1; fi
}
if [ "$(uname -s)" != Darwin ]; then
    skip "CPU instruction budget (regress): the counter is macOS-only; golden hashes and health checks did run"
fi

run "installer CLI (fm1_install.py) against a simulated FM-1" python3 tests/install_test.py

if command -v node >/dev/null 2>&1; then
    run "web pages: editor protocol, samples, packages, update protocol" node web/test_web.mjs
else
    echo "== skip web tests (no node)"
fi

[ $fail -eq 0 ] || { echo "HOST TESTS FAILED"; exit 1; }
[ -z "$skipped" ] || printf 'NOT RUN here:%s\n' "$skipped"
if [ "$HOST_ONLY" = 1 ]; then echo "HOST-ONLY TESTS PASSED (a subset, not the full suite)"; else echo "ALL HOST TESTS PASSED"; fi
