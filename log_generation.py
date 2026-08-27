#!/usr/bin/env python3
"""log_generation.py — record actual wall-clock time for a completed
ComfyUI generation, keyed by cheap-to-compare params (model, resolution,
output duration, step count), so future runs can get a ballpark time
estimate instead of a blind guess.

Usage:
    log_generation.py record <prompt_id> [--server http://127.0.0.1:8188] [--note "..."]
        Pulls timing + a few params out of /history/<prompt_id> and appends
        one line to generation-log.jsonl. Safe to call right after a run
        completes (uses the real execution_start/execution_success
        timestamps ComfyUI already recorded, not wall-clock guessing).

    log_generation.py estimate --model <name> --seconds <N> [--width W] [--height H]
        Looks at prior log entries for the same model, scales by output
        duration (seconds of video) and resolution (megapixels) using the
        closest historical run as a reference point, and prints a rough
        estimate. With zero history for a model, says so plainly instead
        of fabricating a number.

    log_generation.py list
        Prints the log as a table.
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

LOG_PATH = Path(__file__).parent / "generation-log.jsonl"
OUTPUT_DIR = Path(__file__).parent / "output"


class GenerationLogger:
    """Records and estimates ComfyUI generation wall-clock times.

    Pulls real per-run timing from ComfyUI's own /history endpoint plus
    ground-truth output width/height/duration/fps via ffprobe on the
    actual rendered file -- not traced through the node graph's own
    math-expression chains, which would be fragile -- and appends one
    line per run to a local JSONL log so future runs can get a ballpark
    time estimate instead of a blind guess.
    """

    def __init__(
        self, log_path: Path = LOG_PATH, output_dir: Path = OUTPUT_DIR
    ) -> None:
        self._log_path = log_path
        self._output_dir = output_dir

    def record(
        self,
        prompt_id: str,
        server: str,
        note: str,
        resource_stats: Optional[dict[str, object]] = None,
    ) -> None:
        """Fetch a completed run's timing/params from /history and log it.

        Args:
            prompt_id: The ComfyUI prompt id to look up.
            server: Base URL of the running ComfyUI server.
            note: Free-text note to attach to the log entry.
            resource_stats: Optional GPU/RAM/page-fault stats gathered
                during the run (see resource_watcher.py), attached to the
                log entry under "resource" if given.
        """
        hist = self._get(f"{server.rstrip('/')}/history/{prompt_id}")
        entry = hist.get(prompt_id)
        if not isinstance(entry, dict):
            print(
                f"ERROR: prompt_id {prompt_id} not found in /history",
                file=sys.stderr,
            )
            sys.exit(1)

        status = entry.get("status", {})
        messages = status.get("messages", [])
        start_ms: Optional[int] = None
        end_ms: Optional[int] = None
        for kind, payload in messages:
            if kind == "execution_start":
                start_ms = payload.get("timestamp")
            elif kind in ("execution_success", "execution_error"):
                end_ms = payload.get("timestamp")
        if start_ms is None or end_ms is None:
            print(
                "ERROR: couldn't find execution_start/execution_success " "timestamps",
                file=sys.stderr,
            )
            sys.exit(1)
        elapsed_s = (end_ms - start_ms) / 1000.0

        # /history's own 'prompt' field layout varies by ComfyUI version
        # ([id, id, node_dict, ...]); fall back to an empty graph rather
        # than guessing wrong if it doesn't match.
        prompt: dict[str, object] = {}
        prompt_field = entry.get("prompt")
        if (
            isinstance(prompt_field, list)
            and len(prompt_field) > 2
            and isinstance(prompt_field[2], dict)
        ):
            prompt = prompt_field[2]

        unet_node = self._find_node(prompt, "UNETLoader")
        model = unet_node.get("inputs", {}).get("unet_name") if unet_node else None

        # Ground-truth output dimensions/duration from the actual rendered
        # file (via ffprobe), not traced through the node graph's own
        # math-expression chains -- find the first video/image output in
        # /history's 'outputs' section.
        probed: dict[str, object] = {}
        for out in entry.get("outputs", {}).values():
            for key in ("images", "videos", "gifs"):
                for item in out.get(key, []) or []:
                    probed = self._probe_output(
                        item.get("filename", ""), item.get("subfolder", "")
                    )
                    if probed:
                        break
                if probed:
                    break
            if probed:
                break

        record = {
            "prompt_id": prompt_id,
            "recorded_at": end_ms,
            "model": model,
            "status": status.get("status_str"),
            "elapsed_seconds": round(elapsed_s, 1),
            "width": probed.get("width"),
            "height": probed.get("height"),
            "output_duration_seconds": probed.get("output_duration_seconds"),
            "fps": probed.get("fps"),
            "note": note,
        }
        if resource_stats is not None:
            record["resource"] = resource_stats
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        print(f"Logged: {json.dumps(record)}")

    def list_entries(self) -> None:
        """Print every log entry as a table."""
        rows = self._load_log()
        if not rows:
            print("No generation log entries yet.")
            return
        for r in rows:
            mp = None
            if r.get("width") and r.get("height"):
                mp = round(r["width"] * r["height"] / 1_000_000, 2)
            print(
                f"{r.get('model', '?'):55s} "
                f"{r.get('elapsed_seconds', '?'):>7} s  "
                f"{r.get('width', '?')}x{r.get('height', '?')} "
                f"({mp} MP)  output={r.get('output_duration_seconds', '?')}s "
                f"@ {r.get('fps', '?')}fps  "
                f"[{r.get('status', '?')}]  {r.get('note') or ''}"
            )

    def estimate(
        self,
        model: str,
        seconds: float,
        width: Optional[int],
        height: Optional[int],
    ) -> None:
        """Print a ballpark wall-clock estimate scaled from prior runs.

        Args:
            model: Model name to match against prior log entries.
            seconds: Target output video duration in seconds.
            width: Optional target output width, for resolution scaling.
            height: Optional target output height, for resolution scaling.
        """
        rows = [
            r
            for r in self._load_log()
            if r.get("model") == model and r.get("status") == "success"
        ]
        if not rows:
            print(
                f"No successful history for model '{model}' yet -- can't "
                f"estimate. Run one first with "
                f"`log_generation.py record <prompt_id>`."
            )
            return

        # Use the entry with the closest output-seconds as the scaling
        # reference -- rough (LTX's cost isn't perfectly linear in
        # duration/resolution, e.g. distilled-model step count and
        # two-stage upscale sampling are fixed overhead, not purely
        # per-frame), but far better than no estimate.
        def ref_seconds(r: dict[str, object]) -> float:
            return r.get("output_duration_seconds") or 0

        rows = [r for r in rows if ref_seconds(r)]
        if not rows:
            print(
                f"History for '{model}' exists but has no usable "
                f"output-duration data (re-run `record` on those "
                f"prompt_ids, or log a fresh one)."
            )
            return

        best = min(rows, key=lambda r: abs(ref_seconds(r) - seconds))
        best_out_s = ref_seconds(best)
        best_mp = (best.get("width") or 1) * (best.get("height") or 1) / 1_000_000

        scale = seconds / best_out_s
        if width and height:
            target_mp = width * height / 1_000_000
            scale *= target_mp / best_mp if best_mp else 1.0

        est = best["elapsed_seconds"] * scale
        resolution_note = f", {width}x{height}" if width and height else ""
        print(
            f"Estimate for {model}, {seconds}s output{resolution_note}: "
            f"~{est:.0f}s (based on {len(rows)} prior run(s); closest "
            f"reference was {best['elapsed_seconds']}s for "
            f"{best.get('width')}x{best.get('height')}, {best_out_s}s output)"
        )

    def _probe_output(self, filename: str, subfolder: str) -> dict[str, object]:
        """Ground-truth width/height/duration/fps straight from the
        rendered file via ffprobe -- more reliable than tracing the node
        graph's own math-expression chains for width/height/length (those
        can be many hops of ComfyMathExpression away from a literal, not
        worth resolving generically here).
        """
        path = self._output_dir / subfolder / filename
        if not path.exists():
            return {}
        try:
            out = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,r_frame_rate",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            data = json.loads(out.stdout)
            stream = (data.get("streams") or [{}])[0]
            fmt = data.get("format", {})
            fps = None
            if "/" in str(stream.get("r_frame_rate", "")):
                num, den = stream["r_frame_rate"].split("/")
                if float(den):
                    fps = round(float(num) / float(den), 2)
            return {
                "width": stream.get("width"),
                "height": stream.get("height"),
                "output_duration_seconds": (
                    round(float(fmt["duration"]), 2) if fmt.get("duration") else None
                ),
                "fps": fps,
            }
        except (
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            ValueError,
        ):
            return {}

    def _load_log(self) -> list[dict[str, object]]:
        """Read every entry from the JSONL log, oldest first."""
        if not self._log_path.exists():
            return []
        with open(self._log_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def _get(url: str) -> dict[str, object]:
        """Fetch and JSON-decode a URL."""
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)

    @staticmethod
    def _find_node(
        prompt: dict[str, object], class_type: str
    ) -> Optional[dict[str, object]]:
        """Return the first node in a prompt graph matching `class_type`."""
        for node in prompt.values():
            if node.get("class_type") == class_type:
                return node
        return None

    @staticmethod
    def _build_arg_parser() -> argparse.ArgumentParser:
        """Build the `record` / `estimate` / `list` subcommand parser."""
        p = argparse.ArgumentParser(
            description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
        )
        sub = p.add_subparsers(dest="cmd", required=True)

        p_record = sub.add_parser("record")
        p_record.add_argument("prompt_id")
        p_record.add_argument("--server", default="http://127.0.0.1:8188")
        p_record.add_argument("--note", default="")

        sub.add_parser("list")

        p_est = sub.add_parser("estimate")
        p_est.add_argument("--model", required=True)
        p_est.add_argument(
            "--seconds",
            type=float,
            required=True,
            help="target output video duration in seconds",
        )
        p_est.add_argument("--width", type=int)
        p_est.add_argument("--height", type=int)

        return p

    @classmethod
    def main(cls, argv: Optional[list[str]] = None) -> None:
        """CLI entry point: parse argv and dispatch to the matching command."""
        args = cls._build_arg_parser().parse_args(argv)
        logger = cls()
        if args.cmd == "record":
            logger.record(args.prompt_id, args.server, args.note)
        elif args.cmd == "list":
            logger.list_entries()
        elif args.cmd == "estimate":
            logger.estimate(args.model, args.seconds, args.width, args.height)


if __name__ == "__main__":
    GenerationLogger.main()
