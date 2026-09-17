# Cosmos3-Super-Text2Image-nf4: slow-generation investigation + speedup plan

Started on node-gb10-1 2026-09-15, investigating why a standalone diffusers
script (not yet wired into ComfyUI itself) using
`SanDiegoDude/Cosmos3-Super-Text2Image-nf4` took 1h40m+ for a single
`pipe(prompt)` call. Moved to a fresh node-gb10-2 checkout on 2026-09-16 to
avoid contending for GPU/unified-memory with the node-gb10-1 job, and picking
the investigation back up there now.

## Confirmed facts

- **Download/caching was never the problem.** Standard HF hub cache
  (`~/.cache/huggingface/hub`) already dedupes by revision; the 35GB Super
  checkout downloaded in ~4 minutes and will not re-download on subsequent
  `from_pretrained` calls. `HF_HUB_OFFLINE=1` skips even the small
  metadata/ETag check once cached.
- **The slow part is generation itself, not a hang.** Confirmed via
  `sudo env "PATH=$PATH" .venv/bin/py-spy dump --pid <pid>` (needs sudo —
  same-user ptrace is blocked by Yama LSM in this sandbox) — the process was
  genuinely inside real forward-pass code (RMSNorm / decoder-layer loop in
  `diffusers/models/transformers/transformer_cosmos3.py`), sustained 96% GPU
  SM utilization the whole time, not deadlocked.
- **Why it's legitimately slow by construction:**
  - The "Super" transformer config (`transformer/config.json`) is
    `hidden_size=5120`, `num_hidden_layers=64`, `intermediate_size=25600`,
    `use_moe=true` with a **separate MLP per pathway**
    (`mlp` + `mlp_moe_gen` each layer) — a Qwen3-VL-scale (~100B-parameter
    class) multimodal transformer, not a small image-diffusion UNet/DiT.
    (49GB GPU footprint ≈ 100B params × 0.5 bytes/NF4 — checks out.)
  - Default `num_inference_steps=35`
    (`diffusers/pipelines/cosmos/pipeline_cosmos3_omni.py:1330`) — every
    step is a full 64-layer forward pass.
  - It's NF4-quantized via bitsandbytes, which **dequantizes weights
    on-the-fly on every forward call** (no persistent dequant cache) — so
    the ~50GB of packed weights gets reconstructed 35 separate times per
    image, on top of the actual matmul compute.
  - Running eager (interactive REPL, no `torch.compile`/CUDA graphs), so
    per-op launch overhead compounds across 64 layers × 2 MoE pathways ×
    35 steps.
  - Memory pressure was a real but secondary factor: with two other
    ComfyUI GPU processes resident, system RAM (GB10 unified CPU+GPU pool,
    121GB total) dropped to 5.6GB free with 4.1GB swapped. Stopping those
    processes freed it back to ~61GB free / 2.3GB swap without materially
    changing the run's pace — confirms compute cost, not swap thrashing,
    is the dominant driver.

## Attention backend finding

