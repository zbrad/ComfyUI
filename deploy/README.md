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

## Typical flow

```
cd ~/gh/ComfyUI
git commit -am "..."                    # normal dev work on zbrad-local
deploy/cut-release.sh                   # cuts a release at HEAD
deploy/activate-release.sh <printed-path-or-sha>
# ... service now running the new release; if it's bad:
deploy/rollback.sh
```

Old release worktrees are not auto-pruned — remove one with
`git worktree remove <path>` (from `~/gh/ComfyUI`) once you're sure it's no
longer needed, then `rm -rf` the directory doesn't apply since `worktree
remove` already deletes it.
