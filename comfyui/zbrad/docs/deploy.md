# Release-based deployment for comfyui.service

Scripts live in `comfyui/zbrad/scripts/`; see `comfyui/zbrad/README.md` for
setup (`install.sh`) and the settings file.

`comfyui.service` (a systemd user unit) used to run directly out of the dev
checkout — restarting it just re-executed whatever was on
disk at that moment, with no way to roll back except editing that same live
tree in place.

This directory holds the scripts for a git-worktree-based release model
instead:

```
<dev checkout>                                   <- edit/commit here as usual
<parent of dev checkout>/ComfyUI-releases/
    releases/<short-sha>-<UTC timestamp>/        <- one git worktree per release, pinned to a commit
    current -> releases/<active release>          <- what the service actually runs
```

`comfyui.service` points at `<releases>/current` (`WorkingDirectory`
and `ExecStart` both). A release/rollback is just: repoint `current`, restart
the service.

**What's shared across every release** (symlinked into each release
worktree, not duplicated): `.venv/`, `models/`, `output/`, `input/`,
`temp/`, `user/`, `custom_nodes/`. These are exactly the paths `.gitignore`
excludes from the repo, so a plain `git worktree add` wouldn't reproduce
them anyway (git has nothing tracked to check out there beyond a
placeholder file) — the 129G `models/` directory in particular must never
be copied per release.

**What this does *not* cover:** `custom_nodes/` is shared, not
per-release, so a bad change to a custom node (e.g. switching
`custom_nodes/comfyui-crystools` to an experimental branch) is **not**
protected by this rollback mechanism — only core ComfyUI code is. Custom
nodes that are their own git repos (like crystools) have their own
independent rollback: `git checkout <previous-branch-or-tag>` inside that
node's own directory, then restart the service. Same for the shared venv —
rolling back to an old release commit does not roll back Python
dependencies; if a release needs a different dependency set, rebuild/adjust
the shared `.venv` separately.

## Model folders (`extra_model_paths.yaml`)

`comfyui.service` and `test-and-publish.sh` both start ComfyUI with
`--extra-model-paths-config $COMFY_EXTRA_MODEL_PATHS` (default
`~/.config/comfyui/extra_model_paths.yaml`), so the live service and the test
instance see the same model folders. ComfyUI opens that file without checking
it exists and exits if it is missing, and `Restart=on-failure` would then loop,
so `install.sh` writes it from `templates/extra_model_paths.yaml.in` when it is
absent, with `base_path` set to the checkout's own `models/`, and never
overwrites an existing one. To keep models somewhere else, such as a shared
area, edit `base_path` in that file; it is yours after the first write.

## Scripts

- `cut-release.sh <commit-ish>` — create a new release worktree at the
  given commit (defaults to `HEAD`), symlink in the shared dirs, print its
  path. Does **not** activate it.
- `activate-release.sh <release-dir-or-short-sha>` — repoint `current` at
  the given release and restart `comfyui.service`, verifying it comes back
  up. Records the previously-active release so `rollback.sh` can undo it.
- `rollback.sh` — repoint `current` back to whatever it pointed at *before*
  the last `activate-release.sh` call, and restart. Since
  `activate-release.sh` records the previous release on every call
  (including a rollback's own call), running `rollback.sh` twice in a row
  toggles back and forth between the last two releases rather than
  consuming a single undo.
- `list-releases.sh` — list all release worktrees with their commit and
  timestamp, marking the currently active one.
- `test-and-publish.sh [--no-push] <commit-ish>` — the "is this commit good
  enough to release" gate. Cuts a release, boots it as a **separate,
  local-only instance on `COMFY_TEST_PORT`** (`127.0.0.1:8189` by default —
  distinct from the real service's `$COMFY_LISTEN_ADDR:$COMFY_PORT`, so
  testing never touches or contends with the live service), runs
  `tests-unit/` against it with the GPU hidden (the suite assumes a CPU-only
  machine, like upstream CI), and only if everything passes publishes the
  release: an annotated git tag `release/<version>-<sha8>` on the tested
  commit, pushed to `origin`, whose message records the test counts. It never
  touches the running service; deploying is a separate step. On failure
  nothing is tagged and the release is kept on disk for inspection. It stops
  if the tag already exists. `--no-push` creates the tag locally only. Set
  `INTEGRATION_TEST_CMD` to also run a live/workflow-level check against the
  test instance before it's torn down (invoked with `COMFYUI_TEST_URL` set to
  its base URL) — not wired in by default, but `integration_test.sh` (below)
  is a ready-made option.
