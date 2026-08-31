#!/usr/bin/env python3
"""test_workflow_headless.py -- queue a ComfyUI workflow JSON file through a
real, running ComfyUI frontend and wait for it to finish, without a human
at a browser.

Drives an actual headless Chromium (via Playwright) that loads the real
page, so the workflow is converted to the API "prompt" format by ComfyUI's
own `app.graphToPrompt()` / `app.queuePrompt()` -- not a hand-rolled
reimplementation of that logic. A from-scratch reimplementation was tried
and abandoned: ComfyUI's frontend excludes some widgets from the prompt
(e.g. a `control_after_generate` companion widget next to a seed) based on
each widget's own `options.serialize` flag, which lives in per-node-class
frontend code, not in anything `/object_info` exposes -- there is no way
to get that right without running the real app.

Blueprints (see blueprints/*.json) deliberately ship without an output
node -- that's the convention, matching upstream Comfy-Org blueprints --
so a bare blueprint has nothing for ComfyUI to execute. This script
detects a dangling VIDEO/IMAGE output on the graph's sole top-level node
(the single-subgraph shape every blueprint has) and auto-attaches a
matching Save node before queueing. A workflow that already has its own
output node (e.g. anything under user/default/workflows/) is queued as-is.

Requires: `pip install playwright && playwright install chromium`
(no --with-deps needed if the host already has the usual browser shared
libs, which avoids needing sudo).

Usage:
    test_workflow_headless.py <workflow.json> [--url http://host:port]
        [--prompt TEXT] [--timeout SECONDS]

    <workflow.json>   Path to a ComfyUI workflow file (UI format, i.e. the
                      kind saved from the editor / found in blueprints/ and
                      user/default/workflows/ -- not the flattened API
                      "prompt" format).
    --url             ComfyUI base URL (default: http://127.0.0.1:8188).
    --prompt          If set, overwrites the first STRING widget found on
                      the graph's sole top-level node (the usual
                      "positive prompt" slot on a blueprint/template).
    --timeout         Seconds to wait for execution to finish (default 900).

Exit code 0 on successful completion, 1 on any failure (load error,
queueing rejected, execution error, or timeout). Prints progress to
stderr and the final `/history` outputs (as JSON) to stdout on success.

Also importable as a library -- prepare_workflow() + run() are what
comfyui_mcp_server.py wraps as an MCP tool, so multiple agent tools
(not just this CLI) can share one implementation. All progress output
goes to stderr specifically so this stays safe to import into an MCP
stdio server, whose stdout is reserved for the JSON-RPC protocol
stream -- a stray print() to stdout there would corrupt it.
"""

import argparse
import json
import re
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright


def _log(*args: object) -> None:
    print(*args, file=sys.stderr)

SUBGRAPH_TYPE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

SAVE_NODE_FOR_OUTPUT_TYPE = {
    "VIDEO": {
        "type": "SaveVideo",
        "widgets_values": ["video/headless_test", "auto", "auto"],
    },
    "IMAGE": {
        "type": "SaveImage",
        "widgets_values": ["headless_test"],
    },
}


