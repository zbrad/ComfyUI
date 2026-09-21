#!/usr/bin/env bash
# Deploy a release published by test-and-publish.sh: fetch the tags, refuse
# anything that is not a published release tag, cut a release worktree at that
# tag (or reuse the one already cut for that commit), and activate it, which
# restarts comfyui.service. Works on any node with a checkout of the repo; it
# only fetches, so a non-owner node can run it.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [ $# -ne 1 ]; then
    echo "usage: $0 <release-tag>   (release/<version>-<sha8>, from test-and-publish.sh)" >&2
    exit 1
fi

TAG="$1"
case "$TAG" in
    release/*) ;;
    *) echo "error: '$TAG' is not a published release tag (expected release/<version>-<sha8>)" >&2; exit 1 ;;
esac

git -C "$DEV_REPO" fetch -q --tags origin
if ! git -C "$DEV_REPO" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "error: tag $TAG does not exist on origin" >&2
    exit 1
fi
# Published tags are annotated; a lightweight tag was not made by test-and-publish.sh.
if [ "$(git -C "$DEV_REPO" cat-file -t "refs/tags/$TAG")" != "tag" ]; then
    echo "error: $TAG is not an annotated tag, so it was not published by test-and-publish.sh" >&2
    exit 1
fi

COMMIT="$(git -C "$DEV_REPO" rev-parse "${TAG}^{commit}")"
REL="$(ls -d "$RELEASES_ROOT/releases/${COMMIT:0:8}"-* 2>/dev/null | sort | tail -n 1 || true)"
if [ -n "$REL" ]; then
    echo "Reusing release already cut for ${COMMIT:0:8}: $REL" >&2
else
    echo "== Cutting release at $TAG ==" >&2
    REL="$("$ZB_SCRIPTS_DIR/cut-release.sh" "$COMMIT" | grep -oP 'Release ready: \K.*')"
fi

exec "$ZB_SCRIPTS_DIR/activate-release.sh" "$REL"