- `deploy-from-release.sh <release-tag>` — deploy a published release. Fetches
  the tags, refuses anything that is not an annotated `release/*` tag, cuts a
  release worktree at that tag's commit (or reuses the one already cut for it),
  and calls `activate-release.sh`, which restarts the service. It only fetches,
  so any node with a checkout can run it, including one that does not own the
  repo. Untagged commits cannot be deployed this way.
- `integration_test.sh` — point `INTEGRATION_TEST_CMD` at this to queue a
  real workflow through
  [zbrad/comfyui-test-integrations](https://github.com/zbrad/comfyui-test-integrations)'
  `test_workflow_headless.py` against the isolated test instance before
  every publish: `INTEGRATION_TEST_CMD=comfyui/zbrad/scripts/integration_test.sh
  comfyui/zbrad/scripts/test-and-publish.sh HEAD`. Drives an actual headless browser
  (Playwright) so this exercises ComfyUI's real `app.graphToPrompt()`/
  `app.queuePrompt()`, not a hand-rolled reimplementation of that
  conversion — see that repo's `test_workflow_headless.py` docstring for
  why that distinction matters. Defaults to the plain `Text to Video
  (LTX-2.5)` blueprint (no image input to wire up, fastest of the three
  LTX-2.5 blueprints); override `WORKFLOW_PATH`/`INTEGRATION_TEST_PROMPT`/
  `INTEGRATION_TEST_TIMEOUT` to point at a different one. Needs
  `comfyui-test-integrations` cloned as a sibling of this repo
  (`../comfyui-test-integrations`, override via `HARNESS_REPO`) and its
  `requirements.txt` (`playwright`, `mcp`) installed into the shared
  `.venv`, plus a downloaded Chromium (already there as of this writing;
  `.venv/bin/python3 -m playwright install chromium` if not).

`test-and-publish.sh` needs the test packages (pytest and the rest) in the
shared `.venv`. They are a separate, torch-free set that is not there by
default, pinned in `requirements/test.txt`:
`.venv/bin/python -m pip install --no-deps -r comfyui/zbrad/requirements/test.txt`
(one-time; `--no-deps` keeps the tuned torch untouched). The custom nodes' and
the resource watcher's own packages are pinned in `requirements/custom-nodes.txt`
and installed the same way.

## Typical flow

```
cd <dev checkout>
git commit -am "..."                    # normal dev work
INTEGRATION_TEST_CMD=comfyui/zbrad/scripts/integration_test.sh comfyui/zbrad/scripts/test-and-publish.sh HEAD
                                         # tests-unit/ + a real queued workflow on :8189;
                                         # on pass, tags and pushes release/<version>-<sha8>
comfyui/zbrad/scripts/deploy-from-release.sh release/<version>-<sha8>
                                         # on the node that should run it: fetches the tag,
                                         # cuts (or reuses) the release, restarts the service
# ... if the new release is bad anyway:
comfyui/zbrad/scripts/rollback.sh
```

The lower-level scripts still work on their own, but they skip the publish
gate, so a deploy made this way is not tied to a tested tag:

```
comfyui/zbrad/scripts/cut-release.sh                   # cuts a release at HEAD
comfyui/zbrad/scripts/activate-release.sh <printed-path-or-sha>
comfyui/zbrad/scripts/rollback.sh                      # if it's bad
```

Old release worktrees are not auto-pruned — remove one with
`git worktree remove <path>` (from the dev checkout) once you're sure it's no
longer needed, then `rm -rf` the directory doesn't apply since `worktree
remove` already deletes it.
