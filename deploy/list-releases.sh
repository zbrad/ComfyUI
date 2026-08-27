#!/usr/bin/env bash
# List all cut release worktrees with their commit and subject line,
# marking the currently active one with '*'.
set -euo pipefail

RELEASES_DIR=/home/zbrad/gh/ComfyUI-releases/releases
CURRENT_LINK=/home/zbrad/gh/ComfyUI-releases/current

CURRENT_TARGET=""
if [ -L "$CURRENT_LINK" ]; then
    CURRENT_TARGET=$(readlink -f "$CURRENT_LINK")
fi

shopt -s nullglob
RELEASE_DIRS=("$RELEASES_DIR"/*/)
shopt -u nullglob

if [ "${#RELEASE_DIRS[@]}" -eq 0 ]; then
    echo "No releases cut yet."
    exit 0
fi

for d in "${RELEASE_DIRS[@]}"; do
    d="${d%/}"
    MARK=" "
    [ "$d" = "$CURRENT_TARGET" ] && MARK="*"
    SHA=$(git -C "$d" rev-parse --short=8 HEAD 2>/dev/null || echo "??????")
    SUBJECT=$(git -C "$d" log -1 --format=%s 2>/dev/null || echo "?")
    printf "%s %-45s %s  %s\n" "$MARK" "$(basename "$d")" "$SHA" "$SUBJECT"
done
