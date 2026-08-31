#!/usr/bin/env python3
"""comfyui_mcp_server.py -- an MCP server wrapping test_workflow_headless.py,
so any MCP-capable agent tool (not just this one) can queue a ComfyUI
workflow through a real headless browser and get back a structured
pass/fail result, without knowing anything about Playwright or ComfyUI's
frontend internals.

This is the "shareable across agent tools" half of that script: the
underlying logic (attach_output_node_if_missing, prepare_workflow, run)
is unchanged and lives in test_workflow_headless.py -- this file only
adds the MCP transport around it, via the official `mcp` Python SDK's
FastMCP high-level API.

`run()` uses Playwright's *sync* API internally (see that module's
docstring for why -- it needs the genuine app.graphToPrompt()/
app.queuePrompt(), not a reimplementation), so the tool handler below
offloads it to a worker thread via `asyncio.to_thread` rather than
calling it directly from the server's own asyncio event loop.

Requires: `pip install mcp` (in addition to test_workflow_headless.py's
own playwright requirement).

Run directly for a local stdio server (the common case -- an MCP client
spawns this as a subprocess and speaks JSON-RPC over its stdin/stdout):
    python3 comfyui_mcp_server.py

Register with Claude Code (project-scoped, see .mcp.json in this repo)
or any other MCP client's config, e.g. for Hermes agent's
~/.hermes/config.yaml:
    mcp_servers:
      comfyui:
        command: python3
        args: ["/home/zbrad/gh/ComfyUI/comfyui_mcp_server.py"]

Set COMFYUI_URL in the environment (or pass `url` per call) to point at
a non-default ComfyUI instance; defaults to http://127.0.0.1:8188.
"""

import asyncio
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from test_workflow_headless import prepare_workflow, run

DEFAULT_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")

mcp = FastMCP(
    name="comfyui-workflow-runner",
    instructions=(
        "Queue a ComfyUI workflow JSON file (a blueprint under blueprints/, "
        "a template under user/default/workflows/, or any other saved "
        "workflow) through a real running ComfyUI instance and wait for "
        "the result. Use this to verify a workflow actually executes "
        "rather than just being valid JSON."
    ),
)


@mcp.tool()
async def queue_workflow(
    workflow_path: str,
    url: str = DEFAULT_URL,
    prompt: str | None = None,
    timeout: float = 900,
) -> dict:
    """Queue a ComfyUI workflow file and wait for it to finish executing.

    Loads the workflow into a real, running ComfyUI frontend (headless
    Chromium via Playwright) and queues it exactly as a person clicking
    Queue in the browser would -- so the result reflects genuine
    execution, not just JSON validity.

    Blueprints (see blueprints/*.json) ship without an output node by
    convention; if `workflow_path` has that single-subgraph shape with a
    dangling VIDEO/IMAGE output, a matching SaveVideo/SaveImage node is
    attached automatically before queueing.

    Args:
        workflow_path: Path to a ComfyUI workflow JSON file (UI format --
            the kind saved from the editor, not the flattened API
            "prompt" format).
        url: Base URL of a running ComfyUI instance. Defaults to
            $COMFYUI_URL or http://127.0.0.1:8188.
        prompt: If set, overwrites the first STRING widget on the
            workflow's positive-prompt slot before queueing.
        timeout: Seconds to wait for execution to finish (default 900).

    Returns:
        {"success": true, "prompt_id": "...", "outputs": {...}} on success.
        {"success": false, "stage": "...", "error"/"status": ...} on
        failure, where `stage` is one of load_graph_data, queue_prompt,
        queue_lookup, execution, or timeout -- naming where it stopped.
    """
    workflow = json.loads(Path(workflow_path).read_text(encoding="utf-8"))
    workflow = prepare_workflow(workflow, prompt)
    return await asyncio.to_thread(run, url, workflow, timeout)


if __name__ == "__main__":
    mcp.run(transport="stdio")
