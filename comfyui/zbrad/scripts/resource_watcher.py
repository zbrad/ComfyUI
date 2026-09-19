#!/usr/bin/env python3
"""resource_watcher.py — continuously watch a running ComfyUI server and
append resource-consumption stats to generation-log.jsonl for every
generation that completes, automatically (no manual `log_generation.py
record` call needed).

Per generation window (detected by polling /queue for prompt_ids entering
and leaving queue_running), this samples:
  - GPU utilization, GPU temperature, VRAM used (via crystools.monitor
    websocket broadcasts -- already the real numbers on this machine, see
    the ComfyUI-Crystools GB10 VRAM fix; crystools broadcasts every ~1s
    to all connected clients, not just the one that queued the prompt)
  - System RAM used, CPU utilization (same crystools.monitor broadcasts)
  - GPU power draw (nvidia-smi; crystools doesn't report this)
  - Minor/major page faults for the ComfyUI process (/proc/<pid>/stat) --
    this is the metric that turned out to be the actual driver of
    cold-load slowness on GB10 (unmaterialized mmap'd checkpoint traversal,
    see ltx25_dev_vs_distilled_workflows.md), so it's tracked per-run here
    rather than just once during an ad hoc investigation.

Usage:
    resource_watcher.py [--server http://127.0.0.1:8188] [--interval 1.0]
        Runs until interrupted (Ctrl-C or SIGTERM). Safe to run as a
        long-lived companion process/systemd service alongside ComfyUI --
        each generation that completes while it's running gets one
        resource-enriched line appended to generation-log.jsonl.
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

import websocket

from log_generation import GenerationLogger

PROC_STAT_MINFLT_INDEX = 7  # index into the post-comm field list, see _read_page_faults
PROC_STAT_MAJFLT_INDEX = 9


class _Window:
    """Accumulates resource samples for one in-flight generation."""

    def __init__(self, prompt_id: str, start_minflt: int, start_majflt: int) -> None:
        self.prompt_id = prompt_id
        self.start_minflt = start_minflt
        self.start_majflt = start_majflt
        self.gpu_utilization: list[float] = []
        self.gpu_temperature: list[float] = []
        self.vram_used_bytes: list[int] = []
        self.vram_used_percent: list[float] = []
        self.ram_used_bytes: list[int] = []
        self.ram_used_percent: list[float] = []
        self.cpu_utilization: list[float] = []
        self.power_draw_watts: list[float] = []


class ResourceWatcher:
    """Watches a running ComfyUI server and logs per-generation resource
    consumption alongside the timing data log_generation.py already
    tracks.
    """

    def __init__(self, server: str, interval: float) -> None:
        self._server = server.rstrip("/")
        self._ws_url = self._server.replace("http://", "ws://").replace(
            "https://", "wss://"
        ) + "/ws?clientId=resource-watcher"
        self._port = self._server.rsplit(":", 1)[-1]
        self._interval = interval
        self._logger = GenerationLogger()
        self._stop = threading.Event()
        self._latest_lock = threading.Lock()
        self._latest_monitor: Optional[dict[str, object]] = None
        self._windows: dict[str, _Window] = {}

    def run(self) -> None:
        """Run the poll loop until stop() is called."""
        ws_thread = threading.Thread(target=self._websocket_loop, daemon=True)
        ws_thread.start()
        print(f"[resource_watcher] watching {self._server} every {self._interval}s", flush=True)

        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 -- keep the watcher alive across any single bad poll
                print(f"[resource_watcher] tick error: {e}", file=sys.stderr, flush=True)
            self._stop.wait(self._interval)

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self._stop.set()

    def _tick(self) -> None:
        """One poll cycle: reconcile queue_running against open windows,
        and sample every currently-open window.
        """
        running_ids = set(self._get_queue_running_ids())

        for prompt_id in running_ids - self._windows.keys():
            self._open_window(prompt_id)

        for prompt_id in list(self._windows.keys() - running_ids):
            self._close_window(prompt_id)

        if self._windows:
            self._sample_open_windows()

    def _open_window(self, prompt_id: str) -> None:
        pid = self._resolve_comfy_pid()
        minflt, majflt = self._read_page_faults(pid) if pid else (0, 0)
        self._windows[prompt_id] = _Window(prompt_id, minflt, majflt)
        print(f"[resource_watcher] window open: {prompt_id} (comfy pid {pid})", flush=True)

    def _sample_open_windows(self) -> None:
        with self._latest_lock:
            monitor = self._latest_monitor

        power = self._read_power_draw()

        for window in self._windows.values():
            if monitor:
                gpus = monitor.get("gpus") or []
                gpu = gpus[0] if gpus else {}
                if isinstance(gpu.get("gpu_utilization"), (int, float)) and gpu["gpu_utilization"] >= 0:
                    window.gpu_utilization.append(gpu["gpu_utilization"])
                if isinstance(gpu.get("gpu_temperature"), (int, float)) and gpu["gpu_temperature"] >= 0:
                    window.gpu_temperature.append(gpu["gpu_temperature"])
                if isinstance(gpu.get("vram_used"), (int, float)) and gpu["vram_used"] >= 0:
                    window.vram_used_bytes.append(gpu["vram_used"])
                if isinstance(gpu.get("vram_used_percent"), (int, float)) and gpu["vram_used_percent"] >= 0:
                    window.vram_used_percent.append(gpu["vram_used_percent"])
                if isinstance(monitor.get("ram_used"), (int, float)):
                    window.ram_used_bytes.append(monitor["ram_used"])
                if isinstance(monitor.get("ram_used_percent"), (int, float)):
                    window.ram_used_percent.append(monitor["ram_used_percent"])
                if isinstance(monitor.get("cpu_utilization"), (int, float)):
                    window.cpu_utilization.append(monitor["cpu_utilization"])
            if power is not None:
                window.power_draw_watts.append(power)

    def _close_window(self, prompt_id: str) -> None:
        window = self._windows.pop(prompt_id)
        pid = self._resolve_comfy_pid()
        end_minflt, end_majflt = self._read_page_faults(pid) if pid else (
            window.start_minflt,
            window.start_majflt,
        )

        resource_stats = {
            "sample_count": len(window.gpu_utilization) or len(window.power_draw_watts),
            "gpu_utilization_avg": self._avg(window.gpu_utilization),
            "gpu_utilization_max": self._max(window.gpu_utilization),
            "gpu_temperature_max": self._max(window.gpu_temperature),
            "vram_used_max_bytes": self._max(window.vram_used_bytes),
            "vram_used_max_percent": self._max(window.vram_used_percent),
            "ram_used_max_bytes": self._max(window.ram_used_bytes),
            "ram_used_max_percent": self._max(window.ram_used_percent),
            "cpu_utilization_avg": self._avg(window.cpu_utilization),
            "power_draw_avg_watts": self._avg(window.power_draw_watts),
            "minor_page_faults": max(0, end_minflt - window.start_minflt),
            "major_page_faults": max(0, end_majflt - window.start_majflt),
        }

        print(f"[resource_watcher] window closed: {prompt_id}, recording...", flush=True)
        try:
            self._logger.record(
                prompt_id, self._server, note="", resource_stats=resource_stats
            )
        except SystemExit:
            # record() calls sys.exit() on lookup failure (e.g. prompt_id
            # not in /history yet, or execution_error with no timestamps) --
            # don't let that kill the whole watcher.
            print(f"[resource_watcher] could not record {prompt_id}", file=sys.stderr, flush=True)

    def _get_queue_running_ids(self) -> list[str]:
        data = self._get_json(f"{self._server}/queue")
        return [item[1] for item in data.get("queue_running", [])]

    def _websocket_loop(self) -> None:
        """Background thread: keep a websocket connection open and cache
        the most recent crystools.monitor broadcast. Reconnects on drop.
        """
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(self._ws_url, timeout=10)
                while not self._stop.is_set():
                    raw = ws.recv()
                    if isinstance(raw, bytes):
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "crystools.monitor":
                        with self._latest_lock:
                            self._latest_monitor = msg.get("data")
            except Exception as e:  # noqa: BLE001 -- reconnect on any socket-level failure
                print(f"[resource_watcher] websocket error: {e}, reconnecting...", file=sys.stderr, flush=True)
                self._stop.wait(2.0)

    def _resolve_comfy_pid(self) -> Optional[int]:
        """Find the real PID bound to the server's port via `ss`, not
        pgrep -- a pgrep pattern match can hit this watcher's own wrapper
        shell instead of the actual server process.
        """
        try:
            out = subprocess.run(
                ["ss", "-ltnp"], capture_output=True, text=True, timeout=5
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            if f":{self._port} " in line or line.rstrip().endswith(f":{self._port}"):
                m = re.search(r"pid=(\d+)", line)
                if m:
                    return int(m.group(1))
        return None

    @staticmethod
    def _read_page_faults(pid: int) -> tuple[int, int]:
        """Return (minflt, majflt) for a PID from /proc/<pid>/stat, or
        (0, 0) if it can't be read (process gone, permission, etc.).
        """
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return 0, 0
        # comm (field 2) can itself contain spaces/parens, so split after
        # the last ')' rather than by whitespace position from the start.
        fields = raw.rsplit(")", 1)[-1].split()
        try:
            return (
                int(fields[PROC_STAT_MINFLT_INDEX]),
                int(fields[PROC_STAT_MAJFLT_INDEX]),
            )
        except (IndexError, ValueError):
            return 0, 0

    @staticmethod
    def _read_power_draw() -> Optional[float]:
        """One-shot GPU power draw via nvidia-smi -- crystools doesn't
        report this, and it needs no CUDA context of its own to read.
        """
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            return float(out.splitlines()[0])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    @staticmethod
    def _get_json(url: str) -> dict[str, object]:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)

    @staticmethod
    def _avg(values: list[float]) -> Optional[float]:
        return round(sum(values) / len(values), 2) if values else None

    @staticmethod
    def _max(values: list[float]) -> Optional[float]:
        return round(max(values), 2) if values else None

    @staticmethod
    def _build_arg_parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser(
            description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
        )
        p.add_argument("--server", default="http://127.0.0.1:8188")
        p.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help="poll/sample interval in seconds",
        )
        return p

    @classmethod
    def main(cls, argv: Optional[list[str]] = None) -> None:
        args = cls._build_arg_parser().parse_args(argv)
        watcher = cls(args.server, args.interval)
        try:
            watcher.run()
        except KeyboardInterrupt:
            watcher.stop()


if __name__ == "__main__":
    ResourceWatcher.main()
