#!/usr/bin/env bash
# Link the kicad-route CLI and its skill where agents and shells find them, then
# install the routing backends. Re-running is safe; existing non-link paths are
# never replaced.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_link() {
    local source=$1 destination=$2
    mkdir -p -- "$(dirname -- "$destination")"
    if [[ -L "$destination" && $(readlink -f -- "$destination") == "$source" ]]; then
        return
    fi
    if [[ -e "$destination" || -L "$destination" ]]; then
        printf 'Refusing to replace existing path: %s\n' "$destination" >&2
        exit 1
    fi
    ln -s -- "$source" "$destination"
}
install_link "$root/bin/kicad-route" "$HOME/.local/bin/kicad-route"
install_link "$root/skills/kicad-route" "$HOME/.agents/skills/kicad-route"
install_link "$root/skills/kicad-route" "${CODEX_HOME:-$HOME/.codex}/skills/kicad-route"
install_link "$root/skills/kicad-route" "$HOME/.claude/skills/kicad-route"
printf 'Installed the kicad-route CLI and skill links.\n'
if [[ "${1:-}" != "--no-update" ]]; then
    "$root/bin/kicad-route" --out "$root/build/kicad-route" update
fi
