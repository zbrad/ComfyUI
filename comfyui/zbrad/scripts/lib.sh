#!/usr/bin/env bash
# Shared setup for the zbrad deploy scripts. Source this; do not execute it.
#
# Locates the dev checkout from this file's own location (so nothing is
# hardcoded to a home directory), loads the private settings file, and
# defines the helper that links fixed-path files into a checkout.

ZB_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZB_DIR="$(dirname "$ZB_SCRIPTS_DIR")"

# --git-common-dir resolves to the dev checkout's .git even when this file
# is being run from inside a release worktree, so DEV_REPO is always the
# dev checkout and never a release.
DEV_REPO="$(dirname "$(git -C "$ZB_SCRIPTS_DIR" rev-parse --path-format=absolute --git-common-dir)")"
RELEASES_ROOT="$(dirname "$DEV_REPO")/ComfyUI-releases"

ZB_SECRETS_FILE="${COMFY_SECRETS_FILE:-$HOME/.secrets/comfy}"
if [ -f "$ZB_SECRETS_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    . "$ZB_SECRETS_FILE"
    set +a
fi
COMFY_PORT="${COMFY_PORT:-8188}"
COMFY_TEST_PORT="${COMFY_TEST_PORT:-8189}"
COMFY_EXTRA_MODEL_PATHS="${COMFY_EXTRA_MODEL_PATHS:-$HOME/.config/comfyui/extra_model_paths.yaml}"

# zb_link_blueprints <checkout-root>
# ComfyUI only reads blueprints from <root>/blueprints/, so symlink ours
# in from that checkout's own comfyui/zbrad/examples/blueprints/ (relative
# links, so they survive the checkout being moved). A commit that predates
# this layout has no such directory and is left untouched. The links are
# added to the repo's local exclude file so they don't show up as untracked.
zb_link_blueprints() {
    local root="$1" src exclude f name
    src="$root/comfyui/zbrad/examples/blueprints"
    [ -d "$src" ] || return 0
    exclude="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir)/info/exclude"
    mkdir -p "$root/blueprints" "$(dirname "$exclude")"
    for f in "$src"/*.json; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        ln -sfn "../comfyui/zbrad/examples/blueprints/$name" "$root/blueprints/$name"
        grep -qxF "/blueprints/$name" "$exclude" 2>/dev/null || echo "/blueprints/$name" >> "$exclude"
    done
}
