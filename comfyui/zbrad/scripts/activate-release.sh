#!/usr/bin/env bash
# Repoint the `current` symlink at the given release and restart
# comfyui.service, verifying it comes back up. Records the
# previously-active release in .previous so rollback.sh can swap back.
set -euo pipefail

RELEASES_DIR=/home/zbrad/gh/ComfyUI-releases/releases
CURRENT_LINK=/home/zbrad/gh/ComfyUI-releases/current
PREVIOUS_FILE=/home/zbrad/gh/ComfyUI-releases/.previous
SERVICE=comfyui.service
PORT=8188  # keep in sync with ~/.config/systemd/user/comfyui.service

if [ $# -ne 1 ]; then
    echo "usage: $0 <release-dir-or-short-sha>" >&2
    exit 1
fi

ARG="$1"

if [ -d "$ARG" ]; then
    TARGET=$(readlink -f "$ARG")
else
    shopt -s nullglob
    MATCHES=("$RELEASES_DIR/${ARG}"*)
    shopt -u nullglob
    if [ "${#MATCHES[@]}" -eq 0 ]; then
        echo "error: no release matches '$ARG' under $RELEASES_DIR" >&2
        exit 1
    elif [ "${#MATCHES[@]}" -gt 1 ]; then
        echo "error: '$ARG' matches multiple releases, be more specific:" >&2
        printf '  %s\n' "${MATCHES[@]}" >&2
        exit 1
    fi
    TARGET="${MATCHES[0]}"
fi

if [ ! -d "$TARGET" ]; then
    echo "error: release path does not exist: $TARGET" >&2
    exit 1
fi

PREVIOUS=""
if [ -L "$CURRENT_LINK" ]; then
    PREVIOUS=$(readlink -f "$CURRENT_LINK")
fi

echo "Activating release: $TARGET" >&2
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$TARGET" ]; then
    echo "$PREVIOUS" > "$PREVIOUS_FILE"
fi

ln -sfn "$TARGET" "$CURRENT_LINK"

systemctl --user restart "$SERVICE"

echo "Waiting for $SERVICE to come up..." >&2
for _ in $(seq 1 20); do
    if systemctl --user is-active --quiet "$SERVICE" && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
        echo "OK: $SERVICE active and listening on :${PORT}, running $TARGET" >&2
        exit 0
    fi
    sleep 1
done

echo "warning: $SERVICE did not confirm active+listening within 20s -- check: journalctl --user -u $SERVICE -n 50" >&2
exit 1