`Cosmos3AttnProcessor.__call__` (`transformer_cosmos3.py`) calls
`dispatch_attention_fn(..., backend=self._attention_backend)` with
`_attention_backend = None` at the class level. `dispatch_attention_fn`
(`diffusers/models/attention_dispatch.py:407`) resolves `backend=None` to
`_AttentionBackendRegistry.get_active_backend()`, which is seeded from the
`DIFFUSERS_ATTN_BACKEND` env var, **defaulting to `"native"`**
(PyTorch's built-in `scaled_dot_product_attention`).

The GB10-tuned `flash_attn` fork (`2.8.4+gb10.cu133`, from
`ZBrad-LLC/flash-attention`, see that repo's
`project_gb10_native_build.md` memory) **is already installed** in the
ComfyUI venv but is **not used by default** — nothing sets
`DIFFUSERS_ATTN_BACKEND=flash` or calls the equivalent
`set_attention_backend`/`attention_backend()` API.

Caveat: an earlier controlled comparison for a *different* workload
(LTX-2.5, see `flash_attention_gb10_build.md` memory) found attention
backend choice was noise (~2-14s spread) next to a ~40-50s cold/warm gap.
Cosmos3-Super is a much bigger, more attention-heavy model — don't assume
that result transfers; re-measure here specifically.

## Nano checkpoint loading gotcha

`SanDiegoDude/Cosmos3-Nano-nf4`'s `transformer/config.json` sets
`"action_gen": true`, which makes `Cosmos3OmniTransformer.__init__` create
`action_proj_in`/`action_proj_out` submodules
(`transformer_cosmos3.py:478-482`) — but this checkpoint (quantized against
`diffusers 0.39.0.dev0`, two dev-versions behind our `0.41.0.dev0`) doesn't
actually contain weights for them. Under `low_cpu_mem_usage=True` (required
for quantized loads — `low_cpu_mem_usage=False` raises outright), missing
keys are never materialized off the `meta` device, so `pipe.to("cuda")`
dies with `NotImplementedError: Cannot copy out of meta tensor; no data!`.
**Fix**: edited the *local cached* `transformer/config.json` to set
`"action_gen": false` (original backed up alongside as
`config.json.orig-backup`) — action-conditioning isn't needed for plain
text-to-image, and it's gated by the same flag in both `__init__` and
`forward()`, so disabling it doesn't touch the T2I path. Doesn't touch the
published HF repo, only this local cache.

## Baseline result (Nano, 2026-09-16, node-gb10-2)

Native attention (`DIFFUSERS_ATTN_BACKEND` unset → `"native"`/SDPA), eager
execution, default 35 steps, tuned torch `2.15.0+gb10.cu133.tuning-v29`:

- Load time: 62.2s
- Generation time: 2020.5s (33.7 min) — steady ~50.1s/step across all 35
  steps (essentially no cold/warm step-time drift within the run)
- **Total: 2082.8s (~34.7 min)**

Script: `/tmp/.../scratchpad/baseline_nano.py` (session-scoped scratchpad,
not committed — rewrite if resuming in a new session). Compare
attention-backend and `torch.compile` variants against the 50.1s/step
figure, not total time (load time is a one-off, unaffected by either).

## Attention-backend result (Nano, 2026-09-17, node-gb10-2)

`DIFFUSERS_ATTN_BACKEND=flash` (rebuilt `flash_attn` wheel,
`v2.8.4+gb10.cu133.tuning.v21`) vs. the native/SDPA baseline above, same
model/prompt/steps:

| Metric | Native (SDPA) | Flash |
|---|---|---|
| Load time | 62.2s | 78.2s |
| Diffusion-loop avg (tqdm) | 50.16s/it | 49.13s/it |
| Generation time (full `pipe()` call) | 2020.5s | 1978.8s |
| Total | 2082.8s | 2057.0s |

**~2% faster with flash** (41.7s saved on generation) — real, but small,
consistent with the earlier LTX-2.5 finding that attention-backend choice
is close to noise for this hardware. Confirms that finding *transfers* to
Cosmos3-Nano, just not dramatically.

**Oddity, not yet explained**: per-step time drifted upward during the
flash run (steady ~48.6s/it through step ~20, climbing to 50.1s/it by step
35). The baseline run stayed flat throughout (~50.1-50.2s/it, no drift).
Single run each — could be thermal throttling under sustained load or
host noise, not conclusive. Worth a repeat run or `nvidia-smi -q -d
CLOCK,TEMPERATURE` sampling during a future run before drawing a
conclusion, if the drift recurs.

## torch.compile result (Nano, 2026-09-17, node-gb10-2) — the real win

`pipe.transformer = torch.compile(pipe.transformer, mode="reduce-overhead")`
(native/SDPA attention, not combined with flash yet), same model/prompt/
steps as above:

| Metric | Native (SDPA) | Flash | `torch.compile` |
|---|---|---|---|
| Load time | 62.2s | 78.2s | 61.8s |
| Steady-state step time | 50.16s/it | 49.13s/it | **~42.2s/it** |
| First step (one-time compile cost) | ~50s | ~49s | 114.8s |
| Generation time (full call) | 2020.5s | 1978.8s | **1811.8s** |
| Total | 2082.8s | 2057.0s | **1873.5s** |

**~16% faster steady-state per-step** (42.2s vs. 50.16s baseline) — a real
structural win, not noise like the attention-backend result. Clean
compile: zero graph-break/recompile log lines, and the per-step curve
converges smoothly and monotonically (114.8s → 72.1s → 58.4s → ... →
42.15s by step ~20) rather than spiking, which is what CUDA-graph
warmup/capture stabilizing looks like, not repeated recompilation. Even
counting the ~72s one-time compile overhead against this single 35-step
run, total generation time is still ~10.3% faster than baseline — and a
persistent process (the real ComfyUI server, not a one-shot script) pays
that compile cost once and keeps the ~16%/step win on every subsequent
generation.

## Combined result (Nano, 2026-09-17, node-gb10-2) — flash + torch.compile together

`DIFFUSERS_ATTN_BACKEND=flash` + `torch.compile(mode="reduce-overhead")`
together, same model/prompt/steps:

| Metric | Native (SDPA) | Flash | `torch.compile` | Flash + `torch.compile` |
|---|---|---|---|---|
| Load time | 62.2s | 78.2s | 61.8s | 61.9s |
| Steady-state step time | 50.16s/it | 49.13s/it | 42.2s/it | **41.2s/it** |
| Generation time (full call) | 2020.5s | 1978.8s | 1811.8s | **1761.0s** |
| Total | 2082.8s | 2057.0s | 1873.5s | **1822.9s** |

**Best result found: ~18% faster steady-state (41.2s vs. 50.16s baseline),
~12.8% faster full generation time.** The two stack cleanly — no negative
interaction, `torch.compile` does almost all the work, flash adds its
~2.4% on top (matching its standalone effect). This is the config carried
forward to Super below.

## Super result (2026-09-17, node-gb10-2) — confirms the finding transfers

`SanDiegoDude/Cosmos3-Super-Text2Image-nf4` (~100B-param class, downloaded
fresh to this host — 35GB/21 files), `num_inference_steps=15` (not the
default 35, to keep iteration cheap per the original plan). Both runs on
this host, same prompt, for a fair same-host comparison — **do not**
compare against the original node-gb10-1 session's anecdotal "1h40m+"
figure, which was a different host/session under different memory
pressure, not a controlled measurement:

| Metric | Native (SDPA), no compile | Flash + `torch.compile` |
|---|---|---|
| Load time | 280.3s | 212.6s |
| Steady-state step time | ~190.7-191.9s/it (flat, no drift) | ~164-165s/it |
| Generation time (15 steps) | 3124.8s | 2840.6s |
| Total | 3405.1s | 3053.2s |

**~13.7% faster steady-state, ~9.1% faster generation time, ~10.3% faster
total.** Smaller than Nano's ~18%/~12.8% (compile's one-time overhead
amortizes over fewer steps here — only 15 vs. Nano's 35 — and Super's
per-step structural cost is much higher to begin with), but a real,
solidly-positive result on the actual target model, not noise. **Finding
confirmed to transfer from Nano to Super.**

