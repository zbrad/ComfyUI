"""model_collision_guard.py — warn (never block) before ComfyUI executes a
queued prompt while another model-serving systemd --user unit is resident
on this box.

GB10's unified memory is shared between ComfyUI and any other model
server sharing the box -- there is no separate VRAM pool to protect
either side. llama.cpp's nemo.sh (nemo-<alias>.service) is the only such
service today, and its own check_mem gates ITS OWN start against a
resident ComfyUI job (via /proc/meminfo MemAvailable), but nothing
previously covered the mirror-image case: ComfyUI starting a big
generation while another model is already loaded. That's the other half
of the 2026-09-06 incident's actual collision shape (see
~/.claude/projects/-home-zbrad/memory/nemo_watchdog_and_memorymax_plan.md,
"Also flagged, out of scope for this plan").

Deliberately NOT a fixed-MemAvailable-floor gate: generation-log.jsonl
shows RAM staying elevated (79-117GiB) across consecutive real jobs, even
across different models -- consistent with ComfyUI's own deliberate
cache_ram_inactive behavior (main.py, prompt_worker) keeping models
resident between runs. A memory-floor check would false-positive against
ComfyUI's own normal operation constantly, not just against an actual
external collision. Checking directly for a resident unit matching a
known collision-partner's service-name pattern is unambiguous instead --
it's irrelevant to ComfyUI's own memory footprint, so it can't misfire
against normal use.

Named generically (not nemo_collision_guard) since nemo.sh isn't
necessarily the only such service this box will ever run -- new
patterns can be added to _COLLISION_PATTERNS below without renaming this
module again.

Warn-only: a queued prompt may represent minutes of a user's prior work,
so this never discards or delays it -- only logs, so the operator has a
clear signal in the journal if a generation ever wedges again while
another model turns out to have been resident.
"""

import logging
import subprocess

# systemd --user unit glob patterns for other model-serving services that
# share this box's unified memory with ComfyUI. Add a pattern here (not a
# new module) when a new such service shows up.
_COLLISION_PATTERNS = ("nemo-*.service",)


def warn_if_other_model_resident() -> None:
    """Log a warning if any unit matching _COLLISION_PATTERNS is running."""
    units = []
    for pattern in _COLLISION_PATTERNS:
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
                    pattern,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue  # systemctl unavailable or slow -- don't block a generation over this
        units.extend(
            line.split()[0] for line in result.stdout.splitlines() if line.strip()
        )
    if units:
        logging.warning(
            "model_collision_guard: starting a ComfyUI generation while %s "
            "resident -- GB10's unified memory is shared between them, and "
            "this combination wedged the driver once before (2026-09-06 "
            "incident). Not blocking this job, just flagging it in case it "
            "happens again.",
            ", ".join(units),
        )