def http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def attach_output_node_if_missing(workflow: dict) -> dict:
    """If any subgraph-instance node (the blueprint convention -- a node
    whose type is a subgraph UUID) has a dangling VIDEO/IMAGE output,
    attach a matching Save node so there is something for ComfyUI to
    actually execute. Left alone if there's no such dangling output --
    e.g. the workflow already has an output node, or (for a blueprint
    that also needs input nodes wired up, such as an image input) the
    caller already attached one.

    Deliberately keyed on node *type* (a subgraph instance), not "the
    workflow has exactly one node" -- a blueprint that needs input nodes
    wired to it (LoadImage for an image-to-video blueprint, say) is no
    longer a single-node graph, but its output can still be dangling.
    """
    nodes = workflow.get("nodes", [])
    subgraph_nodes = [n for n in nodes if SUBGRAPH_TYPE_RE.match(n.get("type", ""))]

    dangling = [
        (node, i, out)
        for node in subgraph_nodes
        for i, out in enumerate(node.get("outputs", []))
        if out.get("type") in SAVE_NODE_FOR_OUTPUT_TYPE and not out.get("links")
    ]
    if not dangling:
        return workflow

    node, slot_index, out = dangling[0]
    spec = SAVE_NODE_FOR_OUTPUT_TYPE[out["type"]]

    new_node_id = max((n["id"] for n in nodes), default=0) + 1
    new_link_id = max((l[0] for l in workflow.get("links", [])), default=0) + 1

    save_node = {
        "id": new_node_id,
        "type": spec["type"],
        "pos": [node.get("pos", [0, 0])[0] + 900, node.get("pos", [0, 0])[1]],
        "size": [400, 150],
        "flags": {},
        "order": 1,
        "mode": 0,
        "inputs": [{"name": out["type"].lower(), "type": out["type"], "link": new_link_id}],
        "outputs": [],
        "properties": {"cnr_id": "comfy-core", "Node name for S&R": spec["type"]},
        "widgets_values": list(spec["widgets_values"]),
    }
    node["outputs"][slot_index]["links"] = [new_link_id]
    workflow["nodes"].append(save_node)
    workflow.setdefault("links", []).append(
        [new_link_id, node["id"], slot_index, new_node_id, 0, out["type"]]
    )
    workflow["last_node_id"] = max(workflow.get("last_node_id", 0), new_node_id)
    workflow["last_link_id"] = max(workflow.get("last_link_id", 0), new_link_id)
    _log(f"[+] attached {spec['type']} to the dangling {out['type']} output")
    return workflow


def set_prompt_text(workflow: dict, text: str) -> None:
    """Overwrite the first STRING-typed widgets_values entry on the graph's
    sole subgraph-instance node -- the usual "positive prompt" slot on a
    blueprint/template. Matched by node type (a subgraph UUID), not merely
    "has widget values": attach_output_node_if_missing may already have
    added a Save node with its own widgets_values by the time this runs.
    """
    nodes = [
        n
        for n in workflow.get("nodes", [])
        if SUBGRAPH_TYPE_RE.match(n.get("type", "")) and n.get("widgets_values")
    ]
    if len(nodes) != 1:
        raise ValueError(
            "--prompt only works when the workflow has exactly one "
            "subgraph-instance node with widget values (the blueprint shape)"
        )
    node = nodes[0]
    for i, v in enumerate(node["widgets_values"]):
        if isinstance(v, str):
            node["widgets_values"][i] = text
            return
    raise ValueError("no string widget found to set --prompt into")


def prepare_workflow(workflow: dict, prompt: str | None = None) -> dict:
    """Apply the same prep the CLI does -- attach a Save node onto a bare
    blueprint's dangling output, optionally override the prompt text --
    shared by the CLI and the MCP tool so there's exactly one code path.
    """
    workflow = attach_output_node_if_missing(workflow)
    if prompt is not None:
        set_prompt_text(workflow, prompt)
    return workflow