Not yet tried: a full 35-step run on Super with this config (to see the
steady-state number with full compile amortization, matching Nano's
methodology exactly); whether `num_inference_steps` itself can be reduced
further without visible quality loss (separate question from backend/
compile perf, out of scope for this investigation).

## Next steps

1. ~~Pull `SanDiegoDude/Cosmos3-Nano-nf4` as a cheap testbed~~ — done
   2026-09-16, 11.18GB / 17 files, cached under
   `~/.cache/huggingface/hub/models--SanDiegoDude--Cosmos3-Nano-nf4/`.
2. ~~Baseline: run Nano at defaults~~ — done, see above.
3. ~~Try `DIFFUSERS_ATTN_BACKEND=flash` env var~~ — done, see above (~2%
   faster, small but real win).
4. ~~Try `torch.compile(pipe.transformer, mode="reduce-overhead")`~~ — done,
   see above. Clean compile, no graph breaks, ~16% faster steady-state
   step time — the real win of the three variants tried so far.
5. ~~Combine both (flash attention + `torch.compile`)~~ — done, see above.
   Best result: ~18% faster steady-state, ~12.8% faster generation time.
6. ~~Re-test against Super with this combined config~~ — done, see above.
   ~13.7% faster steady-state, confirmed transfers from Nano.
7. ~~Wire this into ComfyUI itself (custom node/pipeline wrapper)~~ — done
   2026-09-17/18. `custom_nodes/scg-Cosmos3-gb10` (fork of
   `SanDiegoDude/scg-Cosmos3`, renamed from `scg-Cosmos3` to avoid a
   folder/name collision with a plain upstream checkout) applies this
   combined config as a `tuned/devices/gb10.conf` runtime profile
   (`cosmos3_wrapper/tuned.py`), auto-detected by GPU name and exposed as
   `attention_backend`/`torch_compile` loader-node inputs (default `auto`
   defers to the profile; explicit values override it). No-ops on
   non-GB10 hardware. See that repo's README for install/setup
   instructions for others.
8. Environment to replicate on the new checkout's venv: `bitsandbytes
   0.50.2`, `diffusers 0.41.0.dev0`, `transformers 5.17.0`, `accelerate
   1.15.0`, tuned `torch` (latest published GB10 release, e.g.
   `2.15.0+gb10.cu133.tuning-v29` — see
   https://github.com/zbrad/pytorch/releases), tuned `flash_attn` (latest
   published GB10 release — see
   https://github.com/ZBrad-LLC/flash-attention/releases; **must be rebuilt
   whenever torch bumps far enough to need it**: torch 2.15.0's ATen headers
   now require C++20, but the previous flash_attn release was compiled with
   `-std=c++17` and is ABI-broken against it — fixed 2026-09-16 by bumping
   `setup.py`'s hardcoded standard, rebuilding, and publishing
   `v2.8.4+gb10.cu133.tuning.v21`; upstream `Dao-AILab/flash-attention` main
   still has the c++17 hardcode as of this writing, so this isn't
   auto-fixed by syncing). Set `HF_HUB_OFFLINE=1` once models are cached.
   **Pre-install system package before any pip install** (a fresh host
   won't have it, and the tuned torch wheel's `.so` fails to import
   without it — hit on node-gb10-2 2026-09-16):
   `sudo apt install -y libopenblas0-pthread`.
9. Don't run this alongside another heavy GPU job on the same node —
   GB10's unified memory means two big resident models compete for the
   same pool (see the memory-pressure note above).
