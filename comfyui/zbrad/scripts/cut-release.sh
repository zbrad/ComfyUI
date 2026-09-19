#!/usr/bin/env bash
# Create a new release worktree pinned to a commit, with the shared
# data/venv/custom_nodes dirs symlinked in. Does NOT activate it --
# run activate-release.sh separately once you're ready to switch to it.
set -euo pipefail

DEV_REPO=/home/zbrad/gh/ComfyUI
RELEASES_DIR=/home/zbrad/gh/ComfyUI-releases/releases
# Directories AND the one shared log file below all have the same
# property: they're gitignored, so `git worktree add` has nothing tracked
# to check out there. Symlinking them back to DEV_REPO keeps them as one
# continuous shared thing across every release instead of each release
# silently starting its own empty copy (129G models/, and for
# generation-log.jsonl specifically: a fragmented, no-longer-"ongoing"
# performance log).
SHARED_DIRS=(.venv models output input temp user custom_nodes)
SHARED_FILES=(generation-log.jsonl)

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

for f in "${SHARED_FILES[@]}"; do
    touch "${DEV_REPO}/${f}"  # so the symlink target exists even before any run has logged anything
    rm -f "${REL:?}/${f}"
    ln -s "${DEV_REPO}/${f}" "${REL}/${f}"
done

echo "Release ready: $REL"
