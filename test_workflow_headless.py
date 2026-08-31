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
queueing rejected, execution error, or timeout). Prints the final
`/history` outputs on success.
"""

import argparse
import json
import re
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

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
    """If the workflow's sole top-level node has a dangling VIDEO/IMAGE
    output (the blueprint convention), attach a matching Save node so
    there is something for ComfyUI to actually execute. Left alone if the
    workflow already has an output node.
    """
    nodes = workflow.get("nodes", [])
    subgraph_nodes = [n for n in nodes if SUBGRAPH_TYPE_RE.match(n.get("type", ""))]
    if len(nodes) != 1 or len(subgraph_nodes) != 1:
        return workflow  # not the single-subgraph blueprint shape; assume complete

    node = subgraph_nodes[0]
    dangling = [
        (i, out)
        for i, out in enumerate(node.get("outputs", []))
        if out.get("type") in SAVE_NODE_FOR_OUTPUT_TYPE and not out.get("links")
    ]
    if not dangling:
        return workflow

    slot_index, out = dangling[0]
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
    print(f"[+] attached {spec['type']} to the dangling {out['type']} output")
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


def run(base_url: str, workflow: dict, timeout: float) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        console_errors = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        print(f"[+] loading {base_url} ...")
        page.goto(base_url, wait_until="networkidle", timeout=60000)
        page.wait_for_function("window.app && window.app.graph", timeout=30000)
        print("[+] app ready")

        before_queue = http_get_json(f"{base_url}/queue")
        before_ids = {
            item[1] for item in before_queue.get("queue_running", []) + before_queue.get("queue_pending", [])
        }

        print("[+] loading workflow into the graph via app.loadGraphData() ...")
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
            print("[!] loadGraphData failed:", load_result["error"])
            browser.close()
            return 1
        print("[+] workflow loaded")

        print("[+] queueing via app.queuePrompt(0, 1) ...")
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
        print("[+] queuePrompt result:", queue_result)
        if not queue_result["ok"] or not queue_result.get("queued"):
            print("[!] queueing failed or was rejected")
            for e in console_errors[-20:]:
                print("    [console]", e)
            browser.close()
            return 1

        prompt_id = None
        for _ in range(20):
            q = http_get_json(f"{base_url}/queue")
            items = q.get("queue_running", []) + q.get("queue_pending", [])
            new_ids = [item[1] for item in items if item[1] not in before_ids]
            if new_ids:
                prompt_id = new_ids[-1]
                break
            time.sleep(1)

        browser.close()  # execution happens server-side; the page isn't needed anymore

    if prompt_id is None:
        print("[!] never saw the job land in /queue")
        return 1
    print(f"[+] prompt_id = {prompt_id}, polling /history ...")

    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        hist = http_get_json(f"{base_url}/history/{prompt_id}")
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status != last_status:
                print("[+] status:", status)
                last_status = status
            if status.get("completed") is True:
                print("[✓] execution completed successfully")
                print(json.dumps(entry.get("outputs", {}), indent=2)[:2000])
                return 0
            if status.get("status_str") == "error":
                print("[✗] execution error:")
                print(json.dumps(status, indent=2))
                return 1
        time.sleep(3)

    print("[!] timed out waiting for completion")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workflow", help="path to a ComfyUI workflow JSON file")
    ap.add_argument("--url", default="http://127.0.0.1:8188")
    ap.add_argument("--prompt", default=None, help="override the prompt widget text")
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()

    workflow = json.loads(open(args.workflow, encoding="utf-8").read())
    workflow = attach_output_node_if_missing(workflow)
    if args.prompt is not None:
        set_prompt_text(workflow, args.prompt)

    return run(args.url, workflow, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
