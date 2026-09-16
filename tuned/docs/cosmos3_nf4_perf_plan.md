# Cosmos3-Super-Text2Image-nf4: slow-generation investigation + speedup plan

Started on node-gb10-1 2026-09-15, investigating why a standalone diffusers
script (not yet wired into ComfyUI itself) using
`SanDiegoDude/Cosmos3-Super-Text2Image-nf4` took 1h40m+ for a single
`pipe(prompt)` call. Picking this back up on a fresh node-gb10-2 checkout so it
doesn't contend for GPU/unified-memory with the still-running node-gb10-1 job.

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

## Next steps (resume here on node-gb10-2, fresh checkout)

1. Pull `SanDiegoDude/Cosmos3-Nano-nf4` as a cheap testbed —
   11.18GB total (8.8GB transformer + 1.4GB VAE + 0.9GB sound tokenizer +
   tokenizer files), vs. Super's 35GB. Confirmed via
   `HfApi().model_info(..., files_metadata=True)`, not downloaded yet as of
   this writing.
2. Baseline: run Nano at defaults (native attention, eager, 35 steps),
   time it.
3. Try `DIFFUSERS_ATTN_BACKEND=flash` env var (confirm the exact toggle —
   env var vs. `pipe.transformer.set_attention_backend("flash")` — against
   the registry API in `attention_dispatch.py`) vs. baseline. Isolate the
   attention-backend win on this specific model before touching Super.
4. Try `torch.compile(pipe.transformer, mode="reduce-overhead")` vs.
   baseline. Watch for graph breaks from bitsandbytes 4-bit ops or the
   dual-pathway (und/gen) MoE control flow — compiling a 64-layer MoE
   transformer may itself take many minutes and could net-lose if breaks
   are frequent. Check step-time *after* warmup, not including compile
   time, to judge the real win.
5. Combine both if independently positive.
6. Only after validating on Nano, re-test against Super — start with a
   reduced `num_inference_steps` (e.g. 15) rather than a full 35-step run,
   to keep iteration cheap.
7. Environment to replicate on the new checkout's venv: `bitsandbytes
   0.50.2`, `diffusers 0.41.0.dev0`, `transformers 5.17.0`, `accelerate
   1.15.0`, tuned `torch 2.14.0.dev...gb10.cu133`, tuned `flash_attn
   2.8.4+gb10.cu133`. Set `HF_HUB_OFFLINE=1` once models are cached.
8. Don't run this alongside another heavy GPU job on the same node —
   GB10's unified memory means two big resident models compete for the
   same pool (see the memory-pressure note above).
