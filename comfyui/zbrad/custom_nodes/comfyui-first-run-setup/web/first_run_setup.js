import { app } from "/scripts/app.js";

// Swaps ComfyUI's frontend-hardcoded stock default graph (a generic
// Flux/SD3-shaped starter template, unrelated to this install's actual
// LTX-2.5 setup) for the real LTX-2.5 Text-to-Video template, but ONLY on
// a genuinely untouched canvas -- never overwrites real user work.
//
// Detection: exact node-type-multiset match against the frontend's own
// live `defaultGraph` object (read from `window.comfyAPI.defaultGraph`,
// the same source the frontend itself uses -- not a hardcoded snapshot,
// so this stays correct across frontend version bumps that change the
// stock template). An empty canvas, a restored real workflow, or our own
// template already loaded will never match this exactly.
//
// Fires from the `afterConfigureGraph` extension hook, which runs after
// every graph load -- the initial default-graph load, a restored saved
// workflow, and this extension's own swap-in call included. A one-shot
// guard prevents re-firing on that last case (and on any later graph
// load in the same page session).

const EXTENSION_NAME = "zbrad.FirstRunSetup";
const WORKFLOW_URL = new URL("./default_workflow.json", import.meta.url).href;
const WORKFLOW_DISPLAY_NAME = "LTX-2.5 Text to Video (Distilled fp8)";

function nodeTypeSignature(graphJson) {
  const nodes = (graphJson && graphJson.nodes) || [];
  if (nodes.length === 0) return "";
  return nodes
    .map((n) => n.type)
    .sort()
    .join("|");
}

let handled = false;

app.registerExtension({
  name: EXTENSION_NAME,

  async afterConfigureGraph() {
    if (handled) return;
    handled = true;

    try {
      const graph = app.graph;
      if (!graph) return;

      const stockDefault = window.comfyAPI?.defaultGraph?.defaultGraph;
      const stockSignature = nodeTypeSignature(stockDefault);
      if (!stockSignature) {
        console.warn(
          `[${EXTENSION_NAME}] could not read the frontend's stock default graph, skipping`,
        );
        return;
      }

      const currentSignature = nodeTypeSignature(graph.serialize());
      if (currentSignature !== stockSignature) {
        // Real user work, an already-loaded workflow, or an empty canvas
        // -- never touch any of those.
        return;
      }

      const resp = await fetch(WORKFLOW_URL);
      if (!resp.ok) {
        console.warn(
          `[${EXTENSION_NAME}] fetch failed: ${resp.status} ${resp.statusText}`,
        );
        return;
      }
      const workflow = await resp.json();
      await app.loadGraphData(workflow, true, true, WORKFLOW_DISPLAY_NAME);

      app.extensionManager?.toast?.add({
        severity: "info",
        summary: "LTX-2.5 workflow loaded",
        detail:
          "This ComfyUI install is set up for LTX-2.5, so the default " +
          "starter graph was swapped for the real Text-to-Video template.",
        life: 8000,
      });
      console.log(
        `[${EXTENSION_NAME}] swapped stock default graph for ${WORKFLOW_DISPLAY_NAME}`,
      );
    } catch (e) {
      // Never let a bug here block a user from getting *some* canvas.
      console.warn(`[${EXTENSION_NAME}] skipped (non-fatal):`, e);
    }
  },
});
