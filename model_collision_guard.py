"""model_collision_guard.py — warn (never block) before ComfyUI executes a
queued prompt while another GPU-resident process is holding a CUDA
context on this box.

GB10's unified memory is shared between ComfyUI and any other model
server sharing the box -- there is no separate VRAM pool to protect
either side. llama.cpp's llmsrv.sh (llmsrv-<alias>.service, formerly
nemo.sh/nemo-<alias>.service until a 2026-09-08 rename) is the process
that actually caused the 2026-09-06 incident, and its own check_mem
gates ITS OWN start against a resident ComfyUI job (via /proc/meminfo
MemAvailable), but nothing previously covered the mirror-image case:
ComfyUI starting a big generation while another model is already loaded
-- the other half of that incident's actual collision shape. See
llama.cpp's docs/gb10/llmsrv-launcher.md ("Memory safety on unified
memory" section) for the full incident writeup and why MemAvailable,
not cgroup accounting, is the signal that actually works on this
platform.

Deliberately NOT a fixed-MemAvailable-floor gate: generation-log.jsonl
shows RAM staying elevated (79-117GiB) across consecutive real jobs, even
across different models -- consistent with ComfyUI's own deliberate
cache_ram_inactive behavior (main.py, prompt_worker) keeping models
resident between runs. A memory-floor check would false-positive against
ComfyUI's own normal operation constantly, not just against an actual
external collision.

Deliberately NOT a systemd-unit-name pattern list either (the first
version of this module was): that only catches launchers with a known,
enumerated unit name -- llmsrv.sh's llmsrv-<alias>.service, or Ollama's
system-wide ollama.service. It misses a bare `llama-server` invoked
directly (no llmsrv.sh wrapper) or a vLLM instance entirely, since
neither has an established systemd-unit convention on this fleet.
Checking `nvidia-smi --query-compute-apps` instead is launcher-agnostic
-- it lists every process actually holding a CUDA context, regardless of
how it was started, confirmed live (2026-09-08) to correctly show
llmsrv.sh's llama-server as a distinct PID from ComfyUI's own. It's also
simpler: no pattern list to keep growing as new launchers show up.

Warn-only: a queued prompt may represent minutes of a user's prior work,
so this never discards or delays it -- only logs, so the operator has a
clear signal in the journal if a generation ever wedges again while
another GPU process turns out to have been resident.
"""

import logging
import os
import subprocess


def warn_if_other_model_resident() -> None:
    """Log a warning if any GPU compute process besides this one is running."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return  # nvidia-smi unavailable or slow -- don't block a generation over this
    if result.returncode != 0:
        return

    own_pid = os.getpid()
    others = []
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        pid_str, process_name, used_mib = parts[0], parts[1], parts[2]
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        others.append(f"{process_name} (pid {pid}, {used_mib}MiB)")

    if others:
        logging.warning(
            "model_collision_guard: starting a ComfyUI generation while %s "
            "also GPU-resident -- GB10's unified memory is shared between "
            "them, and this kind of combination wedged the driver once "
            "before (2026-09-06 incident). Not blocking this job, just "
            "flagging it in case it happens again.",
            ", ".join(others),
        )
