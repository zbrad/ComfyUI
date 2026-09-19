#!/usr/bin/env bash
# Cut a release for a candidate commit, run it up on an isolated TEST_PORT
# (never the real service port), run tests against it, and only activate
# it as the live release (via activate-release.sh, on the real
# comfyui.service port) if everything passes. Production is untouched on
# any failure.
set -euo pipefail

DEV_REPO=/home/zbrad/gh/ComfyUI
SERVICE_PORT=8188                  # comfyui.service's real port -- keep in sync with the unit file
TEST_PORT="${TEST_PORT:-8189}"     # isolated port for the pre-deploy test instance
TEST_HOST=127.0.0.1                # test instance is local-only, unlike the real service's listen address

# Optional: a command to run additional live/integration tests against the
# test instance (e.g. a Playwright workflow check). It's invoked with
# COMFYUI_TEST_URL set to the test instance's base URL. Skipped if unset.
INTEGRATION_TEST_CMD="${INTEGRATION_TEST_CMD:-}"

if [ $# -ne 1 ]; then
    echo "usage: $0 <commit-ish>" >&2
    echo "  env: TEST_PORT (default 8189), INTEGRATION_TEST_CMD (optional)" >&2
    exit 1
fi

COMMITISH="$1"

if [ "$TEST_PORT" = "$SERVICE_PORT" ]; then
    echo "error: TEST_PORT must differ from the service port ($SERVICE_PORT)" >&2
    exit 1
fi

echo "== Cutting release for testing (${COMMITISH}) ==" >&2
REL=$("$DEV_REPO/deploy/cut-release.sh" "$COMMITISH" | grep -oP 'Release ready: \K.*')
echo "Release under test: $REL" >&2

TEST_PID=""
cleanup() {
    if [ -n "$TEST_PID" ] && kill -0 "$TEST_PID" 2>/dev/null; then
        echo "== Stopping test instance (pid $TEST_PID) ==" >&2
        kill "$TEST_PID" 2>/dev/null || true
        wait "$TEST_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "== Starting test instance on ${TEST_HOST}:${TEST_PORT} ==" >&2
"$REL/.venv/bin/python" "$REL/main.py" --listen "$TEST_HOST" --port "$TEST_PORT" \
    > "$REL/test-instance.log" 2>&1 &
TEST_PID=$!

echo "Waiting for test instance to come up..." >&2
UP=0
for _ in $(seq 1 30); do
    if ss -ltn 2>/dev/null | grep -q ":${TEST_PORT} "; then
        UP=1
        break
    fi
    if ! kill -0 "$TEST_PID" 2>/dev/null; then
        echo "error: test instance exited early, see $REL/test-instance.log" >&2
        exit 1
    fi
    sleep 1
done
if [ "$UP" -ne 1 ]; then
    echo "error: test instance did not come up on :${TEST_PORT} within 30s" >&2
    exit 1
fi
echo "Test instance up (pid $TEST_PID)." >&2

echo "== Running tests-unit/ ==" >&2
if ! "$REL/.venv/bin/python" -m pytest "$REL/tests-unit" -q; then
    echo "Unit tests FAILED -- not deploying. Release kept at $REL for inspection." >&2
    exit 1
fi

if [ -n "$INTEGRATION_TEST_CMD" ]; then
    echo "== Running integration test command ==" >&2
    if ! COMFYUI_TEST_URL="http://${TEST_HOST}:${TEST_PORT}" bash -c "$INTEGRATION_TEST_CMD"; then
        echo "Integration tests FAILED -- not deploying. Release kept at $REL for inspection." >&2
        exit 1
    fi
fi

echo "== All tests passed. Stopping test instance before activating on the real service ==" >&2
trap - EXIT
cleanup
sleep 2  # let the GPU/CUDA context from the test instance fully release before the real restart

"$DEV_REPO/deploy/activate-release.sh" "$REL"
