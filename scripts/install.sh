#!/usr/bin/env bash
# Link the kicad-route CLI and its skill where agents and shells find them, then
# install the routing backends. Re-running is safe: a link that already points
# here is left alone, a stale link (another checkout, or dangling) is repointed,
# and an existing regular file or directory is never replaced.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_link() {
    local source=$1 destination=$2
    mkdir -p -- "$(dirname -- "$destination")"
    if [[ -L "$destination" && $(readlink -f -- "$destination") == "$(readlink -f -- "$source")" ]]; then
        return
    fi
    if [[ -L "$destination" ]]; then
        # -n: replace the link itself, never descend into the directory it points to
        printf 'Repointing %s -> %s (was %s)\n' "$destination" "$source" "$(readlink -- "$destination")"
        ln -sfn -- "$source" "$destination"
        return
    fi
    if [[ -e "$destination" ]]; then
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
