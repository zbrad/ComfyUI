# Release-based deployment for comfyui.service

`comfyui.service` (a systemd user unit) used to run directly out of the dev
checkout at `~/gh/ComfyUI` — restarting it just re-executed whatever was on
disk at that moment, with no way to roll back except editing that same live
tree in place.

This directory holds the scripts for a git-worktree-based release model
instead:

```
~/gh/ComfyUI                                    <- dev checkout (edit/commit here as usual)
~/gh/ComfyUI-releases/
    releases/<short-sha>-<UTC timestamp>/        <- one git worktree per release, pinned to a commit
    current -> releases/<active release>          <- what the service actually runs
```

`comfyui.service` points at `~/gh/ComfyUI-releases/current` (`WorkingDirectory`
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
- `test-and-deploy.sh <commit-ish>` — the actual "build passed testing, now
  deploy" gate. Cuts a release, boots it as a **separate, local-only
  instance on `TEST_PORT`** (`127.0.0.1:8189` by default — distinct from
  the real service's `100.112.80.10:8188`, so testing never touches or
  contends with the live service), runs `tests-unit/` against it, and only
  then calls `activate-release.sh` on the real port. The test instance is
  always torn down before production is touched — on failure it's killed
  and production is left alone with the failed release kept on disk for
  inspection; on success it's stopped first (freeing GPU/CUDA state) and
  *then* the real restart happens, so the two are never running against
  the model-loading GPU at the same time. Set `INTEGRATION_TEST_CMD` to
  also run a live/workflow-level check against the test instance before
  it's torn down (invoked with `COMFYUI_TEST_URL` set to its base URL) —
  nothing is wired in by default beyond `tests-unit/`.

`test-and-deploy.sh` needs `tests-unit/requirements.txt` (pytest etc.)
installed in the shared `.venv` — it's a separate, torch-free dependency
set from the app's own requirements, so it's not there by default:
`.venv/bin/pip install -r tests-unit/requirements.txt` (one-time, respects
the venv's existing torch-pin constraint).

## Typical flow

```
cd ~/gh/ComfyUI
git commit -am "..."                    # normal dev work on zbrad-local
deploy/test-and-deploy.sh HEAD          # tests on :8189, deploys to :8188 only if they pass
# ... service now running the new release; if it's bad anyway:
deploy/rollback.sh
```

Lower-level flow without the test gate (e.g. deploying a commit you've
already validated some other way):

```
deploy/cut-release.sh                   # cuts a release at HEAD
deploy/activate-release.sh <printed-path-or-sha>
deploy/rollback.sh                      # if it's bad
```

Old release worktrees are not auto-pruned — remove one with
`git worktree remove <path>` (from `~/gh/ComfyUI`) once you're sure it's no
longer needed, then `rm -rf` the directory doesn't apply since `worktree
remove` already deletes it.
