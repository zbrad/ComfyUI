"""First-run setup: swaps ComfyUI's frontend-hardcoded stock default graph
for this install's real LTX-2.5 workflow on a browser's genuine first visit.

Contributes no nodes -- purely a WEB_DIRECTORY-registered frontend
extension. See web/first_run_setup.js for the actual logic.
"""

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
