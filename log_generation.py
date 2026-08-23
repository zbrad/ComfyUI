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

LOG_PATH = Path(__file__).parent / "generation-log.jsonl"
OUTPUT_DIR = Path(__file__).parent / "output"


def _probe_output(filename: str, subfolder: str) -> dict:
    """Ground-truth width/height/duration/fps straight from the rendered
    file via ffprobe -- more reliable than tracing the node graph's own
    math-expression chains for width/height/length (those can be many
    hops of ComfyMathExpression away from a literal, not worth resolving
    generically here)."""
    path = OUTPUT_DIR / subfolder / filename
    if not path.exists():
        return {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15,
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
            "output_duration_seconds": round(float(fmt["duration"]), 2) if fmt.get("duration") else None,
            "fps": fps,
        }
    except Exception:
        return {}


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def _find_node(prompt: dict, class_type: str) -> dict | None:
    for node_id, node in prompt.items():
        if node.get("class_type") == class_type:
            return node
    return None


def cmd_record(args):
    server = args.server.rstrip("/")
    hist = _get(f"{server}/history/{args.prompt_id}")
    entry = hist.get(args.prompt_id)
    if not entry:
        print(f"ERROR: prompt_id {args.prompt_id} not found in /history", file=sys.stderr)
        sys.exit(1)

    status = entry.get("status", {})
    messages = status.get("messages", [])
    start_ms = end_ms = None
    for kind, payload in messages:
        if kind == "execution_start":
            start_ms = payload.get("timestamp")
        elif kind in ("execution_success", "execution_error"):
            end_ms = payload.get("timestamp")
    if start_ms is None or end_ms is None:
        print("ERROR: couldn't find execution_start/execution_success timestamps", file=sys.stderr)
        sys.exit(1)
    elapsed_s = (end_ms - start_ms) / 1000.0

    prompt = entry.get("prompt", [None, None, {}])[2] if isinstance(entry.get("prompt"), list) else {}
    if not prompt:
        # /history's own 'prompt' field layout varies by ComfyUI version;
        # fall back to re-deriving nothing rather than guessing wrong.
        prompt = {}

    unet_node = _find_node(prompt, "UNETLoader")

    # Ground-truth output dimensions/duration from the actual rendered
    # file (via ffprobe), not traced through the node graph's own
    # math-expression chains -- find the first video/image output in
    # /history's 'outputs' section.
    probed = {}
    for _node_id, out in entry.get("outputs", {}).items():
        for key in ("images", "videos", "gifs"):
            for item in out.get(key, []) or []:
                probed = _probe_output(item.get("filename", ""), item.get("subfolder", ""))
                if probed:
                    break
            if probed:
                break
        if probed:
            break

    record = {
        "prompt_id": args.prompt_id,
        "recorded_at": end_ms,
        "model": unet_node["inputs"]["unet_name"] if unet_node else None,
        "status": status.get("status_str"),
        "elapsed_seconds": round(elapsed_s, 1),
        "width": probed.get("width"),
        "height": probed.get("height"),
        "output_duration_seconds": probed.get("output_duration_seconds"),
        "fps": probed.get("fps"),
        "note": args.note,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"Logged: {json.dumps(record)}")


def _load_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def cmd_list(args):
    rows = _load_log()
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
            f"({mp} MP)  output={r.get('output_duration_seconds', '?')}s @ {r.get('fps', '?')}fps  "
            f"[{r.get('status', '?')}]  {r.get('note') or ''}"
        )


def cmd_estimate(args):
    rows = [r for r in _load_log() if r.get("model") == args.model and r.get("status") == "success"]
    if not rows:
        print(f"No successful history for model '{args.model}' yet -- can't estimate. "
              f"Run one first with `log_generation.py record <prompt_id>`.")
        return

    # Use the entry with the closest output-seconds as the scaling
    # reference -- rough (LTX's cost isn't perfectly linear in
    # duration/resolution, e.g. distilled-model step count and two-stage
    # upscale sampling are fixed overhead, not purely per-frame), but far
    # better than no estimate.
    def ref_seconds(r):
        return r.get("output_duration_seconds") or 0

    rows = [r for r in rows if ref_seconds(r)]
    if not rows:
        print(f"History for '{args.model}' exists but has no usable output-duration data "
              f"(re-run `record` on those prompt_ids, or log a fresh one).")
        return

    best = min(rows, key=lambda r: abs(ref_seconds(r) - args.seconds))
    best_out_s = ref_seconds(best)
    best_mp = (best.get("width") or 1) * (best.get("height") or 1) / 1_000_000

    scale = args.seconds / best_out_s
    if args.width and args.height:
        target_mp = args.width * args.height / 1_000_000
        scale *= target_mp / best_mp if best_mp else 1.0

    est = best["elapsed_seconds"] * scale
    print(f"Estimate for {args.model}, {args.seconds}s output"
          f"{f', {args.width}x{args.height}' if args.width and args.height else ''}: "
          f"~{est:.0f}s (based on {len(rows)} prior run(s); closest reference was "
          f"{best['elapsed_seconds']}s for {best.get('width')}x{best.get('height')}, "
          f"{best_out_s}s output)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_record = sub.add_parser("record")
    p_record.add_argument("prompt_id")
    p_record.add_argument("--server", default="http://127.0.0.1:8188")
    p_record.add_argument("--note", default="")
    p_record.set_defaults(func=cmd_record)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_est = sub.add_parser("estimate")
    p_est.add_argument("--model", required=True)
    p_est.add_argument("--seconds", type=float, required=True, help="target output video duration in seconds")
    p_est.add_argument("--width", type=int)
    p_est.add_argument("--height", type=int)
    p_est.set_defaults(func=cmd_estimate)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
