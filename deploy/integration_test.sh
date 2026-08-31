#!/usr/bin/env bash
# Meant to be pointed at by test-and-deploy.sh's INTEGRATION_TEST_CMD, e.g.:
#   INTEGRATION_TEST_CMD=deploy/integration_test.sh deploy/test-and-deploy.sh HEAD
#
# Queues a real workflow against the isolated test instance
# test-and-deploy.sh already booted (COMFYUI_TEST_URL, set by that
# script -- required here) via test_workflow_headless.py, which drives an
# actual headless browser so this exercises the same app.graphToPrompt()/
# app.queuePrompt() path a real user's click would, not just an API-format
# reimplementation. Exit code propagates: 0 keeps the deploy going, nonzero
# stops it before production is touched (test-and-deploy.sh's own
# behavior, not anything special here).
#
# Defaults to the plain Text to Video (LTX-2.5) blueprint -- no image
# input to wire up, fastest of the three LTX-2.5 blueprints, and
# representative of "does the actual generation pipeline still work" for
# a deploy gate. Override WORKFLOW_PATH to point at a different one (an
# image/first-last-frame blueprint needs a LoadImage node attached to its
# input sockets first -- see test_workflow_headless.py's own docstring
# and the LoadImage-wiring pattern used to verify those blueprints
# initially, not reproduced here since the CI-gate default deliberately
# stays to the one blueprint with no such setup needed).
set -euo pipefail

DEV_REPO=/home/zbrad/gh/ComfyUI
WORKFLOW_PATH="${WORKFLOW_PATH:-$DEV_REPO/blueprints/Text to Video (LTX-2.5).json}"
INTEGRATION_TEST_PROMPT="${INTEGRATION_TEST_PROMPT:-A single red apple resting on a plain wooden table, soft natural light, static camera, three seconds.}"
INTEGRATION_TEST_TIMEOUT="${INTEGRATION_TEST_TIMEOUT:-300}"

if [ -z "${COMFYUI_TEST_URL:-}" ]; then
    echo "error: COMFYUI_TEST_URL not set (this script is meant to be run via" >&2
    echo "  test-and-deploy.sh's INTEGRATION_TEST_CMD, which sets it)" >&2
    exit 1
fi

"$DEV_REPO/.venv/bin/python3" "$DEV_REPO/test_workflow_headless.py" \
    "$WORKFLOW_PATH" \
    --url "$COMFYUI_TEST_URL" \
    --prompt "$INTEGRATION_TEST_PROMPT" \
    --timeout "$INTEGRATION_TEST_TIMEOUT"
