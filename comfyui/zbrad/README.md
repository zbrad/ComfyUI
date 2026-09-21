# comfyui/zbrad

Everything this fork adds on top of upstream ComfyUI, kept out of upstream's
tree. Run `scripts/install.sh` to wire it into a checkout.

## Layout

| Folder | Contents |
|---|---|
| `scripts/` | `install.sh`, release tooling (`cut-release.sh`, `activate-release.sh`, `rollback.sh`, `list-releases.sh`, `test-and-publish.sh`, `deploy-from-release.sh`, `integration_test.sh`), `log_generation.py`, `resource_watcher.py`, `quantize_checkpoint_fp8.py`, and the vendored `tuned-common.sh` / `sync-common.sh` |
| `templates/` | systemd user unit templates and `comfy.example` (the settings file) |
| `custom_nodes/` | nodes that live in this repo instead of their own (`comfyui-first-run-setup`); `install.sh` links them into `custom_nodes/` |
| `config/` | `custom-nodes.txt` and `repos.txt`: what `install.sh` clones |
| `examples/` | `blueprints/` (the `ZB ...` LTX-2.5 blueprints) and `workflows/` (LTX-2.5 workflow templates) |
| `docs/` | `deploy.md` (release model), `cosmos3_nf4_perf_plan.md` |
| `requirements/` | `requirements-no-torch.txt`, `requirements-open.txt`, `custom-nodes.txt` (pinned packages for the custom nodes and the resource watcher), `test.txt` (pinned test-only packages) |

## Install

```bash
mkdir -p ~/.secrets && cp comfyui/zbrad/templates/comfy.example ~/.secrets/comfy
chmod 600 ~/.secrets/comfy          # then edit COMFY_LISTEN_ADDR
comfyui/zbrad/scripts/install.sh --dry-run
comfyui/zbrad/scripts/install.sh
```

`install.sh` is idempotent. It clones missing sibling repos and custom nodes
(never switching an existing clone's branch), links the blueprints, copies the
workflows, writes a default `~/.config/comfyui/extra_model_paths.yaml` if none
exists (never overwriting one), and renders the units into
`~/.config/systemd/user`. It never enables, starts or restarts a service; it
prints the restart command instead.

- **Blueprints are symlinked** into `blueprints/` (ComfyUI only reads that
  folder) and listed in the repo's local `.git/info/exclude`. `cut-release.sh`
  links them into each release too.
- **Workflows are copied**, not linked, into `user/default/workflows/`, because
  ComfyUI's Save writes to those files. A copy that differs is skipped unless
  you pass `--force`.

## Settings (`~/.secrets/comfy`)

Plain `KEY=value` lines, mode 600. Read by the units (`EnvironmentFile=`) and
by the scripts. Everything else is derived from where the checkout lives.

| Variable | Default | Meaning |
|---|---|---|
| `COMFY_LISTEN_ADDR` | `127.0.0.1` | address ComfyUI listens on |
| `COMFY_PORT` | `8188` | service port |
| `COMFY_TEST_PORT` | `8189` | isolated port for `test-and-publish.sh` |
| `COMFY_FRONTEND_ROOT` | `<checkout parent>/ComfyUI_frontend/dist` | `--front-end-root` |
| `COMFY_EXTRA_MODEL_PATHS` | `~/.config/comfyui/extra_model_paths.yaml` | `--extra-model-paths-config`; absolute path in the settings file |

`COMFY_SECRETS_FILE` overrides the file's location.

## Deliberately outside this folder

- `constraints-gb10.txt`: `tuned-common.sh` regenerates it at the repo root, and
  the venv's `pip.conf` points at it.
- `.mcp.json`: Claude Code only reads it from the repo root.
- `model_collision_guard.py` and its two lines in `main.py`: `main.py` imports it.
- `.gitignore`: one added line for `generation-log.jsonl`.

## Notes

- **Machines still running the old units** (hand-written, running
  `resource_watcher.py` from the release root) must run `install.sh` and then
  restart before their next release cut, or the resource-watch unit will fail
  to find the script. `comfyui.service` itself is unaffected.
- **`ComfyUI_frontend`'s `zbrad-local` branch** is based on v1.49.6; upstream now
  pins a newer frontend, so rebase it before serving it with `--front-end-root`.
- **`comfyui-first-run-setup`** is kept in `custom_nodes/` here and linked in by
  `install.sh`. It swaps ComfyUI's stock default graph for the LTX-2.5 Text to
  Video workflow, only on a browser's first visit and never over real work.
