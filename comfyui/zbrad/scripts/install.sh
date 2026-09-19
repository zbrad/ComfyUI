#!/usr/bin/env bash
# Set up this checkout's zbrad additions: clone the sibling repos and custom
# nodes, put the fixed-path files where ComfyUI expects them, and render the
# systemd user units. Idempotent. Never enables, starts or restarts a service,
# and never switches an existing clone's branch.
#
#   install.sh [--dry-run] [--force] [--unit-dir DIR]
#
#   --dry-run    print what would change, change nothing
#   --force      overwrite workflow copies that differ from the tracked ones
#   --unit-dir   render units here instead of ~/.config/systemd/user
#                (skips daemon-reload)
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY=0
FORCE=0
UNIT_DIR="$HOME/.config/systemd/user"
DEFAULT_UNIT_DIR=1
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --force) FORCE=1 ;;
        --unit-dir) UNIT_DIR="${2:?--unit-dir needs a directory}"; DEFAULT_UNIT_DIR=0; shift ;;
        *) echo "usage: $0 [--dry-run] [--force] [--unit-dir DIR]" >&2; exit 1 ;;
    esac
    shift
done

say() { echo "$*" >&2; }
run() {
    if [ "$DRY" -eq 1 ]; then say "  [dry-run] $*"; else "$@"; fi
}

check_secrets() {
    say "== Settings file ($ZB_SECRETS_FILE) =="
    if [ ! -f "$ZB_SECRETS_FILE" ]; then
        say "  missing -- units will listen on 127.0.0.1 only."
        say "  create it from $ZB_DIR/templates/comfy.example (chmod 600)."
        return
    fi
    local mode
    mode="$(stat -c %a "$ZB_SECRETS_FILE")"
    [ "$mode" = "600" ] || say "  warning: mode is $mode, expected 600"
    if [ -z "${COMFY_LISTEN_ADDR:-}" ]; then
        say "  COMFY_LISTEN_ADDR not set -- units will listen on 127.0.0.1 only."
    else
        say "  COMFY_LISTEN_ADDR is set."
    fi
}

# sync_repos <manifest> <target-dir>: clone what is missing, report the rest.
sync_repos() {
    local manifest="$1" target="$2" dir url branch dest cur
    while IFS='|' read -r dir url branch; do
        case "$dir" in ''|'#'*) continue ;; esac
        dest="$target/$dir"
        if [ "$url" = "-" ]; then
            if [ -d "$dest" ]; then say "  $dir: local-only, present"; else say "  $dir: local-only, not present here (nothing to clone)"; fi
        elif [ -d "$dest/.git" ]; then
            cur="$(git -C "$dest" branch --show-current)"
            if [ "$cur" = "$branch" ]; then say "  $dir: present on $cur"; else say "  $dir: present on '$cur', manifest says '$branch' (left alone)"; fi
        elif [ -e "$dest" ]; then
            say "  $dir: exists but is not a git clone (left alone)"
        else
            say "  $dir: cloning $branch"
            run git clone --branch "$branch" "$url" "$dest"
        fi
    done < "$manifest"
}

install_repos() {
    say "== Sibling repos (into $(dirname "$DEV_REPO")) =="
    sync_repos "$ZB_DIR/config/repos.txt" "$(dirname "$DEV_REPO")"
    say "== Custom nodes (into $DEV_REPO/custom_nodes) =="
    run mkdir -p "$DEV_REPO/custom_nodes"
    sync_repos "$ZB_DIR/config/custom-nodes.txt" "$DEV_REPO/custom_nodes"
}

install_blueprints() {
    say "== Blueprints (symlinked into $DEV_REPO/blueprints) =="
    if [ "$DRY" -eq 1 ]; then
        say "  [dry-run] would link $(find "$ZB_DIR/examples/blueprints" -name '*.json' | wc -l) blueprints"
    else
        zb_link_blueprints "$DEV_REPO"
        say "  linked (releases get theirs from cut-release.sh)"
    fi
}

# Copied, not linked: ComfyUI's Save writes into user/default/workflows, and a
# symlink would let that rewrite the tracked file.
install_workflows() {
    local src="$ZB_DIR/examples/workflows" dst="$DEV_REPO/user/default/workflows"
    local f rel new=0 same=0 differ=0
    say "== Workflows (copied into $dst) =="
    while IFS= read -r -d '' f; do
        rel="${f#"$src"/}"
        if [ ! -e "$dst/$rel" ]; then
            run mkdir -p "$(dirname "$dst/$rel")"
            run cp "$f" "$dst/$rel"
            new=$((new + 1))
        elif cmp -s "$f" "$dst/$rel"; then
            same=$((same + 1))
        elif [ "$FORCE" -eq 1 ]; then
            run cp "$f" "$dst/$rel"
            new=$((new + 1))
        else
            say "  differs, skipped (edited locally?): $rel"
            differ=$((differ + 1))
        fi
    done < <(find "$src" -type f -name '*.json' -print0)
    say "  copied $new, unchanged $same, differing $differ (use --force to overwrite)"
}

install_pip_conf() {
    local venv="$DEV_REPO/.venv"
    [ -d "$venv" ] && [ ! -f "$venv/pip.conf" ] && [ -f "$DEV_REPO/constraints-gb10.txt" ] || return 0
    say "== venv pip.conf =="
    if [ "$DRY" -eq 1 ]; then
        say "  [dry-run] would write $venv/pip.conf pinning torch via constraints-gb10.txt"
    else
        printf '[install]\nconstraint = %s/constraints-gb10.txt\n' "$DEV_REPO" > "$venv/pip.conf"
        say "  wrote $venv/pip.conf"
    fi
}

render() {
    sed -e "s#@RELEASES_ROOT@#$RELEASES_ROOT#g" \
        -e "s#@FRONTEND_ROOT@#$(dirname "$DEV_REPO")/ComfyUI_frontend/dist#g" \
        -e "s#@SECRETS_FILE@#$ZB_SECRETS_FILE#g" "$1"
}

install_units() {
    local tpl name out changed=0
    say "== systemd user units (into $UNIT_DIR) =="
    run mkdir -p "$UNIT_DIR"
    for tpl in "$ZB_DIR"/templates/*.service.in; do
        name="$(basename "${tpl%.in}")"
        out="$UNIT_DIR/$name"
        if [ -f "$out" ] && [ "$(render "$tpl")" = "$(cat "$out")" ]; then
            say "  $name: up to date"
            continue
        fi
        if [ -f "$out" ]; then
            say "  $name: differs, backing up existing to $name.bak-$(date -u +%Y%m%dT%H%M%SZ)"
            run cp "$out" "$out.bak-$(date -u +%Y%m%dT%H%M%SZ)"
        else
            say "  $name: new"
        fi
        if [ "$DRY" -eq 0 ]; then render "$tpl" > "$out"; fi
        changed=1
    done
    if [ "$changed" -eq 1 ] && [ "$DRY" -eq 0 ] && [ "$DEFAULT_UNIT_DIR" -eq 1 ]; then
        systemctl --user daemon-reload
        say "  daemon-reload done. Units are NOT enabled or restarted; when ready:"
        say "    systemctl --user restart comfyui.service"
    fi
}

check_secrets
install_repos
install_blueprints
install_workflows
install_pip_conf
install_units
say "Done."