def run(base_url: str, workflow: dict, timeout: float) -> dict:
    """Queue `workflow` against a running ComfyUI at `base_url` and wait
    up to `timeout` seconds for it to finish.

    Returns a structured result, always with a `success` bool and a
    `stage` naming where it stopped if not:
        {"success": False, "stage": "load_graph_data", "error": "..."}
        {"success": False, "stage": "queue_prompt", "error": "...", "console_errors": [...]}
        {"success": False, "stage": "queue_lookup", "error": "..."}
        {"success": False, "stage": "execution", "prompt_id": "...", "status": {...}}
        {"success": False, "stage": "timeout", "prompt_id": "..."}
        {"success": True, "prompt_id": "...", "outputs": {...}}
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        console_errors = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        # Capture the POST /prompt response directly rather than diffing
        # /queue before/after: a job whose inputs are fully cache-hit from
        # a previous run (identical prompt/graph, common when re-testing
        # the same blueprint) can complete and leave the queue in well
        # under a second -- faster than any polling loop reliably catches
        # a "new" arrival. The backend's own response is authoritative and
        # has no such race.
        prompt_response = {}

        def _on_response(resp):
            if resp.request.method == "POST" and resp.url.rstrip("/").endswith("/prompt"):
                try:
                    prompt_response["body"] = resp.json()
                except Exception as e:  # noqa: BLE001 - best effort capture
                    prompt_response["parse_error"] = str(e)

        page.on("response", _on_response)

        _log(f"[+] loading {base_url} ...")
        page.goto(base_url, wait_until="networkidle", timeout=60000)
        page.wait_for_function("window.app && window.app.graph", timeout=30000)
        _log("[+] app ready")

        _log("[+] loading workflow into the graph via app.loadGraphData() ...")
        load_result = page.evaluate(
            """async (wf) => {
                try {
                    await window.app.loadGraphData(wf, true, false, null, {});
                    return { ok: true };
                } catch (e) {
                    return { ok: false, error: String(e && e.stack || e) };
                }
            }""",
            workflow,
        )
        if not load_result["ok"]:
            _log("[!] loadGraphData failed:", load_result["error"])
            browser.close()
            return {"success": False, "stage": "load_graph_data", "error": load_result["error"]}
        _log("[+] workflow loaded")

        _log("[+] queueing via app.queuePrompt(0, 1) ...")
        queue_result = page.evaluate(
            """async () => {
                try {
                    const ok = await window.app.queuePrompt(0, 1);
                    return { ok: true, queued: ok };
                } catch (e) {
                    return { ok: false, error: String(e && e.stack || e) };
                }
            }"""
        )
        _log("[+] queuePrompt result:", queue_result)
        if not queue_result["ok"] or not queue_result.get("queued"):
            _log("[!] queueing failed or was rejected")
            for e in console_errors[-20:]:
                _log("    [console]", e)
            browser.close()
            return {
                "success": False,
                "stage": "queue_prompt",
                "error": queue_result.get("error", "queuePrompt returned false"),
                "console_errors": console_errors[-20:],
            }

        # The response listener is async relative to our page.evaluate call
        # above; queuePrompt() resolving doesn't guarantee the response
        # handler has run yet, so give it a brief moment.
        for _ in range(50):
            if "body" in prompt_response or "parse_error" in prompt_response:
                break
            time.sleep(0.1)

        browser.close()  # execution happens server-side; the page isn't needed anymore

    if "parse_error" in prompt_response:
        _log("[!] failed to parse POST /prompt response:", prompt_response["parse_error"])
        return {"success": False, "stage": "queue_lookup", "error": prompt_response["parse_error"]}
    body = prompt_response.get("body")
    if not body or "prompt_id" not in body:
        _log("[!] never captured a POST /prompt response with a prompt_id")
        return {"success": False, "stage": "queue_lookup", "error": "no prompt_id in POST /prompt response"}
    if body.get("node_errors"):
        _log("[!] server rejected the prompt:", body["node_errors"])
        return {"success": False, "stage": "queue_lookup", "error": "node_errors", "node_errors": body["node_errors"]}
    prompt_id = body["prompt_id"]
    _log(f"[+] prompt_id = {prompt_id}, polling /history ...")

    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        hist = http_get_json(f"{base_url}/history/{prompt_id}")
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status != last_status:
                _log("[+] status:", status)
                last_status = status
            if status.get("completed") is True:
                _log("[✓] execution completed successfully")
                outputs = entry.get("outputs", {})
                _log(json.dumps(outputs, indent=2)[:2000])
                return {"success": True, "prompt_id": prompt_id, "outputs": outputs}
            if status.get("status_str") == "error":
                _log("[✗] execution error:")
                _log(json.dumps(status, indent=2))
                return {"success": False, "stage": "execution", "prompt_id": prompt_id, "status": status}
        time.sleep(3)

    _log("[!] timed out waiting for completion")
    return {"success": False, "stage": "timeout", "prompt_id": prompt_id}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workflow", help="path to a ComfyUI workflow JSON file")
    ap.add_argument("--url", default="http://127.0.0.1:8188")
    ap.add_argument("--prompt", default=None, help="override the prompt widget text")
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()

    workflow = json.loads(open(args.workflow, encoding="utf-8").read())
    workflow = prepare_workflow(workflow, args.prompt)

    result = run(args.url, workflow, args.timeout)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
