#!/usr/bin/env bash
# Render the board figures used by docs/guide from the bundled fixture and the
# candidates the smoke tests leave under build/kicad-route/smoke (run
# `kicad-route smoke` first). Writes docs/figures/*.png. Needs kicad-cli and
# rsvg-convert. KiCad's configuration is sent to a scratch directory so this
# never touches ~/.config/kicad.
set -euo pipefail
cd "$(dirname "$0")/.."

SMOKE=${SMOKE:-build/kicad-route/smoke/runs/route_demo}
OUT=docs/figures
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT
export KICAD_CONFIG_HOME="$SCRATCH/cfg" XDG_CACHE_HOME="$SCRATCH/cfg" \
       XDG_DATA_HOME="$SCRATCH/cfg" XDG_CONFIG_HOME="$SCRATCH/cfg"
mkdir -p "$OUT" "$SCRATCH/cfg"

render() {  # render BOARD NAME: back copper under front copper, outline, front silk
    local board=$1 name=$2
    kicad-cli pcb export svg --page-size-mode 2 --exclude-drawing-sheet --mode-single \
        --layers "B.Cu,F.Cu,Edge.Cuts,F.SilkS" -o "$SCRATCH/$name.svg" "$board" >/dev/null
    rsvg-convert -w 1100 -b white "$SCRATCH/$name.svg" -o "$OUT/$name.png"
    echo "wrote $OUT/$name.png"
}

cand() { echo "$SMOKE/$1/latest/candidate/route_demo/route_demo.kicad_pcb"; }

render fixtures/route_demo/route_demo.kicad_pcb fixture-input
render "$(cand smoke-krt)"          fixture-krt
render "$(cand smoke-krt-default)"  fixture-krt-default
render "$(cand smoke-krt-nets)"     fixture-krt-nets
render "$(cand smoke-freerouting)"  fixture-freerouting
render "$(cand smoke-blocked-krt)"  fixture-blocked
