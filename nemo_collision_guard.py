"""nemo_collision_guard.py — warn (never block) before ComfyUI executes a
queued prompt while a nemo-*.service unit is resident on this box.

GB10's unified memory is shared between ComfyUI and llama.cpp's nemo.sh
--user services (nemo-<alias>.service) -- there is no separate VRAM pool
to protect either side. nemo.sh's own check_mem gates ITS OWN start
against a resident ComfyUI job (via /proc/meminfo MemAvailable), but
nothing previously covered the mirror-image case: ComfyUI starting a big
generation while a large nemo model is already loaded. That's the other
half of the 2026-09-06 incident's actual collision shape (see
~/.claude/projects/-home-zbrad/memory/nemo_watchdog_and_memorymax_plan.md,
"Also flagged, out of scope for this plan").

Deliberately NOT a fixed-MemAvailable-floor gate: generation-log.jsonl
shows RAM staying elevated (79-117GiB) across consecutive real jobs, even
across different models -- consistent with ComfyUI's own deliberate
cache_ram_inactive behavior (main.py, prompt_worker) keeping models
resident between runs. A memory-floor check would false-positive against
ComfyUI's own normal operation constantly, not just against an actual
external collision. Checking for a resident nemo-*.service directly is
unambiguous instead: it's the specific known collision partner, and
irrelevant to ComfyUI's own memory footprint.

Warn-only: a queued prompt may represent minutes of a user's prior work,
so this never discards or delays it -- only logs, so the operator has a
clear signal in the journal if a generation ever wedges again while a
nemo unit turns out to have been resident.
"""

import logging
import subprocess


def warn_if_nemo_resident() -> None:
    """Log a warning if any nemo-*.service unit is currently running."""
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=running",
                "--no-legend",
                "--plain",
                "nemo-*.service",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return  # systemctl unavailable or slow -- don't block a generation over this
    units = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    if units:
        logging.warning(
            "nemo_collision_guard: starting a ComfyUI generation while %s "
            "resident -- GB10's unified memory is shared between them, and "
            "this combination wedged the driver once before (2026-09-06 "
            "incident). Not blocking this job, just flagging it in case it "
            "happens again.",
            ", ".join(units),
        )
