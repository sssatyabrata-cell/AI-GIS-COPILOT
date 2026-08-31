"""
Agent reasoning loop for AI GIS Copilot.

This replaces "guess the intent from keywords" with a real tool-calling
loop: the LLM sees the user's request plus a list of real tools (QGIS
Processing algorithms, layer inspection/edits, web search), decides which
to call, the plugin executes them against the live QGIS session, the LLM
sees the results, and repeats until it has a final answer -- the same
pattern used by general-purpose AI assistants with tool use.

Destructive tools (anything that edits/deletes/writes data) are never
auto-executed: the loop pauses and asks the user to confirm, exactly like
the plugin's existing safety pattern for pending actions.
"""

import json

from .agent_tools import TOOL_SCHEMAS

MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 4000

SYSTEM_PROMPT = (
    "You are AI GIS Copilot, an expert QGIS assistant with real tools to inspect and modify "
    "the user's actual open QGIS project. Prefer using tools over guessing.\n"
    "- Always call list_layers or get_layer_info first if you are not certain what layers exist "
    "or what their fields/CRS are -- never assume a layer name.\n"
    "- Always call search_algorithms before run_processing_algorithm unless you already know the "
    "exact algorithm id and its parameter names with certainty.\n"
    "- run_processing_algorithm validates distance-based operations (like buffer) against the "
    "input layer's CRS before running. If it returns a validation_error about a geographic CRS, "
    "call suggest_projected_crs on that layer to get the exact correct UTM EPSG code, reproject "
    "with 'native:reprojectlayer' using that code, then retry the original operation on the "
    "reprojected output -- do not guess a UTM zone yourself.\n"
    "- For satellite imagery, use search_satellite_imagery (free Sentinel-2/Landsat via STAC). For "
    "real-world administrative boundaries (a city, district, country), use fetch_boundary_from_osm "
    "to add the actual boundary as a layer rather than guessing coordinates. For vegetation/NDVI "
    "questions on a multi-band raster, use compute_ndvi rather than trying to write a raw formula.\n"
    "- For questions about spatial clustering/hotspots of a numeric field (e.g. 'are high crime "
    "values clustered?'), use spatial_autocorrelation for a single summary statistic or "
    "local_spatial_autocorrelation to map per-feature clusters -- if these return an install_hint, "
    "tell the user to run /install pysal rather than trying to compute it yourself.\n"
    "- For styling requests ('color by category', 'shade by population'), use apply_symbology "
    "rather than trying to build a renderer through run_processing_algorithm. For producing a map "
    "output/report/PDF, use create_map_layout.\n"
    "- For questions that require actually SEEING the map (visual patterns, alignment, land cover "
    "appearance) rather than just querying layer metadata, use capture_and_analyze_viewport. If it "
    "returns an error about a missing Gemini key, tell the user plainly rather than guessing an "
    "answer from metadata instead.\n"
    "- For gis-mcp's extended tool library, use search_gis_mcp_tools to find the right tool name "
    "before calling call_gis_mcp_tool -- never guess a tool name, and tell the user to run /bind "
    "gis-mcp if these return an error saying it isn't bound.\n"
    "- run_custom_pyqgis_script is a last resort for requests no existing tool covers -- always "
    "check whether an existing tool already does what's needed first. Every use requires the "
    "user's explicit review and confirmation of the exact code; if it fails, use the real traceback "
    "in the result to write corrected code rather than guessing at the fix.\n"
    "- Use describe_algorithm_parameters when unsure of exact parameter names before "
    "run_processing_algorithm. Use profile_layer_fields for data-quality/'what's in this layer' "
    "questions beyond field names. Use query_features_by_spatial_predicate for 'which features "
    "intersect/are within X' requests. auto_fix_geometry_issues repairs what "
    "scan_project_for_issues flags as invalid. undo_last_layer_edit can only cleanly undo the most "
    "recent add_field on a layer -- say so plainly if asked to undo a delete_field, since the data "
    "is genuinely gone, not just hidden.\n"
    "- For utility-line span QA workflows: compare_line_geometry_changes (raw vs corrected batch "
    "comparison), classify_span_vegremarks (short/long + connectivity confidence classification), "
    "validate_span_categories (gis_vcat/hcat/clrnce rule check, read-only), correct_span_geometry "
    "(collapse wiggly lines to clean terminal-vertex-only spans), and "
    "snap_and_resolve_span_attributes (overlap-scoped snapping between a current and delivered "
    "layer). Ask which fields/layers to use if the project has more than one plausible candidate.\n"
    "- For factual questions you are confident about, answer directly. For anything time-sensitive, "
    "current, or outside your knowledge, call web_search or wikipedia_lookup rather than guessing.\n"
    "- Before asking the user for a parameter they may have specified before (a commonly-used "
    "field name, CRS, or distance), call recall_similar_past_action to check this team's local "
    "history on this machine -- this is how you get more efficient for their recurring workflows "
    "over time, even though your own underlying model weights never change. If shared_memory_path "
    "is configured, matches may also come from the wider team's activity, not just this machine.\n"
    "- If the user asks anything about install/setup progress (e.g. 'when will it be installed', "
    "'is it done yet', 'why isn't it ready'), ALWAYS call check_background_task_status first and "
    "answer from its real result -- never guess or give generic installation instructions when the "
    "actual running state is directly knowable.\n"
    "- After running tools, give a short, plain-English summary of what happened -- not a data dump.\n"
    "- If a request is ambiguous (e.g. which layer, which field), ask a brief clarifying question "
    "instead of guessing when the project has more than one reasonable candidate."
)


