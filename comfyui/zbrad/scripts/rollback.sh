#!/usr/bin/env bash
# Swap `current` back to whatever it pointed at before the last
# activate-release.sh call, and restart the service. Since
# activate-release.sh records .previous on every call (including this
# one), running rollback.sh twice in a row toggles back and forth
# between the last two releases rather than consuming a single undo.
set -euo pipefail

PREVIOUS_FILE=/home/zbrad/gh/ComfyUI-releases/.previous
ACTIVATE_SCRIPT=/home/zbrad/gh/ComfyUI/deploy/activate-release.sh

if [ ! -f "$PREVIOUS_FILE" ]; then
    echo "error: no recorded previous release to roll back to" \
         "(nothing activated via activate-release.sh yet)" >&2
    exit 1
fi

PREVIOUS=$(cat "$PREVIOUS_FILE")
if [ ! -d "$PREVIOUS" ]; then
    echo "error: recorded previous release no longer exists: $PREVIOUS" >&2
    exit 1
fi

echo "Rolling back to: $PREVIOUS" >&2
exec "$ACTIVATE_SCRIPT" "$PREVIOUS"
