#!/usr/bin/env bash
# Create a new release worktree pinned to a commit, with the shared
# data/venv/custom_nodes dirs symlinked in. Does NOT activate it --
# run activate-release.sh separately once you're ready to switch to it.
set -euo pipefail

DEV_REPO=/home/zbrad/gh/ComfyUI
RELEASES_DIR=/home/zbrad/gh/ComfyUI-releases/releases
SHARED_DIRS=(.venv models output input temp user custom_nodes)

COMMITISH="${1:-HEAD}"

cd "$DEV_REPO"
SHA=$(git rev-parse --short=8 "$COMMITISH")
TS=$(date -u +%Y%m%dT%H%M%SZ)
REL="$RELEASES_DIR/${SHA}-${TS}"

if [ -e "$REL" ]; then
    echo "error: release path already exists: $REL" >&2
    exit 1
fi

echo "Cutting release ${SHA} at ${REL}..." >&2
git worktree add --detach "$REL" "$COMMITISH"

for d in "${SHARED_DIRS[@]}"; do
    # $REL is an absolute path we just created above; $d comes from the
    # fixed whitelist above, not user input.
    rm -rf "${REL:?}/${d}"
    ln -s "${DEV_REPO}/${d}" "${REL}/${d}"
done

echo "Release ready: $REL"