class AgentCore:
    def __init__(self, provider, tool_executor, log_fn):
        self.provider = provider
        self.tool_executor = tool_executor
        self.log = log_fn  # callable(str) -> writes a line to the chat log
        self._state = None  # holds in-flight conversation across a confirmation pause
        # Mutable so /bind gis-mcp can extend this list at runtime with
        # dynamically-discovered tools, and /unbind can revert it -- the
        # base TOOL_SCHEMAS import is only ever the starting point.
        self.tool_schemas = list(TOOL_SCHEMAS)

    def run(self, user_query):
        self._state = {"messages": [{"role": "user", "content": user_query}], "step": 0}
        return self._advance()

    def resume_after_confirmation(self, tool_call, approved):
        if self._state is None:
            return "No pending agent action to resume."
        if approved:
            self.log(f"<i>Running {tool_call['name']}...</i>")
            result = self.tool_executor.dispatch(tool_call["name"], tool_call["arguments"])
        else:
            result = {"ok": False, "error": "User declined this action."}
        self._state["messages"].append(
            {"role": "tool", "name": tool_call["name"], "id": tool_call.get("id", tool_call["name"]), "content": self._trim(result)}
        )
        return self._advance()

    def _trim(self, result):
        text = json.dumps(result)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + "...(truncated)"
        return text

    def _advance(self):
        state = self._state
        while state["step"] < MAX_STEPS:
            state["step"] += 1
            result = self.provider.chat(SYSTEM_PROMPT, state["messages"], self.tool_schemas)

            if result.kind == "text":
                self._state = None
                return result.text or "(no answer produced)"

            state["messages"].append(
                {"role": "assistant", "tool_calls": result.tool_calls, "content": ""}
            )

            for tc in result.tool_calls:
                schema = next((t for t in self.tool_schemas if t["name"] == tc["name"]), None)
                if schema and schema.get("destructive"):
                    args_preview = json.dumps(tc.get("arguments", {}))
                    if len(args_preview) > 300:
                        args_preview = args_preview[:300] + "..."
                    return {
                        "needs_confirmation": True,
                        "tool_call": tc,
                        "message": (
                            f"AI wants to run <code>{tc['name']}</code> with <code>{args_preview}</code>.\n"
                            "Type /yes to proceed or /no to cancel."
                        ),
                    }
                self.log(f"<i>Running {tc['name']}...</i>")
                tool_result = self.tool_executor.dispatch(tc["name"], tc["arguments"])
                state["messages"].append(
                    {"role": "tool", "name": tc["name"], "id": tc.get("id", tc["name"]), "content": self._trim(tool_result)}
                )

        self._state = None
        return "Reached the max reasoning steps for this request without a final answer -- try breaking it into smaller steps."
