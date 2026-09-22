#!/usr/bin/env bash
# Cut a release for a candidate commit, run it up on an isolated COMFY_TEST_PORT
# (never the real service port), run tests against it, and if everything passes
# publish it: an annotated git tag release/<version>-<sha8> on the tested commit,
# pushed to origin. Never touches the running service; deploy a published
# release with deploy-from-release.sh.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SERVICE_PORT="$COMFY_PORT"         # comfyui.service's real port
TEST_PORT="$COMFY_TEST_PORT"       # isolated port for the pre-publish test instance
TEST_HOST=127.0.0.1                # test instance is local-only, unlike the real service's listen address

# Optional: a command to run additional live/integration tests against the
# test instance (e.g. a Playwright workflow check). It's invoked with
# COMFYUI_TEST_URL set to the test instance's base URL. Skipped if unset.
INTEGRATION_TEST_CMD="${INTEGRATION_TEST_CMD:-}"

PUSH=1
if [ "${1:-}" = "--no-push" ]; then
    PUSH=0
    shift
fi
if [ $# -ne 1 ]; then
    echo "usage: $0 [--no-push] <commit-ish>" >&2
    echo "  --no-push  create the tag locally but do not push it" >&2
    echo "  env: COMFY_TEST_PORT (default 8189), INTEGRATION_TEST_CMD (optional)" >&2
    exit 1
fi

COMMITISH="$1"

if [ "$TEST_PORT" = "$SERVICE_PORT" ]; then
    echo "error: TEST_PORT must differ from the service port ($SERVICE_PORT)" >&2
    exit 1
fi

COMMIT="$(git -C "$DEV_REPO" rev-parse --verify "${COMMITISH}^{commit}")"
VERSION="$(git -C "$DEV_REPO" show "${COMMIT}:comfyui_version.py" | sed -n 's/^__version__ = "\(.*\)"$/\1/p')"
if [ -z "$VERSION" ]; then
    echo "error: no __version__ in comfyui_version.py at ${COMMIT:0:8}" >&2
    exit 1
fi
TAG="release/${VERSION}-${COMMIT:0:8}"
if git -C "$DEV_REPO" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "error: $TAG already exists" >&2
    exit 1
fi

echo "== Cutting release for testing (${COMMITISH}) ==" >&2
REL=$("$ZB_SCRIPTS_DIR/cut-release.sh" "$COMMIT" | grep -oP 'Release ready: \K.*')
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
"$COMFY_VENV/bin/python" "$REL/main.py" --listen "$TEST_HOST" --port "$TEST_PORT" \
    --extra-model-paths-config "$COMFY_EXTRA_MODEL_PATHS" \
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

echo "== Checking dependencies ==" >&2
# Catches a venv missing something a custom node needs. That kind of gap does
# not crash and no test covers it: the node just quietly does less.
if ! "$COMFY_VENV/bin/python" "$REL/comfyui/zbrad/scripts/check_deps.py" --repo-root "$REL"; then
    echo "Dependency check FAILED -- not publishing. Release kept at $REL for inspection." >&2
    exit 1
fi

echo "== Running tests-unit/ ==" >&2
# CPU only, like upstream CI: with a GPU visible, test_db_init_locking's `import main` breaks test_seedvr2_dtype.
if ! CUDA_VISIBLE_DEVICES="" "$COMFY_VENV/bin/python" -m pytest "$REL/tests-unit" -q --junitxml="$REL/unit-tests.xml"; then
    echo "Unit tests FAILED -- not publishing. Release kept at $REL for inspection." >&2
    exit 1
fi
# The suite swallows pytest's terminal summary, so the counts come from the JUnit report.
UNIT_SUMMARY="$("$COMFY_VENV/bin/python" - "$REL/unit-tests.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suite = root if root.tag == "testsuite" else root.find("testsuite")
tests, failures, errors, skipped = (int(suite.get(k)) for k in ("tests", "failures", "errors", "skipped"))
print(f"unit tests: {tests - failures - errors - skipped} passed, {skipped} skipped")
PY
)"
INTEGRATION_SUMMARY="integration test: not run"

if [ -n "$INTEGRATION_TEST_CMD" ]; then
    echo "== Running integration test command ==" >&2
    if ! COMFYUI_TEST_URL="http://${TEST_HOST}:${TEST_PORT}" bash -c "$INTEGRATION_TEST_CMD"; then
        echo "Integration tests FAILED -- not publishing. Release kept at $REL for inspection." >&2
        exit 1
    fi
    INTEGRATION_SUMMARY="integration test: passed"
fi

trap - EXIT
cleanup

echo "== All tests passed. Publishing $TAG ==" >&2
git -C "$DEV_REPO" tag -a "$TAG" "$COMMIT" -F - <<TAGMSG
ComfyUI ${VERSION} (${COMMIT:0:8})

$(git -C "$DEV_REPO" log -1 --format=%s "$COMMIT")

Tested before publishing: ${UNIT_SUMMARY}; ${INTEGRATION_SUMMARY}.
TAGMSG
if [ "$PUSH" -eq 1 ]; then
    git -C "$DEV_REPO" push origin "refs/tags/$TAG"
    echo "Published $TAG. Deploy it with: $ZB_SCRIPTS_DIR/deploy-from-release.sh $TAG" >&2
else
    echo "Tagged $TAG locally (--no-push), not published. Push it with: git push origin refs/tags/$TAG" >&2
fi
