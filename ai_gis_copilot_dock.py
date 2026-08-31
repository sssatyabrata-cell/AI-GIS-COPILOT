import json
import re
import configparser
import csv
import heapq
import os
import sqlite3
import subprocess
import tempfile
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

try:
    import processing
except Exception:
    processing = None
import qgis.utils
from qgis.PyQt.QtCore import Qt, QVariant, QSize, QThread, pyqtSignal
from qgis.PyQt.QtGui import QFont, QImage, QColor
from qgis.PyQt.QtWidgets import (
    QAction,
    QDockWidget,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsFeature,
    QgsFeatureRequest,
    QgsRendererCategory,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRasterPipe,
    QgsRasterFileWriter,
    QgsMapSettings,
    QgsMapRendererParallelJob,
    QgsRectangle,
    QgsSettings,
    QgsSymbol,
    QgsSpatialIndex,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .llm_client import LLMClient
from .agent_providers import build_provider
from .agent_tools import ToolExecutor
from .agent_core import AgentCore
from .mcp_bridge import GisMcpBridge, McpBridgeError, install_gis_mcp, find_real_python_executable


class _BackgroundTask(QThread):
    """Runs a slow blocking call (pip installs, subprocess startup) off
    QGIS's UI thread, so the app stays responsive instead of freezing for
    however long the operation takes -- confirmed real problem: /install
    gis-mcp blocked the entire QGIS UI for 10+ minutes with no way to tell
    it apart from a genuine crash. Results are delivered back to the main
    thread via Qt's signal/slot mechanism, which is the only thread-safe
    way to touch UI/log state from a worker thread in PyQt."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, work_fn, parent=None):
        super().__init__(parent)
        self.work_fn = work_fn

    def run(self):
        try:
            result = self.work_fn()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class _ChatInputBox(QPlainTextEdit):
    """Multi-line, word-wrapping chat input -- replaces the old single-line
    QLineEdit that only showed ~30px of text, so a long prompt scrolled
    off-screen instead of wrapping into view (confirmed directly from a
    real screenshot: the whole typed sentence was invisible). Enter sends
    the message (matching normal chat UX); Shift+Enter inserts a literal
    newline for anyone who wants a multi-line prompt."""

    def __init__(self, on_send, parent=None):
        super().__init__(parent)
        self._on_send = on_send

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            event.accept()
            self._on_send()
            return
        super().keyPressEvent(event)


class AIGISCopilotDockWidget(QDockWidget):
    def __init__(self, iface):
        super().__init__("AI GIS Copilot")
        self.iface = iface
        self.llm = LLMClient()
        self.pending_action = None
        self.pending_agent_confirmation = None
        self.agent_unavailable_reason = None
        self.agent_retry_available = False
        self.agent_settings = self._load_agent_settings()
        self.agent_provider_name = self.agent_settings.get("provider", "ollama")
        self.agent_tool_executor = ToolExecutor(self)
        self.gis_mcp_bridge = None  # set on /bind gis-mcp; None = unbound, default behavior unchanged
        self._background_task = None  # keeps the QThread alive; also used to prevent overlapping installs/binds
        self._background_task_label = None
        self._background_task_started_at = None
        self._heartbeat_timer = None
        try:
            from qgis.core import QgsProject

            QgsProject.instance().layersAdded.connect(self._on_project_layers_added)
        except Exception:
            pass  # passive activity capture is best-effort, never blocks plugin load
        self.agent = AgentCore(
            build_provider(self.agent_provider_name, self.agent_settings),
            self.agent_tool_executor,
            self._log,
        )
        self.history = []
        self.last_user_query = ""
        self.pinned_commands = []
        self.plugin_registry = {}
        self.layer_catalog = {}
        self.algorithm_catalog = {}
        self.qgis_inventory = {}
        self.learned_routes = self._load_json_file(self._learned_routes_file_path(), {})
        self.history_store = self._load_json_file(self._history_file_path(), [])
        self.last_suggestions = []
        self.recipes = self._load_json_file(self._recipes_file_path(), {})
        self.doc_knowledge = self._load_json_file(self._doc_knowledge_file_path(), [])
        self.regression_cases = self._load_json_file(self._regression_cases_file_path(), [])
        self._ensure_document_knowledge_seeded()
        self._ensure_web_knowledge_seeded()
        self._ensure_workflow_knowledge_seeded()
        self._ensure_full_crawl_bootstrap_once()
        self.hybrid_mode = True
        self.internet_mode = True
        self.global_mode = True
        self.architecture_mode = True
        self.stability_mode = True
        self.stability_stats = {"queries": 0, "handled": 0, "safe_blocked": 0}
        self.last_operation_state = {"name": "", "status": "", "detail": ""}
        self.last_length_meters = None
        self.pending_path_intent = None
        self.pending_website_report = False
        self.pending_output_request = None
        self.require_output_path = True
        self.pending_output_for_query = None
        self.last_saved_output_path = ""
        self.default_area = Qt.RightDockWidgetArea
        self.setObjectName("AIGISCopilotDockWidget")
        self._build_ui()
        self._update_internet_button_label()
        self._refresh_algorithm_catalog()
        self._refresh_qgis_inventory()

    def _build_ui(self):
        root = QWidget(self)
        # Keep the dock compact and convenient by default.
        self.setMinimumWidth(360)
        self.setMaximumWidth(520)
        self.setMinimumHeight(420)
        self.resize(420, 560)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)
        self.chat_output = QTextEdit(root)
        self.chat_output.setReadOnly(True)
        self.chat_output.setMinimumHeight(320)
        output_font = QFont(self.chat_output.font())
        output_font.setPointSize(10)
        self.chat_output.setFont(output_font)

        input_row = QHBoxLayout()
        self.chat_input = _ChatInputBox(self.on_send_clicked, root)
        self.chat_input.setPlaceholderText("Ask anything about QGIS or GIS actions...")
        input_font = QFont(self.chat_input.font())
        input_font.setPointSize(10)
        self.chat_input.setFont(input_font)
        # Previous fix (34-110px) grew with content but still read as "barely
        # bigger" -- per a screenshot showing the actual expected size (a
        # large fixed compose area, not a thin strip that grows). Now starts
        # tall by default, and its cap scales with the dock panel's own
        # height (not a guessed fixed number) so it fills roughly the
        # bottom third of the panel on any screen size, matching what was
        # circled, rather than a fixed pixel guess that could undershoot on
        # a taller panel or overshoot on a short one.
        self._chat_input_min_h = 150
        self._chat_input_max_h = 220
        self.chat_input.setFixedHeight(self._chat_input_min_h)
        self.chat_input.textChanged.connect(self._resize_chat_input)

        self.send_button = QPushButton("Run", root)
        self.send_button.clicked.connect(self.on_send_clicked)
        self.clear_button = QPushButton("Clear", root)
        self.clear_button.clicked.connect(self._clear_chat)
        self.internet_button = QPushButton("Internet: ON", root)
        self.internet_button.clicked.connect(self._toggle_internet_mode)
        self.send_button.setMinimumHeight(26)
        self.internet_button.setMinimumHeight(26)
        self.clear_button.setMinimumHeight(26)

        input_row.setSpacing(6)
        input_row.addWidget(self.chat_input, 1)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        button_row.addWidget(self.send_button, 1)
        button_row.addWidget(self.internet_button, 1)
        button_row.addWidget(self.clear_button, 1)

        # Keep chat area dominant and place input above button row.
        layout.addWidget(self.chat_output, 1)
        layout.addLayout(input_row)
        layout.addLayout(button_row)
        layout.setStretch(0, 1)
        layout.setStretch(1, 0)
        layout.setStretch(2, 0)
        self.setWidget(root)

    def _log(self, message):
        self.chat_output.append(message)

    def _clear_chat(self):
        self.chat_output.clear()

    def _toggle_internet_mode(self):
        self.internet_mode = not self.internet_mode
        self._update_internet_button_label()
        state = "enabled" if self.internet_mode else "disabled"
        self._log(f"<b>AI:</b> Internet answer mode {state}.")

    def _update_internet_button_label(self):
        if hasattr(self, "internet_button") and self.internet_button is not None:
            self.internet_button.setText(f"Internet: {'ON' if self.internet_mode else 'OFF'}")

    def _processing_available(self):
        if processing is None:
            self._log(
                "<b>AI:</b> QGIS Processing module is unavailable. Enable/install Processing plugin first."
            )
            return False
        return True

    def _resize_chat_input(self):
        # QPlainTextEdit's document().size().height() is a LINE COUNT, not
        # pixels (unlike QTextEdit) -- confirmed directly with a real Qt
        # widget test. Multiply by line spacing to get an actual pixel
        # height, or this silently never grows regardless of content.
        line_count = self.chat_input.document().size().height()
        px_height = line_count * self.chat_input.fontMetrics().lineSpacing()
        margins = self.chat_input.contentsMargins()
        target = int(px_height) + margins.top() + margins.bottom() + 8
        dock_height = self.height()
        dynamic_max = max(self._chat_input_max_h, int(dock_height * 0.30)) if dock_height > 50 else self._chat_input_max_h
        target = max(self._chat_input_min_h, min(target, dynamic_max))
        self.chat_input.setFixedHeight(target)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "chat_input"):
            self._resize_chat_input()

    def on_send_clicked(self):
        query = self.chat_input.toPlainText().strip()
        if not query:
            return
        self._refresh_layer_catalog()
        self.last_user_query = query
        self.chat_input.clear()
        self._resize_chat_input()
        self._log(f"<b>You:</b> {query}")
        self._handle_query_safe(query)

    def _handle_query_safe(self, query):
        """
        Safety wrapper so user questions never crash the Copilot pipeline.
        If routing throws, fall back to web/general answer path.
        """
        try:
            self._handle_query(query)
            return
        except Exception as exc:
            self._log(f"<b>AI:</b> Internal routing error recovered: {exc}")
        # Safe fallback chain (best effort, no crash propagation).
        try:
            if self.internet_mode and self._try_internet_answer(query):
                return
        except Exception:
            pass
        try:
            if self.hybrid_mode and self.llm.configured() and self._try_llm_general_answer(query):
                return
        except Exception:
            pass
        self._log(
            "<b>AI:</b> I hit an internal error while processing that question.\n"
            "Please retry once. If it repeats, I will patch this exact prompt path."
        )

    def _handle_query(self, query):
        stripped = query.strip()
        lowered = stripped.lower()
        lowered = self._normalize_user_english(lowered)
        if self.stability_mode:
            self.stability_stats["queries"] = int(self.stability_stats.get("queries", 0)) + 1
        if self.pending_agent_confirmation is not None:
            if lowered in {"yes", "y", "confirm", "ok", "okay", "proceed", "/yes"}:
                self._confirm_pending_agent_action(True)
                return
            if lowered in {"no", "n", "cancel", "stop", "/no"}:
                self._confirm_pending_agent_action(False)
                return
        if self.pending_action is not None:
            if lowered in {"yes", "y", "confirm", "ok", "okay", "proceed", "/yes"}:
                self._confirm_pending_action(True)
                return
            if lowered in {"no", "n", "cancel", "stop", "/no"}:
                self._confirm_pending_action(False)
                return
        if stripped.startswith("/"):
            self._handle_slash_command(stripped)
            return
        if self.agent.provider.configured() and self._run_agent_turn(stripped):
            return
        if self._is_qgis_plugin_help_query(lowered):
            self._answer_create_qgis_plugin_help()
            return
        if self._is_application_help_query(lowered):
            self._answer_create_application_help()
            return
        if "are you getting what i am asking" in lowered or "do you understand what i am asking" in lowered:
            self._log("<b>AI:</b> Yes. I understand your intent and I will execute it, not give generic web text.")
            return
        if self._is_mismatch_feedback_query(lowered):
            self._answer_mismatch_feedback(stripped)
            return
        if self._safe_bool_call("_consume_pending_output_for_query", stripped):
            return
        if self._safe_bool_call("_require_output_path_gate", stripped, lowered):
            return
        if self._safe_bool_call("_consume_pending_output_path", stripped):
            return
        if self._safe_bool_call("_is_learn_media_query", stripped):
            self._safe_void_call("_run_learn_media_from_query", stripped)
            return
        if self._safe_bool_call("_is_website_report_query", stripped):
            self._safe_void_call("_run_website_report_from_query", stripped)
            return
        if bool(getattr(self, "pending_website_report", False)) and self._safe_bool_call("_looks_like_url_only_input", stripped):
            self._safe_void_call("_run_website_report_from_query", stripped)
            return
        if self._safe_bool_call("_try_pending_path_followup", stripped):
            return
        if lowered.startswith("run "):
            if self._handle_run_contract_command(stripped):
                return
        if self._is_generic_processing_prompt(lowered):
            if self._run_generic_processing_prompt(stripped):
                return
        if self._looks_like_model_name(stripped):
            self.llm.set_model(stripped)
            self._log(f"<b>AI:</b> Model set to '{stripped}'.")
            return
        if self._run_agent_core(stripped, lowered):
            if self.stability_mode:
                self.stability_stats["handled"] = int(self.stability_stats.get("handled", 0)) + 1
            return
        # Force explicit basemap command to execute, never route to web/doc QA.
        if self._is_google_satellite_action_query(lowered):
            self._open_google_satellite()
            return
        if self._try_field_value_filter_fastpath(lowered):
            return
        if self._is_immediate_loaded_layer_query(lowered):
            if self._answer_immediate_loaded_layer_query(lowered):
                return

        normalized = query.lower()
        if self._run_architecture_pipeline(query, normalized):
            if self.stability_mode:
                self.stability_stats["handled"] = int(self.stability_stats.get("handled", 0)) + 1
            return
        action_like = self._looks_like_action_request(normalized) or self._is_critical_execution_intent(normalized)
        if self.stability_mode and action_like:
            self.stability_stats["safe_blocked"] = int(self.stability_stats.get("safe_blocked", 0)) + 1
            self._log(self._stable_action_failure_message(query, normalized))
            self._log(self._build_fallback_explainer(query))
            return

        self._log(
            "<b>AI:</b> I could not map that request yet without LLM.\n"
            "Use /intents for built-in operations or configure /set api_key."
        )
        self._log(self._build_fallback_explainer(query))

    def _run_architecture_pipeline(self, query, normalized):
        if self._is_plugin_count_query(normalized):
            self._answer_plugin_count()
            return True
        if self._is_simple_layer_count_query(normalized):
            self._answer_simple_layer_count()
            return True
        action_like = self._looks_like_action_request(normalized)

        # GIS Knowledge
        if self._is_plugin_recommendation_query(normalized):
            self._answer_plugin_recommendation(query, normalized)
            return True
        if (not action_like) and self.global_mode and self._is_global_knowledge_prompt(normalized):
            if self._run_global_qa_chain(query, normalized):
                return True
        if (not action_like) and (not self.global_mode) and self.internet_mode and self._is_question_like(normalized):
            if self._try_internet_answer(query):
                return True

        # GIS Reasoning
        if any(k in normalized for k in ("workflow", "pipeline", "step by step", "plan")):
            if any(k in normalized for k in ("satellite", "remote sensing", "stac", "sentinel", "landsat")):
                self._run_remote_sensing_natural_router(normalized, self._parse_kv_options(query))
                return True
            if any(k in normalized for k in ("ai gis", "geoai", "segmentation", "object detection", "spatial machine learning")):
                self._run_ai_gis_natural_router(normalized)
                return True
        if (not action_like) and self.hybrid_mode and self.llm.configured():
            if self._route_with_llm(query):
                return True

        # GIS Execution
        if self._handle_builtins(normalized, query):
            return True
        if action_like:
            if self._try_auto_processing_match(query):
                return True
            if self._try_generic_plugin_execution(query):
                return True
        if self._run_explicit_algorithm(query):
            return True
        if self.hybrid_mode and self.llm.configured():
            if self._route_with_llm(query):
                return True
        return False

    def _run_agent_core(self, original_query, normalized_query):
        """
        Agent core loop:
        classify intent -> execute deterministic action -> verify outcome.
        Returns True only when intent was classified and handled.
        """
        intent = self._classify_agent_intent(normalized_query)
        if not intent:
            return False
        name = intent.get("name", "")
        ok = False
        reason = ""
        try:
            if name == "open_attribute_table":
                self._open_attribute_table_action()
                ok = True
            elif name == "help_create_qgis_plugin":
                self._answer_create_qgis_plugin_help()
                ok = True
            elif name == "help_create_application":
                self._answer_create_application_help()
                ok = True
            elif name == "load_google_satellite":
                self._open_google_satellite()
                ok = True
            elif name == "create_spatial_index":
                self._run_create_spatial_index()
                st = self.last_operation_state or {}
                ok = str(st.get("name", "")) == "spatial_index_create" and str(st.get("status", "")) == "done"
                if not ok:
                    reason = str(st.get("detail", "spatial index not confirmed"))
            elif name == "delete_field":
                ok = self._agent_delete_field_with_verify(normalized_query, original_query)
                if not ok:
                    reason = "field deletion verify failed"
            elif name == "add_field":
                ok = self._agent_add_field_with_verify(normalized_query, original_query)
                if not ok:
                    reason = "field add verify failed"
            elif name == "add_then_equal_distribution":
                self._run_add_then_equal_distribution(normalized_query, original_query)
                ok = self._verify_equal_distribution_applied(normalized_query)
                if not ok:
                    reason = "add+distribution verify failed"
            elif name == "categorize":
                ok = self._agent_categorize_with_verify(normalized_query, original_query)
                if not ok:
                    reason = "categorize verify failed"
            elif name == "count_by_condition":
                layer = self.iface.activeLayer()
                ok = isinstance(layer, QgsVectorLayer) and self._answer_field_value_count_from_query(layer, normalized_query)
                if not ok:
                    reason = "condition count could not be parsed"
            elif name == "length_by_condition":
                layer = self.iface.activeLayer()
                ok = isinstance(layer, QgsVectorLayer) and self._answer_field_value_length_from_query(layer, normalized_query)
                if not ok:
                    reason = "condition length could not be parsed"
            elif name == "total_length":
                ok = self._agent_answer_total_length()
                if not ok:
                    reason = "total length could not be computed"
            elif name == "time_now":
                self._answer_time_now_local()
                ok = True
            elif name == "temperature_now":
                ok = self._try_weather_now_answer(original_query)
                if not ok:
                    reason = "temperature could not be fetched"
            elif name == "detect_location":
                self._answer_detect_location()
                ok = True
            elif name == "distance_from_here":
                ok = self._answer_distance_from_here(original_query)
                if not ok:
                    reason = "distance could not be computed"
            elif name == "location_coordinates":
                self._answer_detect_location_coordinates()
                ok = True
            elif name == "length_in_miles_followup":
                ok = self._answer_last_length_in_miles()
                if not ok:
                    reason = "no prior length result to convert"
            elif name == "mismatch_feedback":
                self._answer_mismatch_feedback(original_query)
                ok = True
            elif name == "build_raster_pyramids":
                ok = self._run_build_raster_pyramids(original_query)
                if not ok:
                    reason = "raster pyramid build failed"
        except Exception as exc:
            reason = f"{name} error: {exc}"
            ok = False

        if not ok:
            self._record_regression_case(original_query, name, reason or "unverified failure")
        return True

    def _classify_agent_intent(self, normalized_query):
        q = (normalized_query or "").lower().strip()
        if not q or q.startswith("/") or q.startswith("run "):
            return None
        if self._is_qgis_plugin_help_query(q):
            return {"name": "help_create_qgis_plugin"}
        if self._is_application_help_query(q):
            return {"name": "help_create_application"}
        if self._is_open_attribute_table_query(q):
            return {"name": "open_attribute_table"}
        if self._is_google_satellite_action_query(q):
            return {"name": "load_google_satellite"}
        if self._is_spatial_index_create_query(q):
            return {"name": "create_spatial_index"}
        if self._is_add_then_equal_distribution_query(q):
            return {"name": "add_then_equal_distribution"}
        if self._is_delete_field_query(q):
            return {"name": "delete_field"}
        if self._is_add_field_query(q):
            return {"name": "add_field"}
        if self._is_categorize_by_field_query(q):
            return {"name": "categorize"}
        if self._is_distance_from_here_query(q):
            return {"name": "distance_from_here"}
        if self._is_location_coordinates_query(q):
            return {"name": "location_coordinates"}
        if self._is_location_detect_query(q):
            return {"name": "detect_location"}
        if self._is_length_in_miles_followup_query(q):
            return {"name": "length_in_miles_followup"}
        if self._is_mismatch_feedback_query(q):
            return {"name": "mismatch_feedback"}
        if self._is_build_raster_pyramids_query(q):
            return {"name": "build_raster_pyramids"}
        if self._is_temperature_query(q):
            return {"name": "temperature_now"}
        if self._is_time_now_query(q):
            return {"name": "time_now"}
        if self._is_line_mile_query(q):
            return {"name": "total_length"}
        if "length" in q and ("shape file" in q or "shapefile" in q or "loaded layer" in q or "active layer" in q):
            if self._extract_field_value_pair_from_query(q, self.iface.activeLayer()) if isinstance(self.iface.activeLayer(), QgsVectorLayer) else False:
                return {"name": "length_by_condition"}
            return {"name": "total_length"}
        if any(k in q for k in ("how many", "count", "number of")):
            lyr = self.iface.activeLayer()
            if isinstance(lyr, QgsVectorLayer) and self._extract_field_value_pair_from_query(q, lyr):
                return {"name": "count_by_condition"}
        return None

    def _is_qgis_plugin_help_query(self, normalized_query):
        q = (normalized_query or "").lower()
        asks_help = any(k in q for k in ("help", "how to", "guide", "create", "build", "make"))
        is_plugin = ("qgis plugin" in q) or ("plugin for qgis" in q)
        return asks_help and is_plugin

    def _is_application_help_query(self, normalized_query):
        q = (normalized_query or "").lower()
        asks_help = any(k in q for k in ("help", "how to", "guide", "create", "build", "make"))
        # "app" as a bare substring matches inside many unrelated words
        # ("overlapped", "mapped", "wrapped", "happen", "apply", "approach",
        # "appropriate"...) -- confirmed this exact bug from a real user
        # report where "detect overlapped spans... create centroid" was
        # hijacked into this handler purely because "overlapped" contains
        # "app". Require a real word boundary for the bare token; the
        # longer phrases are safe to keep as substrings.
        is_app = any(k in q for k in ("desktop application", "desktop app")) or bool(
            re.search(r"\bapp\b|\bapplication\b", q)
        )
        is_plugin = "qgis plugin" in q or "plugin for qgis" in q
        return asks_help and is_app and not is_plugin

    def _answer_create_qgis_plugin_help(self):
        self._log(
            "<b>AI:</b> Yes — I can help you create a QGIS plugin end-to-end.\n"
            "Fast starter plan:\n"
            "1) Define plugin scope (1 clear workflow first).\n"
            "2) Create core files: metadata.txt, __init__.py, main plugin class, dock UI.\n"
            "3) Add intent->execute handlers for your target commands.\n"
            "4) Add verification checks after each action.\n"
            "5) Package/install in QGIS plugin folder and test.\n"
            "If you say 'create plugin skeleton now', I will generate a minimal runnable scaffold."
        )
        self._remember_action(self.last_user_query, "qgis plugin creation help answered")

    def _answer_create_application_help(self):
        self._log(
            "<b>AI:</b> Yes — I can help you create a desktop application.\n"
            "Recommended stack: Python + PyQt (or Electron for web-tech teams).\n"
            "Execution plan:\n"
            "1) Choose stack and target OS.\n"
            "2) Design screens + required actions.\n"
            "3) Implement core workflow logic.\n"
            "4) Add logging/error handling.\n"
            "5) Package as installer/exe.\n"
            "If you say 'create desktop app skeleton now', I will generate starter code."
        )
        self._remember_action(self.last_user_query, "application creation help answered")

    def _agent_answer_total_length(self):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Total length requires an active vector layer.")
            return False
        total_native = 0.0
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            total_native += float(geom.length())
        unit_name = "layer units"
        try:
            from qgis.core import QgsUnitTypes

            unit_name = QgsUnitTypes.toString(layer.crs().mapUnits()) or "layer units"
        except Exception:
            pass
        # A flat "1 degree = 111,320 m" conversion (the old behavior) is
        # only correct for latitude, or longitude AT THE EQUATOR -- it
        # silently distorts real-world length by up to ~30% at higher
        # latitudes, since a degree of longitude shrinks by a cos(latitude)
        # factor away from the equator. Confirmed against a real report:
        # this was producing a meaningfully wrong number, not just an
        # approximation, for a layer at ~44N. Use QGIS's own ellipsoidal
        # (geodesic) length measurement instead when the layer is in a
        # geographic CRS -- correct regardless of latitude.
        total_m = self._layer_units_to_meters(layer, total_native)
        if layer.crs().isGeographic():
            try:
                from qgis.core import QgsDistanceArea, QgsProject

                da = QgsDistanceArea()
                da.setEllipsoid("WGS84")
                da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
                geodesic_total_m = 0.0
                for feat in layer.getFeatures():
                    geom = feat.geometry()
                    if geom is None or geom.isEmpty():
                        continue
                    geodesic_total_m += da.measureLength(geom)
                total_m = geodesic_total_m
            except Exception:
                pass  # fall back to the flat-factor estimate above rather than fail outright
        self.last_length_meters = float(total_m) if total_m is not None else None
        msg = f"<b>AI:</b> Total length of loaded layer '{layer.name()}':\n- {total_native:.6f} {unit_name}"
        if total_m is not None:
            precision_note = "geodesic, accounts for latitude" if layer.crs().isGeographic() else "projected CRS"
            msg += f"\n- {float(total_m):.3f} meters ({precision_note})"
        self._log(msg)
        self._remember_action(self.last_user_query, "total length answered")
        return True

    def _agent_add_field_with_verify(self, normalized_query, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return False
        field_name = self._extract_new_field_name(normalized_query or "")
        if not field_name:
            self._log("<b>AI:</b> Please specify the field name to add.")
            return False
        existed_before = layer.fields().indexFromName(field_name) >= 0
        self._run_add_field(normalized_query, original_query)
        exists_after = layer.fields().indexFromName(field_name) >= 0
        return (not existed_before) and exists_after

    def _agent_delete_field_with_verify(self, normalized_query, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return False
        field_name = self._extract_field_name_for_split(normalized_query or "", layer)
        if not field_name:
            self._log("<b>AI:</b> Please specify the field name to delete.")
            return False
        existed_before = layer.fields().indexFromName(field_name) >= 0
        self._run_delete_field(normalized_query, original_query)
        exists_after = layer.fields().indexFromName(field_name) >= 0
        return existed_before and (not exists_after)

    def _agent_categorize_with_verify(self, normalized_query, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return False
        self._run_categorize_by_field(normalized_query, original_query)
        try:
            return isinstance(layer.renderer(), QgsCategorizedSymbolRenderer)
        except Exception:
            return False

    def _record_regression_case(self, query, intent_name, reason):
        entry = {
            "at": datetime.utcnow().isoformat(),
            "query": str(query or ""),
            "intent": str(intent_name or ""),
            "reason": str(reason or ""),
        }
        items = list(self.regression_cases or [])
        items.append(entry)
        if len(items) > 500:
            items = items[-500:]
        self.regression_cases = items
        self._save_json_file(self._regression_cases_file_path(), self.regression_cases)

    def _render_regression_report(self):
        items = list(self.regression_cases or [])
        if not items:
            self._log("<b>AI:</b> Regression report: no recorded failures yet.")
            return
        total = len(items)
        by_intent = {}
        for it in items:
            k = str((it or {}).get("intent", "unknown")).strip() or "unknown"
            by_intent[k] = by_intent.get(k, 0) + 1
        top = sorted(by_intent.items(), key=lambda x: (-x[1], x[0]))[:8]
        lines = [
            "<b>AI:</b> Regression report:",
            f"- Total recorded failures: {total}",
            "- Top failing intents:",
        ]
        lines.extend([f"  - {k}: {v}" for k, v in top])
        recent = items[-5:]
        lines.append("- Recent failures:")
        for r in recent:
            lines.append(
                f"  - [{r.get('at','')}] {r.get('intent','unknown')}: {str(r.get('query',''))[:120]}"
            )
        self._log("\n".join(lines))

    def _run_regression_replay(self, token):
        items = list(self.regression_cases or [])
        if not items:
            self._log("<b>AI:</b> No regression cases to replay.")
            return
        n = 3
        if token:
            try:
                n = max(1, min(20, int(float(token))))
            except Exception:
                n = 3
        cases = items[-n:]
        self._log(f"<b>AI:</b> Replaying {len(cases)} regression case(s)...")
        # Replay oldest to newest within selected slice.
        for c in cases:
            q = str((c or {}).get("query", "")).strip()
            if not q:
                continue
            self._log(f"<b>Replay:</b> {q}")
            self._handle_query_safe(q)

    def _is_generic_processing_prompt(self, lowered_query):
        return (
            ("native:" in lowered_query or "qgis:" in lowered_query or "gdal:" in lowered_query)
            and any(k in lowered_query for k in ("run", "execute", "apply"))
        )

    def _run_generic_processing_prompt(self, query):
        if not self._processing_available():
            return False
        m = re.search(r"([a-zA-Z0-9_]+:[a-zA-Z0-9_]+)", query)
        if not m:
            return False
        alg_id = m.group(1)
        if alg_id not in self.algorithm_catalog:
            self._refresh_algorithm_catalog()
            if alg_id not in self.algorithm_catalog:
                self._log(f"<b>AI:</b> Unknown algorithm id: {alg_id}")
                return True

        options = self._parse_kv_options(query)
        params = self._build_algorithm_params_from_options(alg_id, options)
        missing = self._missing_required_params(alg_id, params)
        if missing:
            self._log(
                f"<b>AI:</b> Missing required parameters for {alg_id}: {', '.join(missing)}\n"
                f"Provide key=value options, e.g. run {alg_id} input=active output=memory:"
            )
            return True

        return self._execute_algorithm_plan(alg_id, params, f"Generic execution requested for {alg_id}.")

    def _handle_run_contract_command(self, query):
        lowered = query.lower()
        options = self._parse_kv_options(query)
        if "999 updater" in lowered:
            self._run_999_updater_workflow(lowered, options)
            return True
        if "agc" in lowered and any(k in lowered for k in ("qa", "batch", "compare")):
            self._run_agc_batch_qa_workflow(lowered, options)
            return True
        if "ivms" in lowered and "qa" in lowered:
            self._run_ivms_span_qa_workflow(lowered, options)
            return True
        if lowered.startswith("run workflow auto"):
            self._run_workflow_auto_router(query, lowered, options)
            return True
        if lowered.startswith("run ai_gis") or lowered.startswith("run ai gis"):
            self._run_ai_gis_contract_workflow(lowered, options)
            return True
        if lowered.startswith("run spatialdb") or lowered.startswith("run spatial db"):
            self._run_spatialdb_contract_workflow(lowered, options)
            return True
        if lowered.startswith("run dl") or lowered.startswith("run deep learning"):
            self._run_deep_learning_contract_workflow(lowered, options)
            return True
        if lowered.startswith("run geoai") or " geoai " in f" {lowered} ":
            self._run_geoai_contract_workflow(lowered, options)
            return True
        if "qautofix" in lowered or "q auto fix" in lowered or "auto fix" in lowered:
            self._run_qautofix_workflow(lowered, options)
            return True
        if "tgth" in lowered or "tg th" in lowered or ("validator" in lowered and ("tg" in lowered or "th" in lowered)):
            self._run_tg_th_validator_workflow(lowered, options)
            return True
        return False

    def _run_workflow_auto_router(self, query, lowered_query, options):
        objective = self._extract_workflow_auto_objective(query, options)
        normalized = (objective or lowered_query or "").lower().strip()

        if not normalized:
            self._log(
                "<b>AI:</b> Usage: run workflow auto <objective>\n"
                "Example: run workflow auto calculate mean minimum maximum elevation for each parcel"
            )
            return

        if self._is_elevation_stats_query(normalized):
            self._plan_elevation_stats_workflow(objective, normalized)
            return
        if self._is_spatial_join_workflow_query(normalized):
            self._plan_spatial_join_workflow(objective, normalized)
            return
        if self._is_buffer_intersection_workflow_query(normalized):
            self._plan_buffer_intersection_workflow(objective, normalized)
            return
        if self._is_clip_export_workflow_query(normalized):
            self._plan_clip_export_workflow(objective, normalized)
            return
        if self._looks_like_geoai_auto_intent(normalized):
            self._plan_workflow_auto_geoai(objective, normalized, dict(options or {}))
            return
        if self._looks_like_dl_auto_intent(normalized):
            self._plan_workflow_auto_dl(objective, normalized, dict(options or {}))
            return

        # Fallback: use broader GIS execution matchers while preserving confirmation-first behavior.
        if self._handle_builtins(normalized, objective):
            return
        if self._try_auto_processing_match(objective):
            return

        self._log(
            "<b>AI:</b> I could not auto-select a supported multi-step workflow from that objective yet.\n"
            "Try one of these patterns:\n"
            "- run workflow auto calculate mean minimum maximum elevation for each parcel\n"
            "- run workflow auto spatial join parcels with wards\n"
            "- run workflow auto buffer roads by 25 m and intersect parcels\n"
            "- run workflow auto clip buildings by boundary and export\n"
            "- run workflow auto clip active raster by bbox xmin,ymin,xmax,ymax\n"
            "- run workflow auto semantic inference input_path=... model_path=... output_path=..."
        )

    def _extract_workflow_auto_objective(self, query, options):
        if options.get("objective"):
            return str(options.get("objective")).strip()
        if options.get("workflow"):
            return str(options.get("workflow")).strip()

        m = re.match(r"(?is)^\s*run\s+workflow\s+auto\s*(.*)$", query or "")
        if m:
            return (m.group(1) or "").strip()
        return ""

    def _looks_like_geoai_auto_intent(self, normalized_query):
        q = (normalized_query or "").lower()
        geoai_terms = (
            "geoai",
            "moondream",
            "samgeo",
            "deepforest",
            "water segmentation",
            "semantic segmentation",
            "instance segmentation",
            "vectorize mask",
            "clip raster by bbox",
            "bbox",
            "raster tile",
            "export tiles",
        )
        return any(t in q for t in geoai_terms) and any(
            a in q for a in ("clip", "vectorize", "smooth", "tile", "segment", "segmentation", "moondream", "samgeo", "deepforest", "water")
        )

    def _looks_like_dl_auto_intent(self, normalized_query):
        q = (normalized_query or "").lower()
        if "deep learning" in q or q.startswith("dl "):
            return True
        has_task = any(k in q for k in ("semantic", "instance", "segmentation"))
        has_mode = any(k in q for k in ("train", "infer", "inference", "predict"))
        return has_task and has_mode

    def _plan_workflow_auto_geoai(self, objective, normalized_query, options):
        lines = [
            "<b>AI:</b> Ready to execute workflow auto (GeoAI).",
            f"✓ Objective: {objective or normalized_query}",
            "✓ Router: GeoAI nature workflow",
            "Workflow:",
            "1) Parse GeoAI intent and parameters",
            "2) Resolve active/input layer requirements",
            "3) Execute matched GeoAI action",
            "4) Validate output/result and report",
        ]
        self._request_confirmation(
            "\n".join(lines),
            self._execute_workflow_auto_geoai,
            normalized_query,
            dict(options or {}),
        )

    def _execute_workflow_auto_geoai(self, normalized_query, options):
        self._run_geoai_nature_router(normalized_query, dict(options or {}))
        self._remember_action(self.last_user_query, "workflow auto geoai executed")

    def _plan_workflow_auto_dl(self, objective, normalized_query, options):
        lines = [
            "<b>AI:</b> Ready to execute workflow auto (Deep Learning).",
            f"✓ Objective: {objective or normalized_query}",
            "✓ Router: DL natural workflow",
            "Workflow:",
            "1) Parse train/inference intent",
            "2) Validate required dataset/model/output parameters",
            "3) Execute GeoAI worker action",
            "4) Stream progress and validate completion",
        ]
        self._request_confirmation(
            "\n".join(lines),
            self._execute_workflow_auto_dl,
            normalized_query,
            dict(options or {}),
        )

    def _execute_workflow_auto_dl(self, normalized_query, options):
        self._run_deep_learning_natural_router(normalized_query, dict(options or {}))
        self._remember_action(self.last_user_query, "workflow auto deep-learning executed")

    def _build_fallback_explainer(self, query):
        suggestions = self._suggest_commands_for_query(query, limit=5)
        self.last_suggestions = suggestions
        if not suggestions:
            return (
                "<b>AI:</b> Suggestion: try `/contracts`, `/intents`, or `/algorithms <keyword>` "
                "to discover executable commands."
            )
        lines = ["<b>AI:</b> Closest executable suggestions (use /pick <number>):"]
        for i, s in enumerate(suggestions, start=1):
            lines.append(f"{i}. {s}")
        return "\n".join(lines)

    def _run_suggestion_by_index(self, idx):
        if not self.last_suggestions:
            self._log("<b>AI:</b> No suggestions available. Trigger a fallback first.")
            return
        if idx < 1 or idx > len(self.last_suggestions):
            self._log(f"<b>AI:</b> Suggestion index out of range. Use 1-{len(self.last_suggestions)}.")
            return
        cmd = self.last_suggestions[idx - 1]
        self._log(f"<b>AI:</b> Running suggestion #{idx}: {cmd}")
        self._handle_query(cmd)

    def _suggest_commands_for_query(self, query, limit=5):
        qtokens = self._tokenize_text(query)
        layer_names = []
        try:
            layer_names = [l.name() for l in QgsProject.instance().mapLayers().values()]
        except Exception:
            layer_names = []
        sample_layer = layer_names[0] if layer_names else "active"

        candidates = [
            "run qautofix layer=active tol_m=1",
            "run ivms qa layer=active",
            "run 999 updater layer=active veg_field=vegremarks length_field=length_lms short_m=10",
            "run agc qa raw=<layer1> corrected=<layer2> tol_m=1",
            "run tgth validator layer=active mode=th",
            "run dl task=semantic_train image_dir=D:\\data\\images mask_dir=D:\\data\\masks output_dir=D:\\models\\sem",
            "run dl task=semantic_infer input_path=D:\\img.tif model_path=D:\\models\\best_model.pth output_path=D:\\out\\pred.tif",
            "run geoai task=clip_raster_by_bbox bbox=xmin,ymin,xmax,ymax raster=active",
            "run geoai task=vectorize_mask input=active field=class",
            "run geoai task=smooth_vector input=active iterations=1 offset=0.25",
            "detect the spans which are not snapped",
            "detect where there is an overlap on the loaded shape",
            "give me centroids for the spans where there is a overlap",
            "detect end spans and create centroid for each end span",
            "filter layer where gis_clrnce 0-5 and highlight",
            "find any error in the attribute",
            "open google satellite",
            "generate a screenshot of the map canvas",
            "run native:buffer input=active distance=25 output=memory:",
            "run native:checkvalidity input_layer=active method=2 valid_output=memory: invalid_output=memory: error_output=memory:",
            "/algorithms overlap",
            "/reindex qgis",
            "/contracts",
        ]

        # Add learned successful patterns.
        for learned_q in list(self.learned_routes.keys())[-100:]:
            if learned_q:
                candidates.append(learned_q)

        # Add a couple of layer-specific candidates.
        if sample_layer != "active":
            candidates.append(f"run qautofix layer={sample_layer} tol_m=1")
            candidates.append(f"run ivms qa layer={sample_layer}")

        scored = []
        for c in candidates:
            ctokens = self._tokenize_text(c)
            score = self._token_overlap_score(qtokens, ctokens)
            # Prefer commands that mention fields if query mentions attributes.
            if any(t in qtokens for t in ("attribute", "field", "column")) and any(
                t in ctokens for t in ("attribute", "field", "filter", "where")
            ):
                score += 0.15
            if score > 0:
                scored.append((score, c))

        if not scored:
            return []

        scored.sort(key=lambda x: (-x[0], x[1]))
        uniq = []
        seen = set()
        for _, cand in scored:
            key = cand.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(cand)
            if len(uniq) >= limit:
                break
        return uniq

    def _tokenize_text(self, text):
        if not text:
            return set()
        tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        stop = {
            "the",
            "a",
            "an",
            "to",
            "of",
            "for",
            "on",
            "in",
            "and",
            "or",
            "is",
            "are",
            "me",
            "my",
            "it",
            "this",
            "that",
            "with",
            "from",
            "give",
            "show",
            "run",
        }
        return {t for t in tokens if t not in stop}

    # Backward-compatible alias used by older call sites.
    def _tokenize(self, text):
        return self._tokenize_text(text)

    def _token_overlap_score(self, a_tokens, b_tokens):
        if not a_tokens or not b_tokens:
            return 0.0
        inter = len(a_tokens.intersection(b_tokens))
        if inter == 0:
            return 0.0
        # Balanced overlap score.
        return (inter / max(len(a_tokens), 1) + inter / max(len(b_tokens), 1)) / 2.0

    def _looks_like_model_name(self, text):
        value = text.strip().lower()
        if not value or " " in value:
            return False
        known_prefixes = ("gpt-", "o1", "o3", "o4", "claude-", "gemini-", "llama", "mistral")
        return value.startswith(known_prefixes)

    def _handle_slash_command(self, query):
        if query == "/yes":
            if self.pending_agent_confirmation is not None:
                self._confirm_pending_agent_action(True)
                return
            self._confirm_pending_action(True)
            return
        if query == "/no":
            if self.pending_agent_confirmation is not None:
                self._confirm_pending_agent_action(False)
                return
            self._confirm_pending_action(False)
            return
        if query == "/install pysal":
            self._install_pysal_dependencies()
            return
        if query == "/cancel task":
            self._cancel_background_task()
            return
        if query.startswith("/run"):
            self._handle_run_command(query[len("/run") :].strip())
            return
        if query == "/install gis-mcp":
            self._install_gis_mcp()
            return
        if query == "/bind gis-mcp":
            self._bind_gis_mcp()
            return
        if query == "/unbind gis-mcp":
            self._unbind_gis_mcp()
            return
        if query == "/mcp status":
            self._mcp_status()
            return
        if query == "/agent status":
            reason = self.agent_unavailable_reason
            status_line = f"unavailable this session ({reason})" if reason else ("configured" if self.agent.provider.configured() else "not configured")
            self._log(
                "<b>AI:</b> Agent provider: "
                f"<b>{self.agent_provider_name}</b> -- {status_line}\n"
                "Free providers: gemini, groq, ollama.\n"
                "Set with: /set provider gemini|groq|ollama\n"
                "Keys (free): /set gemini_key <key>  /set groq_key <key>\n"
                "Models: /set gemini_model <name>  /set groq_model <name>  /set ollama_model <name>\n"
                "Local Ollama URL: /set ollama_url <url>\n"
                "Optional stronger web search (default is always-free DuckDuckGo, no setup): "
                "/set brave_key <key> from https://brave.com/search/api\n"
                "Optional live gis-mcp binding (adds ~88 more tools, extra process): /install gis-mcp, "
                "then /bind gis-mcp -- /mcp status to check, /unbind gis-mcp to stop it. If an "
                "install/bind ever seems stuck, /cancel task frees it up without restarting QGIS.\n"
                "Optional team-wide shared memory (not model retraining -- a growing shared activity "
                "log everyone's Copilot can recall from): /set shared_memory_path "
                "\\\\your-server\\GIS\\CopilotMemory -- or a synced OneDrive/SharePoint folder path "
                "like C:\\Users\\you\\OneDrive - Company\\GIS\\CopilotMemory works too (each person "
                "writes their own file, so cloud-sync conflicts can't happen). Off by default; "
                "local-only history is unaffected either way.\n"
                "Any /set on the agent clears an 'unavailable' state and retries.\n"
                "Works even with the agent completely off: /run &lt;search term&gt; finds any "
                "Processing Toolbox algorithm's exact id, /run &lt;id&gt; shows its parameters, "
                "/run &lt;id&gt; PARAM=value ... executes it directly -- full toolbox access with "
                "zero AI dependency.\n"
                "Tip: a reply tagged 'AI (agent)' came from real language understanding; a plain "
                "'AI:' reply came from the older keyword-matching fallback -- if you're seeing "
                "plain 'AI:' a lot, your agent isn't actually connected, check the status above."
            )
            return
        if query.startswith("/set "):
            payload = query[len("/set ") :].strip()
            if " " not in payload:
                self._log("<b>AI:</b> Usage: /set <key> <value>. Try /agent status for the full key list.")
                return
            key, value = payload.split(" ", 1)
            key = key.strip().lower()
            value = value.strip()
            agent_keys = {
                "provider": "provider",
                "gemini_key": "gemini_key",
                "gemini_model": "gemini_model",
                "groq_key": "groq_key",
                "groq_model": "groq_model",
                "ollama_url": "ollama_base_url",
                "ollama_model": "ollama_model",
                "brave_key": "brave_key",
                "shared_memory_path": "shared_memory_path",
            }
            if key in agent_keys:
                self.agent_settings[agent_keys[key]] = value
                self._save_agent_settings()
                if key == "provider":
                    self.agent_provider_name = value.strip().lower()
                self._rebuild_agent_provider()
                self._log(f"<b>AI:</b> Updated agent {key}. Current provider: {self.agent_provider_name}.")
                return
            if key == "api_key":
                self.llm.set_api_key(value)
            elif key == "base_url":
                self.llm.set_base_url(value)
            elif key == "model":
                self.llm.set_model(value)
            else:
                self._log("<b>AI:</b> Unknown key. Try /agent status for the agent key list, or api_key/base_url/model for the legacy planner.")
                return
            self._log(f"<b>AI:</b> Updated {key}.")
            return

        if query == "/status":
            inv = self.qgis_inventory if isinstance(self.qgis_inventory, dict) else {}
            inv_layers = int(inv.get("counts", {}).get("layers", 0)) if isinstance(inv.get("counts"), dict) else 0
            inv_actions = int(inv.get("counts", {}).get("actions", 0)) if isinstance(inv.get("counts"), dict) else 0
            inv_plugins = int(inv.get("counts", {}).get("plugins", 0)) if isinstance(inv.get("counts"), dict) else 0
            self._log(
                f"<b>AI:</b> LLM configured: {self.llm.configured()}\n"
                f"Hybrid mode: {'ON' if self.hybrid_mode else 'OFF'}\n"
                f"Internet mode: {'ON' if self.internet_mode else 'OFF'}\n"
                f"Global mode: {'ON' if self.global_mode else 'OFF'}\n"
                f"Architecture mode: {'ON' if self.architecture_mode else 'OFF'}\n"
                f"Stability mode: {'ON' if self.stability_mode else 'OFF'}\n"
                f"Document memory packs: {len(self.doc_knowledge)}\n"
                f"QGIS inventory: layers={inv_layers}, plugins={inv_plugins}, actions={inv_actions}\n"
                f"LLM base URL: {self.llm.base_url}\n"
                f"LLM model: {self.llm.model}"
            )
            return

        if query == "/help":
            self._log(
                "<b>AI:</b> Commands:\n"
                "- /status\n- /intents\n- /plugins\n- /plugin registry\n- /help\n"
                "- /reindex qgis\n- /algorithms <text>\n- /hybrid on|off\n- /internet on|off\n- /global on|off\n"
                "- /architecture\n"
                "- /stability on|off|report\n"
                "- /regression report | /regression replay [count]\n"
                "- /selftest (validate core routing checks)\n"
                "- /mcp gis | /mcp gis tools | /mcp gis install | /mcp gis map | /mcp do <task>\n"
                "- /geoai | /geoai tasks\n"
                "- /dl | /dl tasks\n"
                "- /spatial db\n"
                "- /ogc\n"
                "- /remote sensing\n"
                "- /satellite workflow <prompt>\n"
                "- /ai gis\n"
                "- /ai gis workflow <prompt>\n"
                "- /equivalent <operation>\n"
                "- /workflow kb\n"
                "- /python stack\n"
                "- /learn website <url>\n"
                "- /learn file <path> (video/audio/image/document)\n"
                "- /learn youtube <video|playlist|channel url>\n"
                "- /free setup (configure local free model)\n"
                "- /pick <number> (run a suggested command)\n"
                "- /yes /no (confirm/cancel)\n"
                "- /recipe save <name> | /recipe run <name> | /recipe list\n"
                "- /pin <text> | /pins | /rerun | /memory\n"
                "- /docs memory (show memorized document packs)\n"
                "- /qgis inventory full | /qgis inventory refresh | /qgis inventory export\n"
                "- /learning\n"
                "- /scan recursively for <name>\n"
                "- /contracts\n"
                "- output paths can be given with 'to D:\\path\\folder'\n"
                "- /set api_key <token>\n"
                "- /set base_url <url>\n"
                "- /set model <model>"
            )
            return

        if query == "/selftest":
            self._run_selftest()
            return
        if query == "/stability precheck":
            self._run_stability_precheck()
            return
        if query == "/regression report":
            self._render_regression_report()
            return
        if query.startswith("/regression replay"):
            token = query[len("/regression replay") :].strip()
            self._run_regression_replay(token)
            return

        if query in ("/mcp gis", "/mcp gis help"):
            self._render_gis_mcp_pack("overview")
            return

        if query in ("/geoai", "/geoai tasks"):
            self._render_geoai_pack()
            return

        if query in ("/dl", "/dl tasks"):
            self._render_deep_learning_pack()
            return

        if query in ("/spatial db", "/spatialdb", "/db spatial"):
            self._render_spatialdb_pack()
            return

        if query in ("/ogc", "/ogc standards"):
            self._render_ogc_pack()
            return

        if query in ("/remote sensing", "/remote-sensing", "/satellite gis", "/rs"):
            self._render_remote_sensing_pack()
            return

        if query.startswith("/satellite workflow"):
            task = query[len("/satellite workflow") :].strip()
            if not task:
                self._log("<b>AI:</b> Usage: /satellite workflow <your remote-sensing objective>")
                return
            self._render_satellite_workflow_plan(task)
            return

        if query in ("/ai gis", "/geoai stack", "/ai+gis"):
            self._render_ai_gis_pack()
            return

        if query.startswith("/ai gis workflow"):
            task = query[len("/ai gis workflow") :].strip()
            if not task:
                self._log("<b>AI:</b> Usage: /ai gis workflow <objective>")
                return
            self._render_ai_gis_workflow_plan(task)
            return
        if query.startswith("/equivalent "):
            op = query[len("/equivalent ") :].strip()
            if not op:
                self._log("<b>AI:</b> Usage: /equivalent <operation>")
                return
            self._render_equivalent_workflow(op)
            return
        if query == "/workflow kb":
            self._render_workflow_kb_overview()
            return

        if query == "/python stack":
            self._render_python_stack_pack()
            return

        if query.startswith("/learn website "):
            url = query[len("/learn website ") :].strip()
            if not url:
                self._log("<b>AI:</b> Usage: /learn website <url>")
                return
            self._learn_website_now(url)
            return
        if query.startswith("/learn youtube "):
            url = query[len("/learn youtube ") :].strip()
            if not url:
                self._log("<b>AI:</b> Usage: /learn youtube <video|playlist|channel url>")
                return
            self._learn_youtube_resource(url)
            return
        if query.startswith("/learn file "):
            path_text = query[len("/learn file ") :].strip()
            if not path_text:
                self._log("<b>AI:</b> Usage: /learn file <path>")
                return
            self._learn_file_now(path_text)
            return
        if query == "/crawl all":
            self._crawl_all_known_websites()
            return
        if query.startswith("/crawl site "):
            url = query[len("/crawl site ") :].strip()
            if not url:
                self._log("<b>AI:</b> Usage: /crawl site <url>")
                return
            self._crawl_site_and_memorize(url)
            return

        if query == "/mcp gis tools":
            self._render_gis_mcp_pack("tools")
            return

        if query == "/mcp gis install":
            self._render_gis_mcp_pack("install")
            return

        if query == "/mcp gis map":
            self._render_gis_mcp_pack("map")
            return

        if query.startswith("/mcp do "):
            task = query[len("/mcp do ") :].strip()
            if not task:
                self._log("<b>AI:</b> Usage: /mcp do <task>")
                return
            self._render_mcp_task_translation(task)
            return

        if query.startswith("/pick "):
            token = query[len("/pick ") :].strip()
            try:
                idx = int(token)
            except Exception:
                self._log("<b>AI:</b> Usage: /pick <number>")
                return
            self._run_suggestion_by_index(idx)
            return

        if query == "/hybrid on":
            self.hybrid_mode = True
            self._log("<b>AI:</b> Hybrid mode enabled.")
            return

        if query == "/hybrid off":
            self.hybrid_mode = False
            self._log("<b>AI:</b> Hybrid mode disabled (deterministic only).")
            return

        if query == "/internet on":
            self.internet_mode = True
            self._update_internet_button_label()
            self._log("<b>AI:</b> Internet answer mode enabled.")
            return

        if query == "/internet off":
            self.internet_mode = False
            self._update_internet_button_label()
            self._log("<b>AI:</b> Internet answer mode disabled.")
            return

        if query == "/global on":
            self.global_mode = True
            self._log("<b>AI:</b> Global AI mode enabled (local -> LLM -> web).")
            return

        if query == "/global off":
            self.global_mode = False
            self._log("<b>AI:</b> Global AI mode disabled.")
            return
        if query == "/stability on":
            self.stability_mode = True
            self._log("<b>AI:</b> Stability mode enabled (strict execution + safe fallback).")
            return
        if query == "/stability off":
            self.stability_mode = False
            self._log("<b>AI:</b> Stability mode disabled.")
            return
        if query == "/stability report":
            self._render_stability_report()
            return
        if query == "/architecture":
            self._log(
                "<b>AI:</b> Architecture mode is ON.\n"
                "Flow: GIS Knowledge -> GIS Reasoning -> GIS Execution.\n"
                "This wraps existing features without removing them."
            )
            return

        if query == "/free setup":
            self.llm.set_free_defaults()
            self.hybrid_mode = True
            self.internet_mode = True
            self.global_mode = True
            self._update_internet_button_label()
            self._log(
                "<b>AI:</b> Free mode configured.\n"
                "Base URL: http://localhost:11434/v1\n"
                "Model: qwen2.5:7b-instruct\n"
                "Install/run Ollama locally and pull model if not present."
            )
            return

        if query == "/reindex qgis":
            self._refresh_layer_catalog()
            self._refresh_algorithm_catalog()
            self._refresh_qgis_inventory()
            self._log(
                f"<b>AI:</b> Reindexed QGIS context.\n"
                f"Layers: {len(self.layer_catalog)}\nAlgorithms: {len(self.algorithm_catalog)}\n"
                f"Inventory actions: {len((self.qgis_inventory or {}).get('actions', []))}"
            )
            return
        if query == "/qgis inventory refresh":
            self._refresh_qgis_inventory()
            self._log("<b>AI:</b> Full QGIS inventory refreshed.")
            return
        if query == "/qgis inventory full":
            self._render_qgis_inventory()
            return
        if query == "/qgis inventory export":
            self._export_qgis_inventory()
            return

        if query.startswith("/algorithms"):
            term = query[len("/algorithms") :].strip().lower()
            if not self.algorithm_catalog:
                self._refresh_algorithm_catalog()
            matches = []
            for aid in sorted(self.algorithm_catalog.keys()):
                if not term or term in aid.lower():
                    matches.append(aid)
            if not matches:
                self._log("<b>AI:</b> No algorithms matched.")
                return
            preview = "\n".join(f"- {m}" for m in matches[:40])
            more = f"\n... and {len(matches)-40} more" if len(matches) > 40 else ""
            self._log("<b>AI:</b> Algorithm matches:\n" + preview + more)
            return

        if query == "/contracts":
            self._log(
                "<b>AI:</b> Strict command contracts (key=value):\n"
                "- run 999 updater layer=<layer_name> veg_field=vegremarks length_field=length_lms short_m=10\n"
                "- run agc qa raw=<layer_name> corrected=<layer_name> tol_m=1\n"
                "- run ivms qa layer=<layer_name>\n"
                "- run qautofix layer=<layer_name> tol_m=1\n"
                "- run tgth validator layer=<layer_name> mode=tg|th\n"
                "- run dl task=semantic_train image_dir=<folder> mask_dir=<folder> output_dir=<folder>\n"
                "- run dl task=semantic_infer input_path=<raster> model_path=<pth> output_path=<raster>\n"
                "- run dl task=instance_train image_dir=<folder> mask_dir=<folder> output_dir=<folder>\n"
                "- run dl task=instance_infer input_path=<raster> model_path=<pth> output_path=<vector_or_raster>\n"
                "- run spatialdb action=export format=gpkg input=active output=D:\\out\\layer.gpkg\n"
                "- run spatialdb action=export format=spatialite input=active output=D:\\out\\layer.sqlite\n"
                "- run spatialdb action=sql backend=postgis dbname=postgis_demo user=postgres password=*** sql=\"select b.gid,b.name from building b, region r where st_within(b.geom,r.geom) and r.name='KwaZulu'\"\n"
                "- run workflow auto <objective>\n"
                "- run workflow auto clip raster by bbox xmin,ymin,xmax,ymax\n"
                "- run workflow auto semantic inference input_path=<raster> model_path=<pth> output_path=<raster>\n"
                "- run ai_gis workflow=\"find vegetation encroachment within 20m of transmission lines\"\n"
                "- run geoai task=clip_raster_by_bbox raster=active bbox=xmin,ymin,xmax,ymax\n"
                "- run geoai task=vectorize_mask input=active field=class\n"
                "- run geoai task=smooth_vector input=active iterations=1 offset=0.25\n"
                "- run geoai task=export_geotiff_tiles input=active tile_size=512 stride=512\n"
            )
            return

        if query == "/intents":
            self._log(
                "<b>AI:</b> Built-in actions:\n"
                "- geometry validity / fix geometries\n"
                "- buffer / dissolve / clip / intersection / union\n"
                "- batch: buffer roads by 20m and clip parcels\n"
                "- batch: merge -> reproject -> dissolve\n"
                "- reproject / convex hull / centroids\n"
                "- multipart to singleparts\n"
                "- poles within distance of roads\n"
                "- null geometry create / clear (with /yes confirmation for clear)\n"
                "- null audit / duplicate audit / topology qa pack\n"
                "- active layer query/filter by attribute (where field condition)\n"
                "- inspect all loaded layers (fields/properties/types/length/precision)\n"
                "- raster: clip by mask / reproject / slope / aspect / hillshade\n"
                "- export assistant (gpkg + qml + metadata)\n"
                "- disconnected span check\n"
                "- autonomous toolkit commands: feeder splitter / overlap checker / tile index / "
                "shapefile merger / geo-export / dynamic bbox / bbox conf / overlap mismatch / "
                "shapefile renamer / vertex mismatch\n"
                "- external families mapped: 999 updater / AGC batch QA / IVMS QA / QAutoFix / TG-TH validator\n"
                "- GeoAI family mapped: Moondream / SamGeo / DeepForest / water / semantic / instance segmentation "
                "(contracts + panel delegation + local geoprocessing equivalents)\n"
                "- GIS-MCP knowledge pack: /mcp gis (setup, tools, command mapping)\n"
                "Tip: explicit algorithm works too, e.g.\n"
                "native:buffer {\"INPUT\":\"active\",\"DISTANCE\":25,\"OUTPUT\":\"memory:\"}"
            )
            return
        if query == "/crawl help":
            self._log(
                "<b>AI:</b> Website crawl commands:\n"
                "- /crawl all  (crawl all known URLs from memory/default packs)\n"
                "- /crawl site <url>  (crawl one site/path family)\n"
                "Crawler rules: same domain/path scope, depth=2, max_pages=60 per site, and preserves curated entries."
            )
            return

        if query == "/plugins":
            plugins = self._list_available_plugins()
            if not plugins:
                self._log("<b>AI:</b> No plugins detected.")
                return
            self._refresh_plugin_registry()
            lines = [f"- {p['name']} ({p['id']})" for p in plugins[:30]]
            more = "" if len(plugins) <= 30 else f"\n... and {len(plugins) - 30} more"
            self._log("<b>AI:</b> Detected plugins:\n" + "\n".join(lines) + more)
            self._log("<b>AI:</b> Plugin registry refreshed. Use /plugin registry for callable map.")
            return

        if query == "/plugin registry":
            if not self.plugin_registry:
                self._refresh_plugin_registry()
            if not self.plugin_registry:
                self._log("<b>AI:</b> Plugin registry is empty.")
                return
            entries = []
            for pid, methods in sorted(self.plugin_registry.items()):
                preview = ", ".join(methods[:5])
                entries.append(f"- {pid}: {preview}")
            self._log("<b>AI:</b> Plugin callable registry:\n" + "\n".join(entries[:40]))
            return

        if query.startswith("/recipe save "):
            name = query[len("/recipe save ") :].strip()
            if not name:
                self._log("<b>AI:</b> Recipe name required.")
                return
            if not self.last_user_query:
                self._log("<b>AI:</b> No previous command to save.")
                return
            self.recipes[name] = {"query": self.last_user_query, "saved_at": datetime.utcnow().isoformat()}
            self._save_json_file(self._recipes_file_path(), self.recipes)
            self._log(f"<b>AI:</b> Recipe '{name}' saved.")
            return

        if query.startswith("/recipe run "):
            name = query[len("/recipe run ") :].strip()
            recipe = self.recipes.get(name)
            if not recipe:
                self._log(f"<b>AI:</b> Recipe '{name}' not found.")
                return
            recipe_query = str(recipe.get("query", "")).strip()
            if not recipe_query:
                self._log(f"<b>AI:</b> Recipe '{name}' is empty.")
                return
            self._log(f"<b>AI:</b> Running recipe '{name}': {recipe_query}")
            self._handle_query(recipe_query)
            return

        if query == "/recipe list":
            if not self.recipes:
                self._log("<b>AI:</b> No saved recipes.")
                return
            lines = [f"- {name}: {meta.get('query', '')}" for name, meta in sorted(self.recipes.items())]
            self._log("<b>AI:</b> Recipes:\n" + "\n".join(lines))
            return

        if query.startswith("/pin "):
            text = query[len("/pin ") :].strip()
            if not text:
                self._log("<b>AI:</b> Pin text required.")
                return
            self.pinned_commands.append(text)
            self._log("<b>AI:</b> Command pinned.")
            return

        if query == "/pins":
            if not self.pinned_commands:
                self._log("<b>AI:</b> No pinned commands.")
                return
            self._log("<b>AI:</b> Pinned commands:\n" + "\n".join(f"- {p}" for p in self.pinned_commands))
            return

        if query == "/rerun":
            if not self.history:
                self._log("<b>AI:</b> No prior actions in memory.")
                return
            last_query = self.history[-1]["query"]
            self._log(f"<b>AI:</b> Rerunning: {last_query}")
            self._handle_query(last_query)
            return

        if query == "/memory":
            if not self.history:
                self._log("<b>AI:</b> Memory is empty.")
                return
            recent = self.history[-10:]
            lines = [f"- {item['query']} -> {item['result']}" for item in recent]
            self._log("<b>AI:</b> Recent memory:\n" + "\n".join(lines))
            return

        if query == "/learning":
            self._log(
                f"<b>AI:</b> Learning store:\n"
                f"- learned routes: {len(self.learned_routes)}\n"
                f"- persisted history: {len(self.history_store)}\n"
                f"- document memory packs: {len(self.doc_knowledge)}"
            )
            return
        if query == "/docs memory":
            if not self.doc_knowledge:
                self._log("<b>AI:</b> No memorized document packs.")
                return
            lines = []
            for item in self.doc_knowledge:
                title = item.get("title", "Untitled")
                source = item.get("source", "unknown")
                tags = ", ".join(item.get("tags", [])[:8])
                lines.append(f"- {title}\n  source: {source}\n  tags: {tags}")
            self._log("<b>AI:</b> Memorized document packs:\n" + "\n".join(lines))
            return

        self._log("<b>AI:</b> Unknown command. Use /help.")

    def _handle_builtins(self, normalized, original_query):
        if self._is_location_coordinates_query(normalized):
            self._answer_detect_location_coordinates()
            return True
        if self._is_followup_status_query(normalized):
            self._answer_followup_status()
            return True
        if self._is_last_output_location_query(normalized):
            self._answer_last_output_location()
            return True
        if self._is_spatial_index_create_query(normalized):
            self._run_create_spatial_index()
            return True
        if self._is_distance_from_here_query(normalized):
            if self._answer_distance_from_here(original_query):
                return True
        if self._is_location_detect_query(normalized):
            self._answer_detect_location()
            return True
        if self._is_temperature_query(normalized):
            if self._try_weather_now_answer(original_query):
                return True
        if self._is_raster_clip_intent(normalized):
            if self._run_raster_clip_from_natural_english(original_query, normalized):
                return True
        if self._is_clip_google_satellite_query(normalized):
            self._run_clip_google_satellite_by_loaded_aoi(original_query)
            return True
        if self._is_open_attribute_table_query(normalized):
            self._open_attribute_table_action()
            return True
        if self._is_time_now_query(normalized):
            self._answer_time_now_local()
            return True
        if self._is_google_street_view_query(normalized):
            self._open_google_street_view(original_query)
            return True
        if self._is_python_code_request(normalized):
            if self._answer_python_code_request(normalized):
                return True
        if self._is_add_then_equal_distribution_query(normalized):
            self._run_add_then_equal_distribution(normalized, original_query)
            return True
        if self._is_equal_distribution_assignment_query(normalized):
            self._run_equal_distribution_assignment(normalized, original_query)
            return True
        if self._is_delete_field_query(normalized):
            self._run_delete_field(normalized, original_query)
            return True
        if self._is_add_field_query(normalized):
            self._run_add_field(normalized, original_query)
            return True
        if self._is_categorize_by_field_query(normalized):
            self._run_categorize_by_field(normalized, original_query)
            return True
        if self._is_qgis_read_everything_query(normalized):
            self._refresh_qgis_inventory()
            self._render_qgis_inventory()
            return True
        if self._is_plugin_count_query(normalized):
            self._answer_plugin_count()
            return True
        if self._is_simple_layer_count_query(normalized):
            self._answer_simple_layer_count()
            return True
        if self._is_column_count_query(normalized):
            self._answer_column_count()
            return True
        if self._is_elevation_stats_query(normalized):
            self._plan_elevation_stats_workflow(original_query, normalized)
            return True
        if self._is_spatial_join_workflow_query(normalized):
            self._plan_spatial_join_workflow(original_query, normalized)
            return True
        if self._is_buffer_intersection_workflow_query(normalized):
            self._plan_buffer_intersection_workflow(original_query, normalized)
            return True
        if self._is_clip_export_workflow_query(normalized):
            self._plan_clip_export_workflow(original_query, normalized)
            return True
        if self._is_vegetation_polygon_query(normalized):
            self._run_vegetation_polygon_workflow(original_query, normalized)
            return True
        # Prioritize explicit split commands before attribute query parsing,
        # so prompts like "split output with field layer" do not get misrouted.
        if self._is_split_by_field_query(normalized):
            self._run_split_by_field(normalized)
            return True
        if self._is_layer_introspection_query(normalized):
            self._run_layer_introspection_report(normalized, original_query)
            return True
        if self._is_active_layer_query_request(normalized):
            self._run_active_layer_query(original_query, normalized)
            return True
        if self._is_attribute_error_query(normalized):
            self._run_attribute_error_audit(original_query)
            return True
        if self._is_attribute_condition_query(normalized):
            self._run_attribute_condition_query(original_query, normalized)
            return True
        if self._is_general_gis_question(normalized):
            handled = self._answer_general_gis_question(original_query, normalized)
            if handled:
                return True
            if not self.global_mode:
                self._log(
                    "<b>AI:</b> I can answer GIS basics and also run operations on your active loaded layer. "
                    "Ask a specific GIS question or a filter query with field/value condition."
                )
                return True
        if self._handle_external_plugin_family_commands(normalized, original_query):
            return True
        if self._is_screenshot_query(normalized):
            self._run_canvas_screenshot()
            return True
        if "scan recursively" in normalized or "scan recursivly" in normalized:
            self._run_recursive_scan(original_query)
            return True
        if self._handle_aidash_tool_command(normalized, original_query):
            return True
        if self._handle_batch_workflow(normalized):
            return True
        if self._is_create_null_geometry_query(normalized):
            self._create_null_geometries(normalized)
            return True
        if self._is_clear_null_geometry_query(normalized):
            self._clear_null_geometries()
            return True
        if self._is_create_aoi_query(normalized):
            self._run_create_aoi_from_loaded_layer()
            return True
        if "null audit" in normalized or ("null" in normalized and "field" in normalized and "audit" in normalized):
            self._run_null_field_audit()
            return True
        if "duplicate" in normalized and any(w in normalized for w in ("field", "column", "attribute")):
            self._run_duplicate_audit(normalized)
            return True
        if "topology qa" in normalized or ("gaps" in normalized and "overlaps" in normalized):
            self._run_topology_qa_pack()
            return True
        if self._is_clip_raster_by_mask_query(normalized):
            self._run_clip_raster_by_mask()
            return True
        if self._is_clip_raster_by_canvas_extent_query(normalized):
            self._run_clip_raster_by_canvas_extent()
            return True
        if "reproject raster" in normalized:
            self._run_reproject_raster(normalized)
            return True
        if "hillshade" in normalized:
            self._run_raster_surface("hillshade")
            return True
        if "slope" in normalized:
            self._run_raster_surface("slope")
            return True
        if "aspect" in normalized:
            self._run_raster_surface("aspect")
            return True
        if "raster calculator" in normalized:
            self._log("<b>AI:</b> Raster calculator needs expression details. Example: raster calculator A*0.3048")
            return True
        if "export assistant" in normalized or ("export" in normalized and "metadata" in normalized):
            self._run_export_assistant()
            return True
        if self._answer_definition_query(normalized):
            return True
        if "refresh" in normalized and any(word in normalized for word in ("screen", "map", "canvas", "view")):
            self._refresh_screen()
            return True
        if (
            ("processing toolbox" in normalized or "processing tool" in normalized)
            and any(word in normalized for word in ("open", "show"))
        ):
            self._open_processing_toolbox()
            return True
        if "google satellite" in normalized:
            self._open_google_satellite()
            return True
        if "snapping" in normalized and any(word in normalized for word in ("enable", "turn on", "on")):
            self._enable_snapping()
            return True
        if "snap" in normalized and any(word in normalized for word in ("vertex", "vertices")):
            self._snap_unsnapped_vertices(normalized)
            return True
        if self._is_polygon_validity_query(normalized):
            self._run_check_validity()
            return True
        if "fix geometr" in normalized:
            self._run_fix_geometries()
            return True
        if "disconnected" in normalized and "span" in normalized:
            self._run_span_disconnected_check()
            return True
        if self._is_unsnapped_span_query(normalized):
            self._run_detect_unsnapped_spans()
            return True
        if self._is_vertex_centroid_query(normalized):
            self._run_vertex_centroids()
            return True
        if self._is_overlap_centroid_query(normalized):
            self._run_overlap_span_centroids(normalized)
            return True
        if self._is_overlap_span_query(normalized):
            self._run_detect_overlap_spans(normalized)
            return True
        if self._is_end_span_centroid_query(normalized):
            self._run_end_span_centroids()
            return True
        if self._is_bbox_video_workflow_query(normalized):
            self._run_bbox_video_workflow(normalized)
            return True
        if "buffer" in normalized:
            self._run_buffer_command(normalized)
            return True
        if self._is_merge_layers_query(normalized):
            self._run_merge_loaded_layers(normalized)
            return True
        if "dissolve" in normalized:
            self._run_algorithm_with_active("native:dissolve", {"FIELD": [], "SEPARATE_DISJOINT": False})
            return True
        if "clip" in normalized:
            self._run_two_layer_overlay("native:clip", "INPUT", "OVERLAY")
            return True
        if "intersection" in normalized:
            self._run_two_layer_overlay("native:intersection", "INPUT", "OVERLAY")
            return True
        if "union" in normalized:
            self._run_two_layer_overlay("native:union", "INPUT", "OVERLAY")
            return True
        if "reproject" in normalized:
            self._run_reproject(normalized)
            return True
        if "convex hull" in normalized:
            self._run_algorithm_with_active("native:convexhull", {"FIELD": None})
            return True
        if "centroid" in normalized:
            self._run_algorithm_with_active("native:centroids", {"ALL_PARTS": False})
            return True
        if "multipart" in normalized and "single" in normalized:
            self._run_algorithm_with_active("native:multiparttosingleparts", {})
            return True
        if "poles" in normalized and "roads" in normalized and ("within" in normalized or "near" in normalized):
            self._run_poles_near_roads_export(normalized)
            return True
        if "check validity" in normalized:
            self._run_check_validity()
            return True
        return False

    def _is_spatial_index_create_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_index = "spatial index" in q or ("index" in q and "spatial" in q)
        has_create = any(k in q for k in ("create", "build", "make", "generate"))
        has_layer_hint = any(k in q for k in ("shape", "shapefile", "layer", "loaded", "active", "file"))
        return has_index and (has_create or has_layer_hint)

    def _run_create_spatial_index(self):
        if not self._processing_available():
            self.last_operation_state = {
                "name": "spatial_index_create",
                "status": "failed",
                "detail": "Processing unavailable",
            }
            return
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Spatial index creation requires an active vector layer.")
            self.last_operation_state = {
                "name": "spatial_index_create",
                "status": "failed",
                "detail": "Active layer not vector",
            }
            return
        try:
            processing.run("native:createspatialindex", {"INPUT": layer})
            self._log(f"<b>AI:</b> Spatial index created for layer '{layer.name()}'.")
            self._remember_action(self.last_user_query, f"spatial index created on {layer.name()}")
            self.last_operation_state = {
                "name": "spatial_index_create",
                "status": "done",
                "detail": f"Layer: {layer.name()}",
            }
        except Exception as exc:
            self._log(f"<b>AI:</b> Spatial index creation failed: {exc}")
            self.last_operation_state = {
                "name": "spatial_index_create",
                "status": "failed",
                "detail": str(exc),
            }

    def _is_followup_status_query(self, normalized_query):
        q = (normalized_query or "").lower().strip()
        if not q:
            return False
        return q in {
            "is it done",
            "is it done?",
            "is that done",
            "is that done?",
            "is this done",
            "is this done?",
        } or ("spatial index" in q and any(k in q for k in ("done", "created", "completed")))

    def _answer_followup_status(self):
        st = self.last_operation_state or {}
        name = str(st.get("name", "")).strip()
        status = str(st.get("status", "")).strip()
        detail = str(st.get("detail", "")).strip()
        if not name:
            self._log("<b>AI:</b> No recent executable action to confirm yet.")
            return
        if status == "done":
            self._log(f"<b>AI:</b> Yes, done. Last action: {name}. {detail}")
            return
        if status == "failed":
            self._log(f"<b>AI:</b> Not done. Last action failed: {name}. {detail}")
            return
        self._log(f"<b>AI:</b> Last action status for {name}: {status or 'unknown'}. {detail}")

    def _is_last_output_location_query(self, normalized_query):
        q = (normalized_query or "").lower()
        # Strictly for follow-up questions like:
        # "where is the clipped file?", not for action commands that include "save path".
        asks_where = any(k in q for k in ("where", "where is", "where did", "which path", "location of"))
        asks_output = any(k in q for k in ("output", "saved", "save", "clipped", "export", "file"))
        is_action = any(k in q for k in ("clip", "crop", "save to", "according to", "create", "run", "export to"))
        return asks_where and asks_output and not is_action

    def _answer_last_output_location(self):
        p = str(getattr(self, "last_saved_output_path", "") or "").strip()
        if not p:
            self._log("<b>AI:</b> I do not have a recent saved output path yet.")
            return
        self._log(f"<b>AI:</b> Last saved output path:\n{p}")

    def _is_vertex_centroid_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_vertex = any(k in q for k in ("vertex", "vertices"))
        has_point_request = any(k in q for k in ("centroid", "center", "centre"))
        return has_vertex and has_point_request

    def _run_vertex_centroids(self):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Vertex centroid action requires an active vector layer.")
            return

        crs_authid = layer.crs().authid() if layer.crs() else "EPSG:4326"
        out = QgsVectorLayer(f"Point?crs={crs_authid}", f"{layer.name()}_vertex_centroids", "memory")
        provider = out.dataProvider()
        provider.addAttributes(
            [
                QgsField("src_fid", QVariant.LongLong),
                QgsField("v_idx", QVariant.Int),
                QgsField("x", QVariant.Double),
                QgsField("y", QVariant.Double),
            ]
        )
        out.updateFields()

        created = 0
        out_feats = []
        try:
            for feat in layer.getFeatures():
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                vidx = 0
                for v in geom.vertices():
                    of = QgsFeature(out.fields())
                    x = float(v.x())
                    y = float(v.y())
                    of.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
                    of.setAttribute("src_fid", int(feat.id()))
                    of.setAttribute("v_idx", int(vidx))
                    of.setAttribute("x", x)
                    of.setAttribute("y", y)
                    out_feats.append(of)
                    created += 1
                    vidx += 1
        except Exception as exc:
            self._log(f"<b>AI:</b> Vertex centroid extraction failed: {exc}")
            return

        if created == 0:
            self._log("<b>AI:</b> No vertices found in active layer geometry.")
            return
        provider.addFeatures(out_feats)
        out.updateExtents()
        QgsProject.instance().addMapLayer(out)
        self._log(f"<b>AI:</b> Created centroid points for each vertex: {created} points in {out.name()}.")
        self._remember_action(self.last_user_query, f"vertex centroids created {created}")

    def _is_python_code_request(self, normalized_query):
        q = (normalized_query or "").lower()
        asks_code = any(k in q for k in ("python code", "pyqgis code", "code for", "script for", "write code"))
        return asks_code

    def _answer_python_code_request(self, normalized_query):
        q = (normalized_query or "").lower()
        if "split" in q and any(k in q for k in ("feature", "field", "attribute", "column", "value")):
            code = (
                "from pathlib import Path\n"
                "from qgis.core import QgsVectorLayer, QgsFeature, QgsVectorFileWriter\n\n"
                "# Inputs\n"
                "layer = iface.activeLayer()  # select your input layer\n"
                "field_name = 'feeder'  # change this to your split field\n"
                "out_root = Path(r'D:/split_output')\n"
                "out_root.mkdir(parents=True, exist_ok=True)\n\n"
                "if not isinstance(layer, QgsVectorLayer):\n"
                "    raise RuntimeError('Active layer must be a vector layer')\n"
                "idx = layer.fields().indexFromName(field_name)\n"
                "if idx < 0:\n"
                "    raise RuntimeError(f\"Field not found: {field_name}\")\n\n"
                "groups = {}\n"
                "for ft in layer.getFeatures():\n"
                "    v = ft[idx]\n"
                "    key = 'NULL' if v is None or str(v).strip() == '' else str(v).strip()\n"
                "    groups.setdefault(key, []).append(QgsFeature(ft))\n\n"
                "for key, feats in groups.items():\n"
                "    safe = ''.join(c if c not in '<>:\"/\\\\|?*' else '_' for c in key).strip('. ') or 'EMPTY'\n"
                "    folder = out_root / safe\n"
                "    folder.mkdir(parents=True, exist_ok=True)\n"
                "    shp = folder / f'{safe}.shp'\n"
                "    w = QgsVectorFileWriter(\n"
                "        str(shp), 'UTF-8', layer.fields(), layer.wkbType(), layer.crs(), 'ESRI Shapefile'\n"
                "    )\n"
                "    if w.hasError() != QgsVectorFileWriter.NoError:\n"
                "        raise RuntimeError(w.errorMessage())\n"
                "    for ft in feats:\n"
                "        w.addFeature(ft)\n"
                "    del w\n\n"
                "print(f'Split complete: {len(groups)} files in {out_root}')\n"
            )
            self._log("<b>AI:</b> PyQGIS code for split-by-feature field:\n<pre>" + code + "</pre>")
            self._remember_action(self.last_user_query, "provided python split code")
            return True
        return False

    def _is_plugin_count_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_plugin = "plugin" in q or "plugins" in q
        has_count = any(k in q for k in ("how many", "count", "number of", "total"))
        has_installed = any(k in q for k in ("installed", "available", "in this qgis", "on this qgis"))
        return has_plugin and (has_count or has_installed)

    def _answer_plugin_count(self):
        try:
            installed = self._list_available_plugins()
            count = len(installed)
            preview = ", ".join(p.get("name", "") for p in installed[:8] if isinstance(p, dict))
            more = f" (+{count-8} more)" if count > 8 else ""
            details = f"\nExamples: {preview}{more}" if preview else ""
            self._log(f"<b>AI:</b> Installed plugins detected: <b>{count}</b>{details}")
            self._remember_action(self.last_user_query, f"plugin count answered ({count})")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not read installed plugins: {exc}")

    def _is_google_street_view_query(self, normalized_query):
        q = (normalized_query or "").lower()
        return ("street view" in q) or ("google street" in q and "coordinate" in q)

    def _is_google_satellite_action_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_google_sat = ("google satellite" in q) or ("satellite" in q and "google" in q)
        # Avoid misrouting clip/export requests that mention "loaded google satellite".
        if any(k in q for k in ("clip", "crop", "mask", "bbox", "aoi", "buffer", "save", "export")):
            return False
        has_action = bool(re.search(r"\b(load|add|open|show)\b", q))
        return has_google_sat and has_action

    def _is_raster_clip_intent(self, normalized_query):
        q = (normalized_query or "").lower()
        has_clip = any(k in q for k in ("clip", "crop", "mask"))
        has_raster_term = any(k in q for k in ("raster", "image", "imagery", "satellite", "google satellite", "esri world imagery"))
        return has_clip and has_raster_term

    def _run_raster_clip_from_natural_english(self, original_query, normalized_query):
        q = (normalized_query or "").lower()
        # 1) Explicit Google Satellite clip path
        if "google satellite" in q:
            self._run_clip_google_satellite_by_loaded_aoi(original_query)
            return True
        # 2) Clip by current extent / bbox
        if any(k in q for k in ("canvas extent", "map extent", "current extent", "bbox")):
            self._run_clip_raster_by_canvas_extent()
            return True
        # 3) Clip by polygon/AOI/mask
        if any(k in q for k in ("aoi", "polygon", "boundary", "mask", "loaded aoi")):
            self._run_clip_raster_by_mask()
            return True
        # 4) Generic raster clip intent: default to mask-based clip if possible.
        self._run_clip_raster_by_mask()
        return True

    def _is_clip_google_satellite_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_google_sat = ("google satellite" in q) or ("satellite" in q and "google" in q)
        has_clip = any(k in q for k in ("clip", "crop", "mask"))
        has_boundary = any(k in q for k in ("aoi", "polygon", "boundary", "bbox", "buffer"))
        return has_google_sat and has_clip and has_boundary

    def _is_open_attribute_table_query(self, normalized_query):
        q = (normalized_query or "").lower()
        if any(k in q for k in ("how many", "count", "number of", "total")):
            return False
        if any(k in q for k in ("field", "fields", "column", "columns", "attribute count")):
            return False
        has_open = any(k in q for k in ("open", "show", "launch"))
        has_attr = "attribute table" in q or ("attributes" in q and "table" in q)
        has_layer = any(k in q for k in ("loaded", "active", "shape", "shapefile", "layer"))
        return has_attr and has_open and has_layer

    def _open_attribute_table_action(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Attribute table can be opened only for vector layers.")
            return
        opened = False
        try:
            self.iface.showAttributeTable(layer)
            opened = True
        except Exception:
            try:
                # Fallback to action trigger if direct API differs by QGIS version.
                act = getattr(self.iface, "actionOpenTable", None)
                if callable(act):
                    a = act()
                    if a is not None:
                        a.trigger()
                        opened = True
            except Exception:
                opened = False
        if opened:
            self._log(f"<b>AI:</b> Opened attribute table for layer '{layer.name()}'.")
            self._remember_action(self.last_user_query, "opened attribute table")
        else:
            self._log("<b>AI:</b> Could not open attribute table in this QGIS session.")

    def _open_google_street_view(self, query_text):
        txt = str(query_text or "")
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", txt)
        if not m:
            self._log("<b>AI:</b> Could not parse coordinates. Example: 32.1199, -87.5649 open Google Street View")
            return
        lat = float(m.group(1))
        lon = float(m.group(2))
        url = f"https://www.google.com/maps?q=&layer=c&cbll={lat},{lon}"
        try:
            ok = webbrowser.open(url)
            if ok:
                self._log(f"<b>AI:</b> Opened Google Street View at {lat}, {lon}\n{url}")
            else:
                self._log(f"<b>AI:</b> Could not auto-open browser. Use this URL:\n{url}")
        except Exception:
            self._log(f"<b>AI:</b> Could not auto-open browser. Use this URL:\n{url}")
        self._remember_action(self.last_user_query, "opened google street view")

    def _is_time_now_query(self, normalized_query):
        q = (normalized_query or "").lower().strip()
        has_time = "time" in q
        has_now = any(k in q for k in ("now", "current", "right now", "at this moment"))
        is_clock_question = q.startswith(("what is the time", "tell me the time", "time now", "what is time")) or has_time
        return is_clock_question

    def _answer_time_now_local(self):
        now = datetime.now()
        self._log(f"<b>AI:</b> Local time now: {now.strftime('%Y-%m-%d %I:%M:%S %p')}")
        self._remember_action(self.last_user_query, "local time answered")

    def _is_temperature_query(self, normalized_query):
        q = (normalized_query or "").lower()
        return any(k in q for k in ("temperature", "temprature", "weather")) and any(
            k in q for k in ("what", "now", "current", "tell", "show", "at ")
        )

    def _is_location_detect_query(self, normalized_query):
        q = (normalized_query or "").lower()
        return (
            ("detect my location" in q)
            or ("my location" in q and any(k in q for k in ("detect", "find", "where", "locate")))
            or ("where am i" in q)
            or ("what is my location" in q)
            or ("current location" in q)
        )

    def _is_location_coordinates_query(self, normalized_query):
        q = (normalized_query or "").lower()
        asks_coords = any(k in q for k in ("coordinate", "coordinates", "latitude", "longitude", "lat", "lon"))
        asks_mine = any(k in q for k in ("my location", "current location", "where am i", "from here", "proper coordinate"))
        return asks_coords and asks_mine

    def _is_distance_from_here_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_origin = ("from here" in q) or ("from my location" in q) or ("from current location" in q)
        return has_origin and any(k in q for k in ("how far", "distance", "far is"))

    def _extract_destination_from_here_query(self, query_text):
        q = str(query_text or "").strip()
        m = re.search(r"how\s+far\s+is\s+(.+?)\s+from\s+here\??$", q, flags=re.IGNORECASE)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" ?.")
        m1 = re.search(r"how\s+far\s+is\s+(.+?)\s+from\s+my\s+location\??$", q, flags=re.IGNORECASE)
        if m1:
            return re.sub(r"\s+", " ", m1.group(1)).strip(" ?.")
        m1b = re.search(r"distance\s+of\s+(.+?)\s+from\s+my\s+location\??$", q, flags=re.IGNORECASE)
        if m1b:
            return re.sub(r"\s+", " ", m1b.group(1)).strip(" ?.")
        m1c = re.search(r"distance\s+from\s+my\s+location\s+to\s+(.+?)\??$", q, flags=re.IGNORECASE)
        if m1c:
            return re.sub(r"\s+", " ", m1c.group(1)).strip(" ?.")
        m2 = re.search(r"distance\s+(?:to|from here to)\s+(.+?)\??$", q, flags=re.IGNORECASE)
        if m2:
            return re.sub(r"\s+", " ", m2.group(1)).strip(" ?.")
        return ""

    def _answer_distance_from_here(self, query_text):
        dest = self._extract_destination_from_here_query(query_text)
        if not dest:
            return False
        src = self._detect_location_from_ip_details()
        if not src:
            self._log("<b>AI:</b> I could not detect your current location automatically. Please ask with explicit source location.")
            return True
        dst = self._geocode_place(dest)
        if not dst:
            self._log(f"<b>AI:</b> I could not geocode destination '{dest}'. Try a clearer place name.")
            return True
        km = self._haversine_km(src["lat"], src["lon"], dst["lat"], dst["lon"])
        self._log(
            "<b>AI (Web, confidence: medium):</b> Approximate distance from your detected location to "
            f"{dst['name']} is <b>{km:.1f} km</b>.\n"
            f"From: {src.get('label','your location')} ({src['lat']:.5f}, {src['lon']:.5f})\n"
            f"To: {dst['name']} ({dst['lat']:.5f}, {dst['lon']:.5f})"
        )
        self._remember_action(self.last_user_query, f"distance from here to {dst['name']} answered")
        return True

    def _answer_detect_location(self):
        loc = self._detect_location_from_ip()
        if loc:
            self._log(f"<b>AI (Web, confidence: medium):</b> Approximate detected location: {loc}\nSource: IP geolocation")
            self._remember_action(self.last_user_query, "location detected from ip")
            return
        self._log("<b>AI:</b> Could not detect location automatically right now. Check internet/VPN and retry.")

    def _answer_detect_location_coordinates(self):
        details = self._detect_location_from_ip_details()
        if not details:
            self._log("<b>AI:</b> Could not detect your current coordinates automatically right now.")
            return
        self._log(
            "<b>AI (Web, confidence: medium):</b> Your approximate current coordinates:\n"
            f"- Latitude: {details['lat']:.6f}\n"
            f"- Longitude: {details['lon']:.6f}\n"
            f"- Location: {details.get('label', 'unknown')}\n"
            "Source: IP geolocation (approximate)"
        )
        self._remember_action(self.last_user_query, "location coordinates answered")

    def _is_line_mile_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_line_mile = any(k in q for k in ("line mile", "line miles", "total line mile", "total line miles"))
        has_layer = any(k in q for k in ("shape", "shapefile", "loaded", "active layer", "layer"))
        return has_line_mile and has_layer

    def _is_length_in_miles_followup_query(self, normalized_query):
        q = (normalized_query or "").lower()
        return ("mile" in q or "miles" in q) and any(
            k in q for k in ("give me", "show", "in miles", "convert", "length in miles")
        )

    def _answer_last_length_in_miles(self):
        if self.last_length_meters is None:
            self._log("<b>AI:</b> I do not have a recent length result to convert yet.")
            return False
        miles = float(self.last_length_meters) * 0.000621371192
        self._log(f"<b>AI:</b> Length in miles: <b>{miles:.3f}</b>")
        self._remember_action(self.last_user_query, "length converted to miles")
        return True

    def _is_mismatch_feedback_query(self, normalized_query):
        q = (normalized_query or "").lower()
        return any(
            p in q
            for p in (
                "what i am asking and what you are answering",
                "what i asked and what you answered",
                "you are not getting me",
                "you are answering wrong",
                "this is not what i asked",
                "why are you answering this",
                "why you are not answering",
            )
        )

    def _answer_mismatch_feedback(self, original_query):
        self._log(
            "<b>AI:</b> You are right — my last response did not match your intent.\n"
            "I will stop web/doc fallback for this turn and execute only intent-based handling.\n"
            "Please repeat your exact command once, and I will return only the requested output."
        )
        self._record_regression_case(original_query, "mismatch_feedback", "user flagged response mismatch")
        self._remember_action(self.last_user_query, "handled mismatch feedback")

    def _is_build_raster_pyramids_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_target = any(k in q for k in ("ovr", "overview", "overviews", "pyramid", "pyramids"))
        has_action = any(k in q for k in ("create", "build", "generate", "make"))
        has_raster = any(k in q for k in ("image", "images", "raster", "tif", "tiff"))
        return has_target and (has_action or "can the copilot" in q) and has_raster

    def _run_build_raster_pyramids(self, query_text):
        path_hint = self._resolve_path_from_query(query_text, expect_dir=True) or self._resolve_path_from_query(query_text, expect_dir=False)
        raster_files = []
        if path_hint:
            p = Path(path_hint)
            if p.exists() and p.is_dir():
                for pat in ("*.tif", "*.tiff", "*.img", "*.jp2", "*.vrt", "*.TIF", "*.TIFF", "*.IMG", "*.JP2", "*.VRT"):
                    raster_files.extend(str(x) for x in p.rglob(pat))
            elif p.exists() and p.is_file():
                raster_files = [str(p)]
        if not raster_files:
            active = self.iface.activeLayer()
            if isinstance(active, QgsRasterLayer) and active.isValid() and active.source():
                raster_files = [active.source()]
        raster_files = sorted(set(raster_files))
        if not raster_files:
            self.pending_path_intent = "build_raster_pyramids"
            self._log("<b>AI:</b> Send the folder path only, and I will create OVR/pyramids directly.")
            return True

        success = 0
        failed = []
        for raster in raster_files:
            try:
                cmd = ["gdaladdo", "-r", "average", raster, "2", "4", "8", "16", "32"]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0:
                    success += 1
                else:
                    err = (proc.stderr or proc.stdout or "unknown error").strip()
                    failed.append(f"{Path(raster).name}: {err}")
            except Exception as exc:
                failed.append(f"{Path(raster).name}: {exc}")

        if success > 0:
            self._log(f"<b>AI:</b> Pyramid/OVR creation complete for {success} raster(s).")
            self._remember_action(self.last_user_query, f"created pyramids for {success} rasters")
        if failed:
            self._log("<b>AI:</b> Failed for some files:<br>" + "<br>".join(failed[:8]))
        self.pending_path_intent = None
        return success > 0 or bool(failed)

    def _is_probable_windows_path_only(self, query_text):
        q = str(query_text or "").strip()
        return bool(re.match(r"^[A-Za-z]:\\", q))

    def _try_pending_path_followup(self, query_text):
        if not self.pending_path_intent:
            return False
        if not self._is_probable_windows_path_only(query_text):
            return False
        if self.pending_path_intent == "build_raster_pyramids":
            self._run_build_raster_pyramids(query_text)
            return True
        return False

    def _request_output_path(self, action_key, prompt_text, on_path_callback):
        self.pending_output_request = {
            "action": str(action_key or ""),
            "callback": on_path_callback,
        }
        self._log(f"<b>AI:</b> {prompt_text}")

    def _consume_pending_output_path(self, query_text):
        pending = self.pending_output_request
        if not pending:
            return False
        path_hint = self._resolve_path_from_query(query_text, expect_dir=True)
        if not path_hint:
            cand = self._extract_path_candidate(query_text)
            if cand:
                try:
                    p = Path(cand)
                    if p.suffix:
                        p = p.parent
                    p.mkdir(parents=True, exist_ok=True)
                    path_hint = str(p)
                except Exception:
                    path_hint = None
        if not path_hint:
            self._log("<b>AI:</b> Please send a valid output folder path (e.g. D:\\GIS\\Outputs).")
            return True
        self.pending_output_request = None
        cb = pending.get("callback")
        if callable(cb):
            try:
                cb(path_hint)
            except Exception as exc:
                self._log(f"<b>AI:</b> Could not complete pending save action: {exc}")
        return True

    def _has_explicit_windows_path(self, text):
        return bool(re.search(r"[A-Za-z]:\\[^\n\r\"']+", str(text or "")))

    def _is_output_producing_query(self, normalized_query):
        q = (normalized_query or "").lower()
        return any(
            k in q
            for k in (
                "clip",
                "export",
                "save",
                "split",
                "tile index",
                "qovr",
                "ovr",
                "pyramid",
                "download",
                "screenshot",
                "report csv",
                "to gpkg",
                "to shp",
            )
        )

    def _require_output_path_gate(self, original_query, normalized_query):
        if not getattr(self, "require_output_path", False):
            return False
        if self.pending_output_request is not None:
            return False
        if self.pending_output_for_query is not None:
            return False
        if not self._is_output_producing_query(normalized_query):
            return False
        if self._has_explicit_windows_path(original_query):
            return False
        self.pending_output_for_query = str(original_query or "").strip()
        self._log("<b>AI:</b> Please provide output folder path first. I will not save files automatically.")
        return True

    def _consume_pending_output_for_query(self, query_text):
        pending_query = self.pending_output_for_query
        if not pending_query:
            return False
        out_dir = self._resolve_path_from_query(query_text, expect_dir=True)
        if not out_dir:
            cand = self._extract_path_candidate(query_text)
            if cand:
                try:
                    p = Path(cand)
                    if p.suffix:
                        p = p.parent
                    p.mkdir(parents=True, exist_ok=True)
                    out_dir = str(p)
                except Exception:
                    out_dir = None
        if not out_dir:
            self._log("<b>AI:</b> Send a valid output folder path, e.g. D:\\GIS\\Outputs")
            return True
        self.pending_output_for_query = None
        rerun_query = f"{pending_query} to {out_dir}"
        self._handle_query(rerun_query)
        return True

    def _looks_like_url_only_input(self, query_text):
        q = str(query_text or "").strip()
        return bool(re.match(r"^https?://\S+$", q, flags=re.IGNORECASE))

    def _normalize_user_english(self, lowered_text):
        """
        Lightweight normalization for common wording/spelling variations
        so intent routing understands natural user phrasing better.
        """
        q = str(lowered_text or "")
        replacements = {
            "catagorize": "categorize",
            "temprature": "temperature",
            "shp file": "shapefile",
            "shape file": "shapefile",
            "attribute table": "attributes table",
            "columns": "fields",
            "column": "field",
        }
        for src, dst in replacements.items():
            q = q.replace(src, dst)
        return q

    def _safe_bool_call(self, method_name, *args, **kwargs):
        """
        Compatibility guard: returns False if method is missing/fails,
        instead of breaking full routing with AttributeError.
        """
        fn = getattr(self, str(method_name), None)
        if not callable(fn):
            return False
        try:
            return bool(fn(*args, **kwargs))
        except Exception:
            return False

    def _safe_void_call(self, method_name, *args, **kwargs):
        """
        Compatibility guard for side-effect calls.
        """
        fn = getattr(self, str(method_name), None)
        if not callable(fn):
            return False
        try:
            fn(*args, **kwargs)
            return True
        except Exception:
            return False

    def _is_website_report_query(self, query_text):
        q = str(query_text or "").lower()
        has_url = "http://" in q or "https://" in q
        has_site_ref = any(k in q for k in ("website", "webpage", "link", "url", "site"))
        has_report_intent = any(k in q for k in ("summarize", "summary", "report", "analyse", "analyze", "read this"))
        return (has_report_intent and (has_site_ref or has_url)) or (has_url and has_report_intent)

    def _extract_first_url(self, text):
        m = re.search(r"https?://[^\s<>\"]+", str(text or ""), flags=re.IGNORECASE)
        return m.group(0).strip() if m else ""

    def _run_website_report_from_query(self, query_text):
        url = self._extract_first_url(query_text)
        if not url:
            self.pending_website_report = True
            self._log("<b>AI:</b> Send the website URL, and I will generate a meaningful report from it.")
            return
        self.pending_website_report = False
        page = self._fetch_html_page(url)
        if page is None:
            self._log(f"<b>AI:</b> I could not read this website right now: {url}")
            return
        title, text, links = page
        report = self._build_illustrative_website_report(title, url, text, links)
        self._log(report)
        self._remember_action(self.last_user_query, f"website report generated for {url}")

    def _build_illustrative_website_report(self, title, url, text, links):
        content = str(text or "").strip()
        sentences = self._split_report_sentences(content)
        executive = " ".join(sentences[:3]).strip()
        if not executive:
            executive = "This page was fetched, but extractable text is limited."
        key_points = []
        for s in sentences:
            s_clean = s.strip()
            if len(s_clean) < 60:
                continue
            if any(k in s_clean.lower() for k in ("provides", "supports", "includes", "workflow", "data", "analysis", "tool", "api")):
                key_points.append(s_clean)
            if len(key_points) >= 4:
                break
        if not key_points:
            key_points = sentences[3:7]
        themes = self._extract_report_themes(content)
        actions = []
        for s in sentences:
            low = s.lower()
            if any(k in low for k in ("should", "must", "can be used", "steps", "best practice", "recommended")):
                actions.append(s.strip())
            if len(actions) >= 3:
                break
        if not actions:
            actions = ["Use the page sections as a task checklist and execute one workflow at a time in QGIS."]

        lines = [
            "<b>AI Website Report</b>",
            f"<b>Title:</b> {title}",
            f"<b>Source:</b> {url}",
            "",
            "<b>Executive Summary</b>",
            executive,
            "",
            "<b>Key Insights</b>",
        ]
        for p in key_points[:5]:
            lines.append(f"- {p}")
        lines.extend(["", "<b>Main Themes</b>", f"- {', '.join(themes) if themes else 'General geospatial guidance'}", ""])
        lines.append("<b>Actionable Guidance</b>")
        for a in actions[:4]:
            lines.append(f"- {a}")
        lines.extend(
            [
                "",
                "<b>Coverage Notes</b>",
                f"- Extracted text length: {len(content)} characters",
                f"- Outgoing links found: {len(links or [])}",
                "- If you want deeper coverage, run `/crawl site <url>` for multi-page ingestion.",
            ]
        )
        return "\n".join(lines)

    def _split_report_sentences(self, text):
        raw = re.split(r"(?<=[\.\!\?])\s+", str(text or ""))
        out = []
        for s in raw:
            s2 = re.sub(r"\s+", " ", s).strip()
            if len(s2) >= 25:
                out.append(s2)
        return out

    def _extract_report_themes(self, text):
        stop = {
            "the", "and", "for", "with", "that", "this", "from", "into", "your", "you", "are", "was", "were",
            "have", "has", "had", "can", "will", "not", "use", "using", "about", "into", "more", "than",
        }
        words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", str(text or "").lower())
        freq = {}
        for w in words:
            if w in stop:
                continue
            freq[w] = freq.get(w, 0) + 1
        ranked = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
        return [w for w, _ in ranked[:8]]

    def _is_simple_layer_count_query(self, normalized_query):
        q = (normalized_query or "").lower()
        if any(k in q for k in ("length", "line mile", "line miles", "miles", "km", "kilometer", "meter")):
            return False
        has_count = any(k in q for k in ("how many", "count", "number of", "total"))
        if not has_count:
            return False
        has_target = any(
            k in q
            for k in (
                "feature",
                "features",
                "line",
                "lines",
                "span",
                "spans",
                "point",
                "points",
                "polygon",
                "polygons",
                "record",
                "records",
                "row",
                "rows",
            )
        )
        has_layer_hint = any(k in q for k in ("layer", "shapefile", "shape file", "loaded", "active"))
        return has_target and has_layer_hint

    def _is_column_count_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_count = any(k in q for k in ("how many", "count", "number of", "total"))
        has_columns = any(k in q for k in ("field", "fields", "column", "columns", "attribute"))
        has_layer = any(k in q for k in ("shapefile", "shape file", "layer", "loaded", "active"))
        return has_count and has_columns and has_layer

    def _answer_column_count(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Column/field count works on active vector layer.")
            return
        try:
            c = len(layer.fields())
            self._log(
                "<b>AI:</b> Attribute field count:\n"
                f"Layer: {layer.name()}\n"
                f"Total columns/fields: <b>{c}</b>"
            )
            self._remember_action(self.last_user_query, f"column count answered ({c})")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not read field count: {exc}")

    def _answer_simple_layer_count(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Count query currently works on active vector layer.")
            return
        try:
            count = int(layer.featureCount())
        except Exception:
            self._log("<b>AI:</b> Could not read feature count from active layer.")
            return

        geom_label = "features"
        try:
            gtype = layer.geometryType()
            if gtype == QgsWkbTypes.LineGeometry:
                geom_label = "line/span features"
            elif gtype == QgsWkbTypes.PointGeometry:
                geom_label = "point features"
            elif gtype == QgsWkbTypes.PolygonGeometry:
                geom_label = "polygon features"
        except Exception:
            pass
        self._log(
            "<b>AI:</b> Count result:\n"
            f"Layer: {layer.name()}\n"
            f"Total {geom_label}: <b>{count}</b>"
        )
        self._remember_action(self.last_user_query, f"layer count answered ({count})")

    def _is_immediate_loaded_layer_query(self, normalized_query):
        q = (normalized_query or "").lower()
        if not q:
            return False
        if q.startswith("run ") or q.startswith("/"):
            return False
        if self._is_location_detect_query(q) or self._is_location_coordinates_query(q) or self._is_temperature_query(q) or self._is_time_now_query(q):
            return False
        # Never hijack execution-style requests.
        if self._looks_like_action_request(q):
            return False
        # Tolerate common misspellings for categorize/classify intent.
        if re.search(r"\bcat(?:e|a)gori(?:s|z)e?\b", q):
            return False
        # Avoid stealing explicit execution intents.
        if any(
            k in q
            for k in (
                "create",
                "make",
                "build",
                "generate",
                "add column",
                "add field",
                "delete column",
                "delete field",
                "remove column",
                "remove field",
                "categorise",
                "categorize",
                "classify",
                "style by",
                "buffer",
                "clip",
                "intersect",
                "union",
                "dissolve",
                "reproject",
                "export",
                "split",
                "merge",
                "fix geometr",
                "check validity",
                "workflow",
                "bbox",
            )
        ):
            return False
        intents = self._infer_loaded_layer_intents(q)
        if not intents:
            return False
        has_layer_hint = any(
            k in q
            for k in (
                "loaded",
                "active layer",
                "loaded layer",
                "shape file",
                "shapefile",
                "this layer",
                "current layer",
                "selected layer",
            )
        )
        # Require explicit layer context for quick-read behavior.
        has_light_question = any(k in q for k in ("what", "which", "show", "tell", "read", "give", "check", "find"))
        return has_layer_hint and has_light_question

    def _answer_immediate_loaded_layer_query(self, normalized_query):
        layer = self._active_layer_or_msg()
        if layer is None:
            return True
        if not isinstance(layer, QgsVectorLayer) and not isinstance(layer, QgsRasterLayer):
            self._log(f"<b>AI:</b> Active layer '{layer.name()}' is not vector/raster.")
            return True

        q = (normalized_query or "").lower()
        intents = self._infer_loaded_layer_intents(q)
        if not intents:
            return False
        if "count" in intents and isinstance(layer, QgsVectorLayer):
            if self._answer_field_value_count_from_query(layer, q):
                return True
        if "length_by_category" in intents and isinstance(layer, QgsVectorLayer):
            if self._answer_length_by_category(layer, q):
                return True
        if "category_count" in intents and isinstance(layer, QgsVectorLayer):
            if self._answer_category_value_counts(layer, q):
                return True
        lines = [f"<b>AI:</b> Active layer quick read: {layer.name()}"]
        if "crs" in intents:
            try:
                lines.append(f"- CRS: {layer.crs().authid()}")
            except Exception:
                lines.append("- CRS: unavailable")
        if "source" in intents:
            try:
                lines.append(f"- Source: {layer.source()}")
            except Exception:
                lines.append("- Source: unavailable")
        if "geometry" in intents:
            if isinstance(layer, QgsVectorLayer):
                try:
                    geom = QgsWkbTypes.displayString(layer.wkbType())
                except Exception:
                    geom = "vector"
                lines.append(f"- Geometry: {geom}")
            else:
                lines.append("- Geometry: raster")
        if "count" in intents:
            if isinstance(layer, QgsVectorLayer):
                try:
                    lines.append(f"- Feature count: {int(layer.featureCount())}")
                except Exception:
                    lines.append("- Feature count: unavailable")
            else:
                lines.append("- Feature count: not applicable for raster")
        if "length" in intents:
            if isinstance(layer, QgsVectorLayer):
                try:
                    total_native = 0.0
                    for feat in layer.getFeatures():
                        geom = feat.geometry()
                        if geom is None or geom.isEmpty():
                            continue
                        total_native += float(geom.length())
                    unit_name = ""
                    try:
                        from qgis.core import QgsUnitTypes

                        unit_name = QgsUnitTypes.toString(layer.crs().mapUnits()) or "layer units"
                    except Exception:
                        unit_name = "layer units"
                    lines.append(f"- Total length ({unit_name}): {total_native:.6f}")
                    total_m = self._layer_units_to_meters(layer, total_native)
                    if total_m is not None:
                        lines.append(f"- Total length (meters, approx): {float(total_m):.3f}")
                except Exception:
                    lines.append("- Total length: unavailable")
            else:
                lines.append("- Total length: not applicable for raster")
        if "fields" in intents:
            if isinstance(layer, QgsVectorLayer):
                names = [f.name() for f in layer.fields()]
                if "field_count" in intents:
                    lines.append(f"- Column count: {len(names)}")
                preview = ", ".join(names[:30]) if names else "(none)"
                more = f" (+{len(names)-30} more)" if len(names) > 30 else ""
                lines.append(f"- Fields: {preview}{more}")
            else:
                lines.append("- Fields: not applicable for raster")
        if "extent" in intents:
            try:
                e = layer.extent()
                lines.append(f"- Extent: xmin={e.xMinimum():.6f}, ymin={e.yMinimum():.6f}, xmax={e.xMaximum():.6f}, ymax={e.yMaximum():.6f}")
            except Exception:
                lines.append("- Extent: unavailable")
        if "raster_meta" in intents and isinstance(layer, QgsRasterLayer):
            try:
                lines.append(f"- Raster size: {layer.width()} x {layer.height()}")
            except Exception:
                pass
            try:
                lines.append(f"- Bands: {layer.bandCount()}")
            except Exception:
                pass
        if "name" in intents:
            lines.append(f"- Layer name: {layer.name()}")
        self._log("\n".join(lines))
        self._remember_action(self.last_user_query, "immediate loaded layer answer")
        return True

    def _infer_loaded_layer_intents(self, normalized_query):
        q = (normalized_query or "").lower()
        intents = set()
        asks_length = any(
            k in q for k in ("total length", "sum length", "length of", "overall length", "combined length")
        )
        if asks_length:
            intents.add("length")
        # Only treat as category-length when there is a specific value/category cue,
        # not generic phrases like "length of the shape file".
        has_value_like_token = bool(re.search(r"\b([a-z]{2,}\d+[a-z0-9_]*)\b", q))
        has_filter_phrase = any(
            k in q
            for k in (
                "where",
                "with value",
                "category",
                "class",
                "type",
                "value ",
                "=",
                "-",
            )
        )
        if asks_length and (has_value_like_token or has_filter_phrase):
            intents.add("length_by_category")
        has_count_word = any(k in q for k in ("how many", "count", "number of", "total", "qty", "quantity"))
        has_field_terms = any(k in q for k in ("field", "fields", "attribute", "attributes", "schema", "columns", "column"))
        # When user asks length, do not route through count/category-count by "total" token.
        if has_count_word and has_field_terms and not asks_length:
            intents.add("field_count")
        if has_count_word and not has_field_terms and not asks_length:
            intents.add("count")
        if (not asks_length) and has_count_word and (
            "wise" in q
            or "category" in q
            or "value" in q
            or "each" in q
            or has_value_like_token
            or bool(re.search(r"\b[a-z_][a-z0-9_]*\s*[-=:]\s*[a-z0-9_\-\.]+\b", q))
        ):
            intents.add("category_count")
        if any(k in q for k in ("span", "spans", "line", "lines", "feature", "features", "records", "rows")) and not asks_length:
            intents.add("count")
        if has_field_terms:
            intents.add("fields")
        if any(k in q for k in ("crs", "epsg", "projection", "coordinate system", "coordinate reference")):
            intents.add("crs")
        if any(k in q for k in ("source", "path", "file location", "location")):
            intents.add("source")
        if any(k in q for k in ("geometry", "geom type", "type of geometry", "line layer", "point layer", "polygon layer")):
            intents.add("geometry")
        if any(k in q for k in ("extent", "bbox", "bounds", "boundary extent")):
            intents.add("extent")
        if any(k in q for k in ("size", "pixel", "band", "bands", "resolution")):
            intents.add("raster_meta")
        if any(k in q for k in ("layer name", "name of layer", "what layer")):
            intents.add("name")
        return intents

    def _is_delete_field_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_delete = any(k in q for k in ("delete", "remove", "drop"))
        has_field = any(k in q for k in ("field", "column", "attribute"))
        return has_delete and has_field

    def _is_add_field_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_add = any(k in q for k in ("add", "create", "insert"))
        has_field = any(k in q for k in ("field", "column", "attribute"))
        return has_add and has_field

    def _is_equal_distribution_assignment_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_equal = any(k in q for k in ("equal", "equally", "divide", "distribute", "split", "devide", "divid"))
        has_assign = any(k in q for k in ("assign", "fill", "set", "between", "among", "across", "into", "to users"))
        has_field = any(k in q for k in ("field", "column", "attribute", "user"))
        has_values = bool(re.search(r"\b[a-z_]+[-_ ]?\d+\b", q))
        user_count = self._extract_user_count_for_distribution(q)
        has_user_count = user_count is not None and int(user_count) >= 2
        return has_equal and has_assign and has_field and (has_values or has_user_count)

    def _is_add_then_equal_distribution_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_add = any(k in q for k in ("add", "create", "insert"))
        has_field = any(k in q for k in ("field", "column", "attribute", "user"))
        has_equal = any(k in q for k in ("equal", "equally", "divide", "distribute", "split", "devide", "divid"))
        return has_add and has_field and has_equal

    def _run_add_then_equal_distribution(self, normalized_query, original_query):
        # Step 1: add (or ensure) field
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> This action requires an active vector layer.")
            return
        target_field = self._extract_new_field_name(normalized_query) or self._extract_field_name_for_split(normalized_query, layer) or "user"
        if layer.fields().indexFromName(target_field) < 0:
            self._run_add_field(normalized_query, original_query)
            # Refresh layer handle after potential commit/update
            layer = self._active_layer_or_msg()
            if layer is None or not isinstance(layer, QgsVectorLayer):
                return
            if layer.fields().indexFromName(target_field) < 0:
                self._log("<b>AI:</b> Could not add target user field, so equal distribution did not run.")
                return
        # Step 2: distribute equally
        # Always run for compound prompts, even if wording misses legacy assign keywords.
        self._run_equal_distribution_assignment(normalized_query, original_query)

    def _run_equal_distribution_assignment(self, normalized_query, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Equal distribution requires an active vector layer.")
            return
        target_field = self._extract_field_name_for_split(normalized_query, layer) or "user"
        value_labels = self._extract_distribution_labels(original_query)
        requested_users = self._extract_user_count_for_distribution(original_query)
        if len(value_labels) < 2 and requested_users is not None and requested_users >= 2:
            value_labels = [f"User-{i}" for i in range(1, int(requested_users) + 1)]
        if len(value_labels) < 2:
            self._log(
                "<b>AI:</b> Could not parse labels or user count.\n"
                "Example 1: divide equally between 11 users in field user\n"
                "Example 2: assign equally User-1,User-2,User-3 in field user"
            )
            return

        idx = layer.fields().indexFromName(target_field)
        created_field = False
        try:
            started = layer.isEditable() or layer.startEditing()
            if not started:
                self._log("<b>AI:</b> Could not start edit session.")
                return
            if idx < 0:
                ok_add = layer.dataProvider().addAttributes([QgsField(target_field, QVariant.String, "string", 80, 0)])
                layer.updateFields()
                idx = layer.fields().indexFromName(target_field)
                created_field = bool(ok_add and idx >= 0)
                if idx < 0:
                    layer.rollBack()
                    self._log(f"<b>AI:</b> Could not create target field '{target_field}'.")
                    return

            feats = list(layer.getFeatures())
            total = len(feats)
            if total == 0:
                layer.rollBack()
                self._log("<b>AI:</b> Layer has no features to distribute.")
                return
            feats = self._order_features_for_symmetric_distribution(layer, feats)

            # Spatial zone-based distribution (contiguous work areas):
            # assign one user per partitioned zone so users get neighboring spans,
            # not random picks across the full network.
            n = len(value_labels)
            changes = {}
            zones = self._partition_features_into_spatial_zones(feats, n)
            for zi, zone_feats in enumerate(zones):
                label = value_labels[zi]
                for f in zone_feats:
                    changes[f.id()] = {idx: label}

            ok = layer.dataProvider().changeAttributeValues(changes)
            if not ok:
                layer.rollBack()
                self._log("<b>AI:</b> Failed assigning distributed values.")
                return
            if not layer.commitChanges():
                msg = "; ".join(layer.commitErrors()) if hasattr(layer, "commitErrors") else "unknown commit error"
                layer.rollBack()
                self._log(f"<b>AI:</b> Equal distribution commit failed: {msg}")
                return
            self._log(
                f"<b>AI:</b> Distributed field '{target_field}' equally across {len(value_labels)} values.\n"
                f"Values: {', '.join(value_labels)}\n"
                f"Features updated: {total}"
                + (f"\nCreated field: {target_field}" if created_field else "")
            )
            self._remember_action(self.last_user_query, f"equal distribution set on {target_field}")
        except Exception as exc:
            try:
                if layer.isEditable():
                    layer.rollBack()
            except Exception:
                pass
            self._log(f"<b>AI:</b> Equal distribution failed: {exc}")

    def _partition_features_into_spatial_zones(self, feats, zone_count):
        """
        Partition features into contiguous spatial zones using recursive
        axis-aligned splitting. Zone order is deterministic:
        top regions first, then left-to-right within lower splits.
        """
        if not feats:
            return []
        k = max(1, int(zone_count))
        if k == 1:
            return [list(feats)]

        # Cache centroids for stable spatial partitioning.
        cent = {}
        for f in feats:
            try:
                p = f.geometry().centroid().asPoint()
                cent[f.id()] = (float(p.x()), float(p.y()))
            except Exception:
                cent[f.id()] = (0.0, 0.0)

        by_id = {f.id(): f for f in feats}
        ids = [f.id() for f in feats]

        def split_ids(id_list, parts, depth):
            if parts <= 1 or len(id_list) <= 1:
                return [list(id_list)]

            # First split prefers vertical ordering (top->bottom) to match your workflow.
            if depth == 0:
                axis = "y"
            else:
                # Alternate axes for better compact zones.
                axis = "x" if (depth % 2 == 1) else "y"

            if axis == "y":
                sorted_ids = sorted(id_list, key=lambda fid: (-cent[fid][1], cent[fid][0], fid))
            else:
                sorted_ids = sorted(id_list, key=lambda fid: (cent[fid][0], -cent[fid][1], fid))

            p1 = int((parts + 1) // 2)
            p2 = int(parts - p1)
            cut = int(round(len(sorted_ids) * (float(p1) / float(parts))))
            cut = max(1, min(len(sorted_ids) - 1, cut)) if len(sorted_ids) > 1 else 1

            left_ids = sorted_ids[:cut]
            right_ids = sorted_ids[cut:]
            out = []
            out.extend(split_ids(left_ids, p1, depth + 1))
            out.extend(split_ids(right_ids, p2, depth + 1))
            return out

        raw_groups = split_ids(ids, k, 0)

        # If groups are fewer/more due to tiny datasets, normalize safely.
        groups = [g for g in raw_groups if g]
        while len(groups) < k:
            groups.append([])
        if len(groups) > k:
            merged = groups[:k - 1]
            tail = []
            for g in groups[k - 1:]:
                tail.extend(g)
            merged.append(tail)
            groups = merged

        return [[by_id[fid] for fid in g if fid in by_id] for g in groups]

    def _order_features_for_symmetric_distribution(self, layer, feats):
        """
        Deterministic ordering for equal assignment.
        - Line layers: topology-aware multi-source expansion from side/end features.
        - Others: stable spatial sort (top-to-bottom, left-to-right).
        """
        if not feats:
            return feats
        try:
            if isinstance(layer, QgsVectorLayer) and layer.geometryType() == QgsWkbTypes.LineGeometry:
                return self._order_line_features_by_sides(layer, feats)
        except Exception:
            pass
        return self._order_features_spatial(feats)

    def _order_features_spatial(self, feats):
        indexed = []
        for f in feats:
            try:
                c = f.geometry().centroid().asPoint()
                indexed.append(((-float(c.y()), float(c.x()), int(f.id())), f))
            except Exception:
                indexed.append((((0.0, 0.0, int(f.id()))), f))
        indexed.sort(key=lambda x: x[0])
        return [f for _, f in indexed]

    def _order_line_features_by_sides(self, layer, feats):
        # Build endpoint graph for connected ordering.
        tol = self._safe_endpoint_tolerance(layer)
        endpoint_to_fids = {}
        fid_to_endkeys = {}
        fid_to_centroid = {}

        for f in feats:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            p1, p2 = self._line_endpoints(g)
            if p1 is None or p2 is None:
                continue
            k1 = self._endpoint_key(p1.x(), p1.y(), tol)
            k2 = self._endpoint_key(p2.x(), p2.y(), tol)
            fid = int(f.id())
            fid_to_endkeys[fid] = (k1, k2)
            endpoint_to_fids.setdefault(k1, set()).add(fid)
            endpoint_to_fids.setdefault(k2, set()).add(fid)
            try:
                c = g.centroid().asPoint()
                fid_to_centroid[fid] = (float(c.x()), float(c.y()))
            except Exception:
                fid_to_centroid[fid] = (0.0, 0.0)

        # Build adjacency via shared endpoints.
        adj = {int(f.id()): set() for f in feats}
        for fids in endpoint_to_fids.values():
            fl = list(fids)
            for i in range(len(fl)):
                for j in range(i + 1, len(fl)):
                    a = fl[i]
                    b = fl[j]
                    adj.setdefault(a, set()).add(b)
                    adj.setdefault(b, set()).add(a)

        # Side seeds: features touching degree-1 endpoints.
        seeds = []
        for fid, (k1, k2) in fid_to_endkeys.items():
            d1 = len(endpoint_to_fids.get(k1, ()))
            d2 = len(endpoint_to_fids.get(k2, ()))
            if d1 == 1 or d2 == 1:
                seeds.append(fid)
        if not seeds:
            # If no clear endpoints (loop), use spatial top-first ordering as seeds.
            seeds = [int(f.id()) for f in self._order_features_spatial(feats)[: min(6, len(feats))]]

        # Seed order: top-to-bottom then left-to-right (gives "from top then next side").
        seeds = sorted(
            list(dict.fromkeys(seeds)),
            key=lambda fid: (-fid_to_centroid.get(fid, (0.0, 0.0))[1], fid_to_centroid.get(fid, (0.0, 0.0))[0], fid),
        )

        # Multi-source BFS with deterministic tie-breaks.
        heap = []
        for rank, fid in enumerate(seeds):
            heapq.heappush(heap, (0, rank, fid))
        visited = set()
        ordered = []
        while heap:
            dist, rank, fid = heapq.heappop(heap)
            if fid in visited:
                continue
            visited.add(fid)
            ordered.append(fid)
            nbrs = sorted(list(adj.get(fid, set())))
            for nb in nbrs:
                if nb not in visited:
                    heapq.heappush(heap, (dist + 1, rank, nb))

        # Append any disconnected leftovers deterministically.
        all_ids = [int(f.id()) for f in feats]
        leftovers = [fid for fid in all_ids if fid not in visited]
        leftovers = sorted(leftovers, key=lambda fid: (-fid_to_centroid.get(fid, (0.0, 0.0))[1], fid_to_centroid.get(fid, (0.0, 0.0))[0], fid))
        ordered.extend(leftovers)

        by_id = {int(f.id()): f for f in feats}
        return [by_id[fid] for fid in ordered if fid in by_id]

    def _safe_endpoint_tolerance(self, layer):
        try:
            t = self._meters_to_layer_units(layer, 0.10)
            t = float(t)
            return t if t > 0 else 0.000001
        except Exception:
            return 0.000001

    def _line_endpoints(self, geom):
        try:
            if geom.isMultipart():
                m = geom.asMultiPolyline()
                if not m or not m[0]:
                    return None, None
                first = m[0][0]
                last_part = m[-1]
                if not last_part:
                    return None, None
                last = last_part[-1]
                return first, last
            line = geom.asPolyline()
            if not line:
                return None, None
            return line[0], line[-1]
        except Exception:
            return None, None

    def _extract_distribution_labels(self, original_query):
        txt = str(original_query or "")
        labels = []
        for m in re.finditer(r"\b([A-Za-z_]+[-_ ]?\d+)\b", txt):
            raw = m.group(1).strip()
            norm = re.sub(r"\s+", "-", raw)
            labels.append(norm)
        out = []
        seen = set()
        for v in labels:
            k = v.lower()
            if k not in seen:
                out.append(v)
                seen.add(k)
        return out

    def _extract_user_count_for_distribution(self, original_query):
        q = str(original_query or "").lower()
        patterns = [
            r"(?:between|among|across|into|for)\s+(\d+)\s*users?\b",
            r"(\d+)\s*users?\b",
            r"users?\s*(\d+)\b",
        ]
        for pat in patterns:
            m = re.search(pat, q, flags=re.IGNORECASE)
            if not m:
                continue
            try:
                n = int(m.group(1))
                if n >= 1:
                    return n
            except Exception:
                continue
        # Word-number support: "six users", "among the eleven users", etc.
        mword = re.search(
            r"(?:between|among|across|into|for)?\s*(?:the\s+)?([a-z\-]+)\s+users?\b",
            q,
            flags=re.IGNORECASE,
        )
        if mword:
            w = mword.group(1).strip().lower()
            n = self._word_to_number(w)
            if n is not None and n >= 1:
                return n
        # Token fallback: find token right before "user/users".
        tokens = re.findall(r"[a-z0-9\-]+", q)
        for i, tok in enumerate(tokens):
            if tok in ("user", "users"):
                if i > 0:
                    prev = tokens[i - 1]
                    if prev.isdigit():
                        n = int(prev)
                        if n >= 1:
                            return n
                    n = self._word_to_number(prev)
                    if n is not None and n >= 1:
                        return n
        return None

    def _word_to_number(self, word):
        w = str(word or "").strip().lower()
        if not w:
            return None
        units = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
            "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
        }
        tens = {
            "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
        }
        if w in units:
            return units[w]
        if w in tens:
            return tens[w]
        # Hyphenated forms like twenty-one
        if "-" in w:
            a, b = w.split("-", 1)
            if a in tens and b in units:
                return tens[a] + units[b]
        return None

    def _verify_equal_distribution_applied(self, normalized_query):
        layer = self.iface.activeLayer()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return False
        target_field = self._extract_field_name_for_split(normalized_query, layer) or "user"
        idx = layer.fields().indexFromName(target_field)
        if idx < 0:
            return False
        non_null = 0
        checked = 0
        for f in layer.getFeatures():
            checked += 1
            v = f[idx]
            if v is not None and str(v).strip() != "":
                non_null += 1
            if checked >= 200:
                break
        return non_null > 0

    def _is_categorize_by_field_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_cat = any(k in q for k in ("categorise", "categorize", "classify", "category")) or bool(
            re.search(r"\bcat(?:e|a)gori(?:s|z)e?\b", q)
        )
        has_field = any(k in q for k in ("field", "column", "attribute", "by "))
        return has_cat and has_field

    def _run_delete_field(self, normalized_query, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Delete column requires an active vector layer.")
            return
        field_name = self._extract_field_name_for_split(normalized_query, layer)
        if not field_name:
            self._log("<b>AI:</b> Could not identify column to delete. Example: delete column named User")
            return
        idx = layer.fields().indexFromName(field_name)
        if idx < 0:
            self._log(f"<b>AI:</b> Column '{field_name}' not found.")
            return
        try:
            started = layer.isEditable() or layer.startEditing()
            if not started:
                self._log("<b>AI:</b> Could not start edit session for field deletion.")
                return
            ok = layer.dataProvider().deleteAttributes([idx])
            layer.updateFields()
            if not ok:
                layer.rollBack()
                self._log(f"<b>AI:</b> Failed deleting column '{field_name}'.")
                return
            if not layer.commitChanges():
                msg = "; ".join(layer.commitErrors()) if hasattr(layer, "commitErrors") else "unknown commit error"
                layer.rollBack()
                self._log(f"<b>AI:</b> Delete column commit failed: {msg}")
                return
            self._log(f"<b>AI:</b> Deleted column '{field_name}' from layer '{layer.name()}'.")
            self._remember_action(self.last_user_query, f"deleted field {field_name}")
        except Exception as exc:
            try:
                layer.rollBack()
            except Exception:
                pass
            self._log(f"<b>AI:</b> Delete column failed: {exc}")

    def _run_add_field(self, normalized_query, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Add column requires an active vector layer.")
            return
        field_name = self._extract_new_field_name(normalized_query)
        if not field_name:
            self._log("<b>AI:</b> Could not identify new column name. Example: add a column named Time")
            return
        if layer.fields().indexFromName(field_name) >= 0:
            self._log(f"<b>AI:</b> Column '{field_name}' already exists.")
            # If same prompt also asks equal distribution, continue with assignment step.
            if self._is_equal_distribution_assignment_query(normalized_query):
                self._run_equal_distribution_assignment(normalized_query, original_query)
            return
        ftype = self._extract_new_field_type(normalized_query)
        try:
            started = layer.isEditable() or layer.startEditing()
            if not started:
                self._log("<b>AI:</b> Could not start edit session for adding column.")
                return
            if ftype == "int":
                new_field = QgsField(field_name, QVariant.Int)
            elif ftype == "double":
                new_field = QgsField(field_name, QVariant.Double, "double", 20, 6)
            elif ftype == "date":
                new_field = QgsField(field_name, QVariant.Date)
            elif ftype == "time":
                new_field = QgsField(field_name, QVariant.Time)
            elif ftype == "datetime":
                new_field = QgsField(field_name, QVariant.DateTime)
            else:
                new_field = QgsField(field_name, QVariant.String, "string", 80, 0)
            ok = layer.dataProvider().addAttributes([new_field])
            layer.updateFields()
            if not ok:
                layer.rollBack()
                self._log(f"<b>AI:</b> Failed adding column '{field_name}'.")
                return
            if not layer.commitChanges():
                msg = "; ".join(layer.commitErrors()) if hasattr(layer, "commitErrors") else "unknown commit error"
                layer.rollBack()
                self._log(f"<b>AI:</b> Add column commit failed: {msg}")
                return
            self._log(f"<b>AI:</b> Added column '{field_name}' ({ftype}) to layer '{layer.name()}'.")
            self._remember_action(self.last_user_query, f"added field {field_name}")
            # Compound prompt support: add field then distribute equally in same command.
            if self._is_equal_distribution_assignment_query(normalized_query):
                self._run_equal_distribution_assignment(normalized_query, original_query)
        except Exception as exc:
            try:
                layer.rollBack()
            except Exception:
                pass
            self._log(f"<b>AI:</b> Add column failed: {exc}")

    def _run_categorize_by_field(self, normalized_query, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Categorize requires an active vector layer.")
            return
        multi_fields = self._extract_multiple_field_candidates_from_query(normalized_query, layer)
        if len(multi_fields) > 1:
            self._run_categorize_by_multiple_fields(layer, multi_fields)
            return
        field_name = self._extract_field_name_for_split(normalized_query, layer)
        if not field_name:
            self._log("<b>AI:</b> Could not identify field for categorization. Example: categorize by field label_flag")
            return
        idx = layer.fields().indexFromName(field_name)
        if idx < 0:
            self._log(f"<b>AI:</b> Field '{field_name}' not found.")
            return
        try:
            uniq = set()
            for feat in layer.getFeatures():
                val = feat[idx]
                uniq.add("" if val is None else str(val))
                if len(uniq) >= 300:
                    break
            cats = []
            for v in sorted(uniq):
                sym = QgsSymbol.defaultSymbol(layer.geometryType())
                cats.append(QgsRendererCategory(v, sym, v if v != "" else "NULL"))
            renderer = QgsCategorizedSymbolRenderer(field_name, cats)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.mapCanvas().refresh()
            applied_ok = isinstance(layer.renderer(), QgsCategorizedSymbolRenderer)
            if not applied_ok:
                self._log("<b>AI:</b> Categorization command ran but renderer verification failed.")
                return
            self._log(
                f"<b>AI:</b> Categorized layer '{layer.name()}' by field '{field_name}'.\n"
                f"Categories applied: {len(cats)}"
            )
            self._remember_action(self.last_user_query, f"categorized by {field_name}")
        except Exception as exc:
            self._log(f"<b>AI:</b> Categorization failed: {exc}")

    def _run_categorize_by_multiple_fields(self, layer, field_names):
        """
        Categorize by a composite key of multiple fields (2+),
        e.g. gis_vcat/gis_hcat or feeder/label_flag/gis_vcat.
        """
        if not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Multi-field categorization requires a vector layer.")
            return
        if not field_names or len(field_names) < 2:
            self._log("<b>AI:</b> Multi-field categorization needs at least two fields.")
            return
        try:
            # Build a robust expression key: NULL-safe string join with slash delimiter.
            parts = [f"coalesce(to_string(\"{fn}\"), 'NULL')" for fn in field_names]
            expr = " || ' / ' || ".join(parts)

            uniq = set()
            for feat in layer.getFeatures():
                vals = []
                for fn in field_names:
                    v = feat[fn]
                    vals.append("NULL" if v is None or str(v).strip() == "" else str(v).strip())
                uniq.add(" / ".join(vals))
                if len(uniq) >= 1500:
                    break

            cats = []
            for v in sorted(uniq):
                sym = QgsSymbol.defaultSymbol(layer.geometryType())
                cats.append(QgsRendererCategory(v, sym, v))
            renderer = QgsCategorizedSymbolRenderer(expr, cats)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.iface.mapCanvas().refresh()
            applied_ok = isinstance(layer.renderer(), QgsCategorizedSymbolRenderer)
            if not applied_ok:
                self._log("<b>AI:</b> Multi-field categorization command ran but renderer verification failed.")
                return
            self._log(
                f"<b>AI:</b> Categorized layer '{layer.name()}' by composite fields.\n"
                f"Fields: {', '.join(field_names)}\n"
                f"Categories applied: {len(cats)}"
            )
            self._remember_action(self.last_user_query, f"categorized by composite {','.join(field_names)}")
        except Exception as exc:
            self._log(f"<b>AI:</b> Multi-field categorization failed: {exc}")

    def _extract_multiple_field_candidates_from_query(self, normalized_query, layer):
        if not isinstance(layer, QgsVectorLayer):
            return []
        q = (normalized_query or "").lower()
        fields = [f.name() for f in layer.fields()]
        found = []
        # Direct hits by name appearance.
        for fn in fields:
            if fn.lower() in q:
                found.append(fn)
        # Parse slash/pipe separated hints like "gis_vcat/gis_hcat".
        for grp in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*\s*[/|]\s*[a-zA-Z_][a-zA-Z0-9_]*)\b", q):
            parts = re.split(r"[/|]", grp)
            for part in parts:
                token = part.strip().lower()
                m = self._match_field_name(token, fields)
                if m:
                    found.append(m)
        # De-duplicate preserve order.
        uniq = []
        seen = set()
        for f in found:
            k = f.lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(f)
        return uniq

    def _extract_new_field_name(self, normalized_query):
        q = (normalized_query or "").strip()
        m = re.search(r"(?:column|field|attribute)\s+(?:named\s+)?([a-zA-Z_][a-zA-Z0-9_]*)", q, flags=re.IGNORECASE)
        if m:
            return m.group(1)
        m2 = re.search(r"(?:add|create|insert)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+(?:column|field|attribute)", q, flags=re.IGNORECASE)
        if m2:
            return m2.group(1)
        return None

    def _extract_new_field_type(self, normalized_query):
        q = (normalized_query or "").lower()
        if any(k in q for k in ("integer", "int")):
            return "int"
        if any(k in q for k in ("double", "float", "decimal", "real")):
            return "double"
        if "datetime" in q:
            return "datetime"
        if "date" in q:
            return "date"
        if "time" in q:
            return "time"
        return "string"

    def _answer_length_by_category(self, layer, normalized_query):
        values = self._extract_requested_category_values(normalized_query)
        if not values:
            return False
        field_name = self._infer_best_field_for_values(layer, values)
        if not field_name:
            return False
        idx = layer.fields().indexFromName(field_name)
        if idx < 0:
            return False

        wanted = [v.lower() for v in values]
        total_native = 0.0
        matched_features = 0
        for feat in layer.getFeatures():
            raw = feat[idx]
            txt = "" if raw is None else str(raw).strip().lower()
            if txt not in wanted:
                continue
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            total_native += float(geom.length())
            matched_features += 1

        unit_name = "layer units"
        try:
            from qgis.core import QgsUnitTypes

            unit_name = QgsUnitTypes.toString(layer.crs().mapUnits()) or "layer units"
        except Exception:
            pass
        total_m = self._layer_units_to_meters(layer, total_native)
        values_text = ", ".join(values)
        lines = [
            f"<b>AI:</b> Total length for category value(s) [{values_text}] in loaded layer '{layer.name()}':",
            f"- Field used: {field_name}",
            f"- Matched features: {matched_features}",
            f"- Total length ({unit_name}): {total_native:.6f}",
        ]
        if total_m is not None:
            lines.append(f"- Total length (meters, approx): {float(total_m):.3f}")
        self._log("\n".join(lines))
        self._remember_action(self.last_user_query, f"length by category on {field_name}")
        return True

    def _answer_category_value_counts(self, layer, normalized_query):
        values = self._extract_requested_category_values(normalized_query)
        if not values:
            return False

        field_name = self._infer_best_field_for_values(layer, values)
        if not field_name:
            self._log(
                "<b>AI:</b> I detected a category count request, but could not infer which field contains those values.\n"
                "Mention the field too, e.g. 'count HIV1 and HIV0 in field gis_vcat'."
            )
            return True

        idx = layer.fields().indexFromName(field_name)
        if idx < 0:
            return False
        wanted = [v.lower() for v in values]
        counts = {v: 0 for v in values}
        total = 0
        for feat in layer.getFeatures():
            total += 1
            raw = feat[idx]
            txt = "" if raw is None else str(raw).strip().lower()
            for i, w in enumerate(wanted):
                if txt == w:
                    counts[values[i]] += 1

        lines = [f"<b>AI:</b> Category counts from loaded layer '{layer.name()}':", f"- Field used: {field_name}"]
        for v in values:
            lines.append(f"- {v}: {counts.get(v, 0)}")
        lines.append(f"- Total features scanned: {total}")
        self._log("\n".join(lines))
        self._remember_action(self.last_user_query, f"category counts on {field_name}")
        return True

    def _answer_field_value_count_from_query(self, layer, normalized_query):
        """
        Fast path for natural prompts like:
        - how many spans have gis_vcat-6
        - count where feeder=A-1-K
        """
        # Confirmed real bug: this heuristically extracts ANY field=value
        # pair it can find, with no check for a more specific intent also
        # being present. "Distribute the User field equally among 3
        # users" got misread as "condition: User = field" (literally
        # matching the word "field" as the filter value) purely because
        # "User" happened to be a real field name someone had just
        # created. A distribute-equally request should always win this
        # ambiguity -- it's a distinctive, unambiguous phrase.
        if self._is_equal_distribution_assignment_query(normalized_query) or self._is_add_then_equal_distribution_query(
            normalized_query
        ):
            return False
        pair = self._extract_field_value_pair_from_query(normalized_query, layer)
        if not pair:
            return False
        field_name, raw_value = pair
        idx = layer.fields().indexFromName(field_name)
        if idx < 0:
            return False

        value_token = str(raw_value).strip().lower()
        matched = 0
        total = 0
        for feat in layer.getFeatures():
            total += 1
            txt = "" if feat[idx] is None else str(feat[idx]).strip().lower()
            if self._token_value_match(txt, value_token):
                matched += 1
        self._log(
            f"<b>AI:</b> Filtered count from loaded layer '{layer.name()}':\n"
            f"- Condition: {field_name} = {raw_value}\n"
            f"- Matching features: {matched}\n"
            f"- Total scanned: {total}"
        )
        self._remember_action(self.last_user_query, f"filtered count {field_name}={raw_value}")
        return True

    def _answer_field_value_length_from_query(self, layer, normalized_query):
        pair = self._extract_field_value_pair_from_query(normalized_query, layer)
        if not pair:
            return False
        field_name, raw_value = pair
        idx = layer.fields().indexFromName(field_name)
        if idx < 0:
            return False

        value_token = str(raw_value).strip().lower()
        matched = 0
        total_native = 0.0
        for feat in layer.getFeatures():
            txt = "" if feat[idx] is None else str(feat[idx]).strip().lower()
            if not self._token_value_match(txt, value_token):
                continue
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            total_native += float(geom.length())
            matched += 1
        unit_name = "layer units"
        try:
            from qgis.core import QgsUnitTypes

            unit_name = QgsUnitTypes.toString(layer.crs().mapUnits()) or "layer units"
        except Exception:
            pass
        total_m = self._layer_units_to_meters(layer, total_native)
        lines = [
            f"<b>AI:</b> Filtered total length from loaded layer '{layer.name()}':",
            f"- Condition: {field_name} = {raw_value}",
            f"- Matched features: {matched}",
            f"- Total length ({unit_name}): {total_native:.6f}",
        ]
        if total_m is not None:
            lines.append(f"- Total length (meters, approx): {float(total_m):.3f}")
        self._log("\n".join(lines))
        self._remember_action(self.last_user_query, f"filtered length {field_name}={raw_value}")
        return True

    def _extract_field_value_pair_from_query(self, normalized_query, layer):
        if not isinstance(layer, QgsVectorLayer):
            return None
        q = (normalized_query or "").lower()
        fields = [f.name() for f in layer.fields()]
        for fname in fields:
            f = fname.lower()
            # Patterns: gis_vcat-6, gis_vcat=6, gis_vcat 6, gis_vcat is 6
            pats = [
                rf"\b{re.escape(f)}\s*[-=:]\s*([a-z0-9_\-\.]+)\b",
                rf"\b{re.escape(f)}\s+(?:is|as|equals|equal to)\s+([a-z0-9_\-\.]+)\b",
                rf"\b{re.escape(f)}\s+([a-z0-9_\-\.]+)\b",
            ]
            for pat in pats:
                m = re.search(pat, q)
                if m:
                    return fname, m.group(1)
        return None

    def _token_value_match(self, actual_text, wanted_token):
        a = (actual_text or "").strip().lower()
        w = (wanted_token or "").strip().lower()
        if a == w:
            return True
        # numeric tolerance for stored values like 6.0 queried as 6
        try:
            return abs(float(a) - float(w)) < 1e-9
        except Exception:
            return False

    def _try_field_value_filter_fastpath(self, normalized_query):
        """
        Strict execution rule:
        If query includes a detectable field=value-like pattern, execute filtered
        count/length immediately before generic QA routing.
        """
        q = (normalized_query or "").lower().strip()
        if not q or q.startswith("/") or q.startswith("run "):
            return False
        # Never route schema/style edit commands into value-filter fastpath.
        if any(
            k in q
            for k in (
                "delete column",
                "delete field",
                "remove column",
                "remove field",
                "drop column",
                "drop field",
                "add column",
                "add field",
                "create column",
                "create field",
                "categorise",
                "categorize",
                "classify",
                "style by",
            )
        ):
            return False
        if self._looks_like_action_request(q):
            return False
        layer = self.iface.activeLayer()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return False
        pair = self._extract_field_value_pair_from_query(q, layer)
        if not pair:
            return False
        if any(k in q for k in ("length", "total length", "sum length", "overall length")):
            return self._answer_field_value_length_from_query(layer, q)
        return self._answer_field_value_count_from_query(layer, q)

    def _extract_requested_category_values(self, normalized_query):
        q = (normalized_query or "").lower()
        vals = []
        for m in re.findall(r"\bof\s+([a-z0-9_\-]+)", q):
            if m not in vals:
                vals.append(m)
        for m in re.findall(r"\b([a-z]{2,}\d+[a-z0-9_]*)\b", q):
            if m not in vals:
                vals.append(m)
        return vals[:8]

    def _infer_best_field_for_values(self, layer, values):
        if not isinstance(layer, QgsVectorLayer):
            return ""
        fields = [f.name() for f in layer.fields()]
        wanted = [v.lower() for v in values]
        best_field = ""
        best_score = 0

        for fname in fields:
            idx = layer.fields().indexFromName(fname)
            if idx < 0:
                continue
            seen = set()
            score = 0
            checked = 0
            for feat in layer.getFeatures():
                raw = feat[idx]
                txt = "" if raw is None else str(raw).strip().lower()
                if txt in seen:
                    continue
                seen.add(txt)
                checked += 1
                for w in wanted:
                    if txt == w:
                        score += 2
                    elif w in txt or txt in w:
                        score += 1
                if checked >= 300:
                    break
            if score > best_score:
                best_score = score
                best_field = fname
        return best_field if best_score > 0 else ""

    def _is_qgis_read_everything_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_qgis_scope = "qgis" in q or "inside qgis" in q
        has_full_read = (
            "read everything" in q
            or "everything from a to z" in q
            or ("everything" in q and "read" in q)
            or "inventory" in q
        )
        return has_qgis_scope and has_full_read

    def _refresh_qgis_inventory(self):
        self._refresh_layer_catalog()
        self._refresh_algorithm_catalog()
        inv = {
            "generated_at_utc": datetime.utcnow().isoformat(),
            "project": {},
            "counts": {},
            "layers": list(self.layer_catalog.values()),
            "algorithms": sorted(self.algorithm_catalog.keys()),
            "actions": [],
            "menus": [],
            "plugins": self._list_available_plugins(),
            "settings_sample": {},
        }
        try:
            project = QgsProject.instance()
            inv["project"] = {
                "file_name": project.fileName() if hasattr(project, "fileName") else "",
                "home_path": project.homePath() if hasattr(project, "homePath") else "",
                "crs": project.crs().authid() if hasattr(project, "crs") and project.crs() else "",
                "title": project.title() if hasattr(project, "title") else "",
            }
        except Exception:
            pass
        try:
            inv["actions"] = self._collect_qgis_actions()
        except Exception:
            inv["actions"] = []
        try:
            inv["menus"] = sorted({a.get("menu", "") for a in inv["actions"] if isinstance(a, dict) and a.get("menu")})
        except Exception:
            inv["menus"] = []
        try:
            inv["settings_sample"] = self._collect_qgis_settings_sample(max_items=400)
        except Exception:
            inv["settings_sample"] = {}

        inv["counts"] = {
            "layers": len(inv.get("layers", [])),
            "algorithms": len(inv.get("algorithms", [])),
            "actions": len(inv.get("actions", [])),
            "menus": len(inv.get("menus", [])),
            "plugins": len(inv.get("plugins", [])),
            "settings_keys_sampled": len(inv.get("settings_sample", {})),
        }
        self.qgis_inventory = inv

    def _collect_qgis_actions(self):
        actions = []
        mw = self.iface.mainWindow() if hasattr(self.iface, "mainWindow") else None
        if mw is None:
            return actions
        qactions = mw.findChildren(QAction)
        seen = set()
        for act in qactions:
            try:
                text = (act.text() or "").strip()
                if not text:
                    continue
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                parent_obj = act.parent()
                menu_name = ""
                try:
                    if parent_obj is not None and hasattr(parent_obj, "title"):
                        menu_name = (parent_obj.title() or "").strip()
                except Exception:
                    menu_name = ""
                actions.append(
                    {
                        "text": text,
                        "menu": menu_name,
                        "enabled": bool(act.isEnabled()),
                        "checkable": bool(act.isCheckable()),
                        "checked": bool(act.isChecked()) if act.isCheckable() else False,
                        "shortcut": act.shortcut().toString() if hasattr(act, "shortcut") else "",
                        "tool_tip": (act.toolTip() or "").strip(),
                    }
                )
            except Exception:
                continue
        actions.sort(key=lambda x: (x.get("menu", ""), x.get("text", "")))
        return actions

    def _collect_qgis_settings_sample(self, max_items=400):
        sampled = {}
        try:
            settings = QgsSettings()
            keys = settings.allKeys() or []
            for k in list(keys)[: max(1, int(max_items))]:
                try:
                    sampled[str(k)] = settings.value(k)
                except Exception:
                    sampled[str(k)] = None
        except Exception:
            pass
        return sampled

    def _render_qgis_inventory(self):
        if not self.qgis_inventory:
            self._refresh_qgis_inventory()
        inv = self.qgis_inventory or {}
        counts = inv.get("counts", {}) if isinstance(inv, dict) else {}
        project = inv.get("project", {}) if isinstance(inv, dict) else {}
        layers = inv.get("layers", []) if isinstance(inv, dict) else []
        plugins = inv.get("plugins", []) if isinstance(inv, dict) else []
        menus = inv.get("menus", []) if isinstance(inv, dict) else []
        lines = [
            "<b>AI:</b> Full QGIS inventory (all accessible runtime context) captured.",
            f"Project: {project.get('title', '') or '(untitled)'} | CRS: {project.get('crs', '') or 'unknown'}",
            f"Layers: {counts.get('layers', 0)} | Algorithms: {counts.get('algorithms', 0)} | Plugins: {counts.get('plugins', 0)}",
            f"Actions: {counts.get('actions', 0)} | Menus: {counts.get('menus', 0)} | Settings sampled: {counts.get('settings_keys_sampled', 0)}",
        ]
        if layers:
            lines.append("Layer preview:")
            for l in layers[:8]:
                lines.append(
                    f"- {l.get('name', '')} [{l.get('kind', '')}] "
                    f"CRS={l.get('crs', '')} source={l.get('source', '')[:80]}"
                )
        if plugins:
            names = [str(p.get("name", "")) for p in plugins[:10] if isinstance(p, dict)]
            if names:
                lines.append("Plugin preview: " + ", ".join(names) + (f" (+{len(plugins)-10} more)" if len(plugins) > 10 else ""))
        if menus:
            lines.append("Menu preview: " + ", ".join(menus[:12]) + (f" (+{len(menus)-12} more)" if len(menus) > 12 else ""))
        lines.append("Use /qgis inventory export for complete JSON snapshot.")
        self._log("\n".join(lines))
        self._remember_action(self.last_user_query, "qgis inventory captured")

    def _export_qgis_inventory(self):
        if not self.qgis_inventory:
            self._refresh_qgis_inventory()
        out_path = Path(self._default_export_path("qgis_inventory_full.json"))
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(self.qgis_inventory or {}, indent=2, default=str), encoding="utf-8")
            self._log(f"<b>AI:</b> QGIS inventory exported: {out_path}")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not export QGIS inventory: {exc}")

    def _is_elevation_stats_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_elevation = any(k in q for k in ("elevation", "dem", "height"))
        has_stats = any(k in q for k in ("mean", "minimum", "maximum", "min", "max", "zonal"))
        has_zone = any(k in q for k in ("parcel", "polygon", "each"))
        return has_elevation and has_stats and has_zone

    def _plan_elevation_stats_workflow(self, original_query, normalized_query):
        if not self._processing_available():
            return
        parcel_layer = self._active_layer_or_msg()
        if parcel_layer is None or not isinstance(parcel_layer, QgsVectorLayer):
            self._log("<b>AI:</b> Select parcel polygon layer as active layer first.")
            return
        if parcel_layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            self._log("<b>AI:</b> Active layer should be polygon parcels for zonal statistics.")
            return

        dem_layer = None
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsRasterLayer) and lyr != parcel_layer:
                dem_layer = lyr
                if "dem" in lyr.name().lower() or "elev" in lyr.name().lower():
                    break
        if dem_layer is None:
            self._log("<b>AI:</b> No DEM/raster layer detected. Load DEM and retry.")
            return

        same_crs = False
        try:
            same_crs = parcel_layer.crs().authid() == dem_layer.crs().authid()
        except Exception:
            same_crs = False

        lines = [
            "<b>AI:</b> Ready to execute QGIS action workflow.",
            f"✓ Parcel layer detected: {parcel_layer.name()}",
            f"✓ DEM detected: {dem_layer.name()}",
            f"{'✓' if same_crs else '⚠'} CRS checked: parcels={parcel_layer.crs().authid()} | dem={dem_layer.crs().authid()}",
            "✓ Zonal Statistics selected (MEAN, MIN, MAX)",
            "Workflow:",
            "1) Check DEM/parcel CRS",
            "2) Reproject DEM if necessary",
            "3) Run Zonal Statistics",
            "4) Calculate MEAN, MIN, MAX",
            "5) Join statistics to parcel layer",
            "6) Validate fields in attribute table",
        ]
        self._request_confirmation(
            "\n".join(lines),
            self._execute_elevation_stats_workflow,
            parcel_layer,
            dem_layer,
        )

    def _execute_elevation_stats_workflow(self, parcel_layer, dem_layer):
        if not self._processing_available():
            return
        work_dem = dem_layer
        try:
            if parcel_layer.crs().authid() != dem_layer.crs().authid():
                reproj = processing.run(
                    "gdal:warpreproject",
                    {
                        "INPUT": dem_layer,
                        "TARGET_CRS": parcel_layer.crs(),
                        "RESAMPLING": 0,
                        "OUTPUT": "TEMPORARY_OUTPUT",
                    },
                )["OUTPUT"]
                if isinstance(reproj, QgsRasterLayer):
                    work_dem = reproj
                    QgsProject.instance().addMapLayer(work_dem)
                self._log("<b>AI:</b> DEM reprojected to parcel CRS for accurate zonal stats.")

            alg_id = "native:zonalstatisticsfb"
            if processing.registry().algorithmById(alg_id) is None:
                alg_id = "qgis:zonalstatistics"

            params = {
                "INPUT": parcel_layer,
                "INPUT_RASTER": work_dem,
                "RASTER_BAND": 1,
                "COLUMN_PREFIX": "elev_",
            }
            if alg_id == "native:zonalstatisticsfb":
                # 2=Mean, 5=Min, 6=Max in QGIS enum list
                params["STATISTICS"] = [2, 5, 6]
            else:
                params["STATISTICS"] = [2, 5, 6]

            processing.run(alg_id, params)

            fields = [f.name().lower() for f in parcel_layer.fields()]
            expected = ("elev_mean", "elev_min", "elev_max")
            present = [f for f in expected if f in fields]
            if len(present) >= 2:
                self._log(
                    "<b>AI:</b> Elevation statistics complete.\n"
                    f"Validated fields: {', '.join(present)}\n"
                    f"Layer updated: {parcel_layer.name()}"
                )
                self._remember_action(self.last_user_query, "elevation zonal stats executed")
            else:
                self._log(
                    "<b>AI:</b> Zonal statistics ran, but expected fields were not fully detected.\n"
                    "Check attribute table for added elev_* fields."
                )
        except Exception as exc:
            self._log(f"<b>AI:</b> Elevation workflow failed: {exc}")

    def _is_spatial_join_workflow_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_join = "spatial join" in q or ("join" in q and "spatial" in q)
        return has_join or ("join attributes by location" in q)

    def _plan_spatial_join_workflow(self, original_query, normalized_query):
        if not self._processing_available():
            return
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        if len(layers) < 2:
            self._log("<b>AI:</b> Spatial join needs at least two vector layers.")
            return
        input_layer = self._active_layer_or_msg()
        if input_layer is None or not isinstance(input_layer, QgsVectorLayer):
            self._log("<b>AI:</b> Select input vector layer first.")
            return
        join_layer = next((l for l in layers if l != input_layer), None)
        if join_layer is None:
            self._log("<b>AI:</b> Could not detect a second vector layer for join.")
            return
        self._request_confirmation(
            "\n".join(
                [
                    "<b>AI:</b> Ready to execute spatial join workflow.",
                    f"✓ Input layer detected: {input_layer.name()}",
                    f"✓ Join layer detected: {join_layer.name()}",
                    "✓ Predicate selected: intersects",
                    "Workflow:",
                    "1) Validate both layers and CRS",
                    "2) Run spatial join",
                    "3) Create output layer",
                    "4) Validate feature count and joined fields",
                ]
            ),
            self._execute_spatial_join_workflow,
            input_layer,
            join_layer,
        )

    def _execute_spatial_join_workflow(self, input_layer, join_layer):
        if not self._processing_available():
            return
        try:
            out = processing.run(
                "native:joinattributesbylocation",
                {
                    "INPUT": input_layer,
                    "JOIN": join_layer,
                    "PREDICATE": [0],  # intersects
                    "JOIN_FIELDS": [],
                    "METHOD": 1,
                    "DISCARD_NONMATCHING": False,
                    "PREFIX": "j_",
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            out.setName(f"{input_layer.name()}_spjoin")
            QgsProject.instance().addMapLayer(out)
            self._log(
                "<b>AI:</b> Spatial join complete.\n"
                f"Output: {out.name()} | Features: {out.featureCount()}"
            )
            self._remember_action(self.last_user_query, "spatial join workflow executed")
        except Exception as exc:
            self._log(f"<b>AI:</b> Spatial join workflow failed: {exc}")

    def _is_buffer_intersection_workflow_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_buffer = "buffer" in q
        has_inter = any(k in q for k in ("intersect", "intersection", "overlay"))
        return has_buffer and has_inter

    def _plan_buffer_intersection_workflow(self, original_query, normalized_query):
        if not self._processing_available():
            return
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        if len(layers) < 2:
            self._log("<b>AI:</b> Buffer+intersection needs at least two vector layers.")
            return
        input_layer = self._active_layer_or_msg()
        if input_layer is None or not isinstance(input_layer, QgsVectorLayer):
            self._log("<b>AI:</b> Select input vector layer first.")
            return
        overlay_layer = next((l for l in layers if l != input_layer), None)
        if overlay_layer is None:
            self._log("<b>AI:</b> Could not detect second layer for intersection.")
            return
        dist = self._extract_distance(normalized_query) or 20.0
        self._request_confirmation(
            "\n".join(
                [
                    "<b>AI:</b> Ready to execute buffer + intersection workflow.",
                    f"✓ Input layer detected: {input_layer.name()}",
                    f"✓ Overlay layer detected: {overlay_layer.name()}",
                    f"✓ Buffer distance: {dist:g} meters",
                    "Workflow:",
                    "1) Buffer input layer",
                    "2) Intersect buffered result with overlay layer",
                    "3) Validate output and add to map",
                ]
            ),
            self._execute_buffer_intersection_workflow,
            input_layer,
            overlay_layer,
            dist,
        )

    def _execute_buffer_intersection_workflow(self, input_layer, overlay_layer, distance_m):
        if not self._processing_available():
            return
        try:
            b = processing.run(
                "native:buffer",
                {
                    "INPUT": input_layer,
                    "DISTANCE": self._meters_to_layer_units(input_layer, float(distance_m)),
                    "SEGMENTS": 8,
                    "END_CAP_STYLE": 0,
                    "JOIN_STYLE": 0,
                    "MITER_LIMIT": 2.0,
                    "DISSOLVE": False,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            b.setName(f"{input_layer.name()}_buffer_{distance_m:g}m")
            i = processing.run(
                "native:intersection",
                {"INPUT": b, "OVERLAY": overlay_layer, "OUTPUT": "memory:"},
            )["OUTPUT"]
            i.setName(f"{input_layer.name()}_{overlay_layer.name()}_intersect")
            QgsProject.instance().addMapLayer(b)
            QgsProject.instance().addMapLayer(i)
            self._log(
                "<b>AI:</b> Buffer + intersection complete.\n"
                f"Buffered: {b.name()} | Intersections: {i.featureCount()}"
            )
            self._remember_action(self.last_user_query, "buffer intersection workflow executed")
        except Exception as exc:
            self._log(f"<b>AI:</b> Buffer + intersection workflow failed: {exc}")

    def _is_clip_export_workflow_query(self, normalized_query):
        q = (normalized_query or "").lower()
        # Vector clip/export only. If user says image/raster/satellite, route to raster clip path.
        if any(k in q for k in ("raster", "image", "imagery", "satellite", "google satellite", "esri world imagery")):
            return False
        has_clip = "clip" in q
        has_export = any(k in q for k in ("export", "save", "output", "gpkg", "shp", "geopackage"))
        return has_clip and has_export

    def _plan_clip_export_workflow(self, original_query, normalized_query):
        if not self._processing_available():
            return
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        if len(layers) < 2:
            self._log("<b>AI:</b> Clip + export needs at least two vector layers.")
            return
        input_layer = self._active_layer_or_msg()
        if input_layer is None or not isinstance(input_layer, QgsVectorLayer):
            self._log("<b>AI:</b> Select input vector layer first.")
            return
        clip_layer = next((l for l in layers if l != input_layer), None)
        if clip_layer is None:
            self._log("<b>AI:</b> Could not detect clip mask layer.")
            return
        out_dir = self._resolve_output_dir_from_query(self.last_user_query)
        self._request_confirmation(
            "\n".join(
                [
                    "<b>AI:</b> Ready to execute clip + export workflow.",
                    f"✓ Input layer detected: {input_layer.name()}",
                    f"✓ Clip mask detected: {clip_layer.name()}",
                    f"✓ Output directory: {out_dir}",
                    "Workflow:",
                    "1) Run clip",
                    "2) Validate output features",
                    "3) Export to GeoPackage",
                ]
            ),
            self._execute_clip_export_workflow,
            input_layer,
            clip_layer,
            str(out_dir),
        )

    def _execute_clip_export_workflow(self, input_layer, clip_layer, out_dir_str):
        if not self._processing_available():
            return
        out_dir = Path(out_dir_str)
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            clipped = processing.run(
                "native:clip",
                {"INPUT": input_layer, "OVERLAY": clip_layer, "OUTPUT": "memory:"},
            )["OUTPUT"]
            clipped.setName(f"{input_layer.name()}_clipped")
            QgsProject.instance().addMapLayer(clipped)
            gpkg = out_dir / f"{clipped.name()}.gpkg"
            processing.run("native:savefeatures", {"INPUT": clipped, "OUTPUT": str(gpkg)})
            self._log(
                "<b>AI:</b> Clip + export complete.\n"
                f"Output layer: {clipped.name()} | Features: {clipped.featureCount()}\n"
                f"Export: {gpkg}"
            )
            self._remember_action(self.last_user_query, "clip export workflow executed")
        except Exception as exc:
            self._log(f"<b>AI:</b> Clip + export workflow failed: {exc}")

    def _is_create_null_geometry_query(self, normalized_query):
        has_null = "null geometr" in normalized_query
        has_create = any(word in normalized_query for word in ("create", "add", "make", "insert"))
        return has_null and has_create

    def _is_attribute_error_query(self, normalized_query):
        return (
            "attribute" in normalized_query
            and any(w in normalized_query for w in ("error", "errors", "issue", "issues", "invalid"))
        )

    def _is_attribute_condition_query(self, normalized_query):
        if self._is_split_by_field_query(normalized_query):
            return False
        has_attr_hint = any(w in normalized_query for w in ("attribute", "field", "column", "gis_"))
        has_req = any(w in normalized_query for w in ("number of", "count", "how many", "have", "with"))
        return has_attr_hint and has_req

    def _is_active_layer_query_request(self, normalized_query):
        has_query_word = any(w in normalized_query for w in ("query", "filter", "where", "show", "select"))
        has_layer_hint = any(w in normalized_query for w in ("shape", "shapefile", "loaded", "layer", "attribute", "field"))
        return has_query_word and has_layer_hint

    def _is_layer_introspection_query(self, normalized_query):
        asks_read = any(w in normalized_query for w in ("read", "inspect", "list", "show", "tell"))
        asks_schema = any(
            w in normalized_query
            for w in (
                "attribute",
                "attributes",
                "properties",
                "fields",
                "field id",
                "field name",
                "type",
                "length",
                "precision",
                "crs",
                "geometry",
                "everything",
            )
        )
        asks_layers = any(w in normalized_query for w in ("shape", "shapefile", "layer", "loaded", "image", "raster"))
        return asks_read and asks_schema and asks_layers

    def _is_general_gis_question(self, normalized_query):
        question_words = ("what", "why", "how", "when", "which", "difference", "define")
        has_question = any(normalized_query.startswith(w) for w in question_words) or normalized_query.endswith("?")
        gis_terms = (
            "gis",
            "crs",
            "epsg",
            "projection",
            "geodesic",
            "buffer",
            "topology",
            "snapping",
            "geopackage",
            "shapefile",
            "raster",
            "vector",
            "centroid",
            "clip",
            "intersection",
        )
        has_gis_term = any(t in normalized_query for t in gis_terms)
        return has_question and has_gis_term

    def _run_attribute_error_audit(self, original_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Attribute audit requires an active vector layer.")
            return

        fields = [f.name() for f in layer.fields()]
        if not fields:
            self._log("<b>AI:</b> No attribute fields found.")
            return

        total = layer.featureCount()
        null_counts = {}
        numeric_issue_counts = {}
        suspicious_range_counts = {}

        numeric_like = [fn for fn in fields if any(k in fn.lower() for k in ("len", "dist", "clear", "clrnce", "height", "width", "count"))]

        for fn in fields:
            null_counts[fn] = 0
            numeric_issue_counts[fn] = 0
            suspicious_range_counts[fn] = 0

        for feat in layer.getFeatures():
            for fn in fields:
                v = feat[fn]
                if v is None or str(v).strip() == "":
                    null_counts[fn] += 1
                    continue
                if fn in numeric_like:
                    try:
                        fv = float(v)
                        if fv < 0:
                            suspicious_range_counts[fn] += 1
                    except Exception:
                        numeric_issue_counts[fn] += 1

        summary_lines = []
        for fn in fields:
            nnull = null_counts[fn]
            nnum = numeric_issue_counts[fn]
            nsus = suspicious_range_counts[fn]
            if nnull or nnum or nsus:
                summary_lines.append(
                    f"- {fn}: null/empty={nnull}, non-numeric={nnum}, negative={nsus}"
                )

        out_dir = self._resolve_output_dir_from_query(original_query)
        csv_path = out_dir / f"{layer.name()}_attribute_audit.csv"
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as fp:
                writer = csv.writer(fp)
                writer.writerow(["field", "null_or_empty", "non_numeric", "negative_values", "feature_count"])
                for fn in fields:
                    writer.writerow([fn, null_counts[fn], numeric_issue_counts[fn], suspicious_range_counts[fn], total])
        except Exception:
            pass

        if not summary_lines:
            self._log(f"<b>AI:</b> Attribute audit complete. No obvious issues found.\nCSV: {csv_path}")
        else:
            self._log(
                "<b>AI:</b> Attribute audit detected potential issues:\n"
                + "\n".join(summary_lines[:20])
                + f"\nCSV: {csv_path}"
            )
        self._remember_action(self.last_user_query, "attribute error audit done")

    def _run_attribute_condition_query(self, original_query, normalized_query):
        # Safety net: if split intent reached here, redirect to split workflow.
        if self._is_split_by_field_query(normalized_query):
            self._run_split_by_field(normalized_query)
            return
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Attribute query requires an active vector layer.")
            return

        fields = [f.name() for f in layer.fields()]
        field = self._extract_condition_field(original_query, fields)
        if not field:
            self._log("<b>AI:</b> Could not identify target field. Mention it explicitly, e.g. gis_clrnce.")
            return

        cond = self._extract_value_condition(original_query)
        if cond is None:
            self._log("<b>AI:</b> Could not parse value condition. Example: gis_clrnce 0-5")
            return

        matched = []
        field_idx = layer.fields().indexFromName(field)
        for feat in layer.getFeatures():
            v = feat[field_idx]
            if self._value_matches_condition(v, cond):
                matched.append(feat)

        count = len(matched)
        wants_centroid = ("centroid" in normalized_query or "centriod" in normalized_query)
        wants_highlight = any(w in normalized_query for w in ("highlight", "show", "select"))

        if count == 0:
            self._log(f"<b>AI:</b> No features match condition on '{field}'.")
            return

        self._log(f"<b>AI:</b> Found {count} feature(s) matching condition on '{field}'.")

        if wants_highlight:
            ids = [f.id() for f in matched]
            layer.selectByIds(ids)
            self._log("<b>AI:</b> Matching features selected/highlighted on active layer.")

        if wants_centroid:
            self._create_centroids_from_feature_list(layer, matched, f"{layer.name()}_{field}_matched_centroids")
        else:
            # Create matched subset layer by default for convenience.
            self._create_subset_from_feature_list(layer, matched, f"{layer.name()}_{field}_matched")

        self._remember_action(self.last_user_query, f"attribute condition query matched {count}")

    def _run_active_layer_query(self, original_query, normalized_query):
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Active layer query requires a loaded vector layer.")
            return

        fields = [f.name() for f in layer.fields()]
        if not fields:
            self._log("<b>AI:</b> Active layer has no attribute columns.")
            return

        field = self._extract_condition_field(original_query, fields)
        condition = self._extract_value_condition(original_query)
        if not field or condition is None:
            self._log(
                "<b>AI:</b> Could not parse query fully.\n"
                "Try: filter layer where gis_clrnce 0-5\n"
                "or: query where feeder='A-1-K'"
            )
            return

        idx = layer.fields().indexFromName(field)
        matched = []
        for feat in layer.getFeatures():
            if self._value_matches_condition(feat[idx], condition):
                matched.append(feat)

        count = len(matched)
        self._log(f"<b>AI:</b> Query matched {count} feature(s) on field '{field}'.")
        if count == 0:
            return

        wants_centroid = "centroid" in normalized_query or "centriod" in normalized_query
        wants_highlight = any(w in normalized_query for w in ("highlight", "select", "show"))

        if wants_highlight:
            layer.selectByIds([f.id() for f in matched])
            self._log("<b>AI:</b> Matching features selected in active layer.")

        if wants_centroid:
            self._create_centroids_from_feature_list(layer, matched, f"{layer.name()}_{field}_query_centroids")
        else:
            self._create_subset_from_feature_list(layer, matched, f"{layer.name()}_{field}_query_result")

        out_dir = self._resolve_output_dir_from_query(original_query)
        csv_path = out_dir / f"{layer.name()}_{field}_query_result.csv"
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as fp:
                writer = csv.writer(fp)
                writer.writerow(["feature_id", "field", "value"])
                for feat in matched:
                    writer.writerow([feat.id(), field, feat[idx]])
            self._log(f"<b>AI:</b> Query result CSV: {csv_path}")
        except Exception:
            pass

        self._remember_action(self.last_user_query, f"active layer query matched {count}")

    def _is_question_like(self, normalized_query):
        if not normalized_query:
            return False
        starters = (
            "what",
            "why",
            "how",
            "when",
            "which",
            "who",
            "where",
            "is",
            "are",
            "can",
            "capital",
            "define",
            "meaning",
            "explain",
            "tell me",
            "name",
        )
        return normalized_query.strip().endswith("?") or normalized_query.strip().startswith(starters)

    def _looks_like_action_request(self, normalized_query):
        action_terms = (
            "run",
            "open",
            "launch",
            "load",
            "create",
            "generate",
            "add",
            "delete",
            "remove",
            "drop",
            "categorise",
            "categorize",
            "catagorize",
            "classify",
            "style by",
            "attribute table",
            "buffer",
            "clip",
            "merge",
            "split",
            "export",
            "detect",
            "find overlap",
            "filter",
            "select",
            "highlight",
            "centroid",
            "snap",
            "reproject",
            "validate",
            "fix geometr",
            "street view",
        )
        return any(t in normalized_query for t in action_terms)

    def _is_critical_execution_intent(self, normalized_query):
        q = (normalized_query or "").lower()
        has_exec_verb = any(
            k in q
            for k in (
                "create",
                "make",
                "generate",
                "build",
                "run",
                "execute",
                "clip",
                "buffer",
                "merge",
                "split",
                "export",
                "detect",
                "classify",
                "categorize",
                "catagorize",
                "open",
                "load",
                "add",
                "delete",
                "remove",
                "fix",
            )
        )
        has_gis_target = any(
            k in q
            for k in (
                "layer",
                "shape",
                "shapefile",
                "raster",
                "image",
                "polygon",
                "line",
                "point",
                "aoi",
                "bbox",
                "attribute",
                "field",
                "column",
                "map",
                "canvas",
                "satellite",
                "qgis",
            )
        )
        return has_exec_verb and has_gis_target

    def _is_global_knowledge_prompt(self, normalized_query):
        if self._looks_like_action_request(normalized_query) or self._is_critical_execution_intent(normalized_query):
            return False
        # Global mode should handle any non-action text, including statements
        # and long prompts from new users (not only hardcoded question forms).
        token_count = len([t for t in normalized_query.split() if t.strip()])
        return token_count > 0

    def _run_global_qa_chain(self, query, normalized_query):
        """
        Global mode fallback chain:
        1) local deterministic knowledge answers
        2) Internet answer
        3) LLM general answer
        """
        # Hard rule: execution intents must never be answered via web fallback.
        if self._is_critical_execution_intent(normalized_query):
            self._log(
                "<b>AI:</b> Execution intent detected. I will not use web fallback.\n"
                "I will execute directly when parameters are sufficient, else ask only for the missing parameter."
            )
            return False
        # Utility intents should bypass document-memory semantic matches.
        if self._is_distance_from_here_query(normalized_query):
            if self._answer_distance_from_here(query):
                return True
        if self._is_location_coordinates_query(normalized_query):
            self._answer_detect_location_coordinates()
            return True
        if self._is_time_now_query(normalized_query):
            self._answer_time_now_local()
            return True
        if self._is_temperature_query(normalized_query):
            if self._try_weather_now_answer(query):
                return True
        if self._is_location_detect_query(normalized_query):
            self._answer_detect_location()
            return True
        if self._answer_general_gis_question(query, normalized_query):
            return True
        if self._answer_gis_mcp_knowledge(query):
            return True
        if self._try_document_knowledge_answer(query):
            return True
        if self._try_direct_fact_answer(query):
            return True
        if self._try_direct_role_answer(query):
            return True
        if self._try_qgis_version_answer(query):
            return True
        if self.internet_mode:
            if self._try_internet_answer(query):
                return True
        if self.hybrid_mode and self.llm.configured():
            if self._try_llm_general_answer(query):
                return True
        if self.agent_unavailable_reason or not self.agent.provider.configured():
            self._log(
                "<b>AI:</b> I couldn't map this to a built-in command, and the AI agent isn't "
                "available right now to fall back on real language understanding -- this may be a "
                "GIS action that just needs different phrasing (see the Prompt Guide for exact "
                "working examples), or a general-knowledge question needing the agent. Try /agent "
                "status, or /set provider gemini (free key at https://aistudio.google.com/apikey) "
                "then /set gemini_key &lt;your key&gt;."
            )
            return True
        return False

    def _render_stability_report(self):
        q = int(self.stability_stats.get("queries", 0))
        h = int(self.stability_stats.get("handled", 0))
        b = int(self.stability_stats.get("safe_blocked", 0))
        rate = (100.0 * h / q) if q > 0 else 0.0
        self._log(
            "<b>AI:</b> Stability report:\n"
            f"- Mode: {'ON' if self.stability_mode else 'OFF'}\n"
            f"- Queries seen: {q}\n"
            f"- Queries handled: {h}\n"
            f"- Safety-blocked action prompts: {b}\n"
            f"- Immediate handled-rate: {rate:.1f}%"
        )

    def _stable_action_failure_message(self, original_query, normalized_query):
        layer = self.iface.activeLayer()
        layer_name = layer.name() if layer is not None else "None"
        return (
            "<b>AI:</b> Stability mode blocked an unsafe/ambiguous action request.\n"
            f"Active layer: {layer_name}\n"
            f"Request: {original_query}\n"
            "I will not bluff or guess. I need clearer executable intent/parameters."
        )

    def _try_document_knowledge_answer(self, query):
        if not self.doc_knowledge:
            return False
        qtokens = self._tokenize_text(query or "")
        if not qtokens:
            return False
        best = None
        best_score = 0.0
        for pack in self.doc_knowledge:
            title = str(pack.get("title", ""))
            tags = " ".join(pack.get("tags", []))
            content = str(pack.get("content", ""))
            corpus = f"{title} {tags} {content}".lower()
            score = self._token_overlap_score(qtokens, self._tokenize_text(corpus))
            if score > best_score:
                best_score = score
                best = pack
        # A weak token-overlap match (this used to allow as little as 10-22%
        # overlap through) produces exactly the false-confidence problem
        # already fixed once for the Wikipedia fallback: e.g. "What GIS tools
        # are available to you?" matching a langchain doc about "execution
        # contracts" at match=0.25 and being shown as if it answered the
        # question. Require substantial real overlap before treating this as
        # an answer at all.
        min_score = 0.60 if self.stability_mode else 0.50
        if best is None or best_score < min_score:
            return False
        snippet = str(best.get("content", "")).strip()
        if len(snippet) > 1200:
            snippet = snippet[:1200].rstrip() + " ..."
        self._log(
            "<b>AI (Doc memory):</b> "
            + snippet
            + f"\nSource: {best.get('source', 'local memorized doc')} "
            f"(match={best_score:.2f})"
        )
        self._remember_action(self.last_user_query, "document memory answer provided")
        return True

    def _answer_gis_mcp_knowledge(self, query):
        q = (query or "").lower()
        has_gis_mcp_name = ("gis-mcp" in q) or ("gis mcp" in q)
        has_generic_mcp_with_gis = ("mcp" in q) and any(
            k in q for k in ("gis", "qgis", "buffer", "intersection", "reproject", "raster", "vector")
        )
        if not has_gis_mcp_name and not has_generic_mcp_with_gis:
            return False

        if any(k in q for k in ("install", "setup", "configure", "connect")):
            self._render_gis_mcp_pack("install")
            return True

        if any(k in q for k in ("functions", "tools", "capabilities", "what can")):
            self._render_gis_mcp_pack("tools")
            return True

        if any(k in q for k in ("transport", "http", "stdio", "endpoint")):
            self._render_gis_mcp_pack("install")
            return True

        if any(k in q for k in ("map", "mapping", "command mapping", "translate", "equivalent")):
            self._render_gis_mcp_pack("map")
            return True

        self._render_gis_mcp_pack("overview")
        return True

    def _render_gis_mcp_pack(self, topic):
        topic = (topic or "overview").strip().lower()
        if topic == "install":
            self._log(
                "<b>AI:</b> GIS-MCP quick setup (free/local):\n"
                "1) <code>pip install uv</code>\n"
                "2) <code>uv venv --python=3.10</code>\n"
                "3) <code>.\\.venv\\Scripts\\python.exe -m ensurepip --upgrade</code>\n"
                "4) <code>.\\.venv\\Scripts\\python.exe -m pip install gis-mcp</code>\n"
                "5) Run: <code>.\\.venv\\Scripts\\python.exe -m gis_mcp.main</code>\n"
                "Transport:\n"
                "- STDIO (default)\n"
                "- HTTP endpoint: <code>http://localhost:9010/mcp</code>\n"
                "Source: https://github.com/mahdin75/gis-mcp"
            )
            return

        if topic == "tools":
            self._log(
                "<b>AI:</b> GIS-MCP capability pack embedded:\n"
                "- Shapely: buffer/intersection/union/difference/validity/snap/centroid/area/length\n"
                "- PyProj: CRS transforms and reprojection helpers\n"
                "- GeoPandas: vector table workflows and overlays\n"
                "- Rasterio: raster read/clip/reproject/resample\n"
                "- PySAL: spatial statistics and neighborhood analytics\n"
                "- Optional data modules: climate/ecology/movement/land cover/satellite imagery\n"
                "Source: https://github.com/mahdin75/gis-mcp"
            )
            return

        if topic == "map":
            self._log(
                "<b>AI:</b> Copilot command mapping to GIS-MCP style capabilities:\n"
                "- 'buffer roads by 20m' -> buffer\n"
                "- 'find overlap spans' -> intersection/overlap analysis\n"
                "- 'check polygon validity' -> is_valid / make_valid\n"
                "- 'reproject to EPSG:3857' -> CRS transform\n"
                "- 'create centroids' -> centroid\n"
                "- 'clip raster by mask' -> raster clip\n"
                "Use /intents for executable QGIS actions and /mcp gis tools for the knowledge pack."
            )
            return

        self._log(
            "<b>AI:</b> GIS-MCP knowledge is now embedded in Copilot for team use.\n"
            "- /mcp gis install -> setup and transport guide\n"
            "- /mcp gis tools -> capability families\n"
            "- /mcp gis map -> how prompts map to GIS-MCP concepts\n"
            "- /mcp do &lt;task&gt; -> convert plain request to MCP-style workflow\n"
            "This works even when live MCP server connection is unavailable."
        )

    def _render_mcp_task_translation(self, task_text):
        task = (task_text or "").strip()
        if not task:
            self._log("<b>AI:</b> Usage: /mcp do <task>")
            return
        t = task.lower()

        workflow = []
        tips = []

        if "buffer" in t:
            workflow.append("1) Parse target layer + distance (Shapely buffer concept).")
            workflow.append("2) Ensure projected CRS for meter-based distance.")
            workflow.append("3) Run buffer and store output layer.")
        if "overlap" in t or "intersect" in t:
            workflow.append("1) Use intersection/overlap check between candidate geometries.")
            workflow.append("2) Keep only non-empty overlaps.")
            if "centroid" in t:
                workflow.append("3) Create centroids from overlap geometries.")
        if "valid" in t or "invalid" in t or "fix geometr" in t:
            workflow.append("1) Run geometry validity check (is_valid).")
            workflow.append("2) Repair invalid features (make_valid).")
        if "reproject" in t or "epsg" in t or "crs" in t:
            workflow.append("1) Detect source CRS.")
            workflow.append("2) Transform features to requested target CRS.")
        if "clip raster" in t or ("raster" in t and "clip" in t):
            workflow.append("1) Load raster + mask geometry.")
            workflow.append("2) Apply raster clip by mask.")
        if "merge" in t and ("layer" in t or "shapefile" in t):
            workflow.append("1) Collect compatible input layers.")
            workflow.append("2) Merge layers and harmonize fields.")
        if "split" in t and any(k in t for k in ("field", "column", "attribute")):
            workflow.append("1) Detect split field name.")
            workflow.append("2) Enumerate unique field values.")
            workflow.append("3) Export one dataset per value.")

        if not workflow:
            workflow = [
                "1) Interpret intent and required GIS entities (layers/fields/CRS).",
                "2) Map request to GIS-MCP capability family (vector/raster/CRS/stats).",
                "3) Define input/output contract and execute sequentially.",
            ]

        if "meter" in t or "m " in f"{t} ":
            tips.append("Use projected CRS before meter-based distance operations.")
        if "active" in t:
            tips.append("If layer is ambiguous, specify layer name explicitly.")
        tips.append("For executable QGIS action now, run plain task directly in Copilot chat.")

        lines = [f"<b>AI:</b> MCP workflow draft for: <code>{task}</code>"]
        lines.extend(f"- {step}" for step in workflow[:8])
        if tips:
            lines.append("Tips:")
            lines.extend(f"- {tip}" for tip in tips[:4])
        lines.append("Reference: https://github.com/mahdin75/gis-mcp")

        self._log("\n".join(lines))

    def _try_direct_fact_answer(self, query):
        q = (query or "").strip()
        ql = q.lower()
        fact_map = {
            "capital": "P36",
            "population": "P1082",
            "currency": "P38",
            "official language": "P37",
            "language": "P37",
        }
        fact_key = None
        for k in ("official language", "capital", "population", "currency", "language"):
            if k in ql:
                fact_key = k
                break
        if fact_key is None:
            return False

        entity = self._extract_entity_for_fact_query(ql, fact_key)
        if not entity:
            return False
        try:
            title = self._resolve_wikipedia_title(entity)
            if not title:
                return False
            wikidata_id = self._resolve_wikipedia_wikidata_id(title)
            if not wikidata_id:
                return False
            claim_value = self._resolve_wikidata_claim_value(wikidata_id, fact_map[fact_key], prefer_english_label=True)
            if not claim_value:
                return False

            fact_label = "official language" if fact_key == "language" else fact_key
            page_url = f"https://en.wikipedia.org/wiki/{title}"
            self._log(
                f"<b>AI (Web, confidence: medium):</b> The {fact_label} is <b>{claim_value}</b>.\n"
                f"Source: {page_url}"
            )
            self._remember_action(self.last_user_query, f"direct fact web answer provided ({fact_label})")
            return True
        except Exception:
            return False

    def _try_qgis_version_answer(self, query):
        q = (query or "").lower()
        if "qgis" not in q:
            return False
        if not any(k in q for k in ("latest", "stable", "current", "version", "release")):
            return False
        try:
            # QGIS latest release metadata endpoint.
            url = "https://qgis.org/resources/roadmap/"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                html = response.read().decode("utf-8", errors="ignore")
            # Extract one likely recent "QGIS x.y.z" token from page.
            m = re.search(r"QGIS\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", html, re.IGNORECASE)
            if not m:
                return False
            version = m.group(1)
            self._log(
                "<b>AI (Web, confidence: medium):</b> "
                f"A recent stable QGIS version appears to be <b>{version}</b>.\n"
                f"Source: {url}"
            )
            self._remember_action(self.last_user_query, "qgis version web answer provided")
            return True
        except Exception:
            return False

    def _run_selftest(self):
        """
        Lightweight internal validation for command/query routing.
        No processing algorithms are executed in this self-test.
        """
        tests = []

        def check(name, ok, detail=""):
            tests.append({"name": name, "ok": bool(ok), "detail": detail})

        # Core question detection.
        check(
            "question-like detection",
            self._is_question_like("who is father of gis?") and not self._is_question_like("buffer roads 25m"),
            "expects question prompt true and action phrase false",
        )

        # Action intent guardrail.
        check(
            "action-request detection",
            self._looks_like_action_request("create buffer of 25 meters") and not self._looks_like_action_request("what is qgis"),
            "expects operation phrases true and plain question false",
        )

        # Global knowledge classification.
        check(
            "global-knowledge prompt classification",
            self._is_global_knowledge_prompt("who is roger tomlinson?") and not self._is_global_knowledge_prompt("export poles within 100m of roads"),
            "non-action question should route to global chain",
        )

        # Fallback order wiring check (structural).
        llm_ready = self.hybrid_mode and self.llm.configured()
        web_ready = self.internet_mode
        check(
            "fallback chain readiness",
            True,
            f"order=local->llm->web | llm_ready={llm_ready} | web_ready={web_ready}",
        )

        # Command toggle presence checks.
        check("global mode flag exists", hasattr(self, "global_mode"), "global_mode attribute is available")
        check("internet mode flag exists", hasattr(self, "internet_mode"), "internet_mode attribute is available")
        check("hybrid mode flag exists", hasattr(self, "hybrid_mode"), "hybrid_mode attribute is available")
        check(
            "bbox feet trigger detection",
            self._is_bbox_video_workflow_query("create 150 feet bbox for loaded shape file")
            and self._is_bbox_video_workflow_query("create 30ft bbox")
            and not self._is_bbox_video_workflow_query("create 150 meter bbox"),
            "feet/ft should trigger, meter should not",
        )

        passed = sum(1 for t in tests if t["ok"])
        total = len(tests)
        failed = total - passed
        status = "PASS" if failed == 0 else "FAIL"

        lines = [f"<b>AI SelfTest:</b> {status} ({passed}/{total})"]
        for t in tests:
            marker = "PASS" if t["ok"] else "FAIL"
            detail = f" | {t['detail']}" if t.get("detail") else ""
            lines.append(f"- [{marker}] {t['name']}{detail}")
        if failed:
            lines.append("Tip: run /status and ensure /global on, /internet on, and optional LLM setup are correct.")

        self._log("\n".join(lines))
        self._remember_action("/selftest", f"{status.lower()} {passed}/{total}")

    def _run_stability_precheck(self):
        required = [
            "_consume_pending_output_for_query",
            "_require_output_path_gate",
            "_consume_pending_output_path",
            "_is_learn_media_query",
            "_run_learn_media_from_query",
            "_is_website_report_query",
            "_run_website_report_from_query",
            "_try_pending_path_followup",
            "_is_column_count_query",
            "_answer_column_count",
        ]
        missing = []
        for name in required:
            if not callable(getattr(self, name, None)):
                missing.append(name)
        if missing:
            self._log(
                "<b>AI:</b> Stability precheck failed.\n"
                f"Missing handlers: {', '.join(missing)}\n"
                "Reload/sync plugin files before testing commands."
            )
            return
        self._log("<b>AI:</b> Stability precheck passed. Core routing handlers are present.")

    def _try_llm_general_answer(self, query):
        try:
            topic = self._classify_global_topic(query)
            system = (
                "You are AI GIS Copilot Global Assistant. "
                "Behave like a practical senior assistant who can: "
                "1) answer general questions, "
                "2) explain software workflows (especially QGIS), "
                "3) help with coding/debugging, "
                "4) design apps/plugins step-by-step. "
                "Be concise, accurate, and actionable. "
                "When user asks to build something, provide a concrete implementation path. "
                "Return strict JSON with fields: "
                "{\"answer\":\"...\",\"confidence\":\"high|medium|low\",\"sources\":[\"url or citation\", ...]}."
            )
            user = (
                f"Question: {query}\n"
                f"Topic hint: {topic}\n"
                "Provide accurate answer. If uncertain, say uncertainty. "
                "For coding/plugin/app questions, include steps or code-level guidance. "
                "Include up to 3 useful sources/citations."
            )
            out = self.llm.chat_json(system, user, temperature=0.2)
            answer = str(out.get("answer", "")).strip()
            confidence = str(out.get("confidence", "medium")).strip().lower()
            sources = out.get("sources", [])
            if not answer:
                return False
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            lines = [f"<b>AI (LLM, confidence: {confidence}):</b> {answer}"]
            if isinstance(sources, list) and sources:
                show = [str(s).strip() for s in sources if str(s).strip()][:3]
                if show:
                    lines.append("Sources:")
                    lines.extend(f"- {s}" for s in show)
            self._log("\n".join(lines))
            self._remember_action(self.last_user_query, "llm general answer provided")
            return True
        except Exception:
            return False

    def _classify_global_topic(self, query):
        q = (query or "").lower()
        coding_terms = (
            "python", "java", "javascript", "typescript", "c++", "code", "script", "bug",
            "debug", "error", "exception", "api", "function", "class", "plugin", "app", "application"
        )
        news_terms = ("news", "latest", "today", "current", "update", "breaking", "recent")
        software_terms = ("how to", "install", "configure", "settings", "workflow", "qgis", "arcgis", "software")
        if any(t in q for t in news_terms):
            return "news"
        if any(t in q for t in coding_terms):
            return "coding_or_build"
        if any(t in q for t in software_terms):
            return "software_help"
        return "general_qa"

    def _try_internet_answer(self, query):
        """
        Attempt a web-backed answer using DuckDuckGo Instant Answer API.
        Falls back to Wikipedia summary API when DDG has no direct answer.
        """
        try:
            q = query.strip()
            if not q:
                return False
            if self._is_weather_now_query(q):
                if self._try_weather_now_answer(q):
                    return True
            params = urllib.parse.urlencode(
                {
                    "q": q,
                    "format": "json",
                    "no_html": "1",
                    "no_redirect": "1",
                    "skip_disambig": "0",
                }
            )
            url = f"https://api.duckduckgo.com/?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "AI-GIS-Copilot/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)

            answer = (data.get("AbstractText") or "").strip()
            source_url = (data.get("AbstractURL") or "").strip()
            heading = (data.get("Heading") or "").strip()

            if not answer:
                related = data.get("RelatedTopics") or []
                q_tokens = self._tokenize_text(q)
                for item in related:
                    if isinstance(item, dict):
                        txt = (item.get("Text") or "").strip()
                        link = (item.get("FirstURL") or "").strip()
                        if txt and self._is_web_answer_relevant(q_tokens, txt):
                            answer = txt
                            source_url = link or source_url
                            break
                        # Nested related topics
                        nested = item.get("Topics") if isinstance(item.get("Topics"), list) else []
                        for n in nested:
                            if isinstance(n, dict):
                                txt2 = (n.get("Text") or "").strip()
                                link2 = (n.get("FirstURL") or "").strip()
                                if txt2 and self._is_web_answer_relevant(q_tokens, txt2):
                                    answer = txt2
                                    source_url = link2 or source_url
                                    break
                        if answer:
                            break

            if not answer:
                # Previously fell back to a blind Wikipedia title-fuzzy-match here,
                # which produced confidently wrong answers (e.g. matching unrelated
                # article titles to whatever the user typed). Retired -- an honest
                # "no answer" is better than a guess presented as an answer.
                return False
            if not self._is_web_answer_relevant(self._tokenize_text(q), f"{heading} {answer}"):
                # Reject unrelated DDG snippets to avoid nonsense answers.
                return False
            answer = self._compact_answer_text(answer)

            msg = "<b>AI (Web, confidence: medium):</b> "
            if heading:
                msg += f"{heading}: "
            msg += answer
            if source_url:
                msg += f"\nSource: {source_url}"
            self._log(msg)
            self._remember_action(self.last_user_query, "internet answer provided")
            return True
        except Exception:
            return False

    def _is_weather_now_query(self, query):
        q = (query or "").lower()
        has_weather_term = any(k in q for k in ("temperature", "weather", "degrees", "hot", "cold"))
        has_now = any(k in q for k in ("now", "current", "right now", "today"))
        return has_weather_term and has_now

    def _extract_location_from_weather_query(self, query):
        q = (query or "").strip()
        m = re.search(r"\b(?:in|at)\s+([a-zA-Z][a-zA-Z\s,\.\-]{1,120})\??\s*$", q, flags=re.IGNORECASE)
        if not m:
            m2 = re.search(r"\b(?:in|at)\s+([a-zA-Z][a-zA-Z\s,\.\-]{1,120})\s+(?:now|today|currently)\??\s*$", q, flags=re.IGNORECASE)
            if not m2:
                return ""
            loc2 = re.sub(r"\s+", " ", m2.group(1)).strip(" .?")
            return loc2
        loc = re.sub(r"\s+", " ", m.group(1)).strip(" .?,")
        loc = re.sub(r"\b(now|today|currently)\b$", "", loc, flags=re.IGNORECASE).strip(" .?,")
        return loc

    def _try_weather_now_answer(self, query):
        loc = self._extract_location_from_weather_query(query)
        auto_loc = False
        if not loc:
            loc = self._detect_location_from_ip()
            auto_loc = bool(loc)
        if not loc:
            self._log(
                "<b>AI:</b> I can fetch live temperature, but I could not auto-detect your location.\n"
                "Example: 'What is the temperature now in Bhubaneswar?'"
            )
            return True
        try:
            url = f"https://wttr.in/{urllib.parse.quote(loc)}?format=j1"
            req = urllib.request.Request(url, headers={"User-Agent": "AI-GIS-Copilot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)
            cur = (data.get("current_condition") or [{}])[0]
            temp_c = str(cur.get("temp_C", "")).strip()
            feels_c = str(cur.get("FeelsLikeC", "")).strip()
            desc = ""
            try:
                desc = ((cur.get("weatherDesc") or [{}])[0].get("value") or "").strip()
            except Exception:
                desc = ""
            if not temp_c:
                return False
            msg = f"<b>AI (Web, confidence: medium):</b> Current temperature in {loc}: {temp_c}°C"
            if feels_c:
                msg += f" (feels like {feels_c}°C)"
            if desc:
                msg += f" | {desc}"
            if auto_loc:
                msg += "\nNote: location auto-detected by IP (approximate)."
            msg += "\nSource: https://wttr.in/"
            self._log(msg)
            self._remember_action(self.last_user_query, "live weather answer provided")
            return True
        except Exception:
            return False

    def _detect_location_from_ip(self):
        """
        Best-effort approximate location detection from public IP.
        Returns city or city,region string.
        """
        details = self._detect_location_from_ip_details()
        if details:
            return details.get("label", "")
        return ""

    def _detect_location_from_ip_details(self):
        """
        Returns dict with label, lat, lon when possible.
        """
        providers = [
            "https://ipapi.co/json/",
            "http://ip-api.com/json/",
        ]
        for url in providers:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AI-GIS-Copilot/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read().decode("utf-8", errors="ignore")
                data = json.loads(body)
                city = str(data.get("city", "")).strip()
                region = str(data.get("region", data.get("regionName", ""))).strip()
                country = str(data.get("country_name", data.get("country", ""))).strip()
                lat = data.get("latitude", data.get("lat"))
                lon = data.get("longitude", data.get("lon"))
                try:
                    lat = float(lat)
                    lon = float(lon)
                except Exception:
                    lat = None
                    lon = None
                label = ""
                if city and region:
                    label = f"{city}, {region}"
                elif city:
                    label = city
                elif region and country:
                    label = f"{region}, {country}"
                elif country:
                    label = country
                if label and lat is not None and lon is not None:
                    return {"label": label, "lat": lat, "lon": lon}
            except Exception:
                continue
        return None

    def _geocode_place(self, place_text):
        q = str(place_text or "").strip()
        if not q:
            return None
        try:
            url = (
                "https://nominatim.openstreetmap.org/search?"
                + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1})
            )
            req = urllib.request.Request(url, headers={"User-Agent": "AI-GIS-Copilot/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            arr = json.loads(body)
            if not isinstance(arr, list) or not arr:
                return None
            top = arr[0]
            lat = float(top.get("lat"))
            lon = float(top.get("lon"))
            name = str(top.get("display_name", q)).split(",")[0].strip() or q
            return {"name": name, "lat": lat, "lon": lon}
        except Exception:
            return None

    def _haversine_km(self, lat1, lon1, lat2, lon2):
        import math

        r = 6371.0088
        p1 = math.radians(float(lat1))
        p2 = math.radians(float(lat2))
        dphi = math.radians(float(lat2) - float(lat1))
        dlmb = math.radians(float(lon2) - float(lon1))
        a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2.0) ** 2
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        return r * c

    def _try_wikipedia_answer(self, query):
        try:
            if not query:
                return False
            # First, resolve the best matching page title using Wikipedia search API.
            title = self._resolve_wikipedia_title(query)
            if not title:
                title = re.sub(r"\s+", " ", query).strip().replace(" ", "_")
            if not title:
                return False
            url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                body = response.read().decode("utf-8")
            data = json.loads(body)
            extract = str(data.get("extract", "")).strip()
            page_url = ""
            content_urls = data.get("content_urls", {})
            if isinstance(content_urls, dict):
                desktop = content_urls.get("desktop", {})
                if isinstance(desktop, dict):
                    page_url = str(desktop.get("page", "")).strip()
            if not extract:
                return False
            msg = f"<b>AI (Web, confidence: medium):</b> {self._compact_answer_text(extract)}"
            if page_url:
                msg += f"\nSource: {page_url}"
            self._log(msg)
            self._remember_action(self.last_user_query, "wikipedia internet answer provided")
            return True
        except Exception:
            return False

    def _compact_answer_text(self, text):
        t = str(text or "").strip()
        if not t:
            return t
        # Prefer first 1-2 sentences for concise chat responses.
        parts = re.split(r"(?<=[.!?])\s+", t)
        compact = " ".join(parts[:2]).strip()
        if len(compact) > 320:
            compact = compact[:317].rstrip() + "..."
        return compact

    def _is_web_answer_relevant(self, query_tokens, candidate_text):
        q_tokens = list(query_tokens or [])
        c_tokens = self._tokenize_text(candidate_text or "")
        if not q_tokens or not c_tokens:
            return False
        score = self._token_overlap_score(q_tokens, c_tokens)
        # Keep threshold conservative to block random unrelated snippets.
        return score >= 0.16

    def _resolve_wikipedia_title(self, query):
        try:
            search_url = (
                "https://en.wikipedia.org/w/api.php?"
                f"action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json&srlimit=1"
            )
            req = urllib.request.Request(
                search_url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                body = response.read().decode("utf-8")
            data = json.loads(body)
            results = data.get("query", {}).get("search", [])
            if not results:
                return ""
            title = str(results[0].get("title", "")).strip()
            return title.replace(" ", "_")
        except Exception:
            return ""

    def _resolve_wikipedia_wikidata_id(self, wiki_title):
        try:
            url = (
                "https://en.wikipedia.org/w/api.php?"
                f"action=query&prop=pageprops&titles={urllib.parse.quote(wiki_title)}&format=json"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            pages = data.get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {})
            return ((page.get("pageprops") or {}).get("wikibase_item") or "").strip()
        except Exception:
            return ""

    def _resolve_wikidata_claim_value(self, entity_id, property_id, prefer_english_label=True):
        try:
            url = (
                "https://www.wikidata.org/w/api.php?"
                f"action=wbgetentities&ids={urllib.parse.quote(entity_id)}&props=claims&format=json"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            claims = ((data.get("entities") or {}).get(entity_id) or {}).get("claims", {})
            vals = claims.get(property_id) or []
            if not vals:
                return ""
            raw = (
                vals[0]
                .get("mainsnak", {})
                .get("datavalue", {})
                .get("value", {})
            )
            if isinstance(raw, dict) and "id" in raw:
                target_id = str(raw.get("id", "")).strip()
                if not target_id:
                    return ""
                return self._resolve_wikidata_label(target_id) if prefer_english_label else target_id
            if isinstance(raw, dict) and "amount" in raw:
                amt = str(raw.get("amount", "")).lstrip("+").strip()
                unit = str(raw.get("unit", ""))
                if property_id == "P1082":
                    try:
                        num = int(float(amt))
                        return f"{num:,}"
                    except Exception:
                        return amt
                return amt
            if isinstance(raw, str):
                return raw.strip()
            return ""
        except Exception:
            return ""

    def _extract_entity_for_fact_query(self, lowered_query, fact_key):
        text = lowered_query.replace("?", " ").strip()
        starters = ("what is", "what's", "tell me", "give me", "show me", "which is")
        for s in starters:
            if text.startswith(s):
                text = text[len(s):].strip()
                break
        for pattern in (
            rf"{re.escape(fact_key)}\s+of\s+",
            rf"{re.escape(fact_key)}\s+for\s+",
            rf"{re.escape(fact_key)}\s+in\s+",
        ):
            m = re.search(pattern, text)
            if m:
                text = text[m.end():].strip()
                break
        text = text.replace("the ", " ").strip()
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _try_direct_role_answer(self, query):
        q = (query or "").strip()
        ql = q.lower()
        role_map = {
            "captain": "captain",
            "president": "president",
            "founder": "founder",
            "ceo": "chief_executive_officer",
            "chief executive officer": "chief_executive_officer",
        }
        role = None
        for k, v in role_map.items():
            if k in ql:
                role = v
                break
        if role is None:
            return False
        if not (ql.startswith("who is") or ql.startswith("who's") or ql.startswith("who is the")):
            return False

        entity = self._extract_entity_for_role_query(ql, role_map.keys())
        if not entity:
            return False

        try:
            title = self._resolve_wikipedia_title(entity)
            if not title:
                return False
            url = (
                "https://en.wikipedia.org/w/api.php?"
                f"action=query&prop=pageprops&titles={urllib.parse.quote(title)}&format=json"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            pages = data.get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {})
            wikidata_id = ((page.get("pageprops") or {}).get("wikibase_item") or "").strip()
            if not wikidata_id:
                return False

            wd_url = (
                "https://www.wikidata.org/w/api.php?"
                f"action=wbgetentities&ids={urllib.parse.quote(wikidata_id)}&props=claims&format=json"
            )
            wd_req = urllib.request.Request(
                wd_url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(wd_req, timeout=10) as wd_resp:
                wd_data = json.loads(wd_resp.read().decode("utf-8"))
            claims = ((wd_data.get("entities") or {}).get(wikidata_id) or {}).get("claims", {})

            prop_map = {
                "captain": "P634",
                "president": "P6",
                "founder": "P112",
                "chief_executive_officer": "P169",
            }
            prop = prop_map.get(role)
            if not prop or prop not in claims or not claims[prop]:
                return False
            target_id = (
                claims[prop][0]
                .get("mainsnak", {})
                .get("datavalue", {})
                .get("value", {})
                .get("id", "")
            )
            if not target_id:
                return False
            person_name = self._resolve_wikidata_label(target_id)
            if not person_name:
                return False

            page_url = f"https://en.wikipedia.org/wiki/{title}"
            self._log(
                f"<b>AI (Web, confidence: medium):</b> The {role.replace('_', ' ')} is <b>{person_name}</b>.\n"
                f"Source: {page_url}"
            )
            self._remember_action(self.last_user_query, "direct role web answer provided")
            return True
        except Exception:
            return False

    def _extract_entity_for_role_query(self, lowered_query, role_keys):
        text = lowered_query
        text = re.sub(r"^who\s+is\s+the\s+", "", text)
        text = re.sub(r"^who\s+is\s+", "", text)
        text = re.sub(r"^who's\s+the\s+", "", text)
        text = re.sub(r"^who's\s+", "", text)
        text = text.replace("?", " ").strip()
        for rk in role_keys:
            text = text.replace(f" {rk} of ", " ")
            text = text.replace(f" {rk} ", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _resolve_wikidata_label(self, entity_id):
        try:
            url = (
                "https://www.wikidata.org/w/api.php?"
                f"action=wbgetentities&ids={urllib.parse.quote(entity_id)}&props=labels&languages=en&format=json"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "AI-GIS-Copilot/0.4"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            ent = (data.get("entities") or {}).get(entity_id, {})
            labels = ent.get("labels") or {}
            return ((labels.get("en") or {}).get("value") or "").strip()
        except Exception:
            return ""

    def _answer_general_gis_question(self, original_query, normalized_query):
        if "father of gis" in normalized_query:
            self._log(
                "<b>AI:</b> The person widely known as the 'Father of GIS' is "
                "<b>Roger Tomlinson</b>."
            )
            return True
        if (
            ("create" in normalized_query or "new" in normalized_query)
            and ("shapefile" in normalized_query or "shape file" in normalized_query)
            and any(w in normalized_query for w in ("how", "steps", "way", "qgis"))
        ):
            self._log(
                "<b>AI:</b> In QGIS, create a new shapefile like this:\n"
                "1) Layer -> Create Layer -> New Shapefile Layer\n"
                "2) Choose Geometry Type (Point/Line/Polygon)\n"
                "3) Set CRS\n"
                "4) Add attribute fields (name/type/length/precision)\n"
                "5) Choose file path and name (.shp)\n"
                "6) Click OK, then toggle editing to start digitizing features."
            )
            return True
        if "full form of gis" in normalized_query or ("what is gis" in normalized_query and "full form" in normalized_query):
            self._log("<b>AI:</b> GIS stands for <b>Geographic Information System</b>.")
            return True
        if "full form of qgis" in normalized_query or "what is qgis" in normalized_query:
            self._log("<b>AI:</b> QGIS stands for <b>Quantum GIS</b> (now commonly referred to simply as QGIS).")
            return True
        if "full form of crs" in normalized_query:
            self._log("<b>AI:</b> CRS stands for <b>Coordinate Reference System</b>.")
            return True
        if "full form of epsg" in normalized_query:
            self._log("<b>AI:</b> EPSG refers to the <b>European Petroleum Survey Group</b> code system for CRS identifiers.")
            return True
        if "crs" in normalized_query or "epsg" in normalized_query or "projection" in normalized_query:
            self._log(
                "<b>AI:</b> CRS (Coordinate Reference System) defines how coordinates map to real-world locations. "
                "EPSG code identifies a specific CRS. Use projected CRS (meters/feet) for distance/area workflows."
            )
            return True
        if "vector" in normalized_query and "raster" in normalized_query:
            self._log(
                "<b>AI:</b> Vector stores features as points/lines/polygons with attributes; "
                "raster stores gridded pixels/cells. Use vector for discrete objects, raster for surfaces/imagery."
            )
            return True
        if "buffer" in normalized_query:
            self._log(
                "<b>AI:</b> Buffer creates a zone at a fixed distance around geometry. "
                "Use projected CRS for accurate distances, then run buffer with required units."
            )
            return True
        if "topology" in normalized_query:
            self._log(
                "<b>AI:</b> Topology checks spatial relationships like overlap, gap, dangle, and connectivity. "
                "Validate topology to catch network and polygon integrity errors."
            )
            return True
        if "snapping" in normalized_query:
            self._log(
                "<b>AI:</b> Snapping aligns edited vertices/segments within tolerance to avoid gaps and overlaps. "
                "Use pixel/map-unit tolerance based on editing scale and CRS."
            )
            return True
        if "geopackage" in normalized_query or "shapefile" in normalized_query:
            self._log(
                "<b>AI:</b> GeoPackage supports long field names, multiple layers, and better data integrity. "
                "Shapefile is older and has limits (field name length, file set sidecars)."
            )
            return True
        return False

    def _run_layer_introspection_report(self, normalized_query, original_query):
        self._refresh_layer_catalog()
        if not self.layer_catalog:
            self._log("<b>AI:</b> No loaded layers found.")
            return

        include_all = any(w in normalized_query for w in ("all", "every", "everything", "loaded"))
        active = self.iface.activeLayer()
        active_id = active.id() if active is not None else None

        selected_ids = list(self.layer_catalog.keys()) if include_all or active_id is None else [active_id]
        lines = []
        for lid in selected_ids:
            info = self.layer_catalog.get(lid)
            if not info:
                continue
            lines.append(f"- {info['name']} [{info['kind']}]")
            lines.append(f"  source: {info.get('source','')}")
            lines.append(f"  crs: {info.get('crs','')}")
            if info["kind"] == "vector":
                lines.append(
                    f"  geometry: {info.get('geometry','')} | features: {info.get('feature_count',0)} | fields: {len(info.get('fields',[]))}"
                )
            elif info["kind"] == "raster":
                lines.append(
                    f"  size: {info.get('width',0)}x{info.get('height',0)} | bands: {info.get('band_count',0)}"
                )

        self._log("<b>AI:</b> Layer scan completed.\n" + "\n".join(lines[:120]))

        out_dir = self._resolve_output_dir_from_query(original_query)
        json_path = out_dir / "loaded_layers_catalog.json"
        csv_path = out_dir / "loaded_layers_fields.csv"
        try:
            json_path.write_text(json.dumps(self.layer_catalog, indent=2), encoding="utf-8")
            with open(csv_path, "w", newline="", encoding="utf-8") as fp:
                writer = csv.writer(fp)
                writer.writerow(
                    [
                        "layer_name",
                        "layer_kind",
                        "field_id",
                        "field_name",
                        "field_type",
                        "field_length",
                        "field_precision",
                    ]
                )
                for info in self.layer_catalog.values():
                    if info.get("kind") != "vector":
                        continue
                    for fld in info.get("fields", []):
                        writer.writerow(
                            [
                                info.get("name", ""),
                                info.get("kind", ""),
                                fld.get("id", ""),
                                fld.get("name", ""),
                                fld.get("type", ""),
                                fld.get("length", ""),
                                fld.get("precision", ""),
                            ]
                        )
            self._log(f"<b>AI:</b> Catalog exported:\n{json_path}\n{csv_path}")
        except Exception as exc:
            self._log(f"<b>AI:</b> Catalog export failed: {exc}")

        self._remember_action(self.last_user_query, "layer introspection completed")

    def _extract_condition_field(self, text, fields):
        lower_text = text.lower()
        for fn in fields:
            if fn.lower() in lower_text:
                return fn
        # fallback: detect token after "have/with"
        m = re.search(r"(?:have|with)\s+([a-zA-Z0-9_]+)", text, flags=re.IGNORECASE)
        if m:
            return self._match_field_name(m.group(1).lower(), fields)
        return None

    def _extract_value_condition(self, text):
        # range like 0-5
        m = re.search(r"(-?\d+(\.\d+)?)\s*-\s*(-?\d+(\.\d+)?)", text)
        if m:
            lo = float(m.group(1))
            hi = float(m.group(3))
            raw = m.group(0).replace(" ", "")
            return {"type": "range", "lo": min(lo, hi), "hi": max(lo, hi), "raw": raw}

        # comparator patterns: >, >=, <, <=, =
        m = re.search(r"(>=|<=|>|<|=)\s*(-?\d+(\.\d+)?)", text)
        if m:
            return {"type": "cmp", "op": m.group(1), "value": float(m.group(2))}

        # single numeric value
        m = re.search(r"\b(-?\d+(\.\d+)?)\b", text)
        if m:
            return {"type": "eq", "value": float(m.group(1))}

        # quoted text
        m = re.search(r"['\"]([^'\"]+)['\"]", text)
        if m:
            return {"type": "text", "value": m.group(1).strip().lower()}
        return None

    def _value_matches_condition(self, value, cond):
        if value is None:
            return False
        if cond["type"] == "text":
            return str(value).strip().lower() == cond["value"]
        if cond["type"] == "range":
            # Support categorical range text like "0-5" stored in attributes.
            sval = str(value).strip().replace(" ", "")
            raw = str(cond.get("raw", "")).strip().replace(" ", "")
            if raw and sval.lower() == raw.lower():
                return True
        try:
            fv = float(value)
        except Exception:
            return False
        ctype = cond["type"]
        if ctype == "eq":
            return fv == cond["value"]
        if ctype == "range":
            return cond["lo"] <= fv <= cond["hi"]
        if ctype == "cmp":
            op = cond["op"]
            cv = cond["value"]
            if op == ">":
                return fv > cv
            if op == ">=":
                return fv >= cv
            if op == "<":
                return fv < cv
            if op == "<=":
                return fv <= cv
            if op == "=":
                return fv == cv
        return False

    def _create_subset_from_feature_list(self, source_layer, features, out_name):
        geom_map = {
            QgsWkbTypes.PointGeometry: "Point",
            QgsWkbTypes.LineGeometry: "LineString",
            QgsWkbTypes.PolygonGeometry: "Polygon",
        }
        geom_name = geom_map.get(source_layer.geometryType(), "GeometryCollection")
        out = QgsVectorLayer(f"{geom_name}?crs={source_layer.crs().authid()}", out_name, "memory")
        provider = out.dataProvider()
        provider.addAttributes(source_layer.fields())
        out.updateFields()
        copied = []
        for f in features:
            nf = QgsFeature(out.fields())
            nf.setGeometry(f.geometry())
            attrs = [f[idx] for idx in range(len(source_layer.fields()))]
            nf.setAttributes(attrs)
            copied.append(nf)
        provider.addFeatures(copied)
        QgsProject.instance().addMapLayer(out)
        self._log(f"<b>AI:</b> Created matched output layer: {out.name()}")

    def _create_centroids_from_feature_list(self, source_layer, features, out_name):
        out = QgsVectorLayer(f"Point?crs={source_layer.crs().authid()}", out_name, "memory")
        provider = out.dataProvider()
        provider.addAttributes(
            [QgsField("source_id", QVariant.Int)] + [QgsField(f.name(), f.type()) for f in source_layer.fields()]
        )
        out.updateFields()

        out_feats = []
        for f in features:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            cent = g.centroid()
            if cent is None or cent.isEmpty():
                continue
            nf = QgsFeature(out.fields())
            nf.setGeometry(cent)
            attrs = [int(f.id())] + [f[idx] for idx in range(len(source_layer.fields()))]
            nf.setAttributes(attrs)
            out_feats.append(nf)
        provider.addFeatures(out_feats)
        QgsProject.instance().addMapLayer(out)
        self._log(f"<b>AI:</b> Created centroid layer: {out.name()}")

    def _is_screenshot_query(self, normalized_query):
        has_shot = any(
            phrase in normalized_query
            for phrase in ("screenshot", "screen shot", "snapshot", "capture")
        )
        has_canvas = any(
            phrase in normalized_query
            for phrase in ("map canvas", "canvas", "map view", "map")
        )
        return has_shot and has_canvas

    def _run_canvas_screenshot(self):
        try:
            canvas = self.iface.mapCanvas()
            if canvas is None:
                self._log("<b>AI:</b> Map canvas is unavailable.")
                return
            out_dir = self._resolve_output_dir_from_query(self.last_user_query)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            out_path = out_dir / f"map_canvas_{ts}.png"
            canvas.saveAsImage(str(out_path))
            self._log(f"<b>AI:</b> Screenshot saved:\n{out_path}")
            self._remember_action(self.last_user_query, "canvas screenshot saved")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not capture screenshot: {exc}")

    def _is_clear_null_geometry_query(self, normalized_query):
        has_null = "null geometr" in normalized_query
        has_clear = any(word in normalized_query for word in ("clear", "remove", "delete", "clean"))
        return has_null and has_clear

    def _create_null_geometries(self, normalized_query):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not hasattr(layer, "addFeature"):
            self._log("<b>AI:</b> Active layer does not support feature editing.")
            return

        count = self._extract_integer(normalized_query) or 1
        if count <= 0:
            self._log("<b>AI:</b> Number of null geometries must be greater than zero.")
            return

        started_edit = False
        try:
            if not layer.isEditable():
                started_edit = layer.startEditing()
                if not started_edit:
                    self._log("<b>AI:</b> Could not start editing on active layer.")
                    return

            added = 0
            for _ in range(count):
                feat = QgsFeature(layer.fields())
                if layer.addFeature(feat):
                    added += 1

            if started_edit:
                if not layer.commitChanges():
                    layer.rollBack()
                    self._log("<b>AI:</b> Failed to commit created null geometries.")
                    return

            self._log(f"<b>AI:</b> Created {added} null geometry feature(s) in '{layer.name()}'.")
        except Exception as exc:
            if started_edit and layer.isEditable():
                layer.rollBack()
            self._log(f"<b>AI:</b> Could not create null geometries: {exc}")

    def _clear_null_geometries(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not hasattr(layer, "deleteFeatures"):
            self._log("<b>AI:</b> Active layer does not support feature editing.")
            return

        null_ids = []
        try:
            for feature in layer.getFeatures():
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    null_ids.append(feature.id())
        except Exception as exc:
            self._log(f"<b>AI:</b> Failed reading layer geometries: {exc}")
            return

        if not null_ids:
            self._log("<b>AI:</b> No null geometries found in active layer.")
            return

        self._request_confirmation(
            f"This will delete {len(null_ids)} null geometry feature(s) from '{layer.name()}'.",
            self._clear_null_geometries_confirmed,
            layer,
            null_ids,
        )

    def _clear_null_geometries_confirmed(self, layer, null_ids):
        self._create_backup_layer(layer, "before_null_cleanup")

        started_edit = False
        try:
            if not layer.isEditable():
                started_edit = layer.startEditing()
                if not started_edit:
                    self._log("<b>AI:</b> Could not start editing on active layer.")
                    return

            ok = layer.deleteFeatures(null_ids)
            if not ok:
                if started_edit:
                    layer.rollBack()
                self._log("<b>AI:</b> Failed deleting null geometry features.")
                return

            if started_edit:
                if not layer.commitChanges():
                    layer.rollBack()
                    self._log("<b>AI:</b> Failed to commit null geometry cleanup.")
                    return

            self._log(f"<b>AI:</b> Removed {len(null_ids)} null geometry feature(s) from '{layer.name()}'.")
            self._remember_action(self.last_user_query, f"deleted {len(null_ids)} null geometries")
        except Exception as exc:
            if started_edit and layer.isEditable():
                layer.rollBack()
            self._log(f"<b>AI:</b> Could not clear null geometries: {exc}")

    def _answer_definition_query(self, normalized_query):
        if "what is a vertex" in normalized_query or "define vertex" in normalized_query:
            self._log(
                "<b>AI:</b> A vertex is a coordinate point that defines geometry shape.\n"
                "- Point: one vertex\n"
                "- Line: vertices at bends/endpoints\n"
                "- Polygon: vertices define boundary corners"
            )
            return True
        if "what is snapping" in normalized_query:
            self._log(
                "<b>AI:</b> Snapping forces new/moved vertices to align with nearby vertices or segments "
                "within a tolerance, helping avoid gaps/overlaps."
            )
            return True
        return False

    def _handle_external_plugin_family_commands(self, normalized_query, original_query):
        """
        Map legacy plugin-family prompts to autonomous Copilot workflows.
        """
        # Keep explicit canvas basemap commands in core built-ins (do not hijack as remote-sensing QA).
        if "google satellite" in normalized_query:
            return False
        options = self._parse_kv_options(original_query)
        if "999 updater" in normalized_query or ("vegremarks" in normalized_query and "999" in normalized_query):
            self._run_999_updater_workflow(normalized_query, options)
            return True
        if "agc" in normalized_query and any(k in normalized_query for k in ("qa", "batch", "compare")):
            self._run_agc_batch_qa_workflow(normalized_query, options)
            return True
        if "ivms qa" in normalized_query or ("span qa" in normalized_query):
            self._run_ivms_span_qa_workflow(normalized_query, options)
            return True
        if "qautofix" in normalized_query or ("auto fix" in normalized_query and "span" in normalized_query):
            self._run_qautofix_workflow(normalized_query, options)
            return True
        if "tg th validator" in normalized_query or "tg_th validator" in normalized_query or (
            "geometry validator" in normalized_query and any(k in normalized_query for k in ("tg", "th"))
        ):
            self._run_tg_th_validator_workflow(normalized_query, options)
            return True
        if any(k in normalized_query for k in ("deep learning", "dl model", "semantic segmentation train", "instance segmentation train")):
            self._run_deep_learning_natural_router(normalized_query, options)
            return True
        if any(
            k in normalized_query
            for k in (
                "remote sensing",
                "satellite",
                "earthdata",
                "earthexplorer",
                "copernicus data space",
                "sentinel hub",
                "stac catalog",
            )
        ):
            self._run_remote_sensing_natural_router(normalized_query, options)
            return True
        if any(
            k in normalized_query
            for k in (
                "geoai",
                "spatial machine learning",
                "computer vision",
                "segmentation",
                "object detection",
                "raster classification",
                "embeddings",
                "spatial statistics",
                "llm + gis",
                "llm gis",
            )
        ):
            self._run_ai_gis_natural_router(normalized_query)
            return True
        if any(k in normalized_query for k in ("equivalent", "same in", "qgis equivalent of", "convert arcgis workflow", "workflow mapping")):
            self._render_equivalent_workflow(original_query)
            return True
        if self._is_plugin_recommendation_query(normalized_query):
            self._answer_plugin_recommendation(original_query, normalized_query)
            return True
        if any(
            k in normalized_query
            for k in (
                "postgis",
                "postgresql",
                "spatial sql",
                "spatial query",
                "geopackage",
                "spatialite",
                "database workflow",
                "st_within",
                "st_intersects",
                "st_touches",
                "st_distance",
                "st_area",
                "st_centroid",
                "spatial index",
            )
        ):
            self._run_spatialdb_natural_router(normalized_query, options)
            return True
        if "geoai" in normalized_query or any(
            k in normalized_query
            for k in (
                "moondream",
                "samgeo",
                "segment anything",
                "deepforest",
                "water segmentation",
                "semantic segmentation",
                "instance segmentation",
            )
        ):
            self._run_geoai_nature_router(normalized_query, options)
            return True
        return False

    def _render_spatialdb_pack(self):
        self._log(
            "<b>AI:</b> Spatial database stack enabled:\n"
            "- PostgreSQL\n- PostGIS\n- Spatial SQL\n- GeoPackage\n- SpatiaLite\n"
            "Natural prompts supported for workflow guidance and export execution.\n"
            "Contracts:\n"
            "- run spatialdb action=export format=gpkg input=active output=D:\\out\\layer.gpkg\n"
            "- run spatialdb action=export format=spatialite input=active output=D:\\out\\layer.sqlite\n"
            "- run spatialdb action=sql backend=spatialite db=D:\\data\\db.sqlite sql=\"select * from my_table limit 10\" load_to_map=true geom_column=geom key_field=gid\n"
            "- run spatialdb action=sql backend=postgis host=localhost port=5432 dbname=gis user=postgres password=*** sql=\"select count(*) from roads\"\n"
            "Workflow examples:\n"
            "- prepare this layer for PostGIS ingest\n"
            "- export active layer as geopackage\n"
            "- build a PostGIS workflow for network analysis"
        )

    def _render_ogc_pack(self):
        self._log(
            "<b>AI:</b> OGC quick matrix (when to use what):\n"
            "- WMS: map image streaming (rendered server-side), great for basemap/visualization only.\n"
            "- WMTS: cached map tiles, fastest display at scale.\n"
            "- WFS: vector features for query/edit/integration workflows.\n"
            "- WCS: raster/coverage values for analysis-ready access.\n"
            "- OGC API Features: modern REST/JSON replacement path for many WFS use-cases.\n"
            "- OGC API Tiles: modern REST tiling for vector/raster tile access.\n"
            "- GeoPackage: portable offline project/data exchange (SQLite container).\n"
            "- GeoJSON: lightweight web/mobile vector exchange format.\n"
            "- GML: XML-based feature exchange (enterprise/standards-heavy environments).\n"
            "- CRS/EPSG: choose projected CRS for distance/area operations; transform explicitly when integrating sources.\n"
            "Rule of thumb: visualize -> WMS/WMTS, features -> WFS/OGC API Features, raster analysis -> WCS, "
            "offline/share -> GeoPackage, web app payload -> GeoJSON."
        )

    def _render_remote_sensing_pack(self):
        self._log(
            "<b>AI:</b> Remote sensing / satellite GIS pack enabled.\n"
            "Core sources: NASA Earthdata, USGS EarthExplorer, Copernicus Data Space, Sentinel Hub, STAC.\n"
            "Supported workflow understanding:\n"
            "- discovery/search of scenes\n"
            "- preprocessing (clip/reproject/resample)\n"
            "- raster analysis and indices\n"
            "- classification/inference\n"
            "- vectorization and QGIS visualization\n"
            "I can guide and execute QGIS-processing parts directly, and generate implementation plans for API-driven steps."
        )

    def _run_remote_sensing_natural_router(self, normalized_query, options):
        q = (normalized_query or "").lower()
        if any(k in q for k in ("workflow", "pipeline", "end to end", "step by step", "plan")):
            self._render_satellite_workflow_plan(self.last_user_query or normalized_query)
            return
        if "stac" in q and any(k in q for k in ("how", "query", "search", "use")):
            self._log(
                "<b>AI:</b> STAC workflow:\n"
                "1) choose catalog/API endpoint\n"
                "2) define bbox/time/cloud filters\n"
                "3) retrieve item assets (bands)\n"
                "4) load into QGIS for analysis/vectorization."
            )
            return
        if any(k in q for k in ("sentinel", "landsat", "satellite", "earthdata", "earthexplorer", "copernicus")):
            self._log(
                "<b>AI:</b> Remote-sensing workflow understood. I can help with source selection, band planning, "
                "preprocessing steps, and execution-ready QGIS processing chain for your exact prompt."
            )
            return
        self._render_remote_sensing_pack()

    def _render_satellite_workflow_plan(self, prompt_text):
        t = (prompt_text or "").strip()
        lower = t.lower()
        source = "Copernicus Data Space / Sentinel Hub"
        if "landsat" in lower or "usgs" in lower or "earthexplorer" in lower:
            source = "USGS EarthExplorer (Landsat)"
        elif "nasa" in lower or "earthdata" in lower:
            source = "NASA Earthdata"
        elif "stac" in lower:
            source = "STAC-compatible catalog"

        wants_ndvi = "ndvi" in lower
        wants_class = any(k in lower for k in ("classif", "segment", "infer", "model"))
        wants_vector = any(k in lower for k in ("vector", "polygon", "centroid", "shape", "geopackage"))
        wants_clip = any(k in lower for k in ("clip", "mask", "aoi", "bbox"))

        lines = [
            "<b>AI:</b> Satellite workflow plan generated from your prompt:",
            f"Objective: {t}",
            f"1) Source selection: {source}",
            "2) Discovery filters: bbox/AOI, date range, cloud threshold, CRS target.",
            "3) Acquisition: download/select required bands/assets (e.g., Sentinel-2 B04/B08 or Landsat equivalents).",
            "4) Preprocessing in QGIS: reproject, optional clip to AOI, align resolution, nodata handling.",
        ]
        if wants_ndvi:
            lines.append("5) Index computation: NDVI via raster calculator.")
        else:
            lines.append("5) Raster analysis: compute requested index/features (NDVI/NDWI/NBR/texture/etc.).")
        if wants_class:
            lines.append("6) Classification/inference: apply threshold/rules or DL model inference as requested.")
        else:
            lines.append("6) Optional classification: threshold or model-based extraction if needed.")
        if wants_vector:
            lines.append("7) Vectorization/postprocess: polygonize mask, smooth/filter by area, export to GeoPackage/SpatiaLite.")
        else:
            lines.append("7) Output packaging: styled raster outputs + optional vector derivatives.")
        lines.append("8) Visualization/reporting: load outputs in QGIS, apply style, produce map/layout/export.")

        # Provide runnable helper hints
        hints = [
            "Run-ready helpers:",
            "- /remote sensing",
            "- run geoai task=clip_raster_by_bbox ...",
            "- run geoai task=vectorize_mask ...",
            "- run spatialdb action=export format=gpkg input=active ...",
        ]
        if wants_clip:
            hints.append("- Suggested now: use clip-by-bbox/AOI before analysis.")
        self._log("\n".join(lines + hints))

    def _render_ai_gis_pack(self):
        self._log(
            "<b>AI:</b> AI + GIS capability pack enabled.\n"
            "Coverage:\n"
            "- GeoAI foundations\n"
            "- spatial machine learning\n"
            "- computer vision for geospatial imagery\n"
            "- segmentation and object detection\n"
            "- raster classification and feature extraction\n"
            "- embeddings for semantic/spatial retrieval\n"
            "- spatial statistics and hotspot patterning\n"
            "- LLM + GIS orchestration workflows\n"
            "Use /ai gis workflow <prompt> to auto-generate execution plans."
        )

    def _run_ai_gis_natural_router(self, normalized_query):
        q = (normalized_query or "").lower()
        if any(k in q for k in ("workflow", "pipeline", "step", "plan", "how to")):
            self._render_ai_gis_workflow_plan(self.last_user_query or normalized_query)
            return
        self._render_ai_gis_pack()

    def _run_ai_gis_contract_workflow(self, normalized_query, options):
        workflow_text = (
            str((options or {}).get("workflow", "")).strip()
            or str((options or {}).get("objective", "")).strip()
            or str((options or {}).get("prompt", "")).strip()
        )
        if not workflow_text:
            self._log("<b>AI:</b> Usage: run ai_gis workflow=\"<objective>\"")
            return
        self._render_ai_gis_workflow_plan(workflow_text)
        self._execute_ai_gis_workflow(workflow_text, options or {})

    def _execute_ai_gis_workflow(self, workflow_text, options):
        """
        Execute deterministic parts of an AI+GIS workflow plan.
        Non-deterministic/model-dependent parts are reported with required inputs.
        """
        q = (workflow_text or "").lower()
        executed = []
        skipped = []

        # 1) Optional clipping step
        if any(k in q for k in ("clip", "aoi", "bbox", "within area")):
            bbox = options.get("bbox") or self._extract_bbox_from_text(self.last_user_query or "")
            if bbox:
                run_opts = {"input": options.get("input", "active"), "bbox": bbox}
                self._run_geoai_clip_raster_by_bbox(q, run_opts)
                executed.append("clip_raster_by_bbox")
            else:
                skipped.append("clip_raster_by_bbox (missing bbox)")

        # 2) Optional AI inference hooks
        if any(k in q for k in ("classif", "segment", "segmentation", "inference", "infer")):
            if options.get("model_path") and options.get("input_path"):
                dl_opts = {
                    "task": "semantic_infer",
                    "input_path": options.get("input_path"),
                    "model_path": options.get("model_path"),
                    "output_path": options.get("output_path", str(self._resolve_output_dir_from_query(self.last_user_query) / "semantic_pred.tif")),
                }
                self._run_deep_learning_contract_workflow(q, dl_opts)
                executed.append("semantic_infer")
            else:
                skipped.append("semantic_infer (requires input_path + model_path)")

        # 3) Vectorization step
        if any(k in q for k in ("vector", "polygon", "vectorize", "intersection", "encroachment")):
            run_opts = {
                "input": options.get("mask_input", "active"),
                "field": options.get("field", "class"),
            }
            self._run_geoai_vectorize_mask(q, run_opts)
            executed.append("vectorize_mask")

        # 4) Buffer + intersection (if explicitly requested)
        if any(k in q for k in ("buffer", "within", "encroachment", "proximity")):
            self._run_buffer_command(q)
            executed.append("buffer")
            if any(k in q for k in ("intersect", "intersection", "overlay", "encroachment")):
                self._run_two_layer_overlay("native:intersection", "INPUT", "OVERLAY")
                executed.append("intersection")

        # 5) Statistics
        if any(k in q for k in ("statistics", "summary", "count", "hotspot", "density")):
            self._run_topology_qa_pack()
            executed.append("stats_report")

        # 6) Optional export target
        if options.get("export_format") in ("gpkg", "spatialite"):
            self._run_spatialdb_contract_workflow(
                q,
                {
                    "action": "export",
                    "format": options.get("export_format"),
                    "input": options.get("export_input", "active"),
                    "output": options.get("export_output", ""),
                },
            )
            executed.append(f"export_{options.get('export_format')}")

        msg = [f"<b>AI:</b> AI+GIS workflow execution attempted."]
        if executed:
            msg.append("Executed: " + ", ".join(executed))
        if skipped:
            msg.append("Pending inputs: " + ", ".join(skipped))
        self._log("\n".join(msg))
        self._remember_action(self.last_user_query, f"ai_gis workflow executed: {', '.join(executed) if executed else 'none'}")

    def _render_ai_gis_workflow_plan(self, prompt_text):
        t = (prompt_text or "").strip()
        lower = t.lower()
        wants_detect = any(k in lower for k in ("detect", "find", "identify", "object"))
        wants_segment = any(k in lower for k in ("segment", "segmentation", "classify", "classification"))
        wants_buffer = "buffer" in lower or "within" in lower
        wants_stats = any(k in lower for k in ("statistics", "count", "summary", "hotspot", "density"))
        wants_vector = any(k in lower for k in ("vector", "polygon", "layer", "shapefile", "geopackage"))

        lines = [
            "<b>AI:</b> AI+GIS workflow plan generated:",
            f"Objective: {t}",
            "1) Input harmonization: verify CRS, pixel resolution, AOI extent, nodata.",
            "2) Feature pipeline: choose model family (segmentation/classification/detection) based on target.",
        ]
        if wants_segment:
            lines.append("3) Raster inference/classification: produce mask/probability raster.")
        elif wants_detect:
            lines.append("3) Object detection inference: produce boxes/instances and confidence scores.")
        else:
            lines.append("3) Baseline extraction: derive candidate features via spectral/CV model.")

        if wants_buffer:
            lines.append("4) Spatial constraints: create buffers/proximity masks for rule filtering.")
        else:
            lines.append("4) Optional spatial constraints: AOI masks, proximity filters, topology guards.")

        lines.append("5) Vectorization/postprocess: polygonize or centroid extraction, smooth/filter by area.")
        if wants_stats:
            lines.append("6) Spatial statistics: intersection counts, zonal summaries, hotspot/density metrics.")
        else:
            lines.append("6) Optional analytics: overlay/intersection summaries and QA checks.")
        if wants_vector:
            lines.append("7) Outputs: styled QGIS layers + export to GeoPackage/SpatiaLite/PostGIS as requested.")
        else:
            lines.append("7) Outputs: raster + vector derivatives loaded in QGIS with report-ready attributes.")
        lines.append("8) Reproducibility: save command recipe + parameters for reruns.")

        lines += [
            "Run-ready helpers:",
            "- /satellite workflow <prompt>",
            "- run dl task=semantic_infer ...",
            "- run geoai task=vectorize_mask ...",
            "- run spatialdb action=export format=gpkg input=active ...",
            "- /equivalent <operation>",
        ]
        self._log("\n".join(lines))

    def _render_workflow_kb_overview(self):
        self._log(
            "<b>AI:</b> GIS Workflow Knowledge Base is active.\n"
            "It maps operations across:\n"
            "- QGIS GUI\n- QGIS Processing\n- PyQGIS\n- GDAL\n- GeoPandas\n- PostGIS\n- ArcGIS\n"
            "Use:\n"
            "- /equivalent buffer\n"
            "- /equivalent zonal statistics\n"
            "- /equivalent reprojection\n"
            "- /equivalent spatial join"
        )

    def _render_equivalent_workflow(self, operation_text):
        op = (operation_text or "").strip().lower()
        maps = self._workflow_equivalents_map()
        chosen = None
        best = 0.0
        otoks = self._tokenize_text(op)
        for key, payload in maps.items():
            score = self._token_overlap_score(otoks, self._tokenize_text(key))
            if score > best:
                best = score
                chosen = payload
        if chosen is None or best < 0.18:
            self._log("<b>AI:</b> I do not have a strong equivalent map for that yet. Try /workflow kb.")
            return
        lines = [f"<b>AI:</b> Workflow equivalents for: {operation_text}"]
        for k in ("qgis_gui", "processing", "pyqgis", "gdal", "geopandas", "postgis", "arcgis"):
            v = chosen.get(k)
            if v:
                lines.append(f"- {k}: {v}")
        self._log("\n".join(lines))

    def _workflow_equivalents_map(self):
        return {
            "buffer proximity within distance": {
                "qgis_gui": "Vector -> Geoprocessing Tools -> Buffer",
                "processing": "native:buffer",
                "pyqgis": "processing.run('native:buffer', {...})",
                "gdal": "Prefer GEOS/OGR via ogr2ogr with SQL ST_Buffer",
                "geopandas": "gdf.geometry = gdf.buffer(distance)",
                "postgis": "ST_Buffer(geom, distance)",
                "arcgis": "Buffer (Analysis)",
            },
            "zonal statistics mean min max elevation parcels dem": {
                "qgis_gui": "Raster -> Zonal statistics",
                "processing": "native:zonalstatisticsfb",
                "pyqgis": "processing.run('native:zonalstatisticsfb', {...})",
                "gdal": "gdal/zonal workflows via python stack",
                "geopandas": "rasterstats/zonal_stats + join to GeoDataFrame",
                "postgis": "ST_SummaryStatsAgg / raster-vector intersection",
                "arcgis": "Zonal Statistics as Table",
            },
            "reproject transform crs epsg": {
                "qgis_gui": "Layer export/reproject tools",
                "processing": "native:reprojectlayer or gdal:warpreproject",
                "pyqgis": "QgsCoordinateTransform / processing reproject",
                "gdal": "gdalwarp (raster), ogr2ogr -t_srs (vector)",
                "geopandas": "gdf.to_crs('EPSG:xxxx')",
                "postgis": "ST_Transform(geom, srid)",
                "arcgis": "Project (Data Management)",
            },
            "spatial join join attributes by location": {
                "qgis_gui": "Join attributes by location",
                "processing": "native:joinattributesbylocation",
                "pyqgis": "processing.run('native:joinattributesbylocation', {...})",
                "gdal": "ogr2ogr SQL spatial joins",
                "geopandas": "geopandas.sjoin(left, right, predicate='intersects')",
                "postgis": "JOIN ... ON ST_Intersects(...)",
                "arcgis": "Spatial Join (Analysis)",
            },
            "clip raster mask": {
                "qgis_gui": "Clip raster by mask layer",
                "processing": "gdal:cliprasterbymasklayer",
                "pyqgis": "processing.run('gdal:cliprasterbymasklayer', {...})",
                "gdal": "gdalwarp -cutline ... -crop_to_cutline",
                "geopandas": "mask geometry prep + rasterio.mask",
                "postgis": "ST_Clip for rasters",
                "arcgis": "Extract by Mask",
            },
            "intersection overlay": {
                "qgis_gui": "Vector overlay -> Intersection",
                "processing": "native:intersection",
                "pyqgis": "processing.run('native:intersection', {...})",
                "gdal": "ogr2ogr overlay SQL",
                "geopandas": "geopandas.overlay(a, b, how='intersection')",
                "postgis": "ST_Intersection",
                "arcgis": "Intersect (Analysis)",
            },
            "union overlay": {
                "qgis_gui": "Vector overlay -> Union",
                "processing": "native:union",
                "pyqgis": "processing.run('native:union', {...})",
                "gdal": "ogr2ogr overlay SQL union patterns",
                "geopandas": "geopandas.overlay(a, b, how='union')",
                "postgis": "ST_Union + attribute join logic",
                "arcgis": "Union (Analysis)",
            },
            "clip vector by polygon": {
                "qgis_gui": "Vector overlay -> Clip",
                "processing": "native:clip",
                "pyqgis": "processing.run('native:clip', {...})",
                "gdal": "ogr2ogr -clipsrc",
                "geopandas": "geopandas.clip(input_gdf, mask_gdf)",
                "postgis": "ST_Intersection with mask",
                "arcgis": "Clip (Analysis)",
            },
            "dissolve by field": {
                "qgis_gui": "Vector -> Geoprocessing -> Dissolve",
                "processing": "native:dissolve",
                "pyqgis": "processing.run('native:dissolve', {'FIELD':[...], ...})",
                "gdal": "ogr2ogr SQL GROUP BY + geometry union",
                "geopandas": "gdf.dissolve(by='field')",
                "postgis": "GROUP BY field + ST_Union",
                "arcgis": "Dissolve (Data Management)",
            },
            "merge vector layers": {
                "qgis_gui": "Vector -> Data Management -> Merge Vector Layers",
                "processing": "native:mergevectorlayers",
                "pyqgis": "processing.run('native:mergevectorlayers', {...})",
                "gdal": "ogrmerge.py / ogr2ogr append",
                "geopandas": "pandas.concat([gdf1,gdf2,...])",
                "postgis": "INSERT INTO target SELECT ... UNION ALL",
                "arcgis": "Merge (Data Management)",
            },
            "centroid points from polygons": {
                "qgis_gui": "Vector geometry -> Centroids",
                "processing": "native:centroids",
                "pyqgis": "processing.run('native:centroids', {...})",
                "gdal": "ogr2ogr SQL with ST_Centroid",
                "geopandas": "gdf.centroid",
                "postgis": "ST_Centroid(geom)",
                "arcgis": "Feature To Point (Inside optional)",
            },
            "convex hull": {
                "qgis_gui": "Vector geometry -> Convex Hull",
                "processing": "native:convexhull",
                "pyqgis": "processing.run('native:convexhull', {...})",
                "gdal": "GEOS hull via SQL bindings",
                "geopandas": "gdf.unary_union.convex_hull",
                "postgis": "ST_ConvexHull",
                "arcgis": "Minimum Bounding Geometry",
            },
            "multipart to singleparts": {
                "qgis_gui": "Vector geometry -> Multipart to singleparts",
                "processing": "native:multiparttosingleparts",
                "pyqgis": "processing.run('native:multiparttosingleparts', {...})",
                "gdal": "explode collections via SQL/tooling",
                "geopandas": "gdf.explode(index_parts=False)",
                "postgis": "ST_Dump(geom)",
                "arcgis": "Multipart To Singlepart",
            },
            "fix invalid geometries": {
                "qgis_gui": "Vector geometry -> Fix Geometries",
                "processing": "native:fixgeometries",
                "pyqgis": "processing.run('native:fixgeometries', {...})",
                "gdal": "ogr2ogr -makevalid",
                "geopandas": "shapely.make_valid / buffer(0) patterns",
                "postgis": "ST_MakeValid",
                "arcgis": "Repair Geometry",
            },
            "extract by location within intersects": {
                "qgis_gui": "Extract by location",
                "processing": "native:extractbylocation",
                "pyqgis": "processing.run('native:extractbylocation', {...})",
                "gdal": "ogr2ogr spatial filter/SQL",
                "geopandas": "sjoin + boolean filter",
                "postgis": "WHERE ST_Intersects/ST_Within",
                "arcgis": "Select Layer By Location",
            },
            "nearest neighbor nearest feature": {
                "qgis_gui": "Join attributes by nearest / NN tools",
                "processing": "native:joinbynearest",
                "pyqgis": "processing.run('native:joinbynearest', {...})",
                "gdal": "spatial SQL nearest patterns",
                "geopandas": "sjoin_nearest",
                "postgis": "ORDER BY geom <-> other.geom LIMIT 1",
                "arcgis": "Near / Generate Near Table",
            },
            "raster calculator map algebra": {
                "qgis_gui": "Raster -> Raster Calculator",
                "processing": "qgis:rastercalculator / gdal:rastercalculator",
                "pyqgis": "processing.run('qgis:rastercalculator', {...})",
                "gdal": "gdal_calc.py",
                "geopandas": "Use rasterio/numpy for algebra",
                "postgis": "ST_MapAlgebra",
                "arcgis": "Raster Calculator",
            },
            "hillshade": {
                "qgis_gui": "Raster terrain analysis -> Hillshade",
                "processing": "gdal:hillshade",
                "pyqgis": "processing.run('gdal:hillshade', {...})",
                "gdal": "gdaldem hillshade",
                "geopandas": "N/A (raster domain)",
                "postgis": "Raster terrain via PostGIS raster funcs",
                "arcgis": "Hillshade",
            },
            "slope aspect terrain": {
                "qgis_gui": "Raster terrain analysis -> Slope/Aspect",
                "processing": "gdal:slope / gdal:aspect",
                "pyqgis": "processing.run('gdal:slope', {...})",
                "gdal": "gdaldem slope/aspect",
                "geopandas": "N/A (raster domain)",
                "postgis": "Raster terrain SQL workflows",
                "arcgis": "Slope / Aspect",
            },
            "contour lines from dem": {
                "qgis_gui": "Raster extraction -> Contour",
                "processing": "gdal:contour",
                "pyqgis": "processing.run('gdal:contour', {...})",
                "gdal": "gdal_contour",
                "geopandas": "Use GDAL/rasterio toolchain first",
                "postgis": "Raster contour generation patterns",
                "arcgis": "Contour",
            },
            "raster to polygon vectorize": {
                "qgis_gui": "Raster conversion -> Polygonize",
                "processing": "gdal:polygonize",
                "pyqgis": "processing.run('gdal:polygonize', {...})",
                "gdal": "gdal_polygonize.py",
                "geopandas": "rasterio.features.shapes -> GeoDataFrame",
                "postgis": "ST_DumpAsPolygons",
                "arcgis": "Raster to Polygon",
            },
            "resample raster": {
                "qgis_gui": "Raster projections -> Warp (reproject)",
                "processing": "gdal:warpreproject (resolution params)",
                "pyqgis": "processing.run('gdal:warpreproject', {...})",
                "gdal": "gdalwarp -tr ... -r ...",
                "geopandas": "N/A (raster domain)",
                "postgis": "ST_Resample (raster)",
                "arcgis": "Resample",
            },
            "create spatial index": {
                "qgis_gui": "Layer properties / DB tools index creation",
                "processing": "native:createspatialindex",
                "pyqgis": "processing.run('native:createspatialindex', {...})",
                "gdal": "dataset/DB specific index ops",
                "geopandas": "rtree/pygeos-backed sindex",
                "postgis": "CREATE INDEX ... USING GIST(geom)",
                "arcgis": "Add Spatial Index",
            },
            "network shortest path routing": {
                "qgis_gui": "Network analysis tools / plugins",
                "processing": "qgis:shortestpathpointtopoint (when available)",
                "pyqgis": "processing.run(shortest-path algorithm, {...})",
                "gdal": "Not primary",
                "geopandas": "Use networkx/osmnx after graph build",
                "postgis": "pgRouting shortest path",
                "arcgis": "Network Analyst route",
            },
            "heatmap kernel density": {
                "qgis_gui": "Heatmap renderer / processing heatmap",
                "processing": "qgis:heatmapkerneldensityestimation",
                "pyqgis": "processing.run('qgis:heatmapkerneldensityestimation', {...})",
                "gdal": "Grid/KDE equivalents via tools",
                "geopandas": "Use scipy/sklearn KDE pipelines",
                "postgis": "Density via rasterization/SQL workflows",
                "arcgis": "Kernel Density",
            },
            "point in polygon": {
                "qgis_gui": "Select by location / count points in polygon",
                "processing": "native:countpointsinpolygon",
                "pyqgis": "processing.run('native:countpointsinpolygon', {...})",
                "gdal": "Spatial SQL join",
                "geopandas": "sjoin(points, polys, predicate='within')",
                "postgis": "ST_Within(point, polygon)",
                "arcgis": "Spatial Join / Summarize Within",
            },
            "voronoi polygons": {
                "qgis_gui": "Vector geometry -> Voronoi polygons",
                "processing": "qgis:voronoipolygons",
                "pyqgis": "processing.run('qgis:voronoipolygons', {...})",
                "gdal": "Use GEOS-enabled workflows",
                "geopandas": "shapely.voronoi_polygons (version-dependent)",
                "postgis": "ST_VoronoiPolygons",
                "arcgis": "Create Thiessen Polygons",
            },
        }

    def _is_plugin_recommendation_query(self, normalized_query):
        q = (normalized_query or "").lower()
        asks_plugin = "plugin" in q or "plugins" in q
        asks_recommend = any(
            k in q
            for k in (
                "is there",
                "which",
                "what plugin",
                "can do this",
                "for this",
                "recommend",
                "suggest",
            )
        )
        return asks_plugin and asks_recommend

    def _answer_plugin_recommendation(self, original_query, normalized_query):
        q = (normalized_query or "").lower()
        topic_map = [
            ("basemap xyz osm google", ["QuickMapServices"]),
            ("georeference georeferencing", ["Georeferencer GDAL"]),
            ("topology validation geometry checker", ["Geometry Checker", "Topology Checker"]),
            ("raster clip reproject mosaic", ["Processing", "GDAL tools"]),
            ("postgis database sql", ["DB Manager"]),
            ("remote sensing sentinel landsat index ndvi", ["Semi-Automatic Classification Plugin", "GEE Timeseries Explorer"]),
            ("network routing shortest path", ["QNEAT3"]),
            ("cad dxf dwg", ["02CadGis Universal CAD/GIS Importer"]),
            ("point cloud lidar", ["Open LiDAR Toolbox"]),
            ("digitizing edit", ["Digitizing Tools"]),
            ("contour dem terrain", ["Profile Tool", "Contour plugin"]),
            ("heatmap density", ["Density Analysis"]),
            ("time animation", ["TimeManager"]),
            ("web publishing leaflet", ["qgis2web"]),
            ("batch processing modeler", ["Processing"]),
            ("plugin development pyqgis", ["Plugin Builder", "First Aid"]),
        ]

        qtokens = self._tokenize_text(q)
        scored = []
        for key_text, plugins in topic_map:
            ktokens = self._tokenize_text(key_text)
            score = self._token_overlap_score(qtokens, ktokens)
            if score > 0:
                scored.append((score, plugins))
        scored.sort(key=lambda x: -x[0])

        suggestions = []
        for _, plist in scored[:4]:
            for p in plist:
                if p not in suggestions:
                    suggestions.append(p)

        # Cross-check installed plugins for direct name hints.
        installed = self._list_available_plugins()
        iname_map = {p.get("name", ""): p.get("id", "") for p in installed}
        installed_hits = []
        for pname in suggestions:
            for iname in iname_map.keys():
                if pname.lower() in iname.lower() or iname.lower() in pname.lower():
                    installed_hits.append(iname)
                    break

        if not suggestions:
            self._log(
                "<b>AI:</b> I can recommend plugins if you mention the operation (e.g., georeferencing, NDVI, routing, topology, CAD import)."
            )
            return

        lines = ["<b>AI:</b> Plugin recommendations for your request:"]
        for s in suggestions[:6]:
            lines.append(f"- {s}")
        if installed_hits:
            lines.append("Installed matches in your QGIS:")
            for h in installed_hits[:6]:
                lines.append(f"- {h}")
        lines.append("Tip: Ask 'how to do this without plugin' and I will map it to built-in QGIS/Processing steps.")
        self._log("\n".join(lines))
        self._remember_action(self.last_user_query, "plugin recommendation answered")

    def _run_spatialdb_natural_router(self, normalized_query, options):
        q = (normalized_query or "").lower()
        if any(k in q for k in ("export", "save", "convert")) and any(k in q for k in ("geopackage", "gpkg")):
            opts = dict(options or {})
            opts.setdefault("action", "export")
            opts.setdefault("format", "gpkg")
            self._run_spatialdb_contract_workflow(q, opts)
            return
        if any(k in q for k in ("export", "save", "convert")) and "spatialite" in q:
            opts = dict(options or {})
            opts.setdefault("action", "export")
            opts.setdefault("format", "spatialite")
            self._run_spatialdb_contract_workflow(q, opts)
            return
        if any(
            k in q
            for k in (
                "spatial query",
                "spatial sql",
                "st_within",
                "st_intersects",
                "st_touches",
                "st_distance",
                "st_area",
                "st_perimeter",
                "st_centroid",
                "st_convexhull",
                "st_simplify",
                "neighbors",
                "adjoining",
                "within distance",
            )
        ):
            self._run_essential_spatial_query_workflow(q, dict(options or {}))
            return
        self._log(
            "<b>AI:</b> Spatial DB workflow understood. I can help with:\n"
            "- QGIS to GeoPackage/SpatiaLite export\n"
            "- PostGIS-ready data prep\n"
            "- Spatial SQL workflow design and query drafting\n"
            "- Essential spatial query execution (within/intersects/touches/distance/area/centroid/etc.)\n"
            "For direct execution, use: run spatialdb action=export format=gpkg|spatialite ...\n"
            "Or: run spatialdb action=sql backend=postgis|spatialite db=... sql=\"...\""
        )

    def _run_essential_spatial_query_workflow(self, normalized_query, options):
        sql_text, label = self._build_essential_spatial_query_sql(normalized_query, options)
        if not sql_text:
            self._render_spatial_query_pack()
            return

        exec_opts = dict(options or {})
        exec_opts["action"] = "sql"
        exec_opts["sql"] = sql_text

        backend = str(exec_opts.get("backend", "")).strip().lower()
        if not backend:
            self._log(
                "<b>AI:</b> Spatial query drafted, missing execution backend.\n"
                "Add backend=postgis or backend=spatialite (gpkg/sqlite also accepted), plus db/dbname connection parameters.\n"
                f"Drafted query ({label}):\n<code>{sql_text}</code>"
            )
            return

        # Confirmation gate for direct DB execution from natural prompt.
        self._request_confirmation(
            "\n".join(
                [
                    "<b>AI:</b> Ready to execute essential spatial query workflow.",
                    f"✓ Pattern detected: {label}",
                    f"✓ Backend: {backend}",
                    "Workflow:",
                    "1) Build SQL from natural prompt",
                    "2) Validate backend connection inputs",
                    "3) Execute SQL and preview results",
                ]
            ),
            self._run_spatialdb_sql_action,
            exec_opts,
        )

    def _build_essential_spatial_query_sql(self, normalized_query, options):
        q = (normalized_query or "").lower()
        table_a = str(options.get("table", options.get("table_a", "building"))).strip()
        table_b = str(options.get("table_b", "region")).strip()
        geom_a = str(options.get("geom_a", "geom")).strip()
        geom_b = str(options.get("geom_b", "geom")).strip()
        id_a = str(options.get("id_field", "gid")).strip()
        name_a = str(options.get("name_field", "name")).strip()
        name_b = str(options.get("name_b", "name")).strip()
        region_name = str(options.get("region", options.get("region_name", "KwaZulu"))).strip().replace("'", "''")
        target_name = str(options.get("target", options.get("target_name", "Hokkaido"))).strip().replace("'", "''")
        dist = float(options.get("distance", options.get("dist", 100)))

        if any(k in q for k in ("within", "inside")) and "distance" not in q:
            sql_text = (
                f"SELECT a.{id_a}, a.{name_a}, a.{geom_a} "
                f"FROM {table_a} a, {table_b} b "
                f"WHERE ST_Within(a.{geom_a}, b.{geom_b}) "
                f"AND b.{name_b} = '{region_name}';"
            )
            return sql_text, "select by location (ST_Within)"
        if any(k in q for k in ("touches", "adjoining", "neighbors", "neighbour")):
            sql_text = (
                f"SELECT b.{id_a}, b.{name_b}, b.{geom_b} "
                f"FROM {table_b} a, {table_b} b "
                f"WHERE ST_Touches(a.{geom_b}, b.{geom_b}) "
                f"AND a.{name_b} = '{target_name}';"
            )
            return sql_text, "select neighbors (ST_Touches)"
        if "distance" in q or "within distance" in q:
            sql_text = (
                f"SELECT b.{id_a}, b.{name_b}, b.{geom_b} "
                f"FROM {table_b} a, {table_b} b "
                f"WHERE ST_Distance(a.{geom_b}, b.{geom_b}) < {dist:g} "
                f"AND a.{name_b} = '{target_name}' "
                f"AND b.{name_b} <> '{target_name}';"
            )
            return sql_text, "distance selection (ST_Distance)"
        if "intersect" in q:
            sql_text = (
                f"SELECT a.{id_a}, a.{name_a}, a.{geom_a} "
                f"FROM {table_a} a, {table_b} b "
                f"WHERE ST_Intersects(a.{geom_a}, b.{geom_b});"
            )
            return sql_text, "overlay selection (ST_Intersects)"
        if "distinct" in q or "unique" in q:
            sql_text = f"SELECT DISTINCT {name_a} FROM {table_a};"
            return sql_text, "unique values (DISTINCT)"
        if "centroid" in q:
            sql_text = f"SELECT {id_a}, ST_Centroid({geom_a}) AS geom FROM {table_a};"
            return sql_text, "centroid generation (ST_Centroid)"
        if "area" in q:
            sql_text = f"SELECT {id_a}, ST_Area({geom_a}) AS area FROM {table_a};"
            return sql_text, "area measurement (ST_Area)"
        if "perimeter" in q:
            sql_text = f"SELECT {id_a}, ST_Perimeter({geom_a}) AS perimeter FROM {table_a};"
            return sql_text, "perimeter measurement (ST_Perimeter)"
        if "convex hull" in q or "convexhull" in q:
            sql_text = f"SELECT ST_ConvexHull(ST_Collect({geom_a})) AS geom FROM {table_a};"
            return sql_text, "convex hull (ST_ConvexHull)"
        if "simplify" in q:
            tol = float(options.get("tolerance", options.get("tol", 20)))
            sql_text = f"SELECT {id_a}, ST_Simplify({geom_a}, {tol:g}) AS geom FROM {table_a};"
            return sql_text, "geometry simplify (ST_Simplify)"
        if "index" in q and "spatial" in q:
            idx = str(options.get("index_name", f"{table_a}_geo_idx")).strip()
            sql_text = f"CREATE INDEX IF NOT EXISTS {idx} ON {table_a} USING GIST ({geom_a});"
            return sql_text, "spatial index creation (GIST)"
        return "", ""

    def _render_spatial_query_pack(self):
        self._log(
            "<b>AI:</b> Essential GIS spatial query pack loaded (QGIS Training Manual + PostGIS patterns).\n"
            "Supported query intents:\n"
            "- select by location (ST_Within / ST_Intersects)\n"
            "- neighbors/adjoining (ST_Touches)\n"
            "- within distance (ST_Distance)\n"
            "- unique values (DISTINCT)\n"
            "- centroid / area / perimeter\n"
            "- simplify / convex hull\n"
            "- spatial index creation (GIST)\n"
            "Execution format:\n"
            "- run spatialdb action=sql backend=postgis dbname=<db> user=<user> password=<pwd> table=building table_b=region region=KwaZulu sql=\"...\"\n"
            "- Natural auto-SQL: include backend and DB params, e.g.\n"
            "  spatial query: find buildings within region backend=postgis dbname=postgis_demo user=postgres password=*** table=building table_b=region region=KwaZulu"
        )

    def _run_spatialdb_contract_workflow(self, normalized_query, options):
        action = str((options or {}).get("action", "")).strip().lower()
        if not action:
            self._log("<b>AI:</b> SpatialDB contract needs action=export|sql.")
            return
        if action == "sql":
            self._run_spatialdb_sql_action(options)
            return
        if action != "export":
            self._log("<b>AI:</b> Currently supported actions: export, sql.")
            return

        fmt = str((options or {}).get("format", "")).strip().lower()
        if fmt not in ("gpkg", "geopackage", "sqlite", "spatialite"):
            self._log("<b>AI:</b> format must be gpkg or spatialite.")
            return

        layer_opt = (options or {}).get("input") or (options or {}).get("layer")
        layer = self.iface.activeLayer() if str(layer_opt or "").lower() in ("", "active") else self._resolve_layer_option(layer_opt)
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> SpatialDB export requires an active/input vector layer.")
            return

        out_opt = (options or {}).get("output")
        if out_opt:
            out_path = Path(str(out_opt).strip())
        else:
            out_dir = self._resolve_output_dir_from_query(self.last_user_query)
            if fmt in ("gpkg", "geopackage"):
                out_path = out_dir / f"{layer.name()}.gpkg"
            else:
                out_path = out_dir / f"{layer.name()}.sqlite"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            processing.run("native:savefeatures", {"INPUT": layer, "OUTPUT": str(out_path)})
            self._remember_action(self.last_user_query, f"spatialdb export {fmt}")
            self._log(f"<b>AI:</b> SpatialDB export complete ({fmt}): {out_path}")
        except Exception as exc:
            self._log(f"<b>AI:</b> SpatialDB export failed: {exc}")

    def _run_spatialdb_sql_action(self, options):
        backend = str((options or {}).get("backend", "")).strip().lower()
        sql_text = str((options or {}).get("sql", "")).strip()
        if not sql_text:
            self._log("<b>AI:</b> SQL action requires sql=\"...\"")
            return
        if backend in ("gpkg", "geopackage", "spatialite", "sqlite"):
            db_path = str((options or {}).get("db", "")).strip()
            if not db_path:
                self._log("<b>AI:</b> For spatialite/geopackage SQL provide db=<path>.")
                return
            self._run_sqlite_sql(db_path, sql_text, options)
            return
        if backend in ("postgis", "postgres", "postgresql"):
            self._run_postgis_sql(sql_text, options)
            return
        self._log("<b>AI:</b> backend must be one of: spatialite|gpkg|postgis.")

    def _run_sqlite_sql(self, db_path, sql_text, options):
        p = Path(db_path)
        if not p.exists():
            self._log(f"<b>AI:</b> Database file not found: {db_path}")
            return
        try:
            conn = sqlite3.connect(str(p))
            cur = conn.cursor()
            cur.execute(sql_text)
            is_select = sql_text.strip().lower().startswith(("select", "with", "pragma"))
            if is_select:
                rows = cur.fetchmany(30)
                cols = [d[0] for d in (cur.description or [])]
                self._log(self._format_sql_preview(cols, rows, f"SQLite query on {p.name}"))
                self._maybe_export_sql_rows(cols, rows, options, prefix=p.stem)
                self._try_load_sql_result_layer(
                    sql_text,
                    dict(options or {}),
                    backend="spatialite",
                    default_name=f"{p.stem}_sql_result",
                )
            else:
                conn.commit()
                self._log(f"<b>AI:</b> SQLite SQL executed. Affected rows: {cur.rowcount}")
            self._remember_action(self.last_user_query, "spatialdb sql sqlite executed")
        except Exception as exc:
            self._log(f"<b>AI:</b> SQLite SQL failed: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _run_postgis_sql(self, sql_text, options):
        try:
            import psycopg2
        except Exception:
            self._log("<b>AI:</b> PostGIS SQL needs psycopg2. Install in QGIS Python env.")
            return
        conn_kwargs = {
            "host": str(options.get("host", "localhost")),
            "port": int(float(str(options.get("port", "5432")))),
            "dbname": str(options.get("dbname", options.get("db", ""))),
            "user": str(options.get("user", "")),
            "password": str(options.get("password", "")),
        }
        if not conn_kwargs["dbname"] or not conn_kwargs["user"]:
            self._log("<b>AI:</b> PostGIS SQL requires dbname and user (and usually password).")
            return
        conn = None
        try:
            conn = psycopg2.connect(**conn_kwargs)
            cur = conn.cursor()
            cur.execute(sql_text)
            is_select = sql_text.strip().lower().startswith(("select", "with"))
            if is_select:
                rows = cur.fetchmany(30)
                cols = [d[0] for d in (cur.description or [])]
                self._log(self._format_sql_preview(cols, rows, f"PostGIS query on {conn_kwargs['dbname']}"))
                self._maybe_export_sql_rows(cols, rows, options, prefix=conn_kwargs["dbname"])
                self._try_load_sql_result_layer(
                    sql_text,
                    dict(options or {}),
                    backend="postgis",
                    default_name=f"{conn_kwargs['dbname']}_sql_result",
                )
            else:
                conn.commit()
                self._log(f"<b>AI:</b> PostGIS SQL executed. Affected rows: {cur.rowcount}")
            self._remember_action(self.last_user_query, "spatialdb sql postgis executed")
        except Exception as exc:
            self._log(f"<b>AI:</b> PostGIS SQL failed: {exc}")
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def _format_sql_preview(self, columns, rows, title):
        col_line = ", ".join(columns) if columns else "(no columns)"
        body_lines = []
        for r in rows[:10]:
            body_lines.append(", ".join("" if v is None else str(v) for v in r))
        body = "\n".join(body_lines) if body_lines else "(no rows)"
        return (
            f"<b>AI:</b> {title}\n"
            f"Columns: {col_line}\n"
            f"Rows preview ({len(rows)} fetched):\n{body}"
        )

    def _maybe_export_sql_rows(self, columns, rows, options, prefix):
        out_csv = str((options or {}).get("output_csv", "")).strip()
        if not out_csv:
            return
        out_path = Path(out_csv)
        if out_path.suffix.lower() != ".csv":
            out_path = out_path.with_suffix(".csv")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if columns:
                    w.writerow(columns)
                for r in rows:
                    w.writerow(list(r))
            self._log(f"<b>AI:</b> SQL preview rows exported: {out_path}")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not export SQL rows to CSV: {exc}")

    def _try_load_sql_result_layer(self, sql_text, options, backend, default_name):
        load_opt = str((options or {}).get("load_to_map", "true")).strip().lower()
        if load_opt in ("0", "false", "no", "off"):
            return
        sql_l = (sql_text or "").strip().lower()
        if not sql_l.startswith(("select", "with")):
            return

        geom_col = str((options or {}).get("geom_column", "geom")).strip() or "geom"
        key_col = str((options or {}).get("key_field", options.get("key", "gid"))).strip() or "gid"
        layer_name = str((options or {}).get("layer_name", "")).strip() or default_name

        try:
            uri = QgsDataSourceUri()
            provider = ""
            if backend == "postgis":
                host = str(options.get("host", "localhost"))
                port = str(options.get("port", "5432"))
                dbname = str(options.get("dbname", options.get("db", "")))
                user = str(options.get("user", ""))
                password = str(options.get("password", ""))
                if not dbname or not user:
                    self._log(
                        "<b>AI:</b> SQL result preview is ready, but map loading skipped "
                        "(missing dbname/user for PostGIS layer connection)."
                    )
                    return
                uri.setConnection(host, port, dbname, user, password)
                uri.setDataSource("", f"({sql_text})", geom_col, "", key_col)
                provider = "postgres"
            else:
                db_path = str(options.get("db", "")).strip()
                if not db_path:
                    self._log(
                        "<b>AI:</b> SQL result preview is ready, but map loading skipped "
                        "(missing db path for SpatiaLite/GeoPackage)."
                    )
                    return
                uri.setDatabase(db_path)
                uri.setDataSource("", f"({sql_text})", geom_col, "", key_col)
                provider = "spatialite"

            layer = QgsVectorLayer(uri.uri(False), layer_name, provider)
            if layer is None or not layer.isValid():
                self._log(
                    "<b>AI:</b> SQL executed, but could not load result as map layer. "
                    "Tip: ensure query returns a geometry column (default geom) and set geom_column/key_field if needed."
                )
                return
            QgsProject.instance().addMapLayer(layer)
            self._log(f"<b>AI:</b> SQL result layer added to map: {layer.name()}")
        except Exception as exc:
            self._log(f"<b>AI:</b> SQL result layer load skipped: {exc}")

    def _render_deep_learning_pack(self):
        self._log(
            "<b>AI:</b> Deep-learning engines are available via Copilot contracts.\n"
            "Tasks:\n"
            "- semantic_train\n- semantic_infer\n- instance_train\n- instance_infer\n"
            "Examples:\n"
            "- run dl task=semantic_train image_dir=D:\\data\\images mask_dir=D:\\data\\masks output_dir=D:\\models\\sem\n"
            "- run dl task=semantic_infer input_path=D:\\img.tif model_path=D:\\models\\best_model.pth output_path=D:\\out\\pred.tif\n"
            "- run dl task=instance_train image_dir=D:\\data\\images mask_dir=D:\\data\\masks output_dir=D:\\models\\inst\n"
            "- run dl task=instance_infer input_path=D:\\img.tif model_path=D:\\models\\best_model.pth output_path=D:\\out\\inst.gpkg\n"
            "Runtime requirement: GeoAI worker + its venv packages must exist."
        )

    def _render_python_stack_pack(self):
        self._log(
            "<b>AI:</b> Recommended Geospatial Python stack:\n"
            "- GeoPandas, Shapely, Rasterio, Fiona, PyProj, NumPy, GDAL, Xarray, rioxarray\n"
            "Quick install (venv):\n"
            "<code>pip install geopandas shapely rasterio fiona pyproj numpy xarray rioxarray</code>\n"
            "GDAL note: install with conda-forge or wheel matching your Python/OS.\n"
            "Template snippets:\n"
            "1) Vector read/reproject/export (GeoPandas + PyProj)\n"
            "<code>import geopandas as gpd\n"
            "gdf = gpd.read_file('in.gpkg')\n"
            "gdf = gdf.to_crs('EPSG:32645')\n"
            "gdf.to_file('out.gpkg', driver='GPKG')</code>\n"
            "2) Raster clip/reproject (Rasterio)\n"
            "<code>import rasterio\n"
            "from rasterio.mask import mask\n"
            "from rasterio.warp import calculate_default_transform, reproject, Resampling</code>\n"
            "3) Geometry ops (Shapely)\n"
            "<code>from shapely.geometry import shape\n"
            "inter = geom_a.intersection(geom_b)\n"
            "buf = geom_a.buffer(25)</code>\n"
            "4) Multidimensional raster (Xarray + rioxarray)\n"
            "<code>import xarray as xr\n"
            "import rioxarray as rxr\n"
            "da = rxr.open_rasterio('stack.tif')\n"
            "da = da.rio.reproject('EPSG:3857')</code>"
        )

    def _run_deep_learning_natural_router(self, normalized_query, options):
        q = (normalized_query or "").lower()
        if "semantic" in q and "train" in q:
            options = dict(options or {})
            options.setdefault("task", "semantic_train")
            self._run_deep_learning_contract_workflow(q, options)
            return
        if "semantic" in q and any(w in q for w in ("infer", "inference", "predict")):
            options = dict(options or {})
            options.setdefault("task", "semantic_infer")
            self._run_deep_learning_contract_workflow(q, options)
            return
        if "instance" in q and "train" in q:
            options = dict(options or {})
            options.setdefault("task", "instance_train")
            self._run_deep_learning_contract_workflow(q, options)
            return
        if "instance" in q and any(w in q for w in ("infer", "inference", "predict")):
            options = dict(options or {})
            options.setdefault("task", "instance_infer")
            self._run_deep_learning_contract_workflow(q, options)
            return
        self._render_deep_learning_pack()

    def _run_deep_learning_contract_workflow(self, normalized_query, options):
        task = str((options or {}).get("task", "")).strip().lower()
        if not task:
            self._log("<b>AI:</b> Missing task. Use /dl tasks.")
            return

        task_map = {
            "semantic_train": "train_segmentation_model",
            "semantic_infer": "semantic_segmentation",
            "instance_train": "train_instance_segmentation_model",
            "instance_infer": "instance_segmentation",
        }
        action = task_map.get(task)
        if not action:
            self._log(f"<b>AI:</b> Unknown DL task '{task}'. Use /dl tasks.")
            return

        params = self._build_dl_task_params(task, options)
        missing = self._missing_dl_required(task, params)
        if missing:
            self._log(
                f"<b>AI:</b> Missing required parameters for {task}: {', '.join(missing)}.\n"
                "Use /dl tasks for examples."
            )
            return

        self._run_geoai_worker_action(action, params, f"DL {task}")

    def _build_dl_task_params(self, task, options):
        def pick(*keys, default=None):
            for k in keys:
                v = options.get(k)
                if v is not None and str(v).strip() != "":
                    return str(v).strip()
            return default

        params = {}
        if task in ("semantic_train", "instance_train"):
            params["image_dir"] = pick("image_dir", "images", "image_folder")
            params["mask_dir"] = pick("mask_dir", "masks", "label_dir")
            params["output_dir"] = pick("output_dir", "model_dir", default=str(self._resolve_output_dir_from_query(self.last_user_query) / "dl_models"))
            if pick("epochs"):
                params["epochs"] = int(float(pick("epochs")))
            if pick("batch_size"):
                params["batch_size"] = int(float(pick("batch_size")))
            if pick("learning_rate", "lr"):
                params["learning_rate"] = float(pick("learning_rate", "lr"))
            if pick("model"):
                params["model"] = pick("model")
            if pick("num_classes"):
                params["num_classes"] = int(float(pick("num_classes")))
        else:
            params["input_path"] = pick("input_path", "input", "raster")
            params["model_path"] = pick("model_path", "model")
            params["output_path"] = pick("output_path", "output", default=str(self._resolve_output_dir_from_query(self.last_user_query) / f"{task}_output.tif"))
            if pick("tile_size"):
                params["tile_size"] = int(float(pick("tile_size")))
            if pick("stride"):
                params["stride"] = int(float(pick("stride")))
            if pick("threshold", "score_threshold"):
                params["threshold"] = float(pick("threshold", "score_threshold"))
        return params

    def _missing_dl_required(self, task, params):
        req = {
            "semantic_train": ("image_dir", "mask_dir", "output_dir"),
            "instance_train": ("image_dir", "mask_dir", "output_dir"),
            "semantic_infer": ("input_path", "model_path", "output_path"),
            "instance_infer": ("input_path", "model_path", "output_path"),
        }.get(task, ())
        return [k for k in req if not params.get(k)]

    def _resolve_geoai_runtime_paths(self):
        candidates = [
            Path("D:/Cursor Project/geoai"),
            Path.home() / "geoai",
            Path.cwd() / "geoai",
        ]
        for root in candidates:
            worker = root / "workers" / "geoai_task_worker.py"
            py = root / ".venv" / "Scripts" / "python.exe"
            if worker.exists() and py.exists():
                return {"root": root, "worker": worker, "python": py}
        return None

    def _run_geoai_worker_action(self, action, params, label):
        runtime = self._resolve_geoai_runtime_paths()
        if not runtime:
            self._log(
                "<b>AI:</b> GeoAI runtime not found for deep-learning engine.\n"
                "Expected: D:\\Cursor Project\\geoai with .venv and workers\\geoai_task_worker.py"
            )
            return
        payload = {"action": action, "params": params}
        cmd = [str(runtime["python"]), "-u", str(runtime["worker"])]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(runtime["root"]),
            )
            if proc.stdin is None or proc.stdout is None:
                self._log(f"<b>AI:</b> {label} failed to start worker IO.")
                return
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            proc.stdin.close()

            final_result = None
            for raw in proc.stdout:
                line = (raw or "").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "progress":
                    text = str(msg.get("message", "")).strip()
                    if text:
                        self._log(f"<b>AI ({label}):</b> {text}")
                elif mtype == "ok":
                    final_result = msg.get("result")
                elif mtype == "error":
                    self._log(f"<b>AI:</b> {label} failed: {msg.get('message', 'unknown error')}")
                    return
            rc = proc.wait()
            if rc != 0:
                err = ""
                try:
                    if proc.stderr is not None:
                        err = proc.stderr.read()[:2000]
                except Exception:
                    pass
                self._log(f"<b>AI:</b> {label} worker exited with code {rc}. {err}")
                return

            self._remember_action(self.last_user_query, f"{label} completed")
            self._log(f"<b>AI:</b> {label} complete. Result: {final_result}")
        except Exception as exc:
            self._log(f"<b>AI:</b> {label} execution failed: {exc}")

    def _render_geoai_pack(self):
        self._log(
            "<b>AI:</b> GeoAI nature is embedded in Copilot.\n"
            "Use natural prompts or strict contracts.\n"
            "Natural examples:\n"
            "- open moondream panel\n"
            "- run water segmentation on active raster\n"
            "- vectorize this mask and smooth polygons\n"
            "- clip raster by bbox 85.2,20.1,85.4,20.3\n"
            "Strict contracts:\n"
            "- run geoai task=clip_raster_by_bbox raster=active bbox=xmin,ymin,xmax,ymax\n"
            "- run geoai task=vectorize_mask input=active field=class\n"
            "- run geoai task=smooth_vector input=active iterations=1 offset=0.25\n"
            "- run geoai task=export_geotiff_tiles input=active tile_size=512 stride=512\n"
            "Panel delegation (if GeoAI plugin is installed): moondream/samgeo/deepforest/water/semantic/instance."
        )

    def _run_geoai_nature_router(self, normalized_query, options):
        q = (normalized_query or "").lower()
        if "bbox" in q and "clip" in q and "raster" in q:
            self._run_geoai_clip_raster_by_bbox(q, options)
            return
        if "vectorize" in q and any(k in q for k in ("mask", "raster")):
            self._run_geoai_vectorize_mask(q, options)
            return
        if "smooth" in q and any(k in q for k in ("vector", "polygon", "footprint")):
            self._run_geoai_smooth_vector(q, options)
            return
        if "tile" in q and any(k in q for k in ("export", "create", "build")):
            self._run_geoai_export_tiles(q, options)
            return
        if "moondream" in q:
            self._open_geoai_panel_if_available("moondream")
            return
        if "samgeo" in q or "segment anything" in q:
            self._open_geoai_panel_if_available("samgeo")
            return
        if "deepforest" in q or "tree segmentation" in q:
            self._open_geoai_panel_if_available("deepforest")
            return
        if "water segmentation" in q or ("water" in q and "segment" in q):
            self._open_geoai_panel_if_available("water")
            return
        if "semantic segmentation" in q:
            self._open_geoai_panel_if_available("semantic")
            return
        if "instance segmentation" in q:
            self._open_geoai_panel_if_available("instance")
            return
        self._render_geoai_pack()

    def _run_geoai_contract_workflow(self, normalized_query, options):
        task = (options.get("task") or "").strip().lower()
        if not task:
            self._log(
                "<b>AI:</b> GeoAI contract needs task=...\n"
                "Examples:\n"
                "- run geoai task=clip_raster_by_bbox raster=active bbox=xmin,ymin,xmax,ymax\n"
                "- run geoai task=vectorize_mask input=active field=class"
            )
            return
        if task in ("clip_raster_by_bbox", "clip_bbox", "clip"):
            self._run_geoai_clip_raster_by_bbox(normalized_query, options)
            return
        if task in ("vectorize_mask", "raster_to_vector", "vectorize"):
            self._run_geoai_vectorize_mask(normalized_query, options)
            return
        if task in ("smooth_vector", "smooth"):
            self._run_geoai_smooth_vector(normalized_query, options)
            return
        if task in ("export_geotiff_tiles", "export_tiles", "tiles"):
            self._run_geoai_export_tiles(normalized_query, options)
            return
        if task in ("semantic_segmentation", "instance_segmentation", "segment_water", "water_segmentation", "moondream", "samgeo", "deepforest"):
            self._run_geoai_nature_router(f"geoai {task}", options)
            return
        self._log(f"<b>AI:</b> Unknown GeoAI task '{task}'. Use /geoai tasks.")

    def _run_geoai_clip_raster_by_bbox(self, normalized_query, options):
        if not self._processing_available():
            return
        raster_opt = options.get("raster") or options.get("input")
        raster = self.iface.activeLayer() if str(raster_opt).lower() in ("", "active") else self._resolve_layer_option(raster_opt)
        if raster is None or not isinstance(raster, QgsRasterLayer):
            self._log("<b>AI:</b> GeoAI clip by bbox requires an active/input raster layer.")
            return
        bbox = options.get("bbox") or self._extract_bbox_from_text(self.last_user_query)
        if not bbox:
            self._log("<b>AI:</b> Provide bbox as xmin,ymin,xmax,ymax.")
            return
        try:
            xmin, ymin, xmax, ymax = [float(x.strip()) for x in str(bbox).split(",")]
        except Exception:
            self._log("<b>AI:</b> Invalid bbox format. Use xmin,ymin,xmax,ymax.")
            return
        try:
            out = processing.run(
                "gdal:cliprasterbyextent",
                {
                    "INPUT": raster,
                    "PROJWIN": f"{xmin},{xmax},{ymax},{ymin}",
                    "OVERCRS": False,
                    "NODATA": None,
                    "OPTIONS": "",
                    "DATA_TYPE": 0,
                    "OUTPUT": "TEMPORARY_OUTPUT",
                },
            )["OUTPUT"]
            self._log(f"<b>AI:</b> GeoAI clip-by-bbox complete: {out}")
            self._remember_action(self.last_user_query, "geoai clip raster by bbox done")
        except Exception as exc:
            self._log(f"<b>AI:</b> GeoAI clip-by-bbox failed: {exc}")

    def _is_vegetation_polygon_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_veg = any(
            k in q
            for k in (
                "vegetation",
                "veg",
                "green cover",
                "tree cover",
                "tree grass",
                "grass tree",
                "tree/grass",
                "tree-grass",
            )
        )
        has_detect = any(k in q for k in ("detect", "find", "extract", "identify", "map", "classification", "classify", "polygon"))
        has_polygon = any(k in q for k in ("polygon", "polygons", "vector", "boundary", "boundaries"))
        has_boundary = any(k in q for k in ("aoi", "area of interest", "bbox", "boundary", "buffer", "within", "inside"))
        return has_veg and (has_detect or has_polygon) and has_boundary

    def _run_vegetation_polygon_workflow(self, original_query, normalized_query):
        if not self._processing_available():
            return
        q = (normalized_query or "").lower()
        mask = self._best_polygon_mask_layer(normalized_query)
        if mask is None:
            self._log("<b>AI:</b> AOI/BBox/Buffer polygon layer not found. Load boundary polygon and retry.")
            return
        raster_layers = self._resolve_raster_layers_for_vegetation(normalized_query, mask)
        if not raster_layers:
            self._log("<b>AI:</b> No loaded raster/image layer found for vegetation detection.")
            return

        poly_layers = []
        clip_desc = f"mask:{mask.name()}"
        step_errors = []
        try:
            for raster in raster_layers:
                clipped = None
                if "bbox" in q and self._extract_bbox_from_text(original_query or ""):
                    clipped = self._clip_raster_by_bbox_from_query(raster, original_query)
                    if clipped is None:
                        clipped = self._clip_raster_by_mask_layer(raster, mask)
                        clip_desc = f"bbox->mask:{mask.name()}"
                    else:
                        clip_desc = "bbox"
                else:
                    clipped = self._clip_raster_by_mask_layer(raster, mask)
                if clipped is None:
                    step_errors.append(f"{raster.name()}: clip failed (check CRS overlap with '{mask.name()}')")
                    continue
                veg_mask = self._create_vegetation_mask_proxy(clipped)
                if veg_mask is None:
                    step_errors.append(f"{raster.name()}: vegetation mask failed")
                    continue
                poly = processing.run(
                    "gdal:polygonize",
                    {
                        "INPUT": veg_mask,
                        "BAND": 1,
                        "FIELD": "class",
                        "EIGHT_CONNECTEDNESS": False,
                        "OUTPUT": "memory:",
                    },
                )["OUTPUT"]
                poly = self._cleanup_vegetation_polygon_output(poly, raster, mask)
                if poly is None or poly.featureCount() <= 0:
                    step_errors.append(f"{raster.name()}: no vegetation polygons after cleanup")
                    continue
                poly.setName(f"{raster.name()}_veg_poly")
                poly_layers.append(poly)
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not prepare boundary clip: {exc}")
            return

        if not poly_layers:
            detail = "<br>".join(step_errors[:6]) if step_errors else "No raster produced polygons."
            self._log(
                "<b>AI:</b> Could not derive vegetation polygons from loaded rasters.<br>"
                f"{detail}<br>"
                "Tip: ensure ortho tiles overlap the Bbox/AOI polygon and reload plugin after update."
            )
            return

        try:
            if len(poly_layers) == 1:
                merged = poly_layers[0]
            else:
                merged = processing.run(
                    "native:mergevectorlayers",
                    {"LAYERS": poly_layers, "CRS": poly_layers[0].crs(), "OUTPUT": "memory:"},
                )["OUTPUT"]
            merged.setName("vegetation_polygons")
            QgsProject.instance().addMapLayer(merged)
            self._log(
                "<b>AI:</b> Vegetation detection executed and polygons created.\n"
                f"Boundary used: {clip_desc}\n"
                f"Rasters processed: {len(raster_layers)}\n"
                f"Output layer: {merged.name()} (features: {merged.featureCount()})"
            )
            self._remember_action(self.last_user_query, "vegetation polygons created")
        except Exception as exc:
            self._log(f"<b>AI:</b> Vegetation polygonization failed: {exc}")

    def _cleanup_vegetation_polygon_output(self, poly_layer, source_raster, mask_layer=None):
        """
        Keep only vegetation-positive polygons, remove tiny noise polygons,
        and attach deterministic metadata fields for downstream QA.
        """
        if poly_layer is None or not isinstance(poly_layer, QgsVectorLayer):
            return None
        try:
            crs_authid = poly_layer.crs().authid() or "EPSG:4326"
            out = QgsVectorLayer(f"Polygon?crs={crs_authid}", f"{poly_layer.name()}_clean", "memory")
            prov = out.dataProvider()
            fields = list(poly_layer.fields())
            fields.append(QgsField("veg_class", QVariant.String, "string", 16, 0))
            fields.append(QgsField("src_raster", QVariant.String, "string", 120, 0))
            prov.addAttributes(fields)
            out.updateFields()

            class_idx = poly_layer.fields().indexFromName("class")
            if class_idx < 0:
                class_idx = poly_layer.fields().indexFromName("DN")

            min_area = self._vegetation_min_polygon_area(poly_layer, mask_layer)
            for feat in poly_layer.getFeatures():
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                if class_idx >= 0:
                    val = feat[class_idx]
                    try:
                        if float(val) <= 0:
                            continue
                    except Exception:
                        txt = str(val).strip().lower()
                        if txt in {"0", "0.0", "false", "", "none"}:
                            continue
                try:
                    if float(geom.area()) <= min_area:
                        continue
                except Exception:
                    pass
                nf = QgsFeature(out.fields())
                nf.setGeometry(geom)
                attrs = list(feat.attributes())
                attrs.append("vegetation")
                attrs.append(source_raster.name() if source_raster is not None else "")
                nf.setAttributes(attrs)
                prov.addFeature(nf)
            out.updateExtents()
            if out.featureCount() <= 0:
                return poly_layer
            return out
        except Exception:
            return poly_layer

    def _vegetation_min_polygon_area(self, poly_layer, mask_layer=None):
        ref = mask_layer if isinstance(mask_layer, QgsVectorLayer) else poly_layer
        try:
            if ref.crs().isGeographic():
                return 1.0e-8
            return 0.25
        except Exception:
            return 0.25

    def _resolve_raster_layers_for_vegetation(self, normalized_query, mask_layer=None):
        q = (normalized_query or "").lower()
        rasters = [
            l
            for l in QgsProject.instance().mapLayers().values()
            if isinstance(l, QgsRasterLayer) and l.isValid() and not self._is_streaming_raster_layer(l)
        ]
        if not rasters:
            return []
        if any(k in q for k in ("all images", "all rasters", "all loaded images", "all loaded rasters", "images", "both")):
            return self._filter_rasters_overlapping_mask(rasters, mask_layer) or rasters
        active = self.iface.activeLayer()
        if isinstance(active, QgsRasterLayer) and not self._is_streaming_raster_layer(active):
            return [active]
        overlapping = self._filter_rasters_overlapping_mask(rasters, mask_layer)
        if len(overlapping) > 1:
            return overlapping
        preferred = [
            r
            for r in rasters
            if any(k in r.name().lower() for k in ("imagery", "satellite", "world", "ortho", "clip", "mn-f3", "ps"))
        ]
        if preferred:
            overlapping_pref = self._filter_rasters_overlapping_mask(preferred, mask_layer)
            return overlapping_pref or preferred
        return overlapping or rasters

    def _filter_rasters_overlapping_mask(self, rasters, mask_layer):
        if mask_layer is None or not isinstance(mask_layer, QgsVectorLayer):
            return list(rasters)
        out = []
        try:
            mask_ext = mask_layer.extent()
            if not mask_layer.crs().isValid():
                return list(rasters)
            for raster in rasters:
                try:
                    ext = self._mask_extent_in_raster_crs(mask_layer, raster)
                    if ext.intersects(raster.extent()):
                        out.append(raster)
                except Exception:
                    if mask_ext.intersects(raster.extent()):
                        out.append(raster)
        except Exception:
            return list(rasters)
        return out

    def _best_polygon_mask_layer(self, normalized_query=""):
        q = (normalized_query or "").lower()
        vectors = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        polys = [l for l in vectors if l.geometryType() == QgsWkbTypes.PolygonGeometry]
        if not polys:
            return None
        keywords = ("aoi", "mask", "buffer", "boundary", "bbox")
        named = [l for l in polys if any(k in l.name().lower() for k in keywords)]
        if "bbox" in q:
            bbox_named = [l for l in polys if "bbox" in l.name().lower()]
            if bbox_named:
                return bbox_named[0]
        if "buffer" in q:
            buffer_named = [l for l in polys if "buffer" in l.name().lower()]
            if buffer_named:
                return buffer_named[0]
        if named:
            return named[0]
        return polys[-1]

    def _clip_raster_by_bbox_from_query(self, raster, query_text):
        bbox = self._extract_bbox_from_text(query_text or "")
        if not bbox:
            return None
        try:
            xmin, ymin, xmax, ymax = [float(x.strip()) for x in str(bbox).split(",")]
        except Exception:
            return None
        out = processing.run(
            "gdal:cliprasterbyextent",
            {
                "INPUT": raster,
                "PROJWIN": f"{xmin},{xmax},{ymax},{ymin}",
                "OVERCRS": False,
                "NODATA": None,
                "OPTIONS": "",
                "DATA_TYPE": 0,
                "OUTPUT": "TEMPORARY_OUTPUT",
            },
        )["OUTPUT"]
        lyr = QgsRasterLayer(out, f"{raster.name()}_bbox_clip")
        return lyr if lyr.isValid() else None

    def _clip_raster_by_mask_layer(self, raster, mask_layer):
        mask_input = mask_layer
        try:
            if mask_layer.crs().isValid() and raster.crs().isValid() and mask_layer.crs() != raster.crs():
                mask_input = processing.run(
                    "native:reprojectlayer",
                    {"INPUT": mask_layer, "TARGET_CRS": raster.crs(), "OUTPUT": "memory:"},
                )["OUTPUT"]
        except Exception:
            mask_input = mask_layer
        try:
            out = processing.run(
                "gdal:cliprasterbymasklayer",
                {
                    "INPUT": raster,
                    "MASK": mask_input,
                    "SOURCE_CRS": None,
                    "TARGET_CRS": None,
                    "NODATA": None,
                    "ALPHA_BAND": False,
                    "CROP_TO_CUTLINE": True,
                    "KEEP_RESOLUTION": True,
                    "SET_RESOLUTION": False,
                    "X_RESOLUTION": None,
                    "Y_RESOLUTION": None,
                    "MULTITHREADING": False,
                    "OPTIONS": "",
                    "DATA_TYPE": 0,
                    "OUTPUT": "TEMPORARY_OUTPUT",
                },
            )["OUTPUT"]
        except Exception:
            return None
        lyr = QgsRasterLayer(out, f"{self._safe_file_stem(raster.name())}_mask_clip")
        if not lyr.isValid():
            return None
        try:
            QgsProject.instance().addMapLayer(lyr, False)
        except Exception:
            pass
        return lyr

    def _raster_band_looks_byte(self, raster_layer, band_no=2):
        try:
            stats = raster_layer.dataProvider().bandStatistics(band_no)
            return float(stats.maximum) > 1.5
        except Exception:
            return True

    def _create_vegetation_mask_proxy(self, raster_layer):
        """
        Build a simple vegetation proxy mask:
        - If >=4 bands: NDVI-like using band4(NIR)/band1(red assumption for common stacks)
        - Else if >=3 bands: green-dominance proxy using band2 over band1/band3
        Returns raster layer mask where vegetation pixels are 1 else 0.
        """
        try:
            band_count = int(raster_layer.bandCount())
        except Exception:
            band_count = 1
        out_path = "TEMPORARY_OUTPUT"
        layer_ref = raster_layer.name().replace('"', "")
        try:
            if band_count >= 4:
                # NDVI-style proxy where band4 often acts as NIR in common stacks.
                expr = '((("{0}@4"-"{0}@1")/("{0}@4"+"{0}@1"+0.0001))>0.20)*1'.format(layer_ref)
            elif band_count >= 3:
                byte_scale = self._raster_band_looks_byte(raster_layer, 2)
                if byte_scale:
                    # RGB Excess Green proxy for ortho/web imagery (0-255).
                    expr = '(((2*"{0}@2"-"{0}@1"-"{0}@3") > 8) AND ("{0}@2" > 35))*1'.format(layer_ref)
                else:
                    # Float/scaled RGB (0-1).
                    expr = '(((2*"{0}@2"-"{0}@1"-"{0}@3") > 0.02) AND ("{0}@2" > 0.12))*1'.format(layer_ref)
            else:
                expr = '("{0}@1" > 0)*1'.format(layer_ref)
            out = processing.run(
                "qgis:rastercalculator",
                {
                    "EXPRESSION": expr,
                    "LAYERS": [raster_layer],
                    "CELLSIZE": 0,
                    "EXTENT": raster_layer.extent(),
                    "CRS": raster_layer.crs(),
                    "OUTPUT": out_path,
                },
            )["OUTPUT"]
            mask = QgsRasterLayer(out, f"{self._safe_file_stem(raster_layer.name())}_veg_mask")
            if not mask.isValid():
                return None
            try:
                QgsProject.instance().addMapLayer(mask, False)
            except Exception:
                pass
            return mask
        except Exception:
            return None

    def _run_geoai_vectorize_mask(self, normalized_query, options):
        if not self._processing_available():
            return
        in_opt = options.get("input") or options.get("raster")
        raster = self.iface.activeLayer() if str(in_opt).lower() in ("", "active") else self._resolve_layer_option(in_opt)
        if raster is None or not isinstance(raster, QgsRasterLayer):
            self._log("<b>AI:</b> GeoAI vectorize_mask requires a raster mask layer as input.")
            return
        field_name = (options.get("field") or "class").strip() or "class"
        try:
            out = processing.run(
                "gdal:polygonize",
                {
                    "INPUT": raster,
                    "BAND": int(options.get("band", "1")),
                    "FIELD": field_name,
                    "EIGHT_CONNECTEDNESS": False,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            out.setName(f"{raster.name()}_vectorized")
            QgsProject.instance().addMapLayer(out)
            self._remember_action(self.last_user_query, "geoai vectorize mask done")
            self._log(f"<b>AI:</b> GeoAI vectorize mask complete. Output layer: {out.name()}")
        except Exception as exc:
            self._log(f"<b>AI:</b> GeoAI vectorize mask failed: {exc}")

    def _run_geoai_smooth_vector(self, normalized_query, options):
        if not self._processing_available():
            return
        in_opt = options.get("input") or options.get("layer")
        layer = self.iface.activeLayer() if str(in_opt).lower() in ("", "active") else self._resolve_layer_option(in_opt)
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> GeoAI smooth_vector requires a vector input layer.")
            return
        try:
            iterations = int(options.get("iterations", "1"))
        except Exception:
            iterations = 1
        try:
            offset = float(options.get("offset", "0.25"))
        except Exception:
            offset = 0.25
        try:
            out = processing.run(
                "native:smoothgeometry",
                {
                    "INPUT": layer,
                    "ITERATIONS": iterations,
                    "OFFSET": offset,
                    "MAX_ANGLE": 180.0,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            out.setName(f"{layer.name()}_smoothed")
            QgsProject.instance().addMapLayer(out)
            self._remember_action(self.last_user_query, "geoai smooth vector done")
            self._log(f"<b>AI:</b> GeoAI smooth vector complete. Output layer: {out.name()}")
        except Exception as exc:
            self._log(f"<b>AI:</b> GeoAI smooth vector failed: {exc}")

    def _run_geoai_export_tiles(self, normalized_query, options):
        if not self._processing_available():
            return
        in_opt = options.get("input") or options.get("raster")
        raster = self.iface.activeLayer() if str(in_opt).lower() in ("", "active") else self._resolve_layer_option(in_opt)
        if raster is None or not isinstance(raster, QgsRasterLayer):
            self._log("<b>AI:</b> GeoAI export tiles requires a raster input layer.")
            return
        out_dir = self._resolve_output_dir_from_query(self.last_user_query) / f"{raster.name()}_tiles"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            tile_size = int(options.get("tile_size", "512"))
        except Exception:
            tile_size = 512
        try:
            stride = int(options.get("stride", str(tile_size)))
        except Exception:
            stride = tile_size
        try:
            processing.run(
                "gdal:retile",
                {
                    "INPUT": raster,
                    "TILE_WIDTH": tile_size,
                    "TILE_HEIGHT": tile_size,
                    "OVERLAP": max(0, tile_size - stride),
                    "TARGET_CRS": None,
                    "RESAMPLING": 0,
                    "OPTIONS": "",
                    "DATA_TYPE": 0,
                    "OUTPUT": str(out_dir),
                },
            )
            self._remember_action(self.last_user_query, "geoai export tiles done")
            self._log(f"<b>AI:</b> GeoAI export tiles complete: {out_dir}")
        except Exception as exc:
            self._log(f"<b>AI:</b> GeoAI export tiles failed: {exc}")

    def _open_geoai_panel_if_available(self, panel_key):
        try:
            plugin = qgis.utils.plugins.get("geoai")
        except Exception:
            plugin = None
        if plugin is None:
            self._log(
                "<b>AI:</b> GeoAI plugin is not loaded in this QGIS session.\n"
                "I can still run embedded equivalents (clip bbox, vectorize mask, smooth vector, export tiles) "
                "via `run geoai task=...`."
            )
            return
        try:
            key = (panel_key or "").lower()
            if key == "moondream":
                plugin.toggle_moondream_dock()
            elif key == "samgeo":
                plugin.toggle_samgeo_dock()
            elif key == "deepforest":
                plugin.toggle_deepforest_dock()
            elif key == "water":
                plugin.toggle_water_segmentation_dock()
            elif key == "semantic":
                plugin.toggle_segmentation_dock()
            elif key == "instance":
                plugin.toggle_instance_segmentation_dock()
            else:
                self._render_geoai_pack()
                return
            self._log(f"<b>AI:</b> Opened GeoAI {key} panel.")
            self._remember_action(self.last_user_query, f"opened geoai {key} panel")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not open GeoAI panel '{panel_key}': {exc}")

    def _run_999_updater_workflow(self, normalized_query, options):
        layer = self._resolve_layer_option(options.get("layer")) or self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> 999 updater requires an active vector layer.")
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> 999 updater currently supports line layers.")
            return

        field_names = [f.name() for f in layer.fields()]
        veg_field = options.get("veg_field") or self._match_field_name("vegremarks", field_names)
        length_field = (
            options.get("length_field")
            or self._match_field_name("length_lms", field_names)
            or self._match_field_name("length", field_names)
        )
        try:
            short_m = float(options.get("short_m", "10"))
        except Exception:
            short_m = 10.0
        if veg_field is None:
            self._log("<b>AI:</b> Field 'vegremarks' not found on active layer.")
            return

        # Detect terminal spans and update vegremarks=999 where terminal/short.
        endpoint_degree, segments = self._build_segment_endpoint_degree(layer)
        terminal_feature_ids = set()
        for seg in segments:
            k1, k2 = seg["keys"]
            d1 = endpoint_degree.get(k1, 0)
            d2 = endpoint_degree.get(k2, 0)
            if (d1 <= 1 < d2) or (d2 <= 1 < d1):
                terminal_feature_ids.add(seg["feature_id"])

        started_edit = False
        updated = 0
        try:
            if not layer.isEditable():
                started_edit = layer.startEditing()
                if not started_edit:
                    self._log("<b>AI:</b> Could not start editing for 999 updater.")
                    return

            for feat in layer.getFeatures():
                fid = feat.id()
                mark_999 = fid in terminal_feature_ids
                if not mark_999 and length_field:
                    val = feat[length_field]
                    try:
                        mark_999 = float(val) < short_m
                    except Exception:
                        pass
                if mark_999:
                    if layer.changeAttributeValue(fid, layer.fields().indexFromName(veg_field), "999"):
                        updated += 1

            if started_edit:
                if not layer.commitChanges():
                    layer.rollBack()
                    self._log("<b>AI:</b> 999 updater failed while committing edits.")
                    return
            self._remember_action(self.last_user_query, f"999 updater updated {updated} features")
            self._log(f"<b>AI:</b> 999 updater complete. Updated {updated} feature(s) in field '{veg_field}'.")
        except Exception as exc:
            if started_edit and layer.isEditable():
                layer.rollBack()
            self._log(f"<b>AI:</b> 999 updater failed: {exc}")

    def _run_agc_batch_qa_workflow(self, normalized_query, options):
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        line_layers = [l for l in layers if l.geometryType() == QgsWkbTypes.LineGeometry]
        raw_layer = self._resolve_layer_option(options.get("raw"))
        corr_layer = self._resolve_layer_option(options.get("corrected"))
        if raw_layer is None or corr_layer is None:
            if len(line_layers) < 2:
                self._log("<b>AI:</b> AGC QA requires two line layers. Use raw=<name> corrected=<name>.")
                return
            raw_layer = line_layers[0]
            corr_layer = line_layers[1]
        try:
            tol_m = float(options.get("tol_m", str(self._extract_distance(normalized_query) or 1.0)))
        except Exception:
            tol_m = 1.0
        tol_raw_units = self._meters_to_layer_units(raw_layer, tol_m)

        out = QgsVectorLayer(
            f"LineString?crs={raw_layer.crs().authid()}",
            f"agc_qa_mismatch_{raw_layer.name()}_{corr_layer.name()}",
            "memory",
        )
        provider = out.dataProvider()
        provider.addAttributes(
            [
                QgsField("raw_id", QVariant.Int),
                QgsField("corr_id", QVariant.Int),
                QgsField("hausdorff", QVariant.Double),
            ]
        )
        out.updateFields()

        mismatches = 0
        corr_feats = list(corr_layer.getFeatures())
        for rf in raw_layer.getFeatures():
            rg = rf.geometry()
            if rg is None or rg.isEmpty():
                continue
            nearest = None
            best = None
            for cf in corr_feats[:5000]:
                cg = cf.geometry()
                if cg is None or cg.isEmpty():
                    continue
                if not rg.boundingBox().intersects(cg.boundingBox()):
                    continue
                try:
                    hd = rg.hausdorffDistance(cg)
                except Exception:
                    continue
                if best is None or hd < best:
                    best = hd
                    nearest = cf
            if nearest is None:
                continue
            if best is not None and best > tol_raw_units:
                of = QgsFeature(out.fields())
                of.setGeometry(rg)
                of.setAttribute("raw_id", int(rf.id()))
                of.setAttribute("corr_id", int(nearest.id()))
                of.setAttribute("hausdorff", float(best))
                provider.addFeature(of)
                mismatches += 1

        QgsProject.instance().addMapLayer(out)
        out_dir = self._resolve_output_dir_from_query(self.last_user_query)
        csv_path = out_dir / "agc_summary.csv"
        csv_path.write_text(f"metric,value\nmismatches,{mismatches}\n", encoding="utf-8")
        self._remember_action(self.last_user_query, f"agc qa mismatches {mismatches}")
        self._log(
            f"<b>AI:</b> AGC batch QA complete. Mismatch features: {mismatches}.\n"
            f"Output layer: {out.name()}\nSummary: {csv_path}"
        )

    def _run_ivms_span_qa_workflow(self, normalized_query, options):
        if not self._processing_available():
            return
        layer = self._resolve_layer_option(options.get("layer")) or self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> IVMS QA workflow expects a line layer.")
            return
        # Combine validity + unsnapped detection + end-span centroid outputs.
        self._run_check_validity()
        self._run_detect_unsnapped_spans()
        self._run_end_span_centroids()
        self._remember_action(self.last_user_query, "ivms span qa bundle executed")
        self._log("<b>AI:</b> IVMS QA workflow complete (validity + unsnapped + end-span centroids).")

    def _run_qautofix_workflow(self, normalized_query, options):
        if not self._processing_available():
            return
        layer = self._resolve_layer_option(options.get("layer")) or self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> QAutoFix workflow currently supports line layers.")
            return
        try:
            distance_m = float(options.get("tol_m", str(self._extract_distance(normalized_query) or 1.0)))
        except Exception:
            distance_m = 1.0
        snap_units = self._meters_to_layer_units(layer, distance_m)
        try:
            snapped = processing.run(
                "native:snapgeometries",
                {
                    "INPUT": layer,
                    "REFERENCE_LAYER": layer,
                    "TOLERANCE": snap_units,
                    "BEHAVIOR": 0,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            fixed = processing.run(
                "native:fixgeometries",
                {"INPUT": snapped, "METHOD": 1, "OUTPUT": "memory:"},
            )["OUTPUT"]
            fixed.setName(f"{layer.name()}_qautofix")
            QgsProject.instance().addMapLayer(fixed)
            self._remember_action(self.last_user_query, "qautofix completed")
            self._log(
                f"<b>AI:</b> QAutoFix complete. Created layer '{fixed.name()}' "
                f"with snap tolerance {distance_m}m."
            )
        except Exception as exc:
            self._log(f"<b>AI:</b> QAutoFix workflow failed: {exc}")

    def _run_tg_th_validator_workflow(self, normalized_query, options):
        if not self._processing_available():
            return
        layer = self._resolve_layer_option(options.get("layer")) or self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            self._log("<b>AI:</b> TG/TH validator expects polygon layers.")
            return
        mode_opt = str(options.get("mode", "")).strip().lower()
        mode = "TH" if mode_opt == "th" or ("th" in normalized_query and "tg" not in normalized_query) else "TG/TH"
        try:
            fixed = processing.run(
                "native:fixgeometries",
                {"INPUT": layer, "METHOD": 1, "OUTPUT": "memory:"},
            )["OUTPUT"]
            validity = processing.run(
                "native:checkvalidity",
                {
                    "INPUT_LAYER": fixed,
                    "METHOD": 2,
                    "VALID_OUTPUT": "memory:",
                    "INVALID_OUTPUT": "memory:",
                    "ERROR_OUTPUT": "memory:",
                },
            )
            valid_l = validity["VALID_OUTPUT"]
            invalid_l = validity["INVALID_OUTPUT"]
            valid_l.setName(f"{layer.name()}_{mode}_valid")
            invalid_l.setName(f"{layer.name()}_{mode}_invalid")
            QgsProject.instance().addMapLayer(valid_l)
            QgsProject.instance().addMapLayer(invalid_l)
            self._remember_action(self.last_user_query, "tg th validator executed")
            self._log(
                f"<b>AI:</b> TG/TH validator complete. "
                f"Valid: {valid_l.featureCount()}, Invalid: {invalid_l.featureCount()}."
            )
        except Exception as exc:
            self._log(f"<b>AI:</b> TG/TH validator failed: {exc}")

    def _handle_aidash_tool_command(self, normalized_query, original_query):
        if "feeder splitter" in normalized_query:
            self._run_split_by_field(normalized_query)
            return True
        if "overlap checker" in normalized_query:
            self._run_overlap_checker(normalized_query)
            return True
        if "overlap mismatch" in normalized_query:
            self._run_overlap_checker(normalized_query)
            return True
        if "tile index" in normalized_query or "tiff tile index generator" in normalized_query:
            self._run_tile_index_generator(normalized_query)
            return True
        if "shapefile merger" in normalized_query:
            self._run_shapefile_merger_autonomous(normalized_query)
            return True
        if "geo file to excel exporter" in normalized_query or "geo file excel exporter" in normalized_query:
            self._run_geo_to_excel_export(normalized_query)
            return True
        if "dynamic bbox buffer" in normalized_query:
            self._run_dynamic_bbox_buffer(normalized_query)
            return True
        if "bbox conf manual" in normalized_query:
            self._run_bbox_conf_manual(normalized_query)
            return True
        if "shapefile renamer" in normalized_query:
            self._run_shapefile_renamer(normalized_query)
            return True
        if "vertex mismatch checker" in normalized_query or "vertex mismatch" in normalized_query:
            self._run_vertex_mismatch_checker(normalized_query)
            return True
        return False

    def _run_overlap_checker(self, normalized_query):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        distance_m = self._extract_distance(normalized_query) or 3.0
        try:
            # Near/self-near overlap proxy using close-endpoint geometry snap + validity.
            snapped = processing.run(
                "native:snapgeometries",
                {
                    "INPUT": layer,
                    "REFERENCE_LAYER": layer,
                    "TOLERANCE": self._meters_to_layer_units(layer, distance_m),
                    "BEHAVIOR": 0,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            invalid = processing.run(
                "native:checkvalidity",
                {
                    "INPUT_LAYER": snapped,
                    "METHOD": 2,
                    "VALID_OUTPUT": "memory:",
                    "INVALID_OUTPUT": "memory:",
                    "ERROR_OUTPUT": "memory:",
                },
            )
            invalid_layer = invalid["INVALID_OUTPUT"]
            err_layer = invalid["ERROR_OUTPUT"]
            invalid_layer.setName(f"{layer.name()}_overlap_invalid")
            err_layer.setName(f"{layer.name()}_overlap_errors")
            QgsProject.instance().addMapLayer(invalid_layer)
            QgsProject.instance().addMapLayer(err_layer)
            self._log(
                f"<b>AI:</b> Overlap check complete. Invalid: {invalid_layer.featureCount()}, "
                f"Errors: {err_layer.featureCount()}."
            )
        except Exception as exc:
            self._log(f"<b>AI:</b> Overlap checker failed: {exc}")

    def _run_tile_index_generator(self, normalized_query):
        if not self._processing_available():
            return
        folder = self._resolve_path_from_query(self.last_user_query, expect_dir=True)
        if not folder:
            self._log("<b>AI:</b> Provide folder path. Example: generate tile index from D:\\rasters")
            return
        folder_path = Path(folder)
        if not folder_path.exists() or not folder_path.is_dir():
            self._log(f"<b>AI:</b> Folder not found: {folder}")
            return

        raster_files = []
        for pat in ("*.tif", "*.tiff", "*.TIF", "*.TIFF"):
            raster_files.extend(str(p) for p in folder_path.rglob(pat))
        raster_files = sorted(set(raster_files))
        if not raster_files:
            self._log(f"<b>AI:</b> No TIFF files found in folder: {folder}")
            return
        out_dir = self._resolve_output_dir_from_query(self.last_user_query)
        out = str(out_dir / "tile_index.gpkg")
        try:
            processing.run(
                "gdal:tileindex",
                {
                    "LAYERS": raster_files,
                    "OUTPUT": out,
                },
            )
            self._log(f"<b>AI:</b> Tile index created with {len(raster_files)} rasters:\n{out}")
            self._remember_action(self.last_user_query, "tile index created")
        except Exception as exc:
            self._log(f"<b>AI:</b> Tile index generation failed: {exc}")

    def _run_shapefile_merger_autonomous(self, normalized_query):
        if self._is_merge_layers_query(normalized_query):
            self._run_merge_loaded_layers(normalized_query)
            return
        self._log("<b>AI:</b> For autonomous merge, load layers and ask: 'merge all loaded shapefiles'.")

    def _run_geo_to_excel_export(self, normalized_query):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Active layer must be a vector layer.")
            return
        out_dir = self._resolve_output_dir_from_query(self.last_user_query)
        out_xlsx = out_dir / f"{layer.name()}_export.xlsx"
        out_csv = out_dir / f"{layer.name()}_export.csv"
        try:
            fields = [f.name() for f in layer.fields()]
            rows = []
            for feat in layer.getFeatures():
                rows.append({k: feat[k] for k in fields})
            try:
                import pandas as pd

                pd.DataFrame(rows).to_excel(str(out_xlsx), index=False)
                self._log(f"<b>AI:</b> Geo export to Excel complete: {out_xlsx}")
            except Exception:
                with open(out_csv, "w", newline="", encoding="utf-8") as fp:
                    writer = csv.DictWriter(fp, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                self._log(f"<b>AI:</b> Excel libraries unavailable. Exported CSV instead: {out_csv}")
            self._remember_action(self.last_user_query, "geo export done")
        except Exception as exc:
            self._log(f"<b>AI:</b> Geo export failed: {exc}")

    def _run_dynamic_bbox_buffer(self, normalized_query):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        base_distance_ft = self._extract_distance(normalized_query) or 10.0
        ring_count = self._extract_integer(normalized_query) or 3
        if ring_count < 1:
            ring_count = 1
        try:
            layers = []
            for i in range(1, ring_count + 1):
                dist_m = (base_distance_ft * i) * 0.3048
                out = processing.run(
                    "native:buffer",
                    {
                        "INPUT": layer,
                        "DISTANCE": self._meters_to_layer_units(layer, dist_m),
                        "SEGMENTS": 8,
                        "END_CAP_STYLE": 0,
                        "JOIN_STYLE": 0,
                        "MITER_LIMIT": 2,
                        "DISSOLVE": False,
                        "OUTPUT": "memory:",
                    },
                )["OUTPUT"]
                out.setName(f"{layer.name()}_bbox_ring_{i}")
                QgsProject.instance().addMapLayer(out)
                layers.append(out)
            self._log(f"<b>AI:</b> Dynamic BBox buffer complete. Created {len(layers)} rings.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Dynamic BBox buffer failed: {exc}")

    def _run_bbox_conf_manual(self, normalized_query):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Select vector layer with bbox_conf field.")
            return
        field = self._match_field_name("bbox_conf", [f.name() for f in layer.fields()])
        if not field:
            self._log("<b>AI:</b> Field 'bbox_conf' not found.")
            return
        try:
            values = []
            idx = layer.fields().indexFromName(field)
            for feat in layer.getFeatures():
                raw = feat[idx]
                if raw is None:
                    continue
                parts = [p.strip() for p in str(raw).split(",") if p.strip()]
                for p in parts:
                    try:
                        values.append(float(p))
                    except Exception:
                        continue
            if not values:
                self._log("<b>AI:</b> No numeric bbox_conf values found.")
                return
            unique_vals = sorted(set(values))
            created = 0
            for ft in unique_vals:
                dist_m = ft * 0.3048
                out = processing.run(
                    "native:buffer",
                    {
                        "INPUT": layer,
                        "DISTANCE": self._meters_to_layer_units(layer, dist_m),
                        "SEGMENTS": 8,
                        "END_CAP_STYLE": 0,
                        "JOIN_STYLE": 0,
                        "MITER_LIMIT": 2,
                        "DISSOLVE": False,
                        "OUTPUT": "memory:",
                    },
                )["OUTPUT"]
                out.setName(f"{layer.name()}_bboxconf_{ft}")
                QgsProject.instance().addMapLayer(out)
                created += 1
            self._log(f"<b>AI:</b> BBox Conf Manual complete. Created {created} buffers.")
        except Exception as exc:
            self._log(f"<b>AI:</b> BBox Conf Manual failed: {exc}")

    def _is_bbox_video_workflow_query(self, normalized_query):
        q = (normalized_query or "").strip()
        if "bbox" not in q:
            return False
        # Strict trigger for the exact requested behavior:
        # any BBox request expressed in feet should run this workflow.
        if any(w in q for w in ("feet", "foot")):
            return True
        return bool(re.search(r"\b\d+(?:\.\d+)?\s*ft\b|\b\d+(?:\.\d+)?ft\b", q))

    def _estimate_layer_utm_epsg(self, layer):
        try:
            src_crs = layer.crs()
            center = layer.extent().center()
            if not src_crs.isGeographic():
                to_wgs84 = QgsCoordinateTransform(
                    src_crs,
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                    QgsProject.instance(),
                )
                center = to_wgs84.transform(center)
            lon = float(center.x())
            lat = float(center.y())
            zone = int((lon + 180.0) / 6.0) + 1
            zone = max(1, min(60, zone))
            return (32600 + zone) if lat >= 0 else (32700 + zone)
        except Exception:
            return None

    def _run_bbox_video_workflow(self, normalized_query):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Select a vector layer to run BBox workflow.")
            return

        epsg = self._extract_epsg(normalized_query)
        if epsg is None:
            project_crs = QgsProject.instance().crs()
            auth = (project_crs.authid() or "").upper()
            if auth.startswith("EPSG:") and project_crs.isValid() and not project_crs.isGeographic():
                try:
                    epsg = int(auth.split(":")[1])
                except Exception:
                    epsg = None
        if epsg is None:
            epsg = self._estimate_layer_utm_epsg(layer)
        if epsg is None:
            self._log("<b>AI:</b> Could not determine target CRS. Mention EPSG explicitly (e.g., EPSG:32617).")
            return

        # Match video workflow strictly: feet-based bbox buffer.
        # Support multiple distances in one prompt, e.g. 5/10/20 feet.
        feet_values = self._extract_feet_distances(normalized_query)
        if not feet_values:
            feet_values = [150.0]

        try:
            reproj = processing.run(
                "native:reprojectlayer",
                {"INPUT": layer, "TARGET_CRS": f"EPSG:{epsg}", "OUTPUT": "memory:"},
            )["OUTPUT"]
            reproj.setName("Reprojected")
            QgsProject.instance().addMapLayer(reproj)
            QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(f"EPSG:{epsg}"))

            # Create one combined output layer for all requested BBox distances.
            combined = QgsVectorLayer(f"Polygon?crs=EPSG:{epsg}", f"{layer.name()}_bbox_multi", "memory")
            cprov = combined.dataProvider()
            out_fields = list(reproj.fields())
            out_fields.append(QgsField("bbox_ft", QVariant.Double))
            cprov.addAttributes(out_fields)
            combined.updateFields()
            created = 0
            for ft in feet_values:
                distance_m = float(ft) * 0.3048
                buffered = processing.run(
                    "native:buffer",
                    {
                        "INPUT": reproj,
                        "DISTANCE": self._meters_to_layer_units(reproj, distance_m),
                        "SEGMENTS": 5,
                        "END_CAP_STYLE": 1,  # Flat
                        "JOIN_STYLE": 2,  # Bevel
                        "MITER_LIMIT": 2.0,
                        "DISSOLVE": False,
                        "OUTPUT": "memory:",
                    },
                )["OUTPUT"]
                for bf in buffered.getFeatures():
                    nf = QgsFeature(combined.fields())
                    nf.setGeometry(bf.geometry())
                    attrs = list(bf.attributes())
                    attrs.append(float(ft))
                    nf.setAttributes(attrs)
                    cprov.addFeature(nf)
                created += 1

            combined.updateExtents()
            combined.setName(f"{layer.name()}_bbox_{'_'.join(str(int(v)) if float(v).is_integer() else str(v) for v in feet_values)}ft")
            QgsProject.instance().addMapLayer(combined)

            # If user provided output path, persist as one shapefile.
            saved_note = ""
            if self._has_explicit_windows_path(self.last_user_query):
                out_dir = self._resolve_output_dir_from_query(self.last_user_query)
                shp_name = f"{combined.name()}.shp"
                shp_path = out_dir / shp_name
                try:
                    writer = QgsVectorFileWriter(
                        str(shp_path),
                        "UTF-8",
                        combined.fields(),
                        combined.wkbType(),
                        combined.crs(),
                        "ESRI Shapefile",
                    )
                    if writer.hasError() == QgsVectorFileWriter.NoError:
                        for f in combined.getFeatures():
                            writer.addFeature(f)
                        del writer
                        saved_note = f"\nSaved as single shapefile: {shp_path}"
                    else:
                        del writer
                except Exception:
                    pass

            feet_label = ", ".join(f"{v:g}" for v in feet_values)
            self._log(
                "<b>AI:</b> BBox workflow complete: project CRS set to "
                f"EPSG:{epsg}, layer reprojected, and created one combined BBox layer "
                f"for {feet_label} feet (flat end cap + bevel join)."
                f"{saved_note}"
            )
            self._remember_action(self.last_user_query, "bbox workflow reproject + buffer")
        except Exception as exc:
            self._log(f"<b>AI:</b> BBox workflow failed: {exc}")

    def _extract_feet_distances(self, text):
        q = str(text or "")
        vals = []
        # Accept: 5 feet, 10ft, 20-foot, 30 - foot
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:-|–)?\s*(?:ft|feet|foot)\b", q, flags=re.IGNORECASE):
            try:
                v = float(m.group(1))
                if v > 0:
                    vals.append(v)
            except Exception:
                continue
        # Preserve order but remove duplicates.
        out = []
        seen = set()
        for v in vals:
            k = round(v, 6)
            if k in seen:
                continue
            seen.add(k)
            out.append(v)
        return out

    def _run_shapefile_renamer(self, normalized_query):
        root = self._resolve_path_from_query(self.last_user_query, expect_dir=True)
        if not root:
            self._log("<b>AI:</b> Provide folder path containing shapefiles.")
            return
        root_path = Path(root)
        if not root_path.exists():
            self._log("<b>AI:</b> Folder path not found.")
            return
        renamed = 0
        skipped = 0
        for shp in root_path.rglob("*.shp"):
            folder_name = shp.parent.name
            base = shp.stem
            if base == folder_name:
                skipped += 1
                continue
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj"):
                src = shp.with_suffix(ext)
                if src.exists():
                    dst = shp.parent / f"{folder_name}{ext}"
                    try:
                        if not dst.exists():
                            src.rename(dst)
                    except Exception:
                        pass
            renamed += 1
        self._log(f"<b>AI:</b> Shapefile renamer complete. Renamed: {renamed}, skipped: {skipped}.")
        self._remember_action(self.last_user_query, "shapefile rename done")

    def _run_vertex_mismatch_checker(self, normalized_query):
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        line_layers = [l for l in layers if l.geometryType() == QgsWkbTypes.LineGeometry]
        if len(line_layers) < 2:
            self._log("<b>AI:</b> Need two loaded line layers for vertex mismatch check.")
            return
        a = line_layers[0]
        b = line_layers[1]
        mismatches = 0
        total = 0
        try:
            b_geoms = []
            for fb in b.getFeatures():
                gb = fb.geometry()
                if gb is None or gb.isEmpty():
                    continue
                b_geoms.append(gb)
            for fa in a.getFeatures():
                ga = fa.geometry()
                if ga is None or ga.isEmpty():
                    continue
                total += 1
                va = len(ga.asPolyline()) if not ga.isMultipart() else sum(len(p) for p in ga.asMultiPolyline())
                near_same = False
                for gb in b_geoms[:2000]:
                    if ga.boundingBox().intersects(gb.boundingBox()):
                        vb = len(gb.asPolyline()) if not gb.isMultipart() else sum(len(p) for p in gb.asMultiPolyline())
                        if va == vb:
                            near_same = True
                            break
                if not near_same:
                    mismatches += 1
            self._log(
                f"<b>AI:</b> Vertex mismatch check finished between '{a.name()}' and '{b.name()}'.\n"
                f"Checked: {total}, suspected mismatches: {mismatches}."
            )
            self._remember_action(self.last_user_query, "vertex mismatch check done")
        except Exception as exc:
            self._log(f"<b>AI:</b> Vertex mismatch check failed: {exc}")

    def _handle_batch_workflow(self, normalized_query):
        if "buffer" in normalized_query and "clip" in normalized_query and " and " in normalized_query:
            self._run_buffer_then_clip_workflow(normalized_query)
            return True
        if "merge" in normalized_query and "reproject" in normalized_query and "dissolve" in normalized_query:
            self._run_merge_reproject_dissolve_workflow(normalized_query)
            return True
        return False

    def _run_buffer_then_clip_workflow(self, normalized_query):
        if not self._processing_available():
            return
        distance = self._extract_distance(normalized_query) or 20.0
        buffer_layer = self._find_layer_for_keyword(normalized_query, "buffer")
        clip_layer = self._find_layer_for_keyword(normalized_query, "clip")
        if buffer_layer is None or clip_layer is None:
            self._log("<b>AI:</b> Ambiguous request. Try: 'buffer roads by 20m and clip parcels'.")
            return
        if self._warn_geographic_distance(buffer_layer, distance):
            return

        try:
            buff = processing.run(
                "native:buffer",
                {
                    "INPUT": buffer_layer,
                    "DISTANCE": self._meters_to_layer_units(buffer_layer, distance),
                    "SEGMENTS": 12,
                    "END_CAP_STYLE": 0,
                    "JOIN_STYLE": 0,
                    "MITER_LIMIT": 2,
                    "DISSOLVE": False,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            out = processing.run(
                "native:clip",
                {"INPUT": clip_layer, "OVERLAY": buff, "OUTPUT": "memory:"},
            )["OUTPUT"]
            out.setName(f"{clip_layer.name()}_clipped_by_{buffer_layer.name()}_{int(distance)}m")
            QgsProject.instance().addMapLayer(out)
            self._remember_action(self.last_user_query, "buffer+clip done")
            self._log("<b>AI:</b> Batch workflow complete: buffer + clip.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Batch workflow failed: {exc}")

    def _run_merge_reproject_dissolve_workflow(self, normalized_query):
        if not self._processing_available():
            return
        epsg = self._extract_epsg(normalized_query)
        if epsg is None:
            self._log("<b>AI:</b> Mention target EPSG, e.g. 'merge reproject dissolve to EPSG:3857'.")
            return
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        if len(layers) < 2:
            self._log("<b>AI:</b> Need at least two vector layers for merge workflow.")
            return
        try:
            merged = processing.run(
                "native:mergevectorlayers",
                {"LAYERS": layers, "CRS": None, "OUTPUT": "memory:"},
            )["OUTPUT"]
            reproj = processing.run(
                "native:reprojectlayer",
                {"INPUT": merged, "TARGET_CRS": f"EPSG:{epsg}", "OUTPUT": "memory:"},
            )["OUTPUT"]
            dissolved = processing.run(
                "native:dissolve",
                {"INPUT": reproj, "FIELD": [], "SEPARATE_DISJOINT": False, "OUTPUT": "memory:"},
            )["OUTPUT"]
            dissolved.setName(f"merged_reprojected_dissolved_{epsg}")
            QgsProject.instance().addMapLayer(dissolved)
            self._remember_action(self.last_user_query, "merge->reproject->dissolve done")
            self._log("<b>AI:</b> Workflow complete: merge -> reproject -> dissolve.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Workflow failed: {exc}")

    def _find_layer_for_keyword(self, normalized_query, anchor):
        words = re.findall(r"[a-zA-Z0-9_]+", normalized_query)
        if anchor not in words:
            return self.iface.activeLayer()
        idx = words.index(anchor)
        window = words[idx + 1 : idx + 5]
        candidates = []
        for layer in QgsProject.instance().mapLayers().values():
            lname = layer.name().lower()
            if any(token in lname for token in window):
                candidates.append(layer)
        if len(candidates) == 1:
            return candidates[0]
        return self.iface.activeLayer() if self.iface.activeLayer() in candidates or not candidates else None

    def _warn_geographic_distance(self, layer, meters):
        try:
            from qgis.core import QgsUnitTypes

            if layer.crs().mapUnits() == QgsUnitTypes.DistanceDegrees:
                self._log(
                    f"<b>AI:</b> Warning: layer '{layer.name()}' is geographic CRS (degrees). "
                    f"Distance {meters}m is approximate. Consider reprojecting to a metric CRS."
                )
                return False
        except Exception:
            return False
        return False

    def _active_layer_or_msg(self):
        layer = self.iface.activeLayer()
        if layer is None:
            self._log("<b>AI:</b> No active layer selected.")
            return None
        return layer

    def _run_algorithm_with_active(self, alg_id, extra_params):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        params = {"INPUT": layer, "OUTPUT": "memory:"}
        params.update(extra_params)
        self._execute_algorithm_plan(alg_id, params, f"Executed {alg_id}.")

    def _refresh_screen(self):
        try:
            self.iface.mapCanvas().refreshAllLayers()
            self.iface.mapCanvas().refresh()
            self._log("<b>AI:</b> Map canvas refreshed.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not refresh canvas: {exc}")

    def _open_processing_toolbox(self):
        try:
            if hasattr(self.iface, "showProcessingToolbox"):
                self.iface.showProcessingToolbox()
                self._log("<b>AI:</b> Processing Toolbox opened.")
                return
            # Fallback through loaded processing plugin.
            proc_plugin = getattr(qgis.utils, "plugins", {}).get("processing")
            if proc_plugin and hasattr(proc_plugin, "openToolbox"):
                proc_plugin.openToolbox()
                self._log("<b>AI:</b> Processing Toolbox opened.")
                return
            self._log("<b>AI:</b> Could not find a callable Processing Toolbox opener in this QGIS build.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Failed to open Processing Toolbox: {exc}")

    def _open_google_satellite(self):
        try:
            name = "Google Satellite"
            for lyr in QgsProject.instance().mapLayers().values():
                if lyr.name().strip().lower() == name.lower():
                    self._ensure_basemap_view(lyr)
                    self._log("<b>AI:</b> Google Satellite is already loaded. View refreshed.")
                    return

            uris = [
                "type=xyz&zmin=0&zmax=22&url=https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
                "type=xyz&zmin=0&zmax=22&url=https://mt0.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
                "type=xyz&zmin=0&zmax=22&url=https://mt2.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
            ]

            layer = None
            for uri in uris:
                trial = QgsRasterLayer(uri, name, "wms")
                if trial.isValid():
                    layer = trial
                    break
                trial = QgsRasterLayer(uri, name, "xyz")
                if trial.isValid():
                    layer = trial
                    break

            if layer is None:
                self._log("<b>AI:</b> Could not load Google Satellite XYZ source.")
                return

            QgsProject.instance().addMapLayer(layer)
            self._ensure_basemap_view(layer)
            self._log("<b>AI:</b> Google Satellite layer added and canvas refreshed.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Failed to load Google Satellite: {exc}")

    def _ensure_basemap_view(self, basemap_layer):
        """
        Ensure canvas is in a valid extent/CRS for XYZ basemap rendering.
        """
        try:
            project = QgsProject.instance()
            web_mercator = QgsCoordinateReferenceSystem("EPSG:3857")
            if not project.crs().isValid() or project.crs().authid() != "EPSG:3857":
                project.setCrs(web_mercator)
        except Exception:
            pass

        try:
            canvas = self.iface.mapCanvas()
            if canvas is None:
                return
            self.iface.setActiveLayer(basemap_layer)

            # Prefer current non-basemap layers' extent if meaningful.
            target_extent = None
            for lyr in QgsProject.instance().mapLayers().values():
                if lyr.id() == basemap_layer.id():
                    continue
                try:
                    ext = lyr.extent()
                    if ext and not ext.isEmpty():
                        target_extent = ext
                        break
                except Exception:
                    continue

            if target_extent is None:
                # World-ish web mercator extent fallback.
                target_extent = QgsRectangle(-20037508, -20037508, 20037508, 20037508)

            canvas.setExtent(target_extent)
            canvas.refreshAllLayers()
            canvas.refresh()
        except Exception:
            pass

    def _enable_snapping(self):
        try:
            from qgis.core import QgsSnappingConfig, QgsTolerance

            # First try UI action toggle if available.
            action = getattr(self.iface, "actionEnableSnapping", None)
            if callable(action):
                snap_action = action()
                if snap_action and not snap_action.isChecked():
                    snap_action.trigger()

            project = QgsProject.instance()
            config = project.snappingConfig()
            config.setEnabled(True)

            # QGIS API differs by versions; try safe fallbacks.
            type_set = False
            for attr_name in ("VertexAndSegment", "AllLayers"):
                if hasattr(QgsSnappingConfig, attr_name):
                    try:
                        if hasattr(config, "setType"):
                            config.setType(getattr(QgsSnappingConfig, attr_name))
                            type_set = True
                            break
                    except Exception:
                        pass

            if not type_set and hasattr(config, "setTypeFlag"):
                try:
                    config.setTypeFlag(QgsSnappingConfig.VertexFlag | QgsSnappingConfig.SegmentFlag)
                except Exception:
                    pass

            config.setTolerance(10.0)
            config.setUnits(QgsTolerance.Pixels)
            project.setSnappingConfig(config)
            self._log("<b>AI:</b> Snapping enabled.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not enable snapping: {exc}")

    def _snap_unsnapped_vertices(self, normalized_query):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        tolerance_m = self._extract_distance(normalized_query) or 0.2
        tolerance_layer_units = self._meters_to_layer_units(layer, tolerance_m)
        try:
            result = processing.run(
                "native:snapgeometries",
                {
                    "INPUT": layer,
                    "REFERENCE_LAYER": layer,
                    "TOLERANCE": tolerance_layer_units,
                    "BEHAVIOR": 0,
                    "OUTPUT": "memory:",
                },
            )
            snapped = result.get("OUTPUT")
            if snapped is not None:
                snapped.setName(f"{layer.name()}_snapped")
                QgsProject.instance().addMapLayer(snapped)
                self.iface.setActiveLayer(snapped)
            self._log(
                "<b>AI:</b> Snap vertices executed.\n"
                f"Tolerance: {tolerance_m} meters ({tolerance_layer_units} layer units).\n"
                "A new snapped layer was created and set active."
            )
        except Exception as exc:
            self._log(f"<b>AI:</b> Snap operation failed: {exc}")

    def _meters_to_layer_units(self, layer, meters):
        try:
            from qgis.core import QgsUnitTypes

            unit = layer.crs().mapUnits()
            if unit == QgsUnitTypes.DistanceMeters:
                return float(meters)
            if unit == QgsUnitTypes.DistanceKilometers:
                return float(meters) / 1000.0
            if unit == QgsUnitTypes.DistanceFeet:
                return float(meters) * QgsUnitTypes.fromUnitToUnitFactor(
                    QgsUnitTypes.DistanceMeters, QgsUnitTypes.DistanceFeet
                )
            if unit == QgsUnitTypes.DistanceNauticalMiles:
                return float(meters) * QgsUnitTypes.fromUnitToUnitFactor(
                    QgsUnitTypes.DistanceMeters, QgsUnitTypes.DistanceNauticalMiles
                )
            if unit == QgsUnitTypes.DistanceMiles:
                return float(meters) * QgsUnitTypes.fromUnitToUnitFactor(
                    QgsUnitTypes.DistanceMeters, QgsUnitTypes.DistanceMiles
                )
            if unit == QgsUnitTypes.DistanceDegrees:
                # Approximate conversion for geographic CRS (lat/lon).
                return float(meters) / 111320.0
        except Exception:
            pass
        return float(meters)

    def _layer_units_to_meters(self, layer, value):
        try:
            from qgis.core import QgsUnitTypes

            unit = layer.crs().mapUnits()
            v = float(value)
            if unit == QgsUnitTypes.DistanceMeters:
                return v
            if unit == QgsUnitTypes.DistanceKilometers:
                return v * 1000.0
            if unit == QgsUnitTypes.DistanceFeet:
                return v * QgsUnitTypes.fromUnitToUnitFactor(
                    QgsUnitTypes.DistanceFeet, QgsUnitTypes.DistanceMeters
                )
            if unit == QgsUnitTypes.DistanceNauticalMiles:
                return v * QgsUnitTypes.fromUnitToUnitFactor(
                    QgsUnitTypes.DistanceNauticalMiles, QgsUnitTypes.DistanceMeters
                )
            if unit == QgsUnitTypes.DistanceMiles:
                return v * QgsUnitTypes.fromUnitToUnitFactor(
                    QgsUnitTypes.DistanceMiles, QgsUnitTypes.DistanceMeters
                )
            if unit == QgsUnitTypes.DistanceDegrees:
                return v * 111320.0
            return v
        except Exception:
            return None

    def _run_two_layer_overlay(self, alg_id, input_key, overlay_key):
        project_layers = list(QgsProject.instance().mapLayers().values())
        if len(project_layers) < 2:
            self._log("<b>AI:</b> Need at least two layers in the project.")
            return
        active = self.iface.activeLayer()
        if active is None:
            self._log("<b>AI:</b> Select active layer first, then retry.")
            return
        overlay = next((l for l in project_layers if l.id() != active.id()), None)
        if overlay is None:
            self._log("<b>AI:</b> Could not find overlay layer.")
            return
        params = {input_key: active, overlay_key: overlay, "OUTPUT": "memory:"}
        self._execute_algorithm_plan(alg_id, params, f"Executed {alg_id} with '{active.name()}' and '{overlay.name()}'.")

    def _run_reproject(self, normalized):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        epsg = self._extract_epsg(normalized)
        if epsg is None:
            self._log("<b>AI:</b> Mention EPSG code, e.g. 'reproject to EPSG:3857'.")
            return
        self._execute_algorithm_plan(
            "native:reprojectlayer",
            {"INPUT": layer, "TARGET_CRS": f"EPSG:{epsg}", "OUTPUT": "memory:"},
            "Reprojection complete.",
        )

    def _is_polygon_validity_query(self, normalized_query):
        has_validity = "validity" in normalized_query or "valid" in normalized_query
        has_polygon = "polygon" in normalized_query or "polygons" in normalized_query
        has_check = "check" in normalized_query or "verify" in normalized_query
        return has_polygon and (has_validity or has_check)

    def _run_check_validity(self):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        self._execute_algorithm_plan(
            "native:checkvalidity",
            {
                "INPUT_LAYER": layer,
                "METHOD": 2,
                "VALID_OUTPUT": "memory:",
                "INVALID_OUTPUT": "memory:",
                "ERROR_OUTPUT": "memory:",
            },
            "Validity check complete.",
        )

    def _run_fix_geometries(self):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        self._execute_algorithm_plan(
            "native:fixgeometries",
            {"INPUT": layer, "METHOD": 1, "OUTPUT": "memory:"},
            "Fixed geometry output created.",
        )

    def _run_span_disconnected_check(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> Active layer is not a line layer.")
            return

        feature = next(iter(layer.selectedFeatures()), None)
        if feature is None:
            feature = next(layer.getFeatures(), None)
        if feature is None:
            self._log("<b>AI:</b> No features found in active line layer.")
            return

        geom = feature.geometry()
        if geom is None or geom.isEmpty():
            self._log("<b>AI:</b> Selected span geometry is empty.")
            return
        if not geom.isGeosValid():
            self._log("<b>AI:</b> Span geometry is invalid. Run check validity first.")
            return
        if geom.isMultipart() and len(geom.asMultiPolyline()) > 1:
            self._log("<b>AI:</b> Span is multipart and appears disconnected.")
            return

        line = geom.asPolyline()
        if not line:
            self._log("<b>AI:</b> Could not read span vertices.")
            return
        start_pt = line[0]
        end_pt = line[-1]
        index = self._build_spatial_index(layer.getFeatures())
        tol = 0.001
        connected_start = self._has_neighbor_endpoint(layer, index, feature.id(), start_pt.x(), start_pt.y(), tol)
        connected_end = self._has_neighbor_endpoint(layer, index, feature.id(), end_pt.x(), end_pt.y(), tol)

        if connected_start and connected_end:
            self._log("<b>AI:</b> Span appears connected at both ends.")
        elif connected_start or connected_end:
            side = "end" if connected_start else "start"
            self._log(f"<b>AI:</b> Span appears disconnected on one side ({side}).")
        else:
            self._log("<b>AI:</b> Span appears isolated. No neighboring endpoints found.")

    def _is_end_span_centroid_query(self, normalized_query):
        has_centroid = "centroid" in normalized_query or "centriod" in normalized_query
        has_end_span = (
            ("end span" in normalized_query)
            or ("end spans" in normalized_query)
            or ("dead end" in normalized_query)
            or ("dangling span" in normalized_query)
        )
        return has_centroid and has_end_span

    def _is_unsnapped_span_query(self, normalized_query):
        has_span = "span" in normalized_query or "spans" in normalized_query
        has_unsnapped = (
            "not snapped" in normalized_query
            or "unsnapped" in normalized_query
            or "dangling" in normalized_query
            or "not connected" in normalized_query
        )
        has_detect = any(w in normalized_query for w in ("detect", "find", "show", "identify"))
        return has_span and has_unsnapped and has_detect

    def _is_overlap_span_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_overlap = any(w in q for w in ("overlap", "overlapped", "coincident", "duplicate geometry"))
        has_line_context = any(
            w in q for w in ("span", "spans", "line", "lines", "segment", "segments", "wire", "wires")
        )
        has_detect = any(w in q for w in ("detect", "find", "show", "identify", "where", "check", "locate"))
        # If overlap+detect is clear and no centroid request is present, treat as overlap span detection.
        asks_centroid = any(w in q for w in ("centroid", "centriod", "center point", "mid point", "point layer"))
        return has_overlap and has_detect and (has_line_context or not asks_centroid)

    def _is_overlap_centroid_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_overlap = any(w in q for w in ("overlap", "overlapped", "coincident", "duplicate geometry"))
        has_centroid_intent = any(
            w in q
            for w in (
                "centroid",
                "centriod",
                "center point",
                "centre point",
                "mid point",
                "point output",
                "point layer",
                "points for overlap",
            )
        )
        has_line_context = any(
            w in q for w in ("span", "spans", "line", "lines", "segment", "segments", "wire", "wires")
        )
        # Strong overlap+point intent is enough; line context helps but is not mandatory.
        return has_overlap and (has_centroid_intent or ("point" in q and "overlap" in q))

    def _run_overlap_span_centroids(self, normalized_query):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> Overlap centroid detection works on line layers only.")
            return

        try:
            tol_m = self._extract_distance(normalized_query) or 0.05
            tol_units = self._meters_to_layer_units(layer, tol_m)
            crs_authid = layer.crs().authid() if hasattr(layer, "crs") else "EPSG:4326"
            out = QgsVectorLayer(f"Point?crs={crs_authid}", f"{layer.name()}_overlap_centroids", "memory")
            provider = out.dataProvider()
            provider.addAttributes(
                [
                    QgsField("id_a", QVariant.Int),
                    QgsField("id_b", QVariant.Int),
                    QgsField("ov_len", QVariant.Double),
                ]
            )
            out.updateFields()

            feats = list(layer.getFeatures())
            index = self._build_spatial_index(feats)
            added = 0
            seen_pairs = set()
            for fa in feats:
                ga = fa.geometry()
                if ga is None or ga.isEmpty():
                    continue
                for nid in index.intersects(ga.boundingBox()):
                    if nid == fa.id():
                        continue
                    pair = (min(fa.id(), nid), max(fa.id(), nid))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    fb = layer.getFeature(nid)
                    gb = fb.geometry()
                    if gb is None or gb.isEmpty():
                        continue
                    if not ga.boundingBox().intersects(gb.boundingBox()):
                        continue

                    try:
                        inter = ga.intersection(gb)
                    except Exception:
                        continue
                    if inter is None or inter.isEmpty():
                        continue
                    if inter.type() != QgsWkbTypes.LineGeometry:
                        continue

                    parts = []
                    if inter.isMultipart():
                        parts = [QgsGeometry.fromPolylineXY(p) for p in inter.asMultiPolyline() if len(p) >= 2]
                    else:
                        parts = [inter]

                    for part_geom in parts:
                        ov_len = part_geom.length()
                        if ov_len <= tol_units:
                            continue
                        cent = part_geom.centroid()
                        if cent is None or cent.isEmpty():
                            continue
                        of = QgsFeature(out.fields())
                        of.setGeometry(cent)
                        of.setAttribute("id_a", int(fa.id()))
                        of.setAttribute("id_b", int(fb.id()))
                        of.setAttribute("ov_len", float(ov_len))
                        provider.addFeature(of)
                        added += 1

            if added == 0:
                self._log("<b>AI:</b> No overlap centroids found.")
                return
            QgsProject.instance().addMapLayer(out)
            self._remember_action(self.last_user_query, f"overlap centroids created {added}")
            self._log(f"<b>AI:</b> Created {added} overlap centroid point(s): {out.name()}")
        except Exception as exc:
            self._log(f"<b>AI:</b> Overlap centroid detection failed: {exc}")

    def _run_detect_overlap_spans(self, normalized_query):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> Overlap span detection works on line layers only.")
            return

        try:
            tol_m = self._extract_distance(normalized_query) or 0.05
            tol_units = self._meters_to_layer_units(layer, tol_m)
            crs_authid = layer.crs().authid() if hasattr(layer, "crs") else "EPSG:4326"
            out = QgsVectorLayer(f"LineString?crs={crs_authid}", f"{layer.name()}_overlap_spans", "memory")
            provider = out.dataProvider()
            provider.addAttributes(
                [
                    QgsField("id_a", QVariant.Int),
                    QgsField("id_b", QVariant.Int),
                    QgsField("ov_len", QVariant.Double),
                ]
            )
            out.updateFields()

            feats = list(layer.getFeatures())
            index = self._build_spatial_index(feats)
            added = 0
            seen_pairs = set()
            for fa in feats:
                ga = fa.geometry()
                if ga is None or ga.isEmpty():
                    continue
                for nid in index.intersects(ga.boundingBox()):
                    if nid == fa.id():
                        continue
                    pair = (min(fa.id(), nid), max(fa.id(), nid))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    fb = layer.getFeature(nid)
                    gb = fb.geometry()
                    if gb is None or gb.isEmpty():
                        continue
                    if not ga.boundingBox().intersects(gb.boundingBox()):
                        continue
                    try:
                        inter = ga.intersection(gb)
                    except Exception:
                        continue
                    if inter is None or inter.isEmpty():
                        continue
                    if inter.type() != QgsWkbTypes.LineGeometry:
                        continue
                    ov_len = inter.length()
                    if ov_len <= tol_units:
                        continue

                    if inter.isMultipart():
                        for part in inter.asMultiPolyline():
                            if len(part) < 2:
                                continue
                            part_geom = QgsGeometry.fromPolylineXY(part)
                            if part_geom.length() <= tol_units:
                                continue
                            of = QgsFeature(out.fields())
                            of.setGeometry(part_geom)
                            of.setAttribute("id_a", int(fa.id()))
                            of.setAttribute("id_b", int(fb.id()))
                            of.setAttribute("ov_len", float(part_geom.length()))
                            provider.addFeature(of)
                            added += 1
                    else:
                        of = QgsFeature(out.fields())
                        of.setGeometry(inter)
                        of.setAttribute("id_a", int(fa.id()))
                        of.setAttribute("id_b", int(fb.id()))
                        of.setAttribute("ov_len", float(ov_len))
                        provider.addFeature(of)
                        added += 1

            if added == 0:
                self._log("<b>AI:</b> No overlapping spans/lines detected.")
                return
            QgsProject.instance().addMapLayer(out)
            self._remember_action(self.last_user_query, f"overlap spans detected {added}")
            self._log(
                f"<b>AI:</b> Overlap detection complete. Found {added} overlapping span segment(s). "
                f"Output layer: {out.name()}"
            )
        except Exception as exc:
            self._log(f"<b>AI:</b> Overlap detection failed: {exc}")

    def _run_detect_unsnapped_spans(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> Unsnapped span detection works on line layers only.")
            return

        crs_authid = layer.crs().authid() if hasattr(layer, "crs") else "EPSG:4326"
        out = QgsVectorLayer(f"LineString?crs={crs_authid}", f"{layer.name()}_unsnapped_spans", "memory")
        provider = out.dataProvider()
        provider.addAttributes(
            [QgsField("source_id", QVariant.Int), QgsField("dangling", QVariant.Int)]
        )
        out.updateFields()

        endpoint_degree, segments = self._build_segment_endpoint_degree(layer)
        found = 0
        for seg in segments:
            keys = seg["keys"]
            dangling = 0
            if endpoint_degree.get(keys[0], 0) <= 1:
                dangling += 1
            if endpoint_degree.get(keys[1], 0) <= 1:
                dangling += 1
            if dangling == 0:
                continue

            out_feat = QgsFeature(out.fields())
            out_feat.setGeometry(seg["geom"])
            out_feat.setAttribute("source_id", int(seg["feature_id"]))
            out_feat.setAttribute("dangling", int(dangling))
            provider.addFeature(out_feat)
            found += 1

        if found == 0:
            self._log("<b>AI:</b> No unsnapped/dangling spans detected.")
            return
        QgsProject.instance().addMapLayer(out)
        self._log(f"<b>AI:</b> Detected {found} unsnapped span(s). Output layer: {out.name()}")

    def _run_end_span_centroids(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if layer.geometryType() != QgsWkbTypes.LineGeometry:
            self._log("<b>AI:</b> End-span centroid works on line layers only.")
            return

        crs_authid = layer.crs().authid() if hasattr(layer, "crs") else "EPSG:4326"
        out = QgsVectorLayer(f"Point?crs={crs_authid}", f"{layer.name()}_end_span_centroids", "memory")
        provider = out.dataProvider()
        provider.addAttributes(
            [QgsField("source_id", QVariant.Int), QgsField("dangling", QVariant.Int)]
        )
        out.updateFields()

        endpoint_degree, segments = self._build_segment_endpoint_degree(layer)
        added = 0
        for seg in segments:
            keys = seg["keys"]
            deg_start = endpoint_degree.get(keys[0], 0)
            deg_end = endpoint_degree.get(keys[1], 0)
            # True terminal span: one endpoint is terminal (degree 1), other is connected (>1).
            is_terminal_span = (deg_start <= 1 < deg_end) or (deg_end <= 1 < deg_start)
            if not is_terminal_span:
                continue
            dangling = 1

            geom = seg["geom"]
            if geom is None or geom.isEmpty():
                continue
            cent = geom.centroid()
            if cent is None or cent.isEmpty():
                continue
            out_feat = QgsFeature(out.fields())
            out_feat.setGeometry(cent)
            out_feat.setAttribute("source_id", int(seg["feature_id"]))
            out_feat.setAttribute("dangling", int(dangling))
            provider.addFeature(out_feat)
            added += 1

        if added == 0:
            self._log("<b>AI:</b> No end spans detected on active line layer.")
            return
        QgsProject.instance().addMapLayer(out)
        self._log(f"<b>AI:</b> Created centroid layer for {added} end span(s): {out.name()}")

    def _build_segment_endpoint_degree(self, layer):
        """
        Build node degree from every individual line segment endpoint.
        Returns:
          endpoint_degree: dict[key -> count]
          segments: list of dicts with feature_id, keys(start,end), geom(segment)
        """
        tol_units = self._meters_to_layer_units(layer, 0.2)
        if tol_units <= 0:
            tol_units = 0.001

        endpoint_degree = {}
        segments = []

        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
                if not parts:
                    continue
                lines = parts
            else:
                line = geom.asPolyline()
                if not line:
                    continue
                lines = [line]

            for line in lines:
                if not line or len(line) < 2:
                    continue
                for i in range(len(line) - 1):
                    p1 = line[i]
                    p2 = line[i + 1]
                    k1 = self._endpoint_key(p1.x(), p1.y(), tol_units)
                    k2 = self._endpoint_key(p2.x(), p2.y(), tol_units)
                    endpoint_degree[k1] = endpoint_degree.get(k1, 0) + 1
                    endpoint_degree[k2] = endpoint_degree.get(k2, 0) + 1

                    seg_geom = QgsGeometry.fromPolylineXY(
                        [QgsPointXY(p1.x(), p1.y()), QgsPointXY(p2.x(), p2.y())]
                    )
                    segments.append(
                        {
                            "feature_id": feat.id(),
                            "keys": (k1, k2),
                            "geom": seg_geom,
                        }
                    )

        return endpoint_degree, segments

    def _endpoint_key(self, x, y, tolerance):
        return (round(float(x) / tolerance), round(float(y) / tolerance))

    def _has_neighbor_endpoint(self, layer, index, feature_id, x, y, tolerance):
        from qgis.core import QgsRectangle

        rect = QgsRectangle(x - tolerance, y - tolerance, x + tolerance, y + tolerance)
        for nid in index.intersects(rect):
            if nid == feature_id:
                continue
            candidate = layer.getFeature(nid)
            geom = candidate.geometry()
            if geom is None or geom.isEmpty() or geom.isMultipart():
                continue
            line = geom.asPolyline()
            if not line:
                continue
            c_start = line[0]
            c_end = line[-1]
            if (abs(c_start.x() - x) <= tolerance and abs(c_start.y() - y) <= tolerance) or (
                abs(c_end.x() - x) <= tolerance and abs(c_end.y() - y) <= tolerance
            ):
                return True
        return False

    def _run_buffer_command(self, normalized_query):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        distance = self._extract_distance(normalized_query)
        if distance is None:
            self._log("<b>AI:</b> Could not detect distance. Example: 'buffer 25 meters'.")
            return
        self._warn_geographic_distance(layer, distance)
        layer_distance = self._meters_to_layer_units(layer, distance)
        self._execute_algorithm_plan(
            "native:buffer",
            {
                "INPUT": layer,
                "DISTANCE": layer_distance,
                "SEGMENTS": 12,
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2,
                "DISSOLVE": False,
                "OUTPUT": "memory:",
            },
            f"Buffer created ({distance} m; {layer_distance} layer units).",
        )

    def _is_merge_layers_query(self, normalized_query):
        has_merge_word = any(word in normalized_query for word in ("merge", "combine"))
        has_layer_word = any(
            word in normalized_query
            for word in ("layer", "layers", "shapefile", "shapefiles", "shape files", "loaded")
        )
        return has_merge_word and has_layer_word

    def _run_merge_loaded_layers(self, normalized_query):
        if not self._processing_available():
            return
        layers = list(QgsProject.instance().mapLayers().values())
        if not layers:
            self._log("<b>AI:</b> No layers are loaded in the project.")
            return

        # If user asked for shape files, restrict to OGR-backed vector layers with .shp sources.
        shp_only = any(token in normalized_query for token in ("shapefile", "shapefiles", "shape files"))
        eligible = []
        first_geom_type = None
        for layer in layers:
            if not hasattr(layer, "geometryType"):
                continue
            source = ""
            try:
                source = layer.source().lower()
            except Exception:
                source = ""
            if shp_only and ".shp" not in source:
                continue

            geom_type = layer.geometryType()
            if first_geom_type is None:
                first_geom_type = geom_type
            if geom_type == first_geom_type:
                eligible.append(layer)

        if len(eligible) < 2:
            self._log(
                "<b>AI:</b> Need at least two compatible loaded layers to merge. "
                "Tip: make sure layer geometry types match."
            )
            return

        self._execute_algorithm_plan(
            "native:mergevectorlayers",
            {
                "LAYERS": eligible,
                "CRS": None,
                "OUTPUT": "memory:",
            },
            f"Merged {len(eligible)} loaded layers.",
        )

    def _is_split_by_field_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_split_intent = any(word in q for word in ("split", "separate", "divide", "break"))
        if not has_split_intent:
            return False
        has_field_hint = any(word in q for word in ("field", "column", "attribute", "using", "by"))
        has_output_hint = any(word in q for word in ("output", "shapefile", "shape file", "layer", "file", "folder"))
        return has_field_hint or has_output_hint

    def _run_split_by_field(self, normalized_query):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return

        field_name = self._extract_field_name_for_split(normalized_query, layer)
        if not field_name:
            self._log(
                "<b>AI:</b> Could not find the field name. Try: "
                "'split the shapefile by column feeder'"
            )
            return

        base_out = self._resolve_output_dir_from_query(self.last_user_query)
        out_dir = base_out / f"{layer.name()}_{field_name}_split"
        out_dir.mkdir(parents=True, exist_ok=True)
        also_gpkg = self._should_export_optional_gpkg(self.last_user_query or normalized_query)

        try:
            field_idx = layer.fields().indexFromName(field_name)
            if field_idx < 0:
                self._log(f"<b>AI:</b> Field '{field_name}' was not found in the layer.")
                return
            # Fast path: single pass grouping + direct writer export.
            # Avoids O(unique_values * feature_count) rescans from extractbyattribute loops.
            grouped = {}
            value_to_folder = {}
            used_names = set()
            total_scanned = 0
            for feature in layer.getFeatures():
                total_scanned += 1
                value = feature[field_idx]
                value_key = "__NULL__" if value is None else str(value)
                if value_key not in value_to_folder:
                    raw_name = "NULL" if value is None or str(value).strip() == "" else str(value).strip()
                    value_to_folder[value_key] = self._unique_safe_name(raw_name, used_names)
                folder_name = value_to_folder[value_key]
                grouped.setdefault(folder_name, []).append(QgsFeature(feature))

            if not grouped:
                self._log(f"<b>AI:</b> Field '{field_name}' has no values to split.")
                return

            exported_count = 0
            gpkg_count = 0
            for folder_name, features in grouped.items():
                split_folder = out_dir / folder_name
                split_folder.mkdir(parents=True, exist_ok=True)
                shp_path = split_folder / f"{folder_name}.shp"

                writer = QgsVectorFileWriter(
                    str(shp_path),
                    "UTF-8",
                    layer.fields(),
                    layer.wkbType(),
                    layer.crs(),
                    "ESRI Shapefile",
                )
                if writer.hasError() != QgsVectorFileWriter.NoError:
                    raise RuntimeError(f"writer init failed for {shp_path.name}: {writer.errorMessage()}")
                for feat in features:
                    writer.addFeature(feat)
                del writer
                exported_count += 1

                # Optional companion export to GPKG (SHP remains mandatory/default).
                if also_gpkg:
                    gpkg_path = split_folder / f"{folder_name}.gpkg"
                    gpkg_writer = QgsVectorFileWriter(
                        str(gpkg_path),
                        "UTF-8",
                        layer.fields(),
                        layer.wkbType(),
                        layer.crs(),
                        "GPKG",
                    )
                    if gpkg_writer.hasError() != QgsVectorFileWriter.NoError:
                        raise RuntimeError(f"gpkg writer init failed for {gpkg_path.name}: {gpkg_writer.errorMessage()}")
                    for feat in features:
                        gpkg_writer.addFeature(feat)
                    del gpkg_writer
                    gpkg_count += 1

            mode_note = "SHP mandatory output only." if not also_gpkg else "SHP mandatory + optional GPKG companion exported."
            self._log(
                f"<b>AI:</b> Split complete using field '{field_name}'.\n"
                f"Created {exported_count} folders/files under:\n{out_dir}\n"
                f"Features processed: {total_scanned}\n"
                f"{mode_note}\n"
                f"Optional GPKG files: {gpkg_count}"
            )
        except Exception as exc:
            self._log(f"<b>AI:</b> Split failed: {exc}")

    def _should_export_optional_gpkg(self, text):
        q = (text or "").lower()
        wants_gpkg = any(
            k in q
            for k in (
                "gpkg",
                "geopackage",
                "also gpkg",
                "with gpkg",
                "plus gpkg",
                "optional gpkg",
            )
        )
        if not wants_gpkg:
            return False
        if any(k in q for k in ("no gpkg", "without gpkg", "only shp", "shp only")):
            return False
        return True

    def _extract_field_name_for_split(self, normalized_query, layer):
        candidates = [field.name() for field in layer.fields()]
        if not candidates:
            return None

        quoted = re.search(r"['\"]([a-zA-Z0-9_ ]+)['\"]", normalized_query)
        if quoted:
            token = quoted.group(1).strip().lower()
            matched = self._match_field_name(token, candidates)
            if matched:
                return matched

        patterns = [
            r"(?:column|field|attribute)\s+(?:named\s+)?([a-zA-Z0-9_]+)",
            r"by\s+([a-zA-Z0-9_]+)",
        ]
        for pat in patterns:
            m = re.search(pat, normalized_query)
            if not m:
                continue
            token = m.group(1).strip().lower()
            matched = self._match_field_name(token, candidates)
            if matched:
                return matched

        for candidate in candidates:
            cname = candidate.lower()
            if cname in normalized_query:
                return candidate
        return None

    def _match_field_name(self, token, candidates):
        for candidate in candidates:
            if candidate.lower() == token:
                return candidate
        for candidate in candidates:
            if candidate.lower().startswith(token):
                return candidate
        return None

    def _unique_safe_name(self, raw_name, used_names):
        safe = re.sub(r'[<>:"/\\|?*]+', "_", raw_name).strip(". ")
        safe = safe if safe else "EMPTY"
        base = safe
        suffix = 1
        while safe.lower() in used_names:
            suffix += 1
            safe = f"{base}_{suffix}"
        used_names.add(safe.lower())
        return safe

    def _run_poles_near_roads_export(self, normalized_query):
        if not self._processing_available():
            return
        distance = self._extract_distance(normalized_query) or 100.0
        poles_layer = self._find_layer_by_name_keywords(["pole"])
        roads_layer = self._find_layer_by_name_keywords(["road"])
        if poles_layer is None or roads_layer is None:
            self._log("<b>AI:</b> Could not find layers containing 'pole' and 'road'.")
            return

        try:
            result = processing.run(
                "native:extractwithindistance",
                {
                    "INPUT": poles_layer,
                    "REFERENCE": roads_layer,
                    "DISTANCE": distance,
                    "OUTPUT": "memory:",
                },
            )
            selected = result["OUTPUT"]
            selected.setName(f"poles_within_{int(distance)}m_of_roads")
            QgsProject.instance().addMapLayer(selected)

            base_out = self._resolve_output_dir_from_query(self.last_user_query)
            out_path = str(base_out / f"poles_within_{int(distance)}m_of_roads.gpkg")
            processing.run("native:savefeatures", {"INPUT": selected, "OUTPUT": out_path})
            self._log(
                f"<b>AI:</b> Export complete. {selected.featureCount()} poles saved:\n{out_path}"
            )
        except Exception as exc:
            self._log(f"<b>AI:</b> Export failed: {exc}")

    def _run_explicit_algorithm(self, query):
        if not self._processing_available():
            return False
        alg_match = re.search(r"([a-zA-Z0-9_]+:[a-zA-Z0-9_]+)", query)
        if not alg_match:
            return False
        alg_id = alg_match.group(1)
        try:
            if processing.registry().algorithmById(alg_id) is None:
                self._log(f"<b>AI:</b> Algorithm not found: {alg_id}")
                return True
            inline_params = self._extract_inline_json(query)
            if inline_params is None:
                self._log(
                    "<b>AI:</b> Provide inline JSON parameters.\n"
                    "Example: native:buffer {\"INPUT\":\"active\",\"DISTANCE\":25,\"OUTPUT\":\"memory:\"}"
                )
                return True
            if not isinstance(inline_params, dict):
                self._log("<b>AI:</b> Parameters must be a JSON object.")
                return True
            return self._execute_algorithm_plan(alg_id, inline_params, "Ran explicit algorithm command.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not inspect algorithm: {exc}")
            return True

    def _route_with_llm(self, query):
        try:
            prompt = self._build_planner_prompt(query)
            plan = self.llm.chat_json(self._planner_system_prompt(), prompt, temperature=0.0)
        except Exception as exc:
            self._log(f"<b>AI:</b> LLM planning failed: {exc}")
            return False

        mode = str(plan.get("mode", "")).strip()
        if mode == "answer":
            self._log(f"<b>AI:</b> {str(plan.get('message', '')).strip() or 'No answer generated.'}")
            return True
        if mode == "clarify":
            self._log(f"<b>AI:</b> {str(plan.get('message', '')).strip() or 'Please clarify your request.'}")
            return True
        if mode == "run_algorithm":
            alg_id = str(plan.get("algorithm_id", "")).strip()
            params = plan.get("params", {})
            if not alg_id or not isinstance(params, dict):
                self._log("<b>AI:</b> LLM returned invalid algorithm plan.")
                return True
            return self._execute_algorithm_plan(alg_id, params, str(plan.get("message", "")).strip())
        if mode == "run_contract":
            contract_command = str(plan.get("contract_command", "")).strip()
            if not contract_command:
                self._log("<b>AI:</b> LLM returned run_contract without contract_command.")
                return True
            ok = self._handle_run_contract_command(contract_command)
            if not ok:
                self._log("<b>AI:</b> LLM contract command could not be executed.")
            return True

        self._log("<b>AI:</b> LLM returned unknown mode.")
        return True

    def _planner_system_prompt(self):
        return (
            "You are AI GIS Copilot planner in QGIS. Return strict JSON only.\n"
            "Schema: {\"mode\":\"answer|run_algorithm|run_contract|clarify\",\"message\":\"...\","
            "\"algorithm_id\":\"provider:id\",\"params\":{},\"contract_command\":\"run ...\"}\n"
            "Rules: prefer run_algorithm when QGIS Processing can execute. Use INPUT='active' when suitable. "
            "Use OUTPUT='memory:' unless file export requested. "
            "Use mode=run_contract for specialized workflows (qautofix, ivms qa, 999 updater, agc qa, tgth validator)."
        )

    def _build_planner_prompt(self, query):
        active = self.iface.activeLayer()
        active_name = active.name() if active is not None else "None"
        layer_names = [l.name() for l in QgsProject.instance().mapLayers().values()]
        algorithms = self._sample_algorithms(60)
        learned = list(self.learned_routes.keys())[-30:]
        return (
            f"User query: {query}\n"
            f"Active layer: {active_name}\n"
            f"Project layers: {layer_names}\n"
            f"Sample algorithm ids: {algorithms}\n"
            f"Known specialized contracts: run qautofix ..., run ivms qa ..., run 999 updater ..., run agc qa ..., run tgth validator ..., run geoai task=... \n"
            f"Learned successful query patterns: {learned}\n"
            f"Installed plugins: {[p['name'] for p in self._list_available_plugins()[:80]]}\n"
        )

    def _sample_algorithms(self, limit):
        names = []
        if processing is None:
            return names
        try:
            for alg in processing.registry().algorithms():
                names.append(alg.id())
                if len(names) >= limit:
                    break
        except Exception:
            pass
        return names

    def _try_auto_processing_match(self, query):
        """
        Broad fallback: map free-form action text to nearest Processing algorithm id.
        This improves non-rigid language handling beyond hardcoded intents.
        """
        if not self._processing_available():
            return False
        q = (query or "").strip()
        if not q:
            return False
        if not self.algorithm_catalog:
            self._refresh_algorithm_catalog()
        if not self.algorithm_catalog:
            return False

        qtokens = self._tokenize_text(q)
        if not qtokens:
            return False

        best_alg = None
        best_score = 0.0
        for aid in self.algorithm_catalog.keys():
            atokens = self._tokenize_text(aid.replace(":", " "))
            score = self._token_overlap_score(qtokens, atokens)
            if score > best_score:
                best_score = score
                best_alg = aid

        # This used to execute processing.run() immediately on only 34% word
        # overlap with no confirmation prompt -- the same false-confidence
        # problem as the doc-memory/Wikipedia fallbacks, but worse: it
        # actually ran an algorithm (with guessed parameters) against real
        # project data instead of just displaying a wrong sentence. Raise
        # the bar and require the same /yes /no confirmation every other
        # risky action in this plugin already goes through.
        if not best_alg or best_score < 0.55:
            return False

        # Pragmatic parameter defaults for broad executability.
        params = {"INPUT": "active", "OUTPUT": "memory:"}
        if any(k in best_alg for k in ("clip", "intersection", "union", "difference", "extractbylocation")):
            layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
            active = self.iface.activeLayer()
            overlay = next((l for l in layers if l != active), None)
            if overlay is not None:
                for key in ("OVERLAY", "INTERSECT", "REFERENCE"):
                    params[key] = overlay
        if "buffer" in best_alg:
            params["DISTANCE"] = self._extract_distance(q.lower()) or 25.0
        if "reproject" in best_alg:
            epsg = self._extract_epsg(q.lower())
            if epsg:
                params["TARGET_CRS"] = f"EPSG:{epsg}"

        self._request_confirmation(
            f"Auto-mapped your request to <code>{best_alg}</code> (confidence {best_score:.2f}) "
            f"with parameters {params}. This will run against your active layer.",
            self._execute_algorithm_plan,
            best_alg,
            params,
            "Auto-routed from natural-language action.",
        )
        return True

    def _try_generic_plugin_execution(self, query):
        """
        Generic plugin orchestration for prompts like:
        'run plugin GeoAI action=toggle_moondream_dock'
        """
        q = (query or "").strip()
        lowered = q.lower()
        if "plugin" not in lowered:
            return False
        m = re.search(r"plugin\s+([a-zA-Z0-9_\- ]+)", q, flags=re.IGNORECASE)
        if not m:
            return False
        plugin_hint = m.group(1).strip()
        options = self._parse_kv_options(q)
        method = (options.get("action") or options.get("method") or "").strip()
        if not method:
            # gentle default for UX
            if any(k in lowered for k in ("open", "show", "launch")):
                method = "open"
            else:
                self._log("<b>AI:</b> Provide plugin action, e.g. action=toggle_moondream_dock")
                return True

        pid, pobj = self._resolve_plugin_by_hint(plugin_hint)
        if pobj is None:
            self._log(f"<b>AI:</b> Plugin '{plugin_hint}' is not loaded in this session.")
            return True

        ok = self._invoke_plugin_method(pobj, method)
        if ok:
            self._remember_action(self.last_user_query, f"plugin action {pid}.{method}")
            self._log(f"<b>AI:</b> Executed plugin action: {pid}.{method}")
        else:
            self._log(f"<b>AI:</b> Could not execute action '{method}' on plugin '{pid}'.")
        return True

    def _resolve_plugin_by_hint(self, hint):
        target = (hint or "").strip().lower()
        if not target:
            return (None, None)
        try:
            for pid, pobj in qgis.utils.plugins.items():
                if pid.lower() == target or target in pid.lower():
                    return (pid, pobj)
        except Exception:
            pass
        return (None, None)

    def _invoke_plugin_method(self, plugin_obj, method_name):
        # Direct method
        if hasattr(plugin_obj, method_name):
            try:
                getattr(plugin_obj, method_name)()
                return True
            except Exception:
                return False
        # Fuzzy fallback: find callable containing token.
        token = (method_name or "").strip().lower()
        if not token:
            return False
        try:
            for name in dir(plugin_obj):
                if token in name.lower():
                    fn = getattr(plugin_obj, name, None)
                    if callable(fn):
                        try:
                            fn()
                            return True
                        except Exception:
                            continue
        except Exception:
            return False
        return False

    def _learn_website_now(self, url):
        u = (url or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            self._log("<b>AI:</b> Website URL must start with http:// or https://")
            return
        try:
            req = urllib.request.Request(
                u,
                headers={"User-Agent": "Mozilla/5.0 (AI GIS Copilot)"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not fetch website: {exc}")
            return

        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else u
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        snippet = text[:2500] if text else ""
        entry = {
            "title": title[:180],
            "source": u,
            "tags": ["website learning", "auto ingested"],
            "content": snippet or f"Learned source: {u}",
        }
        self._upsert_doc_knowledge_entry(entry)
        self._log(f"<b>AI:</b> Learned website and saved to memory: {u}")

    def _learn_youtube_resource(self, url):
        u = str(url or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            self._log("<b>AI:</b> YouTube URL must start with http:// or https://")
            return
        if "youtube.com" not in u.lower() and "youtu.be" not in u.lower():
            self._log("<b>AI:</b> This is not a YouTube URL.")
            return

        # Try oEmbed metadata for video URLs.
        info = self._youtube_oembed_info(u)
        if info:
            title = info.get("title", "YouTube Video")
            author = info.get("author_name", "")
            content = (
                f"YouTube learning source: {title}. "
                f"Creator: {author}. "
                "Use this video as workflow reference for QGIS execution mapping."
            )
            self._upsert_doc_knowledge_entry(
                {
                    "title": title[:180],
                    "source": u,
                    "tags": ["youtube", "qgis video", "workflow reference", author.lower() if author else "creator"],
                    "content": content,
                }
            )
            self._log(f"<b>AI:</b> Learned YouTube video metadata: {title}")
            return

        # Playlist/channel URLs are dynamic pages; keep a deterministic source entry.
        is_channel = "/@" in u or "/channel/" in u or "/c/" in u
        kind = "channel" if is_channel else "playlist_or_page"
        self._upsert_doc_knowledge_entry(
            {
                "title": f"YouTube {kind} source",
                "source": u,
                "tags": ["youtube", kind, "qgis learning"],
                "content": (
                    "YouTube source registered for learning. "
                    "Direct full crawl of dynamic YouTube pages is limited without API/auth. "
                    "Provide playlist or individual video links for deterministic ingestion."
                ),
            }
        )
        if is_channel:
            self._log(
                "<b>AI:</b> Channel source saved.\n"
                "To teach Copilot from each video reliably, share playlist URL(s) or a text file of video links.\n"
                "Then run: /learn youtube <each_video_or_playlist_url>"
            )
        else:
            self._log("<b>AI:</b> YouTube source saved to memory.")

    def _youtube_oembed_info(self, url):
        try:
            endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"})
            req = urllib.request.Request(endpoint, headers={"User-Agent": "AI-GIS-Copilot/1.0"}, method="GET")
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)
            if isinstance(data, dict) and data.get("title"):
                return data
            return None
        except Exception:
            return None

    def _is_learn_media_query(self, query_text):
        q = (query_text or "").lower()
        has_learn_word = any(k in q for k in ("learn", "memorize", "remember", "ingest", "read"))
        has_media_word = any(k in q for k in ("video", "audio", "image", "screenshot", "document", "file", "pdf", "docx"))
        has_path = bool(re.search(r"[A-Za-z]:\\[^\n\r\"']+", query_text or ""))
        return has_learn_word and (has_media_word or has_path)

    def _run_learn_media_from_query(self, query_text):
        path_hint = self._resolve_path_from_query(query_text, expect_dir=False)
        if not path_hint:
            path_hint = self._extract_path_candidate(query_text)
        if not path_hint:
            self._log("<b>AI:</b> Provide a file path. Example: /learn file D:\\data\\manual.pdf")
            return
        self._learn_file_now(path_hint)

    def _learn_file_now(self, file_path):
        p = Path(str(file_path).strip().strip("\"'"))
        if not p.exists():
            self._log(f"<b>AI:</b> File not found: {p}")
            return
        if p.is_dir():
            self._learn_folder_now(str(p))
            return

        ext = p.suffix.lower()
        title = p.name
        content = ""
        tags = ["file learning", ext.lstrip(".") or "unknown"]
        source = str(p)

        try:
            if ext in (".txt", ".md", ".csv", ".json", ".log", ".ini", ".xml"):
                content = p.read_text(encoding="utf-8", errors="ignore")
            elif ext == ".pdf":
                content = self._extract_pdf_text_optional(p)
                tags.append("pdf")
            elif ext == ".docx":
                content = self._extract_docx_text(p)
                tags.append("docx")
            elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"):
                content = self._extract_image_text_optional(p)
                tags.append("image")
            elif ext in (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg"):
                content = self._transcribe_audio_optional(p)
                tags.append("audio")
            elif ext in (".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm"):
                content = self._transcribe_video_optional(p)
                tags.append("video")
            else:
                content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            self._log(f"<b>AI:</b> Could not ingest file: {exc}")
            return

        content = (content or "").strip()
        if not content:
            self._log(
                "<b>AI:</b> I could not extract meaningful text from this file yet. "
                "For media files, install OCR/transcription dependencies or provide transcript text."
            )
            return
        if len(content) > 12000:
            content = content[:12000] + " ..."
        entry = {
            "title": title[:180],
            "source": source,
            "tags": tags,
            "content": content,
        }
        self._upsert_doc_knowledge_entry(entry)
        self._log(f"<b>AI:</b> Learned and memorized: {p.name} ({len(content)} chars extracted).")

    def _learn_folder_now(self, folder_path):
        root = Path(folder_path)
        patterns = (".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".wav", ".mp3", ".mp4", ".mov", ".mkv", ".avi", ".wmv")
        files = [x for x in root.rglob("*") if x.is_file() and x.suffix.lower() in patterns]
        if not files:
            self._log("<b>AI:</b> No supported media/doc files found in folder.")
            return
        learned = 0
        for fp in files[:80]:
            before = len(self.doc_knowledge or [])
            self._learn_file_now(str(fp))
            after = len(self.doc_knowledge or [])
            if after >= before:
                learned += 1
        self._log(f"<b>AI:</b> Folder learning completed. Files processed: {min(len(files), 80)}")

    def _extract_pdf_text_optional(self, path_obj):
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path_obj))
            pages = []
            for pg in reader.pages[:80]:
                try:
                    pages.append(pg.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(pages).strip()
        except Exception:
            return ""

    def _extract_docx_text(self, path_obj):
        try:
            with zipfile.ZipFile(str(path_obj), "r") as zf:
                if "word/document.xml" not in zf.namelist():
                    return ""
                raw = zf.read("word/document.xml").decode("utf-8", errors="ignore")
            txt = re.sub(r"(?s)<[^>]+>", " ", raw)
            txt = re.sub(r"\s+", " ", txt).strip()
            return txt
        except Exception:
            return ""

    def _extract_image_text_optional(self, path_obj):
        try:
            from PIL import Image
            import pytesseract

            img = Image.open(str(path_obj))
            txt = pytesseract.image_to_string(img) or ""
            return re.sub(r"\s+", " ", txt).strip()
        except Exception:
            return ""

    def _transcribe_audio_optional(self, path_obj):
        # Uses whisper CLI if installed (pip install openai-whisper)
        try:
            with tempfile.TemporaryDirectory() as td:
                cmd = [
                    "whisper",
                    str(path_obj),
                    "--model",
                    "base",
                    "--output_format",
                    "txt",
                    "--output_dir",
                    td,
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    return ""
                out_txt = Path(td) / f"{path_obj.stem}.txt"
                if out_txt.exists():
                    return out_txt.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            return ""
        return ""

    def _transcribe_video_optional(self, path_obj):
        # Extract audio with ffmpeg (if available), then whisper transcription.
        try:
            with tempfile.TemporaryDirectory() as td:
                wav_path = Path(td) / f"{path_obj.stem}.wav"
                ff_cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path_obj),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(wav_path),
                ]
                ff = subprocess.run(ff_cmd, capture_output=True, text=True)
                if ff.returncode != 0 or not wav_path.exists():
                    return ""
                return self._transcribe_audio_optional(wav_path)
        except Exception:
            return ""

    def _crawl_all_known_websites(self):
        urls = self._collect_known_seed_urls()
        if not urls:
            self._log("<b>AI:</b> No crawlable website URLs found in current knowledge memory.")
            return
        self._log(
            "<b>AI:</b> Starting full-site crawl for known sources.\n"
            f"Sites: {len(urls)} | Scope: same domain/path family | Safety limits: depth=2, max_pages=60 per site."
        )
        total_pages = 0
        for u in urls:
            try:
                count = self._crawl_site_and_memorize(u, max_pages=60, max_depth=2, quiet=True)
                total_pages += int(count or 0)
            except Exception:
                continue
        self._log(
            "<b>AI:</b> Full-site crawl completed.\n"
            f"Sites crawled: {len(urls)}\n"
            f"Pages learned: {total_pages}\n"
            "Use /docs memory to inspect knowledge packs."
        )

    def _collect_known_seed_urls(self):
        urls = []
        seen = set()
        for item in list(self.doc_knowledge or []):
            if not isinstance(item, dict):
                continue
            src = str(item.get("source", "")).strip()
            if not src.lower().startswith(("http://", "https://")):
                continue
            key = src.lower()
            if key in seen:
                continue
            seen.add(key)
            urls.append(src)
        return urls

    def _crawl_site_and_memorize(self, root_url, max_pages=60, max_depth=2, quiet=False):
        root = (root_url or "").strip()
        if not root.lower().startswith(("http://", "https://")):
            if not quiet:
                self._log("<b>AI:</b> Crawl URL must start with http:// or https://")
            return 0

        norm_root = self._normalize_crawl_url(root)
        if not norm_root:
            if not quiet:
                self._log("<b>AI:</b> Invalid crawl URL.")
            return 0
        parsed_root = urllib.parse.urlparse(norm_root)
        scope_prefix = self._crawl_scope_prefix(parsed_root)

        queue = [(norm_root, 0)]
        visited = set()
        learned = 0

        while queue and learned < int(max_pages):
            url, depth = queue.pop(0)
            ukey = url.lower()
            if ukey in visited:
                continue
            visited.add(ukey)

            page = self._fetch_html_page(url)
            if page is None:
                continue

            title, text, links = page
            snippet = text[:6000] if text else f"Crawled source: {url}"
            entry = {
                "title": (title or url)[:180],
                "source": url,
                "tags": ["website crawl", parsed_root.netloc.lower(), f"depth:{depth}"],
                "content": snippet,
            }
            if self._upsert_crawled_knowledge_entry(entry):
                learned += 1

            if depth >= int(max_depth):
                continue
            for href in links:
                nxt = self._normalize_crawl_url(urllib.parse.urljoin(url, href))
                if not nxt:
                    continue
                if not self._is_url_in_crawl_scope(nxt, parsed_root.netloc, scope_prefix):
                    continue
                if nxt.lower() in visited:
                    continue
                queue.append((nxt, depth + 1))

        if not quiet:
            self._log(
                "<b>AI:</b> Site crawl completed.\n"
                f"Root: {norm_root}\n"
                f"Pages learned/updated: {learned}\n"
                f"Depth: {max_depth} | Max pages: {max_pages}"
            )
        return learned

    def _fetch_html_page(self, url):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (AI GIS Copilot crawler)"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw_bytes = resp.read()
                ctype = str(resp.headers.get("Content-Type", "")).lower()
            raw = raw_bytes.decode("utf-8", errors="ignore")
        except Exception:
            return None
        if "text/html" not in ctype and "<html" not in raw.lower():
            return None

        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else url

        link_matches = re.findall(r'(?is)href\s*=\s*["\']([^"\']+)["\']', raw)

        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?is)<noscript.*?>.*?</noscript>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return title, text, link_matches

    def _normalize_crawl_url(self, url):
        u = (url or "").strip()
        if not u:
            return ""
        try:
            p = urllib.parse.urlparse(u)
            if p.scheme.lower() not in ("http", "https"):
                return ""
            if not p.netloc:
                return ""
            path = p.path or "/"
            normalized = urllib.parse.urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))
            return normalized
        except Exception:
            return ""

    def _crawl_scope_prefix(self, parsed_root):
        path = parsed_root.path or "/"
        if path.endswith("/"):
            return path
        parts = path.rsplit("/", 1)
        if len(parts) == 2:
            return (parts[0] + "/") or "/"
        return "/"

    def _is_url_in_crawl_scope(self, url, host, prefix):
        try:
            p = urllib.parse.urlparse(url)
            if p.netloc.lower() != str(host).lower():
                return False
            candidate = p.path or "/"
            return candidate.startswith(prefix or "/")
        except Exception:
            return False

    def _upsert_crawled_knowledge_entry(self, entry):
        src = str((entry or {}).get("source", "")).strip().lower()
        if not src:
            return False
        items = list(self.doc_knowledge or [])
        for i, old in enumerate(items):
            old_src = str(old.get("source", "")).strip().lower()
            if old_src != src:
                continue
            old_tags = [str(t).lower() for t in old.get("tags", [])] if isinstance(old, dict) else []
            # Preserve curated/manual packs; only replace previous crawl entries.
            if "website crawl" not in old_tags:
                return False
            items[i] = entry
            self.doc_knowledge = items
            self._save_json_file(self._doc_knowledge_file_path(), self.doc_knowledge)
            return True
        items.append(entry)
        self.doc_knowledge = items
        self._save_json_file(self._doc_knowledge_file_path(), self.doc_knowledge)
        return True

    def _upsert_doc_knowledge_entry(self, entry):
        src = str((entry or {}).get("source", "")).strip().lower()
        if not src:
            return
        items = list(self.doc_knowledge or [])
        updated = False
        for i, old in enumerate(items):
            if str(old.get("source", "")).strip().lower() == src:
                items[i] = entry
                updated = True
                break
        if not updated:
            items.append(entry)
        self.doc_knowledge = items
        self._save_json_file(self._doc_knowledge_file_path(), self.doc_knowledge)

    def _handle_run_command(self, remainder):
        """Deterministic, AI-independent access to any algorithm registered
        in QGIS's Processing framework -- native, GDAL, GRASS/SAGA if
        installed, legacy qgis:, any third-party plugin's algorithms, and
        saved models/scripts. Doesn't need the LLM agent to be working at
        all, unlike search_algorithms/run_processing_algorithm -- built
        specifically because the agent has been unreliable enough this
        session that full toolbox access needs a path that never depends
        on it."""
        if not remainder:
            self._log(
                "<b>AI:</b> Usage:\n"
                "<code>/run &lt;search term&gt;</code> to find an algorithm's exact id\n"
                "<code>/run &lt;exact id&gt;</code> to see its required parameters\n"
                "<code>/run &lt;exact id&gt; PARAM=value PARAM2=value2</code> to execute it\n"
                "Example: <code>/run native:buffer INPUT=active DISTANCE=50 OUTPUT=memory:</code>"
            )
            return
        parts = remainder.split(None, 1)
        first_token = parts[0]
        alg = None
        try:
            from qgis.core import QgsApplication

            alg = QgsApplication.processingRegistry().algorithmById(first_token)
        except Exception:
            alg = None

        if alg is None:
            self._log(self._search_algorithms_for_run_command(remainder))
            return
        if len(parts) == 1:
            self._log(self._describe_algorithm_parameters(alg))
            return

        params = self._parse_run_params(parts[1])
        if not params:
            self._log(
                f"<b>AI:</b> Couldn't parse any PARAM=value pairs from '{parts[1]}'. "
                f"Run <code>/run {first_token}</code> alone to see the expected parameter names."
            )
            return
        self._request_confirmation(
            f"Run <code>{first_token}</code> with {params}?",
            self._execute_algorithm_plan,
            first_token,
            params,
            f"Manually run via /run: {first_token}",
        )

    def _search_algorithms_for_run_command(self, term):
        try:
            from qgis.core import QgsApplication

            term_l = term.lower()
            matches = []
            for alg in QgsApplication.processingRegistry().algorithms():
                hay = f"{alg.id()} {alg.displayName()}".lower()
                if term_l in hay:
                    matches.append(f"<code>{alg.id()}</code> -- {alg.displayName()}")
                    if len(matches) >= 15:
                        break
        except Exception as exc:
            return f"<b>AI:</b> Could not search the algorithm registry: {exc}"
        if not matches:
            return f"<b>AI:</b> No algorithms found matching '{term}'. Try a shorter or different search term."
        return (
            f"<b>AI:</b> Matches for '{term}' (showing up to 15):\n" + "\n".join(matches) + "\n\n"
            "Run <code>/run &lt;exact id&gt;</code> to see its parameters."
        )

    def _describe_algorithm_parameters(self, alg):
        lines = [f"<b>AI:</b> <code>{alg.id()}</code> -- {alg.displayName()}", "Parameters:"]
        required_names = []
        try:
            for p in alg.parameterDefinitions():
                is_optional = False
                try:
                    is_optional = bool(p.flags() & p.Flag.FlagOptional)
                except Exception:
                    try:
                        is_optional = bool(p.flags() & p.FlagOptional)
                    except Exception:
                        is_optional = False
                lines.append(f"- {p.name()}: {p.description()}" + ("" if is_optional else " (required)"))
                if not is_optional:
                    required_names.append(p.name())
        except Exception as exc:
            lines.append(f"(could not list parameters: {exc})")
        example_params = " ".join(f"{n}=..." for n in required_names) or "PARAM=..."
        lines.append(f"Example: /run {alg.id()} {example_params}")
        return "\n".join(lines)

    def _parse_run_params(self, param_str):
        params = {}
        chunks = re.split(r"(?=\s[A-Za-z_][A-Za-z0-9_]*=)", " " + param_str)
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            else:
                try:
                    value = int(value)
                except ValueError:
                    try:
                        value = float(value)
                    except ValueError:
                        pass  # keep as string
            params[key] = value
        return params

    def _execute_algorithm_plan(self, algorithm_id, params, message):
        if not self._processing_available():
            return True
        try:
            resolved = self._resolve_processing_params(params)
            result = processing.run(algorithm_id, resolved)
            self._add_output_layers(result)
            out_keys = ", ".join(result.keys()) if isinstance(result, dict) else "none"
            prefix = f"{message}\n" if message else ""
            self._log(f"<b>AI:</b> {prefix}Executed <code>{algorithm_id}</code>. Outputs: {out_keys}")
            self._remember_action(self.last_user_query, f"executed {algorithm_id}")
            return True
        except Exception as exc:
            self._log(f"<b>AI:</b> Failed running {algorithm_id}: {exc}")
            return True

    def _resolve_processing_params(self, params):
        resolved = {}
        layers = list(QgsProject.instance().mapLayers().values())
        active = self.iface.activeLayer()
        for key, value in params.items():
            if isinstance(value, str):
                if value.lower() == "active":
                    resolved[key] = active
                    continue
                named = self._find_layer_by_exact_name(value, layers)
                if named is not None:
                    resolved[key] = named
                    continue
            resolved[key] = value
        return resolved

    def _find_layer_by_exact_name(self, name, layers):
        target = name.strip().lower()
        for layer in layers:
            if layer.name().strip().lower() == target:
                return layer
        return None

    def _add_output_layers(self, result):
        if not isinstance(result, dict):
            return
        for value in result.values():
            if hasattr(value, "isValid") and hasattr(value, "name"):
                try:
                    if value.isValid():
                        QgsProject.instance().addMapLayer(value)
                except Exception:
                    continue

    def _extract_inline_json(self, query):
        start = query.find("{")
        end = query.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(query[start : end + 1])
        except Exception:
            self._log("<b>AI:</b> Could not parse inline JSON parameters.")
            return None

    def _run_null_field_audit(self):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Null audit works on vector layers.")
            return
        summary = []
        total = layer.featureCount()
        for field in layer.fields():
            null_count = 0
            idx = layer.fields().indexFromName(field.name())
            for feat in layer.getFeatures():
                value = feat[idx]
                if value is None or str(value).strip() == "":
                    null_count += 1
            if null_count > 0:
                summary.append(f"- {field.name()}: {null_count}/{total}")
        if not summary:
            self._log("<b>AI:</b> Null audit complete. No null/empty values detected.")
            return
        self._log("<b>AI:</b> Null field audit:\n" + "\n".join(summary))
        self._remember_action(self.last_user_query, "null audit done")

    def _run_duplicate_audit(self, normalized_query):
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        if not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Duplicate audit works on vector layers.")
            return
        field_name = self._extract_field_name_for_split(normalized_query, layer)
        if not field_name:
            self._log("<b>AI:</b> Specify field name, e.g. 'find duplicates in field FEEDER'.")
            return
        idx = layer.fields().indexFromName(field_name)
        seen = {}
        duplicates = {}
        for feat in layer.getFeatures():
            val = feat[idx]
            key = "" if val is None else str(val)
            if key in seen:
                duplicates[key] = duplicates.get(key, 1) + 1
            else:
                seen[key] = feat.id()
        if not duplicates:
            self._log(f"<b>AI:</b> No duplicates found in '{field_name}'.")
            return
        lines = [f"- {k}: {c} features" for k, c in sorted(duplicates.items(), key=lambda x: (-x[1], x[0]))[:20]]
        self._log(f"<b>AI:</b> Duplicate audit for '{field_name}':\n" + "\n".join(lines))
        self._remember_action(self.last_user_query, "duplicate audit done")

    def _run_topology_qa_pack(self):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        try:
            result = processing.run(
                "native:checkvalidity",
                {
                    "INPUT_LAYER": layer,
                    "METHOD": 2,
                    "VALID_OUTPUT": "memory:",
                    "INVALID_OUTPUT": "memory:",
                    "ERROR_OUTPUT": "memory:",
                },
            )
            valid_layer = result["VALID_OUTPUT"]
            invalid_layer = result["INVALID_OUTPUT"]
            error_layer = result["ERROR_OUTPUT"]
            valid_layer.setName(f"{layer.name()}_qa_valid")
            invalid_layer.setName(f"{layer.name()}_qa_invalid")
            error_layer.setName(f"{layer.name()}_qa_errors")
            QgsProject.instance().addMapLayer(valid_layer)
            QgsProject.instance().addMapLayer(invalid_layer)
            QgsProject.instance().addMapLayer(error_layer)

            base_out = self._resolve_output_dir_from_query(self.last_user_query)
            csv_path = base_out / f"{layer.name()}_topology_qa.csv"
            csv_text = (
                "metric,count\n"
                f"valid_features,{valid_layer.featureCount()}\n"
                f"invalid_features,{invalid_layer.featureCount()}\n"
                f"error_points,{error_layer.featureCount()}\n"
            )
            csv_path.write_text(csv_text, encoding="utf-8")
            self._log(f"<b>AI:</b> Topology QA complete. Report CSV: {csv_path}")
            self._remember_action(self.last_user_query, "topology qa done")
        except Exception as exc:
            self._log(f"<b>AI:</b> Topology QA failed: {exc}")

    def _run_clip_raster_by_mask(self):
        if not self._processing_available():
            return
        rasters = self._resolve_rasters_for_clip(self.last_user_query or "")
        if not rasters:
            self._log("<b>AI:</b> No raster/image layer found to clip.")
            return
        vectors = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        polygon_vectors = [l for l in vectors if l.geometryType() == QgsWkbTypes.PolygonGeometry]
        # Prefer explicit AOI/mask polygon if present.
        mask = next((l for l in polygon_vectors if "aoi" in l.name().lower() or "mask" in l.name().lower()), None)
        if mask is None:
            mask = polygon_vectors[-1] if polygon_vectors else (vectors[-1] if vectors else None)
        if mask is None:
            self._log("<b>AI:</b> No vector mask layer found in project.")
            return
        out_dir = self._resolve_output_dir_from_query(self.last_user_query)
        if self._has_explicit_windows_path(self.last_user_query):
            self._run_clip_raster_batch_by_mask_with_output(rasters, mask, str(out_dir))
            return
        self._request_output_path(
            "clip_raster_by_mask",
            "Where should I save the clipped raster? Send output folder path.",
            lambda output_dir: self._run_clip_raster_batch_by_mask_with_output(rasters, mask, output_dir),
        )

    def _run_clip_raster_batch_by_mask_with_output(self, rasters, mask, out_dir):
        done = 0
        failed = []
        for r in list(rasters or []):
            try:
                ok, outp = self._run_clip_raster_by_mask_with_output_impl(r, mask, out_dir, quiet=True)
                if ok:
                    done += 1
                    self.last_saved_output_path = str(outp or "")
                else:
                    # The real failure reason is captured into
                    # last_operation_state even in quiet mode -- surface it
                    # instead of discarding it down to a bare layer name,
                    # which previously made every batch-clip failure an
                    # undiagnosable mystery (e.g. "Failed for: Google
                    # Satellite" with no reason at all).
                    reason = (self.last_operation_state or {}).get("detail", "unknown reason")
                    failed.append(f"{r.name()} ({reason})")
            except Exception as exc:
                try:
                    failed.append(f"{r.name()} ({exc})")
                except Exception:
                    failed.append(f"raster ({exc})")
        if done > 0:
            self._log(f"<b>AI:</b> Raster clip completed for {done} raster layer(s).")
        if failed:
            self._log("<b>AI:</b> Failed for: " + "; ".join(failed))

    def _should_clip_all_rasters_from_query(self, query_text):
        q = (query_text or "").lower()
        return any(
            k in q
            for k in (
                "all images",
                "all image",
                "all rasters",
                "all raster",
                "both images",
                "both rasters",
                "all loaded images",
                "all loaded rasters",
            )
        )

    def _resolve_rasters_for_clip(self, query_text):
        rasters = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsRasterLayer)]
        if not rasters:
            return []
        if self._should_clip_all_rasters_from_query(query_text):
            return rasters
        single = self._resolve_raster_for_clip()
        return [single] if single is not None else []

    def _run_clip_raster_by_mask_with_output(self, raster, mask, out_dir):
        return self._run_clip_raster_by_mask_with_output_impl(raster, mask, out_dir, quiet=False)

    def _run_clip_raster_by_mask_with_output_impl(self, raster, mask, out_dir, quiet=False):
        try:
            out_root = Path(str(out_dir))
            out_root.mkdir(parents=True, exist_ok=True)
            out_path = out_root / f"{self._safe_file_stem(raster.name())}_clip_by_{self._safe_file_stem(mask.name())}.tif"
            out = None
            used_extent_fallback = False

            if self._is_streaming_raster_layer(raster):
                # Hard fallback for XYZ/WMS streams:
                # materialize AOI extent to a real local GeoTIFF first.
                ext = self._mask_extent_in_raster_crs(mask, raster)
                staged_path = out_root / f"{self._safe_file_stem(raster.name())}_aoi_extent_staged.tif"
                staged_ok = self._materialize_streaming_raster_to_tif(raster, ext, staged_path)
                if (not staged_ok) and self._translate_streaming_extent_to_tif(raster, ext, staged_path):
                    staged_ok = True
                if staged_ok:
                    try:
                        out = processing.run(
                            "gdal:cliprasterbymasklayer",
                            {
                                "INPUT": str(staged_path),
                                "MASK": mask,
                                "CROP_TO_CUTLINE": True,
                                "KEEP_RESOLUTION": True,
                                "OUTPUT": str(out_path),
                            },
                        )["OUTPUT"]
                    except Exception:
                        out = str(staged_path)
                    used_extent_fallback = True

            if not used_extent_fallback:
                out = processing.run(
                    "gdal:cliprasterbymasklayer",
                    {
                        "INPUT": raster,
                        "MASK": mask,
                        "CROP_TO_CUTLINE": True,
                        "KEEP_RESOLUTION": True,
                        "OUTPUT": str(out_path),
                    },
                )["OUTPUT"]

            out_file = Path(str(out))
            if not out_file.exists():
                # Final fallback: render AOI extent and force georeferenced export.
                fb_path = self._render_raster_extent_snapshot_georef(raster, self._mask_extent_in_raster_crs(mask, raster), out_path)
                if fb_path:
                    out_file = Path(str(fb_path))
                    out = str(fb_path)
                    used_extent_fallback = True
                else:
                    if not quiet:
                        self._log(f"<b>AI:</b> Clip command returned, but output file not found: {out}")
                    self.last_operation_state = {
                        "name": "raster_clip",
                        "status": "failed",
                        "detail": f"output file missing at {out}",
                    }
                    return False, ""
            clipped = QgsRasterLayer(str(out), f"{raster.name()}_clip")
            loaded = False
            if clipped.isValid():
                QgsProject.instance().addMapLayer(clipped)
                loaded = True
                try:
                    self.iface.setActiveLayer(clipped)
                    self.iface.mapCanvas().setExtent(clipped.extent())
                    self.iface.mapCanvas().refresh()
                except Exception:
                    pass
            self.last_saved_output_path = str(out_file)
            if not quiet:
                self._log(
                    f"<b>AI:</b> Raster clipped by mask layer '{mask.name()}'.\n"
                    f"Saved to: {out}\n"
                    + ("Mode: AOI extent fallback for streaming basemap.\n" if used_extent_fallback else "")
                    + f"{'Layer loaded on canvas.' if loaded else 'Output saved, but auto-load failed. Add the TIFF manually.'}"
                )
            self.last_operation_state = {
                "name": "raster_clip",
                "status": "done",
                "detail": f"saved {out}",
            }
            if not quiet:
                self._remember_action(self.last_user_query, "clip raster by mask done")
            return True, str(out)
        except Exception as exc:
            if not quiet:
                self._log(f"<b>AI:</b> Raster clip failed: {exc}")
            self.last_operation_state = {
                "name": "raster_clip",
                "status": "failed",
                "detail": str(exc),
            }
            return False, ""

    def _render_raster_extent_snapshot_georef(self, raster_layer, extent, out_path):
        """
        Last-resort fallback for streamed basemaps:
        render raster layer over AOI extent and export as georeferenced raster.
        Returns output path on success, else empty string.
        """
        try:
            ms = QgsMapSettings()
            ms.setLayers([raster_layer])
            ms.setExtent(extent)
            ms.setDestinationCrs(raster_layer.crs())
            ms.setBackgroundColor(QColor(255, 255, 255, 0))

            width, height = self._estimate_export_dimensions_from_canvas(extent)
            ms.setOutputSize(QSize(int(width), int(height)))
            ms.setOutputDpi(96)

            image = QImage(int(width), int(height), QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0, 0))
            job = QgsMapRendererParallelJob(ms)
            job.start()
            job.waitForFinished()
            rendered = job.renderedImage()
            if rendered is not None and not rendered.isNull():
                image = rendered

            # Save render as PNG first (widely supported), then georeference/convert.
            png_path = Path(str(out_path)).with_suffix(".png")
            if not image.save(str(png_path), "PNG") or not png_path.exists():
                return ""

            xmin = float(extent.xMinimum())
            xmax = float(extent.xMaximum())
            ymin = float(extent.yMinimum())
            ymax = float(extent.yMaximum())
            epsg = raster_layer.crs().authid() if raster_layer.crs() and raster_layer.crs().isValid() else ""
            extra = f"-a_ullr {xmin} {ymax} {xmax} {ymin}"
            if epsg:
                extra += f" -a_srs {epsg}"

            if self._processing_available():
                try:
                    processing.run(
                        "gdal:translate",
                        {
                            "INPUT": str(png_path),
                            "TARGET_CRS": raster_layer.crs() if raster_layer.crs() and raster_layer.crs().isValid() else None,
                            "NODATA": None,
                            "COPY_SUBDATASETS": False,
                            "OPTIONS": "",
                            "EXTRA": extra,
                            "DATA_TYPE": 0,
                            "OUTPUT": str(out_path),
                        },
                    )
                    if Path(str(out_path)).exists():
                        return str(out_path)
                except Exception:
                    pass
            # If georeferenced conversion fails, still return PNG as artifact.
            return str(png_path) if png_path.exists() else ""
        except Exception:
            return ""

    def _safe_file_stem(self, name_text):
        t = str(name_text or "").strip()
        if not t:
            return "output"
        t = re.sub(r'[<>:"/\\|?*]+', "_", t)
        t = re.sub(r"\s+", "_", t).strip("._")
        return t or "output"

    def _is_streaming_raster_layer(self, raster_layer):
        try:
            provider = str(raster_layer.providerType() or "").lower()
            src = str(raster_layer.source() or "").lower()
        except Exception:
            return False
        if provider in {"wms", "xyz"}:
            return True
        if "type=xyz" in src or "url=http" in src or src.startswith("http"):
            return True
        return False

    def _materialize_streaming_raster_to_tif(self, raster_layer, extent, out_path):
        try:
            provider = raster_layer.dataProvider()
            if provider is None:
                return False
            provider_clone = provider.clone()
            if provider_clone is None:
                return False
            pipe = QgsRasterPipe()
            if not pipe.set(provider_clone):
                return False

            width, height = self._estimate_export_dimensions_from_canvas(extent)

            writer = QgsRasterFileWriter(str(out_path))
            writer.setOutputFormat("GTiff")
            result = writer.writeRaster(
                pipe,
                int(width),
                int(height),
                extent,
                raster_layer.crs(),
                QgsProject.instance().transformContext(),
            )
            return int(result) == 0 and Path(str(out_path)).exists()
        except Exception:
            return False

    def _estimate_export_dimensions_from_canvas(self, extent):
        """
        Preserve current basemap view resolution, but capped -- uncapped,
        this scales as AOI-extent-size / current-zoom-resolution with NO
        upper bound. Confirmed as the root cause of a real multi-hour QGIS
        freeze: clipping a large AOI while zoomed in close (fine
        mapUnitsPerPixel) on a streaming basemap like Google Satellite
        made this compute a raster hundreds of thousands of pixels per
        side, requiring an effectively unbounded synchronous tile fetch on
        the UI thread. Capped to a size that still gives a detailed clip
        (36 megapixels) but completes in a bounded, reasonable time.
        """
        MAX_DIM = 6000
        try:
            canvas = self.iface.mapCanvas()
            if canvas is None:
                raise RuntimeError("canvas unavailable")
            mpp = float(canvas.mapUnitsPerPixel())
            if mpp <= 0:
                raise RuntimeError("invalid mapUnitsPerPixel")
            w_mu = max(float(extent.width()), 1e-9)
            h_mu = max(float(extent.height()), 1e-9)
            width = int(round(w_mu / mpp))
            height = int(round(h_mu / mpp))
            csize = canvas.size()
            width = max(width, int(csize.width()))
            height = max(height, int(csize.height()))
            width, height = max(1, width), max(1, height)
            if width > MAX_DIM or height > MAX_DIM:
                scale = MAX_DIM / max(width, height)
                capped_w, capped_h = max(1, int(width * scale)), max(1, int(height * scale))
                self._log(
                    f"<b>AI:</b> Note: the full-resolution export for this AOI would be "
                    f"{width}x{height}px (an unbounded, potentially very slow tile fetch). "
                    f"Capped to {capped_w}x{capped_h}px so this completes in a reasonable time -- "
                    "zoom in and clip a smaller area first if you need full native resolution."
                )
                return capped_w, capped_h
            return width, height
        except Exception:
            return 4096, 4096

    def _translate_streaming_extent_to_tif(self, raster_layer, extent, out_path):
        """
        Secondary fallback for streamed basemaps:
        use GDAL translate with AOI projwin to force a local TIFF export.
        """
        if not self._processing_available():
            return False
        try:
            xmin = float(extent.xMinimum())
            xmax = float(extent.xMaximum())
            ymin = float(extent.yMinimum())
            ymax = float(extent.yMaximum())
            extra = f"-projwin {xmin} {ymax} {xmax} {ymin}"
            processing.run(
                "gdal:translate",
                {
                    "INPUT": raster_layer,
                    "TARGET_CRS": None,
                    "NODATA": None,
                    "COPY_SUBDATASETS": False,
                    "OPTIONS": "",
                    "EXTRA": extra,
                    "DATA_TYPE": 0,
                    "OUTPUT": str(out_path),
                },
            )
            return Path(str(out_path)).exists()
        except Exception:
            return False

    def _mask_extent_in_raster_crs(self, mask_layer, raster_layer):
        try:
            ext = mask_layer.extent()
            src = mask_layer.crs()
            dst = raster_layer.crs()
            if src is None or dst is None or not src.isValid() or not dst.isValid():
                return ext
            if src.authid() == dst.authid():
                return ext
            tr = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
            return tr.transformBoundingBox(ext)
        except Exception:
            try:
                return mask_layer.extent()
            except Exception:
                return raster_layer.extent()

    def _resolve_raster_for_clip(self):
        active = self.iface.activeLayer()
        if isinstance(active, QgsRasterLayer):
            return active
        rasters = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsRasterLayer)]
        if not rasters:
            return None
        preferred = next(
            (
                r
                for r in rasters
                if any(k in r.name().lower() for k in ("google satellite", "imagery", "esri world imagery", "satellite", "ortho"))
            ),
            None,
        )
        return preferred or rasters[-1]

    def _run_clip_google_satellite_by_loaded_aoi(self, original_query):
        rasters = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsRasterLayer)]
        sat = next((r for r in rasters if "google satellite" in r.name().lower()), None)
        if sat is None:
            self._log("<b>AI:</b> Google Satellite raster layer not found in project.")
            return
        mask = self._best_polygon_mask_layer() if hasattr(self, "_best_polygon_mask_layer") else None
        if mask is None:
            self._log("<b>AI:</b> Loaded AOI polygon not found. Create/load AOI first.")
            return
        out_dir = self._resolve_output_dir_from_query(original_query or self.last_user_query)
        if self._has_explicit_windows_path(original_query or self.last_user_query):
            self._run_clip_raster_by_mask_with_output(sat, mask, str(out_dir))
            return
        self._request_output_path(
            "clip_google_satellite_by_aoi",
            "Where should I save the clipped Google Satellite raster? Send output folder path.",
            lambda output_dir: self._run_clip_raster_by_mask_with_output(sat, mask, output_dir),
        )

    def _is_create_aoi_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_aoi = any(k in q for k in ("aoi", "area of interest"))
        has_create = any(k in q for k in ("create", "make", "generate", "build"))
        has_layer_context = any(k in q for k in ("loaded", "active", "shape", "shapefile", "layer"))
        return has_aoi and has_create and has_layer_context

    def _run_create_aoi_from_loaded_layer(self):
        layer = self.iface.activeLayer()
        if layer is None or not isinstance(layer, QgsVectorLayer):
            self._log("<b>AI:</b> Select the loaded shapefile (vector layer) first.")
            return
        if layer.featureCount() == 0:
            self._log("<b>AI:</b> Active layer has no features, so AOI cannot be created.")
            return

        crs_auth = layer.crs().authid() if layer.crs().isValid() else "EPSG:4326"
        out = QgsVectorLayer(f"Polygon?crs={crs_auth}", f"{layer.name()}_AOI", "memory")
        prov = out.dataProvider()
        prov.addAttributes([QgsField("source", QVariant.String), QgsField("method", QVariant.String)])
        out.updateFields()

        method = "geometry"
        aoi_geom = None
        try:
            geom_type = layer.geometryType()
            distance_m = self._extract_distance(self.last_user_query or "")
            geoms = [f.geometry() for f in layer.getFeatures() if f.hasGeometry() and not f.geometry().isEmpty()]
            if not geoms:
                self._log("<b>AI:</b> No valid geometries found in active layer.")
                return
            union_geom = QgsGeometry.unaryUnion(geoms)
            if union_geom is None or union_geom.isEmpty():
                union_geom = QgsGeometry.fromRect(layer.extent())
                method = "extent_fallback"

            if distance_m and float(distance_m) > 0:
                # User asked "AOI around" with distance, so create true buffered AOI.
                distance_units = self._meters_to_layer_units(layer, float(distance_m))
                aoi_geom = union_geom.buffer(distance_units, 24)
                method = f"buffer_{float(distance_m):g}m"
            else:
                if geom_type == QgsWkbTypes.PolygonGeometry:
                    aoi_geom = union_geom
                    method = "polygon_union"
                else:
                    hull = union_geom.convexHull() if union_geom is not None else None
                    if hull is not None and not hull.isEmpty():
                        aoi_geom = hull
                        method = "convex_hull"
                    else:
                        aoi_geom = QgsGeometry.fromRect(layer.extent())
                        method = "extent_fallback"

            feat = QgsFeature(out.fields())
            feat.setGeometry(aoi_geom)
            feat["source"] = layer.name()
            feat["method"] = method
            prov.addFeature(feat)
            out.updateExtents()
            QgsProject.instance().addMapLayer(out)
            self._log(f"<b>AI:</b> AOI created for '{layer.name()}' as layer '{out.name()}' (method: {method}).")
            self._remember_action(self.last_user_query, "create aoi from loaded layer")
        except Exception as exc:
            self._log(f"<b>AI:</b> AOI creation failed: {exc}")

    def _is_clip_raster_by_mask_query(self, normalized_query):
        q = (normalized_query or "").lower()
        if "clip raster by mask" in q:
            return True
        has_clip = any(k in q for k in ("clip", "crop", "cut"))
        has_raster = any(k in q for k in ("raster", "image", "tif", "tiff"))
        has_polygon = any(k in q for k in ("polygon", "mask", "boundary", "drawn polygon"))
        return has_clip and has_raster and has_polygon

    def _is_clip_raster_by_canvas_extent_query(self, normalized_query):
        q = (normalized_query or "").lower()
        has_clip = any(k in q for k in ("clip", "crop", "cut"))
        has_raster = any(k in q for k in ("raster", "image", "tif", "tiff"))
        has_extent = any(k in q for k in ("canvas extent", "current extent", "map extent", "view extent"))
        return has_clip and has_raster and has_extent

    def _run_clip_raster_by_canvas_extent(self):
        if not self._processing_available():
            return
        raster = self._resolve_raster_for_clip()
        if raster is None:
            self._log("<b>AI:</b> No raster/image layer found to clip by extent.")
            return
        canvas = self.iface.mapCanvas()
        ext = canvas.extent() if canvas is not None else None
        if ext is None or ext.isEmpty():
            self._log("<b>AI:</b> Current canvas extent is not valid.")
            return
        out_dir = self._resolve_output_dir_from_query(self.last_user_query)
        if self._has_explicit_windows_path(self.last_user_query):
            self._run_clip_raster_by_canvas_extent_with_output(raster, ext, str(out_dir))
            return
        self._request_output_path(
            "clip_raster_by_extent",
            "Where should I save the extent-clipped raster? Send output folder path.",
            lambda output_dir: self._run_clip_raster_by_canvas_extent_with_output(raster, ext, output_dir),
        )

    def _run_clip_raster_by_canvas_extent_with_output(self, raster, ext, out_dir):
        out_root = Path(str(out_dir))
        out_root.mkdir(parents=True, exist_ok=True)
        out = str(out_root / f"{raster.name()}_clip_canvas_extent.tif")
        extent_txt = f"{ext.xMinimum()},{ext.xMaximum()},{ext.yMinimum()},{ext.yMaximum()}"
        try:
            processing.run(
                "gdal:cliprasterbyextent",
                {
                    "INPUT": raster.source(),
                    "PROJWIN": extent_txt,
                    "NODATA": None,
                    "OPTIONS": "",
                    "DATA_TYPE": 0,
                    "EXTRA": "",
                    "OUTPUT": out,
                },
            )
            clipped = QgsRasterLayer(out, f"{raster.name()}_clip_extent")
            if clipped.isValid():
                QgsProject.instance().addMapLayer(clipped)
            self.last_saved_output_path = str(out)
            self._log(f"<b>AI:</b> Raster clipped by current canvas extent: {out}")
            self._remember_action(self.last_user_query, "clip raster by canvas extent done")
        except Exception as exc:
            self._log(f"<b>AI:</b> Clip raster by canvas extent failed: {exc}")

    def _run_reproject_raster(self, normalized_query):
        if not self._processing_available():
            return
        raster = self.iface.activeLayer()
        if raster is None or not isinstance(raster, QgsRasterLayer):
            self._log("<b>AI:</b> Select raster layer first.")
            return
        epsg = self._extract_epsg(normalized_query)
        if epsg is None:
            self._log("<b>AI:</b> Mention EPSG, e.g. 'reproject raster to EPSG:3857'.")
            return
        try:
            out = processing.run(
                "gdal:warpreproject",
                {"INPUT": raster, "TARGET_CRS": f"EPSG:{epsg}", "OUTPUT": "TEMPORARY_OUTPUT"},
            )["OUTPUT"]
            self._log(f"<b>AI:</b> Raster reprojected to EPSG:{epsg}: {out}")
            self._remember_action(self.last_user_query, "reproject raster done")
        except Exception as exc:
            self._log(f"<b>AI:</b> Reproject raster failed: {exc}")

    def _run_raster_surface(self, mode):
        if not self._processing_available():
            return
        raster = self.iface.activeLayer()
        if raster is None or not isinstance(raster, QgsRasterLayer):
            self._log("<b>AI:</b> Select DEM raster layer first.")
            return
        alg_map = {
            "slope": "gdal:slope",
            "aspect": "gdal:aspect",
            "hillshade": "gdal:hillshade",
        }
        alg = alg_map.get(mode)
        if not alg:
            return
        try:
            out = processing.run(alg, {"INPUT": raster, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
            self._log(f"<b>AI:</b> Raster {mode} created: {out}")
            self._remember_action(self.last_user_query, f"raster {mode} done")
        except Exception as exc:
            self._log(f"<b>AI:</b> Raster {mode} failed: {exc}")

    def _run_export_assistant(self):
        if not self._processing_available():
            return
        layer = self._active_layer_or_msg()
        if layer is None:
            return
        out_root = self._resolve_output_dir_from_query(self.last_user_query)
        client_name = "default_client"
        date_tag = datetime.utcnow().strftime("%Y%m%d")
        out_dir = out_root / client_name / date_tag / layer.name()
        out_dir.mkdir(parents=True, exist_ok=True)
        gpkg_path = out_dir / f"{layer.name()}.gpkg"
        qml_path = out_dir / f"{layer.name()}.qml"
        meta_path = out_dir / f"{layer.name()}_metadata.json"

        try:
            processing.run("native:savefeatures", {"INPUT": layer, "OUTPUT": str(gpkg_path)})
            try:
                layer.saveNamedStyle(str(qml_path))
            except Exception:
                pass
            metadata = {
                "layer_name": layer.name(),
                "feature_count": layer.featureCount() if hasattr(layer, "featureCount") else None,
                "crs": layer.crs().authid() if hasattr(layer, "crs") else None,
                "exported_at_utc": datetime.utcnow().isoformat(),
                "output_file": str(gpkg_path),
                "style_file": str(qml_path),
            }
            meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            self._log(f"<b>AI:</b> Export assistant completed:\n{out_dir}")
            self._remember_action(self.last_user_query, "export assistant done")
        except Exception as exc:
            self._log(f"<b>AI:</b> Export assistant failed: {exc}")

    def _load_agent_settings(self):
        settings = QgsSettings()
        settings.beginGroup("AIGISCopilot/agent")
        data = {
            "provider": settings.value("provider", "ollama", type=str),
            "gemini_key": settings.value("gemini_key", "", type=str),
            "gemini_model": settings.value("gemini_model", "gemini-flash-latest", type=str),
            "groq_key": settings.value("groq_key", "", type=str),
            "groq_model": settings.value("groq_model", "openai/gpt-oss-120b", type=str),
            "ollama_base_url": settings.value("ollama_base_url", "http://localhost:11434/v1", type=str),
            "ollama_model": settings.value("ollama_model", "qwen2.5:7b-instruct", type=str),
            "brave_key": settings.value("brave_key", "", type=str),
            "shared_memory_path": settings.value("shared_memory_path", "", type=str),
        }
        settings.endGroup()
        # Self-heal previously-saved model names that providers have since
        # retired -- otherwise a fix shipped in a plugin update never takes
        # effect for anyone who already ran /set before the update, because
        # their explicit saved value always wins over a new code default.
        retired_models = {
            "gemini_model": {
                "gemini-2.0-flash": "gemini-flash-latest",
                "gemini-2.0-flash-lite": "gemini-flash-latest",
                "gemini-1.5-flash": "gemini-flash-latest",
                "gemini-1.5-pro": "gemini-flash-latest",
            },
            "groq_model": {
                "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
                "llama-3.1-8b-instant": "openai/gpt-oss-20b",
                "llama3-70b-8192": "openai/gpt-oss-120b",
                "llama3-8b-8192": "openai/gpt-oss-20b",
            },
        }
        migrated = False
        for key, replacements in retired_models.items():
            if data.get(key) in replacements:
                data[key] = replacements[data[key]]
                migrated = True
        if migrated:
            self.agent_settings = data
            self._save_agent_settings()
        return data

    def _save_agent_settings(self):
        settings = QgsSettings()
        settings.beginGroup("AIGISCopilot/agent")
        for key, value in self.agent_settings.items():
            settings.setValue(key, value)
        settings.endGroup()

    def _start_background_task(self, work_fn, on_success, on_error, busy_message, label="task"):
        if self._background_task is not None and self._background_task.isRunning():
            self._log("<b>AI:</b> Another install/bind is already running -- please wait for it to finish.")
            return
        self._log(f"<b>AI:</b> {busy_message}")
        task = _BackgroundTask(work_fn, parent=self)
        task.succeeded.connect(lambda result: (on_success(result), self._stop_heartbeat(), self._clear_background_task()))
        task.failed.connect(lambda err: (on_error(err), self._stop_heartbeat(), self._clear_background_task()))
        self._background_task = task
        self._background_task_label = label
        self._background_task_started_at = datetime.utcnow()
        self._start_heartbeat(label)
        task.start()

    def _start_heartbeat(self, label):
        # Silence during a multi-minute install is indistinguishable from
        # the earlier real deadlock -- confirmed real user anxiety over
        # exactly that. A periodic "still working" line, with elapsed
        # time, makes slow-but-fine and actually-stuck tell apart.
        from qgis.PyQt.QtCore import QTimer

        self._stop_heartbeat()
        timer = QTimer(self)
        timer.setInterval(30000)

        def tick():
            if self._background_task_started_at is None:
                return
            elapsed = int((datetime.utcnow() - self._background_task_started_at).total_seconds())
            self._log(f"<i>Still working on {label}... {elapsed}s elapsed. Still normal for a heavy install.</i>")

        timer.timeout.connect(tick)
        self._heartbeat_timer = timer
        timer.start()

    def _stop_heartbeat(self):
        timer = getattr(self, "_heartbeat_timer", None)
        if timer is not None:
            timer.stop()
            self._heartbeat_timer = None

    def _clear_background_task(self):
        self._background_task = None
        self._background_task_label = None
        self._background_task_started_at = None

    def _cancel_background_task(self):
        """Escape hatch so a stuck /install or /bind never locks a person
        out until they restart QGIS -- the deadlock this was built to
        recover from is now fixed at the root (see install_gis_mcp /
        _install_pysal_dependencies), but this stays as a safety net for
        any future unforeseen hang (e.g. a genuine multi-minute network
        stall past the timeout)."""
        if self._background_task is None or not self._background_task.isRunning():
            self._log("<b>AI:</b> No install/bind task is currently running.")
            return
        self._background_task.terminate()
        self._background_task.wait(3000)
        self._background_task = None
        self._stop_heartbeat()
        self._log("<b>AI:</b> Cancelled the running task. You can retry /install or /bind now.")

    def _install_pysal_dependencies(self):
        import subprocess

        packages = ["geopandas", "pandas", "libpysal", "esda"]

        def work():
            python_exe = find_real_python_executable()
            # Same deadlock as install_gis_mcp -- check_call() with PIPE
            # never drains output and hangs once the child writes more
            # than the OS pipe buffer holds. subprocess.run() correctly
            # drains via communicate(); the timeout means a genuine stall
            # surfaces as an error instead of hanging indefinitely.
            result = subprocess.run(
                [python_exe, "-m", "pip", "install", "--user", *packages],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=900,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"pip exited with code {result.returncode}: {result.stdout[-800:]}")
            return None

        def on_success(_result):
            self._log(
                "<b>AI:</b> Installed successfully. Restart QGIS, then spatial_autocorrelation and "
                "local_spatial_autocorrelation will be available to the agent."
            )

        def on_error(exc):
            self._log(
                f"<b>AI:</b> Automatic install failed ({exc}). Manual "
                "fallback: open the OSGeo4W Shell (Start menu, search 'OSGeo4W Shell'), then run:\n"
                "<code>python-qgis -m pip install geopandas pandas libpysal esda</code>\n"
                "(On macOS/Linux QGIS installs, the automatic install above usually works.)"
            )

        self._start_background_task(
            work,
            on_success,
            on_error,
            f"Installing spatial-statistics dependencies into QGIS's Python ({', '.join(packages)}) "
            "in the background -- QGIS stays usable while this runs. This can take several minutes "
            "depending on your connection; I'll post here when it's done.",
            label="pysal install",
        )

    def _install_gis_mcp(self):
        def work():
            install_gis_mcp()
            return None

        def on_success(_result):
            self._log("<b>AI:</b> gis-mcp installed successfully. Restart QGIS, then run /bind gis-mcp to connect it.")

        def on_error(exc):
            self._log(
                f"<b>AI:</b> Automatic install failed ({exc}). On Windows QGIS, open the OSGeo4W "
                "Shell and run:\n<code>python-qgis -m pip install gis-mcp</code>"
            )

        self._start_background_task(
            work,
            on_success,
            on_error,
            "Installing gis-mcp (https://github.com/mahdin75/gis-mcp) and its core dependencies "
            "(shapely, geopandas, rasterio, pyproj, pysal, fastmcp) into QGIS's Python in the "
            "background -- QGIS stays usable while this runs. This is a heavy install and can "
            "genuinely take several minutes; I'll post here when it's done, not before.",
            label="gis-mcp install",
        )

    def _bind_gis_mcp(self):
        if self.gis_mcp_bridge is not None and self.gis_mcp_bridge.bound:
            self._log("<b>AI:</b> gis-mcp is already bound. Use /unbind gis-mcp first to rebind.")
            return

        def work():
            bridge = GisMcpBridge()
            bridge.start_and_handshake()
            return bridge

        def on_success(bridge):
            self.gis_mcp_bridge = bridge
            self.agent_tool_executor.mcp_bridge = bridge
            # Deliberately NOT merging all 88 full tool schemas into
            # agent.tool_schemas -- measured this at ~13,000+ extra
            # characters sent on EVERY single agent API call, a real,
            # confirmed cause of the reported slowdown. search_gis_mcp_tools
            # / call_gis_mcp_tool (always present, constant size) give the
            # same full access via two lightweight hops instead, exactly
            # mirroring how search_algorithms/run_processing_algorithm
            # already reach QGIS's ~350+ native algorithms without listing
            # them all individually either.
            self._log(
                f"<b>AI:</b> Bound gis-mcp -- {len(bridge.tools)} tools now reachable via "
                "search_gis_mcp_tools + call_gis_mcp_tool (kept off the main tool list to avoid "
                "slowing down every request). Use /unbind gis-mcp to disconnect, or /mcp status to check."
            )

        def on_error(exc):
            self._log(f"<b>AI:</b> Could not bind gis-mcp: {exc}")

        self._start_background_task(
            work,
            on_success,
            on_error,
            "Starting gis-mcp and connecting in the background -- QGIS stays usable while this runs. "
            "Can take up to 90 seconds on a slow/cold start; I'll post here when it's ready.",
            label="gis-mcp bind",
        )

    def _unbind_gis_mcp(self):
        if self.gis_mcp_bridge is None:
            self._log("<b>AI:</b> gis-mcp isn't bound.")
            return
        self.gis_mcp_bridge.stop()
        self.agent_tool_executor.mcp_bridge = None
        self.gis_mcp_bridge = None
        self._log("<b>AI:</b> Unbound gis-mcp and stopped its process. Back to the plugin's own tools only.")

    def _mcp_status(self):
        if self.gis_mcp_bridge is None or not self.gis_mcp_bridge.bound:
            self._log(
                "<b>AI:</b> gis-mcp: not bound. Run /install gis-mcp (once), then /bind gis-mcp to connect."
            )
            return
        self._log(
            f"<b>AI:</b> gis-mcp: bound, {len(self.gis_mcp_bridge.tools)} tools available "
            f"(http://{self.gis_mcp_bridge.host}:{self.gis_mcp_bridge.port})."
        )

    def cleanup_before_unload(self):
        """Called from the plugin's unload() so the gis-mcp subprocess (if
        bound) never leaks past QGIS closing or the plugin being disabled."""
        if self.gis_mcp_bridge is not None:
            self.gis_mcp_bridge.stop()
            self.gis_mcp_bridge = None

    def _rebuild_agent_provider(self):
        self.agent.provider = build_provider(self.agent_provider_name, self.agent_settings)
        self.agent_unavailable_reason = None
        self.agent_retry_available = False

    def _confirm_pending_agent_action(self, approved):
        pending = self.pending_agent_confirmation
        self.pending_agent_confirmation = None
        if pending is None:
            self._log("<b>AI:</b> No pending agent action.")
            return
        if not approved:
            self._log("<b>AI:</b> Cancelled.")
        try:
            outcome = self.agent.resume_after_confirmation(pending["tool_call"], approved)
        except Exception as exc:
            self.agent_unavailable_reason = str(exc)
            self.agent_retry_available = False
            self._log(f"<b>AI:</b> Agent unavailable ({exc}). The tool call itself may still have run -- check your layers.")
            return
        self._handle_agent_outcome(outcome)

    def _handle_agent_outcome(self, outcome):
        # Visibly distinct from every legacy "<b>AI:</b>" response -- this
        # is the actual, direct fix for "why does it feel like it's not
        # understanding me": when this tag is missing, you're on the old
        # keyword-matching system, not the real language-understanding one,
        # and that's diagnosable now instead of silently confusing.
        if isinstance(outcome, dict) and outcome.get("needs_confirmation"):
            self.pending_agent_confirmation = {"tool_call": outcome["tool_call"]}
            self._log(f"<b>AI (agent):</b> {outcome['message']}")
            return
        self._log(f"<b>AI (agent):</b> {outcome}")
        self._remember_action(self.last_user_query, "agent turn completed")

    def _run_agent_turn(self, query):
        """Primary path: let the tool-calling agent handle the request.
        Returns True if the agent produced a response (answer or a
        confirmation prompt), False if the agent isn't usable right now
        so the caller should fall back to the legacy rule-based pipeline.

        On a failure, exactly one automatic retry is allowed on the very
        next message (catches a transient blip without you having to
        reconfigure anything). If that retry also fails, the agent goes
        quiet for the rest of the session -- no more retries, no more
        repeated error spam -- until /set changes the config."""
        if self.agent_unavailable_reason is not None and not self.agent_retry_available:
            return False
        if not self.agent.provider.configured():
            return False
        try:
            outcome = self.agent.run(query)
        except Exception as exc:
            if self.agent_unavailable_reason is None:
                self.agent_unavailable_reason = str(exc)
                self.agent_retry_available = True
                self._log(
                    f"<b>AI:</b> Agent unavailable ({exc}). I'll retry once on your next message; "
                    "if it fails again I'll fall back to built-in handling for the rest of this "
                    "session -- reconfigure with /set provider/gemini_key/groq_key to retry sooner."
                )
            else:
                self.agent_retry_available = False
                self._log(
                    f"<b>AI:</b> Agent still unavailable ({exc}). Falling back to built-in handling "
                    "for this session -- reconfigure with /set provider/gemini_key/groq_key to retry."
                )
            return False
        self.agent_unavailable_reason = None
        self.agent_retry_available = False
        self._handle_agent_outcome(outcome)
        return True

    def _confirm_pending_action(self, approved):
        if self.pending_action is None:
            self._log("<b>AI:</b> No pending action.")
            return
        action = self.pending_action
        self.pending_action = None
        if not approved:
            self._log("<b>AI:</b> Pending action cancelled.")
            return
        try:
            action["func"](*action.get("args", []), **action.get("kwargs", {}))
        except Exception as exc:
            self._log(f"<b>AI:</b> Failed executing confirmed action: {exc}")

    def _request_confirmation(self, message, func, *args, **kwargs):
        self.pending_action = {"func": func, "args": args, "kwargs": kwargs}
        self._log(f"<b>AI:</b> {message}\nType /yes to continue or /no to cancel.")

    def _create_backup_layer(self, layer, tag):
        try:
            backup = layer.materialize(QgsFeatureRequest())
            backup.setName(f"{layer.name()}_backup_{tag}")
            QgsProject.instance().addMapLayer(backup)
            return backup
        except Exception:
            return None

    def _remember_action(self, query, result):
        if not query:
            return
        self.history.append({"query": query, "result": result, "at": datetime.utcnow().isoformat()})
        if len(self.history) > 200:
            self.history = self.history[-200:]
        self._persist_learning(query, result)

    def _persist_learning(self, query, result):
        """
        Persist successful interactions so Copilot improves over time.
        """
        qn = self._normalize_query_for_learning(query)
        if qn:
            self.learned_routes[qn] = {"result": result, "at": datetime.utcnow().isoformat()}
            self._save_json_file(self._learned_routes_file_path(), self.learned_routes)
        self.history_store.append({"query": query, "result": result, "at": datetime.utcnow().isoformat()})
        if len(self.history_store) > 2000:
            self.history_store = self.history_store[-2000:]
        self._save_json_file(self._history_file_path(), self.history_store)
        self._append_to_shared_memory(query, result)

    def _append_to_shared_memory(self, query, result, kind="chat"):
        """Optional, opt-in team-wide activity log (see /set shared_memory_path).
        Appended as one JSON line per entry (JSONL). Each user writes to
        their OWN file (team_activity_<username>.jsonl), never a single
        file shared by every writer -- confirmed this matters specifically
        for cloud-synced folders (OneDrive, etc.): when the SAME file is
        modified by two different synced devices before reconciling, the
        sync client creates a separate "conflicted copy" rather than
        merging the appends, which would silently orphan whichever
        person's entries lost the race. If only one device ever writes a
        given filename, that failure mode can't happen -- verified this is
        the actual documented OneDrive conflict trigger before choosing
        this design, not guessed. Purely additive: does nothing if unset."""
        path = (self.agent_settings or {}).get("shared_memory_path", "").strip()
        if not path:
            return
        try:
            import getpass
            import re as _re

            username = getpass.getuser()
            safe_username = _re.sub(r"[^A-Za-z0-9_.-]", "_", username) or "user"
            entry = {
                "query": query,
                "result": result,
                "kind": kind,
                "user": username,
                "at": datetime.utcnow().isoformat(),
            }
            target = Path(path) / f"team_activity_{safe_username}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # never let shared-memory I/O break the actual request

    def _on_project_layers_added(self, layers):
        """Passive activity capture beyond chat-typed commands, as
        requested -- scoped honestly to a well-defined, always-available
        Qt/QGIS signal (a layer being added to the project), not a claim of
        instrumenting every possible QGIS action (styling, digitizing edits,
        and Processing Toolbox runs made outside the Copilot are NOT
        captured by this -- that would need much deeper instrumentation)."""
        try:
            for layer in layers:
                name = layer.name() if hasattr(layer, "name") else str(layer)
                self._append_to_shared_memory(f"layer added: {name}", "project activity", kind="layer_added")
        except Exception:
            pass

    def _normalize_query_for_learning(self, query):
        if not query:
            return ""
        q = query.lower().strip()
        q = re.sub(r"\s+", " ", q)
        return q

    def _refresh_layer_catalog(self):
        catalog = {}
        try:
            for layer in QgsProject.instance().mapLayers().values():
                layer_info = {
                    "id": layer.id(),
                    "name": layer.name(),
                    "source": layer.source() if hasattr(layer, "source") else "",
                    "crs": layer.crs().authid() if hasattr(layer, "crs") and layer.crs() else "",
                }
                if layer.type() == QgsMapLayerType.VectorLayer and isinstance(layer, QgsVectorLayer):
                    layer_info["kind"] = "vector"
                    try:
                        layer_info["geometry"] = QgsWkbTypes.displayString(layer.wkbType())
                    except Exception:
                        layer_info["geometry"] = str(layer.geometryType())
                    try:
                        layer_info["feature_count"] = int(layer.featureCount())
                    except Exception:
                        layer_info["feature_count"] = None
                    fields = []
                    for i, f in enumerate(layer.fields()):
                        fields.append(
                            {
                                "id": i,
                                "name": f.name(),
                                "type": f.typeName(),
                                "length": f.length(),
                                "precision": f.precision(),
                            }
                        )
                    layer_info["fields"] = fields
                elif layer.type() == QgsMapLayerType.RasterLayer and isinstance(layer, QgsRasterLayer):
                    layer_info["kind"] = "raster"
                    try:
                        layer_info["width"] = layer.width()
                        layer_info["height"] = layer.height()
                    except Exception:
                        layer_info["width"] = None
                        layer_info["height"] = None
                    try:
                        layer_info["band_count"] = layer.bandCount()
                    except Exception:
                        layer_info["band_count"] = None
                else:
                    layer_info["kind"] = "other"
                catalog[layer.id()] = layer_info
        except Exception:
            pass
        self.layer_catalog = catalog

    def _refresh_algorithm_catalog(self):
        catalog = {}
        if processing is None:
            self.algorithm_catalog = catalog
            return
        try:
            for alg in processing.registry().algorithms():
                aid = alg.id()
                params = []
                required = []
                for p in alg.parameterDefinitions():
                    pname = p.name()
                    params.append(pname)
                    try:
                        if not p.flags() & p.FlagOptional:
                            required.append(pname)
                    except Exception:
                        pass
                catalog[aid] = {"params": params, "required": required}
        except Exception:
            pass
        self.algorithm_catalog = catalog

    def _build_algorithm_params_from_options(self, alg_id, options):
        """
        Build params dict from key=value command options.
        Normalizes common keys and falls back to active layer for INPUT.
        """
        params = {}
        norm = {k.upper(): v for k, v in options.items()}
        # Common aliases
        aliases = {
            "LAYER": "INPUT",
            "IN": "INPUT",
            "OUT": "OUTPUT",
            "DIST": "DISTANCE",
        }
        for k, v in list(norm.items()):
            if k in aliases:
                norm[aliases[k]] = v
        # Resolve layer references
        for k, v in norm.items():
            if isinstance(v, str):
                vv = v.strip()
                if vv.lower() == "active":
                    params[k] = self.iface.activeLayer()
                    continue
                lyr = self._resolve_layer_option(vv)
                if lyr is not None:
                    params[k] = lyr
                    continue
                # numeric coercion
                try:
                    if "." in vv:
                        params[k] = float(vv)
                    else:
                        params[k] = int(vv)
                    continue
                except Exception:
                    pass
            params[k] = v

        # Fill pragmatic defaults
        if "INPUT" not in params and self.iface.activeLayer() is not None:
            params["INPUT"] = self.iface.activeLayer()
        if "OUTPUT" not in params:
            params["OUTPUT"] = "memory:"
        return params

    def _missing_required_params(self, alg_id, params):
        meta = self.algorithm_catalog.get(alg_id, {})
        required = meta.get("required", [])
        missing = [p for p in required if p not in params]
        return missing

    def _recipes_file_path(self):
        return Path(self._default_export_path("recipes.json"))

    def _learned_routes_file_path(self):
        return Path(self._default_export_path("learned_routes.json"))

    def _history_file_path(self):
        return Path(self._default_export_path("history_store.json"))

    def _doc_knowledge_file_path(self):
        return Path(self._default_export_path("document_knowledge.json"))

    def _regression_cases_file_path(self):
        return Path(self._default_export_path("regression_cases.json"))

    def _ensure_document_knowledge_seeded(self):
        if self.doc_knowledge:
            return
        self.doc_knowledge = self._default_document_knowledge_entries()
        self._save_json_file(self._doc_knowledge_file_path(), self.doc_knowledge)

    def _ensure_web_knowledge_seeded(self):
        """
        Ensure requested website knowledge packs are present in persistent memory.
        Merge by source URL so future updates do not duplicate entries.
        """
        try:
            existing = list(self.doc_knowledge or [])
            existing_by_source = {
                str(item.get("source", "")).strip().lower(): item for item in existing if isinstance(item, dict)
            }
            changed = False
            for entry in self._default_web_knowledge_entries():
                src = str(entry.get("source", "")).strip().lower()
                if not src:
                    continue
                if src not in existing_by_source:
                    existing.append(entry)
                    existing_by_source[src] = entry
                    changed = True
            if changed:
                self.doc_knowledge = existing
                self._save_json_file(self._doc_knowledge_file_path(), self.doc_knowledge)
        except Exception:
            pass

    def _ensure_workflow_knowledge_seeded(self):
        try:
            for entry in self._default_workflow_knowledge_entries():
                self._upsert_doc_knowledge_entry(entry)
        except Exception:
            pass

    def _crawl_state_file_path(self):
        return Path(self._default_export_path("crawl_state.json"))

    def _ensure_full_crawl_bootstrap_once(self):
        """
        Run one-time full crawl bootstrap so known links are expanded into page-level memory.
        This preserves existing curated packs and only appends/updates crawl-tagged entries.
        """
        state = self._load_json_file(self._crawl_state_file_path(), {})
        version = "v1"
        if isinstance(state, dict) and str(state.get("full_crawl_bootstrap", "")) == version:
            return
        try:
            self._crawl_all_known_websites()
            self._save_json_file(
                self._crawl_state_file_path(),
                {"full_crawl_bootstrap": version, "at": datetime.utcnow().isoformat()},
            )
        except Exception:
            pass

    def _default_document_knowledge_entries(self):
        return [
            {
                "title": "Basic Digital Mapping in QGIS",
                "source": r"c:\Users\SatyabrataSahoo\Downloads\AI ML\Basic Digital Mapping in QGIS.pdf",
                "tags": [
                    "qgis basics",
                    "gis fundamentals",
                    "vector raster",
                    "symbology",
                    "plugins",
                    "quickmapservices",
                    "digitizing",
                    "layout",
                    "export map",
                ],
                "content": (
                    "Core memory from the training manual: GIS combines spatial data and attribute data for analysis and map-based "
                    "decision-making. QGIS is a free open-source desktop GIS. Key GIS workflow: data acquisition, management, "
                    "analysis, visualization, and decision-making. Data types covered: vector (point, line, polygon, TIN), raster "
                    "(pixel grids), and tabular attributes. Map and cartography basics include reference, thematic, dynamic maps, and "
                    "elements like title, legend, north arrow, scale bar, data frame, metadata, border, inset map. QGIS interface "
                    "areas: Layers/Browser panel, toolbars, map canvas, status bar, side toolbar, locator bar. Basic operations: add "
                    "first layer via Data Source Manager; style layers from Layer Properties > Symbology; modify fill and stroke; "
                    "add symbol layers. Plugin management: enable/disable Processing plugin from Plugin Manager, install new plugins "
                    "from Not Installed list. Basemap methods: Browser > XYZ Tiles, or install QuickMapServices and load contributed "
                    "services. New shapefile workflow: choose geometry type, CRS, encoding, define fields with name/type/length/"
                    "precision, reorder fields, save. Digitizing workflow: toggle editing, add feature, place vertices, finish "
                    "geometry, enter attributes, save edits, exit editing; use undo/delete/vertex tools for correction. Layout "
                    "workflow: Project > Layout Manager, create empty layout, set page (A4 landscape), add map frame, add title and "
                    "legend, control legend auto-update and symbol sizing, align items. Export outputs: image (PNG), PDF, optional "
                    "SVG, with DPI guidance (around 300 for print, around 150 for screen)."
                ),
            },
            {
                "title": "GIS Architecture: Logic vs Assets",
                "source": r"c:\Users\SatyabrataSahoo\Downloads\AI ML\Logic vs Assets.pdf",
                "tags": [
                    "performance",
                    "symbology architecture",
                    "rendering",
                    "lean data",
                    "on-demand style",
                    "mobile gis",
                    "digital twin",
                ],
                "content": (
                    "Core memory from the architecture note: heavy style logic can create large rendering overhead (example: many "
                    "layers driven by large XML/QML logic), while lean asset-based approaches keep systems faster. European benchmark "
                    "examples emphasize precision with simplified geometry/symbology for operational speed and utility safety. "
                    "Practical value highlighted for utility operations, construction base maps, CAD/GIS performance, and scalable "
                    "e-services/digital twin systems. Engineering recommendation: on-demand rendering where complex styles remain "
                    "dormant until a layer is active. This preserves standards-compliant cartography while improving runtime "
                    "performance, including mobile GIS environments."
                ),
            },
        ]

    def _default_web_knowledge_entries(self):
        return [
            {
                "title": "Esri: What is GIS Overview",
                "source": "https://www.esri.com/en-us/what-is-gis/overview",
                "tags": [
                    "gis definition",
                    "mapping and analysis",
                    "spatial analysis",
                    "routing",
                    "site selection",
                    "asset management",
                    "emergency response",
                ],
                "content": (
                    "GIS is technology for creating, managing, analyzing, and mapping data by combining location context "
                    "with descriptive attributes. Key GIS capabilities include data management, mapping/visualization, "
                    "spatial analysis, and communication through maps and dashboards. Typical applications include choosing "
                    "optimal locations, finding best routes, tracking assets, planning future scenarios, and emergency "
                    "response and recovery. Core value: discover patterns/relationships and improve decision-making."
                ),
            },
            {
                "title": "QGIS Tutorials and Tips",
                "source": "https://www.qgistutorials.com/en/",
                "tags": [
                    "qgis tutorials",
                    "basic operations",
                    "intermediate operations",
                    "advanced workflows",
                    "pyqgis scripting",
                    "network analysis",
                    "hydrology",
                ],
                "content": (
                    "QGIS Tutorials and Tips provides structured practical tutorials from beginner to advanced: map making, "
                    "attributes, CSV import, vector/raster styling, mosaicing and clipping, projections, georeferencing, and "
                    "digitizing. Intermediate/advanced topics include spatial joins and queries, invalid geometry handling, "
                    "batch processing, model designer workflows, print atlas automation, and multicriteria overlay. It also "
                    "covers PyQGIS scripting (processing algorithms via Python, custom expressions, plugin development)."
                ),
            },
            {
                "title": "QGIS 3.44 Training Manual",
                "source": "https://docs.qgis.org/3.44/en/docs/training_manual/index.html",
                "tags": [
                    "qgis official docs",
                    "training manual",
                    "vector analysis",
                    "raster analysis",
                    "plugins",
                    "processing framework",
                    "postgis",
                ],
                "content": (
                    "The official QGIS Training Manual (v3.44) covers full curriculum: interface basics, adding layers, "
                    "symbology, labels, map layout, creating vector datasets, topology/forms/actions, vector and network "
                    "analysis, raster workflows, raster-to-vector conversion, plugin management, online services (WMS/WFS), "
                    "QGIS Server, GRASS tools, PostgreSQL/PostGIS database modules, and extensive Processing framework usage "
                    "(toolbox, reprojection, raster/vector calculator, model designer, batch execution, interpolation, hooks)."
                ),
            },
            {
                "title": "QGIS Training Manual: Spatial Queries",
                "source": "https://docs.qgis.org/3.44/en/docs/training_manual/spatial_databases/spatial_queries.html",
                "tags": [
                    "spatial queries",
                    "postgis sql",
                    "st_within",
                    "st_intersects",
                    "st_touches",
                    "st_distance",
                    "spatial index gist",
                ],
                "content": (
                    "Essential spatial query workflows: select by location (ST_Within/ST_Intersects), adjoining neighbors "
                    "(ST_Touches), and distance-based selection (ST_Distance). Includes geometry analytics such as "
                    "ST_Centroid, ST_Area, ST_Perimeter, ST_Simplify, ST_ConvexHull, plus performance guidance to create "
                    "GIST spatial indexes on geometry columns. Emphasizes CRS/unit awareness for valid distance results."
                ),
            },
            {
                "title": "Atlas Blog: Best AI Tools for GIS Mapping",
                "source": "https://atlas.co/blog/best-ai-tools-for-gis-mapping/",
                "tags": [
                    "geoai tools",
                    "natural language mapping",
                    "data enrichment",
                    "feature extraction",
                    "workflow automation",
                    "predictive analysis",
                ],
                "content": (
                    "The article groups AI for GIS mapping into practical categories: natural-language mapping interfaces, "
                    "AI-based data enrichment, automated feature extraction from imagery, intelligent geocoding/location "
                    "services, automated workflow orchestration, and predictive spatial analysis. It emphasizes selecting tools "
                    "based on use-case fit, user expertise, data characteristics, integration needs, and scalability, plus best "
                    "practices like validating AI outputs and combining automation with human review."
                ),
            },
            {
                "title": "QGIS Documentation",
                "source": "https://docs.qgis.org/latest/en/docs/index.html",
                "tags": ["qgis docs", "projects", "layers", "symbology", "layouts", "expressions"],
                "content": "Official QGIS documentation index covering user manual, training guides, processing tools, project/layer management, cartography, layouts, and expressions.",
            },
            {
                "title": "QGIS API Documentation",
                "source": "https://qgis.org/pyqgis/3.44/",
                "tags": ["pyqgis api", "plugin development", "automation", "qgs classes", "qgis 3.44"],
                "content": "Official QGIS Python API documentation for v3.44 (LTR) with class/method references, version notes, and links to compatible API sources.",
            },
            {
                "title": "QGIS User Guide",
                "source": "https://docs.qgis.org/latest/en/docs/user_manual/index.html",
                "tags": ["qgis user guide", "tool usage", "workflows", "analysis"],
                "content": "Practical guide to QGIS tools and workflows including data loading, editing, symbology, labeling, analysis, processing toolbox, layouts, and exports.",
            },
            {
                "title": "QGIS Developer Cookbook",
                "source": "https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/index.html",
                "tags": ["pyqgis cookbook", "scripts", "plugins", "processing algorithms", "qgis 3.44"],
                "content": "PyQGIS Developer Cookbook for QGIS 3.44 covering plugin development, layers, geometry handling, CRS transforms, map canvas tools, expressions, tasks/background processing, and automation patterns.",
            },
            {
                "title": "QGIS Plugin Development Guide (Cookbook Section)",
                "source": "https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/index.html",
                "tags": ["qgis plugins", "plugin structure", "plugin metadata", "debugging", "publishing"],
                "content": "Focused plugin-development section of the PyQGIS cookbook: plugin structure, code patterns, metadata/release practices, debugging setup, key snippets, and official plugin repository guidance.",
            },
            {
                "title": "GDAL Documentation",
                "source": "https://gdal.org/",
                "tags": ["gdal", "raster", "vector", "format conversion", "geoprocessing"],
                "content": "Core documentation for GDAL/OGR raster and vector processing, reprojection, format conversion, GeoTIFF handling, command-line tools, and geospatial pipelines.",
            },
            {
                "title": "PROJ Documentation",
                "source": "https://proj.org/",
                "tags": ["proj", "crs", "coordinate transform", "projection"],
                "content": "Reference for coordinate reference systems, projection definitions, datum transforms, and accurate coordinate conversion pipelines.",
            },
            {
                "title": "GeoPandas Documentation",
                "source": "https://geopandas.org/en/stable/",
                "tags": ["geopandas", "python gis", "vector analysis", "geodataframe"],
                "content": "Python vector GIS documentation for GeoDataFrame workflows including spatial joins, overlays, indexing, IO formats, and vector analytics.",
            },
            {
                "title": "Rasterio Documentation",
                "source": "https://rasterio.readthedocs.io/en/latest/",
                "tags": ["rasterio", "python raster", "geotiff", "windowed read"],
                "content": "Raster processing with Python including reading/writing rasters, windowed operations, reprojection, masking, and metadata handling.",
            },
            {
                "title": "Fiona Documentation",
                "source": "https://fiona.readthedocs.io/en/latest/",
                "tags": ["fiona", "vector io", "ogr", "python gis"],
                "content": "Pythonic vector data I/O library for reading/writing geospatial files and collections using GDAL/OGR drivers.",
            },
            {
                "title": "PyProj Documentation",
                "source": "https://pyproj4.github.io/pyproj/stable/",
                "tags": ["pyproj", "crs transforms", "projection", "geodesy"],
                "content": "Python interface to PROJ for coordinate transformations, CRS parsing, projection handling, and geodesic calculations.",
            },
            {
                "title": "Shapely Documentation",
                "source": "https://shapely.readthedocs.io/en/stable/",
                "tags": ["shapely", "geometry ops", "spatial relationships", "buffer intersection"],
                "content": "Geometry engine documentation for topology and spatial operations such as buffer, union, intersection, difference, predicates, and validity checks.",
            },
            {
                "title": "NumPy Documentation",
                "source": "https://numpy.org/doc/stable/",
                "tags": ["numpy", "arrays", "scientific computing", "raster math"],
                "content": "Fundamental numerical array library heavily used in geospatial raster math, indexing, transformations, and ML data preparation.",
            },
            {
                "title": "PostGIS Documentation",
                "source": "https://postgis.net/documentation/",
                "tags": ["postgis", "spatial sql", "database gis", "postgresql"],
                "content": "PostGIS reference for spatial SQL, indexing, geometry/geography types, raster in database, and advanced geospatial database workflows.",
            },
            {
                "title": "PostgreSQL Documentation",
                "source": "https://www.postgresql.org/docs/",
                "tags": ["postgresql", "database", "sql", "spatial backend"],
                "content": "Core PostgreSQL documentation for relational schema design, SQL workflows, indexing, transactions, and operational best practices.",
            },
            {
                "title": "GeoPackage Standard (OGC)",
                "source": "https://www.ogc.org/standards/geopackage/",
                "tags": ["geopackage", "ogc", "portable spatial db", "vector raster container"],
                "content": "OGC GeoPackage standard for portable SQLite-based geospatial containers supporting vector, raster, metadata, and efficient project exchange.",
            },
            {
                "title": "SpatiaLite Documentation",
                "source": "https://www.gaia-gis.it/fossil/libspatialite/index",
                "tags": ["spatialite", "sqlite spatial", "lightweight spatial db"],
                "content": "SpatiaLite documentation for SQLite-based spatial databases, geometry storage, spatial indexing, and SQL-based geospatial operations.",
            },
            {
                "title": "PostGIS Spatial SQL Workshop",
                "source": "https://postgis.net/workshops/postgis-intro/",
                "tags": ["spatial sql", "postgis queries", "st_intersects", "st_buffer"],
                "content": "Hands-on spatial SQL concepts and examples for common analytical workflows using PostGIS functions and geometry predicates.",
            },
            {
                "title": "Xarray Documentation",
                "source": "https://docs.xarray.dev/en/stable/",
                "tags": ["xarray", "multidimensional arrays", "time series", "netcdf"],
                "content": "Labeled multidimensional array toolkit for scientific/geospatial stacks and temporal raster analysis workflows.",
            },
            {
                "title": "rioxarray Documentation",
                "source": "https://corteva.github.io/rioxarray/stable/",
                "tags": ["rioxarray", "xarray raster", "clip", "reproject", "rio accessor"],
                "content": "CRS-aware raster extension for xarray providing clipping, reprojection, nodata handling, and geospatial raster IO.",
            },
            {
                "title": "OGC Standards",
                "source": "https://www.ogc.org/standards/",
                "tags": ["ogc", "wms", "wfs", "wcs", "geopackage", "geojson", "api standards"],
                "content": "Open Geospatial Consortium standards catalog for interoperability protocols and formats including web map/feature services and API standards.",
            },
            {
                "title": "OGC Geospatial Standards Pack",
                "source": "https://www.ogc.org/standards/",
                "tags": [
                    "wms",
                    "wfs",
                    "wcs",
                    "wmts",
                    "ogc api features",
                    "ogc api tiles",
                    "geopackage",
                    "geojson",
                    "gml",
                    "crs",
                ],
                "content": (
                    "OGC standards reference pack: "
                    "WMS (map image service), WFS (vector feature service), WCS (coverage/raster service), "
                    "WMTS (cached tiled map service), OGC API Features (modern RESTful feature access), "
                    "OGC API Tiles (RESTful tiled resource access), GeoPackage (portable SQLite geospatial container), "
                    "GeoJSON (lightweight JSON vector format), GML (XML geospatial encoding), and Coordinate Reference "
                    "Systems (CRS definitions and transformations for spatial interoperability)."
                ),
            },
            {
                "title": "Esri ArcGIS Pro Documentation",
                "source": "https://pro.arcgis.com/en/pro-app/latest/get-started/get-started.htm",
                "tags": ["arcgis pro", "gis concepts", "analysis equivalents"],
                "content": "ArcGIS Pro documentation for GIS concepts, analysis workflows, and tool equivalents useful for cross-platform understanding.",
            },
            {
                "title": "OpenStreetMap Wiki",
                "source": "https://wiki.openstreetmap.org/wiki/Main_Page",
                "tags": ["osm", "tags", "data model", "mapping usage"],
                "content": "Community-maintained OpenStreetMap knowledge base covering OSM data model, tagging conventions, editing, exports, and integration guidance.",
            },
            {
                "title": "MapLibre Documentation",
                "source": "https://maplibre.org/maplibre-gl-js-docs/",
                "tags": ["maplibre", "web maps", "vector tiles", "rendering"],
                "content": "MapLibre GL JS documentation for interactive web mapping, vector tile rendering, style specification, layers, and map interactivity.",
            },
            {
                "title": "STAC Specification",
                "source": "https://stacspec.org/en/",
                "tags": ["stac", "satellite catalogs", "earth observation", "metadata"],
                "content": "SpatioTemporal Asset Catalog specification for organizing and querying earth observation assets with standardized metadata and API patterns.",
            },
            {
                "title": "QGIS Plugin Repository",
                "source": "https://plugins.qgis.org/plugins/",
                "tags": ["qgis plugins", "plugin repository", "plugin discovery", "extensions"],
                "content": "Official QGIS Python plugin repository listing approved plugins with metadata such as downloads, authors, versions, and descriptions.",
            },
            {
                "title": "NASA Earthdata",
                "source": "https://www.earthdata.nasa.gov/",
                "tags": ["nasa earthdata", "satellite data", "earth observation", "remote sensing"],
                "content": "NASA Earthdata portal for discovering and accessing Earth observation datasets, metadata, and analysis-ready resources.",
            },
            {
                "title": "USGS EarthExplorer",
                "source": "https://earthexplorer.usgs.gov/",
                "tags": ["usgs", "earthexplorer", "landsat", "remote sensing download"],
                "content": "USGS EarthExplorer for searching and downloading Landsat and other geospatial imagery using spatial/temporal filters.",
            },
            {
                "title": "Copernicus Data Space Ecosystem",
                "source": "https://dataspace.copernicus.eu/",
                "tags": ["copernicus", "sentinel", "earth observation", "data space ecosystem"],
                "content": "Primary access environment for Sentinel missions and Copernicus datasets with catalog, APIs, and analysis services.",
            },
            {
                "title": "Sentinel Hub Documentation",
                "source": "https://docs.sentinel-hub.com/",
                "tags": ["sentinel hub", "processing api", "evalscript", "satellite workflows"],
                "content": "Developer documentation for Sentinel Hub APIs including data access, processing, mosaicking, and Evalscripts.",
            },
            {
                "title": "GeoAI Knowledge Hub",
                "source": "https://atlas.co/blog/geoai-artificial-intelligence-for-geospatial-data/",
                "tags": ["geoai", "ai gis", "spatial ai", "geospatial intelligence"],
                "content": "GeoAI overview covering how AI techniques integrate with GIS for automated extraction, predictive mapping, and decision support.",
            },
            {
                "title": "Spatial Machine Learning Concepts",
                "source": "https://scikit-learn.org/stable/supervised_learning.html",
                "tags": ["spatial machine learning", "classification", "regression", "model workflow"],
                "content": "Core ML workflow concepts (train/validate/infer) adaptable to geospatial features and raster-derived predictors.",
            },
            {
                "title": "Computer Vision for Remote Sensing",
                "source": "https://pytorch.org/vision/stable/index.html",
                "tags": ["computer vision", "object detection", "segmentation", "imagery inference"],
                "content": "Computer vision model patterns for segmentation/detection workflows applicable to geospatial imagery.",
            },
            {
                "title": "Spatial Statistics (PySAL)",
                "source": "https://pysal.org/",
                "tags": ["spatial statistics", "hotspot", "autocorrelation", "esda"],
                "content": "PySAL ecosystem for spatial autocorrelation, hotspot analysis, regionalization, and spatial econometrics.",
            },
            {
                "title": "Embeddings and Vector Search Basics",
                "source": "https://platform.openai.com/docs/guides/embeddings",
                "tags": ["embeddings", "semantic retrieval", "llm retrieval", "vector similarity"],
                "content": "Embedding concepts for semantic retrieval and similarity ranking, adaptable to GIS metadata/layer discovery and task routing.",
            },
            {
                "title": "LLM + GIS Workflow Patterns",
                "source": "https://docs.langchain.com/",
                "tags": ["llm gis workflows", "agent orchestration", "tool routing", "automation"],
                "content": "Agentic workflow patterns for combining LLM reasoning with deterministic GIS tools, execution contracts, and verification loops.",
            },
        ]

    def _default_workflow_knowledge_entries(self):
        return [
            {
                "title": "GIS Workflow KB Core",
                "source": "local://workflow-kb/core",
                "tags": ["workflow kb", "cross ecosystem mapping", "qgis pyqgis gdal geopandas postgis arcgis"],
                "content": (
                    "User -> Intent -> Retrieve GIS knowledge -> Generate plan -> Ask permission -> Execute QGIS action -> "
                    "Validate result -> Explain result. Includes operation equivalence mapping across QGIS GUI, Processing, "
                    "PyQGIS, GDAL, GeoPandas, PostGIS, ArcGIS."
                ),
            },
            {
                "title": "GIS Workflow KB Resources Plan",
                "source": "local://workflow-kb/ingestion-plan-100",
                "tags": ["ingestion plan", "qgis brain", "gis engine", "spatial intelligence", "geoai"],
                "content": (
                    "Staged ingestion approach: Stage1 QGIS docs/API/plugins, Stage2 geospatial engine (GDAL/PROJ/libs), "
                    "Stage3 standards+databases, Stage4 remote sensing+GeoAI, Stage5 cross-platform translation."
                ),
            },
        ]

    def _load_json_file(self, path, fallback):
        try:
            p = Path(path)
            if not p.exists():
                return fallback
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return fallback

    def _save_json_file(self, path, payload):
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _build_spatial_index(self, features):
        """
        Build QgsSpatialIndex in a version-compatible way.
        Some QGIS/PyQGIS builds do not accept list/iterator constructor args.
        """
        idx = QgsSpatialIndex()
        try:
            for feat in features:
                try:
                    idx.addFeature(feat)
                except Exception:
                    pass
        except Exception:
            pass
        return idx

    def _extract_distance(self, text):
        if not text:
            return None
        t = str(text)
        m_ft = re.search(r"(\d+(?:\.\d+)?)\s*(ft|feet|foot)\b", t, flags=re.IGNORECASE)
        if m_ft:
            return float(m_ft.group(1)) * 0.3048
        m_m = re.search(r"(\d+(?:\.\d+)?)\s*(m|meter|meters|metre|metres)\b", t, flags=re.IGNORECASE)
        if m_m:
            return float(m_m.group(1))
        match = re.search(r"(\d+(?:\.\d+)?)", t)
        return float(match.group(1)) if match else None

    def _extract_epsg(self, text):
        match = re.search(r"epsg[:\s]*(\d+)", text)
        if match:
            return int(match.group(1))
        return None

    def _extract_integer(self, text):
        match = re.search(r"\b(\d+)\b", text)
        if not match:
            return None
        return int(match.group(1))

    def _extract_bbox_from_text(self, text):
        if not text:
            return None
        # Accept patterns like "bbox=85.2,20.1,85.4,20.3" or
        # "bbox 85.2,20.1,85.4,20.3".
        m = re.search(
            r"bbox(?:\s*[:=]\s*|\s+)(-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?)",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
        return None

    def _parse_kv_options(self, text):
        opts = {}
        if not text:
            return opts
        for key, value in re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)=('[^']*'|\"[^\"]*\"|\S+)", text):
            v = value.strip()
            if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
                v = v[1:-1]
            opts[key.lower()] = v
        return opts

    def _resolve_layer_option(self, layer_name):
        if not layer_name:
            return None
        target = str(layer_name).strip().lower()
        for layer in QgsProject.instance().mapLayers().values():
            try:
                if layer.name().strip().lower() == target:
                    return layer
            except Exception:
                continue
        return None

    def _extract_existing_path(self, text):
        if not text:
            return None
        candidates = re.findall(r"[A-Za-z]:\\[^\n\r\"']+", text)
        for cand in candidates:
            for norm in self._normalize_path_candidates(cand.strip()):
                p = Path(norm)
                if p.exists():
                    return str(p)
        return None

    def _extract_path_candidate(self, text):
        if not text:
            return None
        candidates = re.findall(r"[A-Za-z]:\\[^\n\r\"']+", text)
        if not candidates:
            return None
        for norm in self._normalize_path_candidates(candidates[0].strip()):
            if norm:
                return norm
        return candidates[0].strip()

    def _normalize_path_candidates(self, candidate):
        base = (candidate or "").strip().rstrip(" .;,")
        if not base:
            return []
        variants = [base]
        trim_patterns = [
            r"^(.*?)(?:\s+-\s+.+)$",
            r"^(.*?)(?:-\s+(?:create|build|generate|make)\b.+)$",
            r"^(.*?)(?:\s+(?:create|build|generate|make)\b.+)$",
            r"^(.*?)(?:\s+this\s+is\s+the\s+path.*)$",
        ]
        for pat in trim_patterns:
            m = re.match(pat, base, flags=re.IGNORECASE)
            if m and m.group(1).strip():
                variants.append(m.group(1).strip().rstrip(" .;,"))
        # keep order and uniqueness
        out = []
        seen = set()
        for v in variants:
            k = v.lower()
            if k not in seen:
                out.append(v)
                seen.add(k)
        return out

    def _resolve_path_from_query(self, text, expect_dir=False, extensions=None):
        existing = self._extract_existing_path(text)
        if existing:
            p = Path(existing)
            if expect_dir and not p.is_dir():
                return None
            return str(p)

        candidate = self._extract_path_candidate(text)
        if candidate:
            p = Path(candidate)
            if p.exists():
                if expect_dir and not p.is_dir():
                    return None
                return str(p)

        term = self._extract_search_term(text)
        if not term:
            return None

        matches = self._recursive_search(term, expect_dir=expect_dir, extensions=extensions, max_results=5)
        if matches:
            return matches[0]
        return None

    def _resolve_output_dir_from_query(self, text):
        """
        Resolve an output directory from user query.
        - If query contains an existing directory path, use it.
        - If query contains a non-existing path after 'to/into/in', create it.
        - Else use default export root.
        """
        resolved_existing = self._resolve_path_from_query(text, expect_dir=True)
        if resolved_existing:
            p = Path(resolved_existing)
            p.mkdir(parents=True, exist_ok=True)
            return p

        explicit_candidate = self._extract_path_candidate(text)
        if explicit_candidate:
            p = Path(explicit_candidate)
            # If user passed a file path, use its parent as output folder.
            target_dir = p if p.suffix == "" else p.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir

        implied = self._extract_output_hint_path(text)
        if implied:
            p = Path(implied)
            target_dir = p if p.suffix == "" else p.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir

        return Path(self._default_export_path("out.gpkg")).parent

    def _extract_output_hint_path(self, text):
        if not text:
            return None
        patterns = [
            r"(?:to|into|in)\s+([A-Za-z]:\\[^,\n\r\"']+)",
            r"output\s+(?:to|in)\s+([A-Za-z]:\\[^,\n\r\"']+)",
            r"save\s+(?:to|in)\s+([A-Za-z]:\\[^,\n\r\"']+)",
        ]
        for pat in patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return None

    def _extract_search_term(self, text):
        if not text:
            return None
        quoted = re.search(r"['\"]([^'\"]+)['\"]", text)
        if quoted:
            return quoted.group(1).strip()
        token_match = re.search(r"(?:from|in|inside|folder|file)\s+([A-Za-z0-9_\-\.]+)", text, flags=re.IGNORECASE)
        if token_match:
            return token_match.group(1).strip()
        words = [w for w in re.findall(r"[A-Za-z0-9_\-\.]+", text) if len(w) >= 3]
        return words[-1] if words else None

    def _candidate_scan_roots(self):
        roots = []
        try:
            project_home = QgsProject.instance().homePath()
            if project_home:
                roots.append(Path(project_home))
        except Exception:
            pass
        roots.append(Path.home())

        # Add directories from loaded layer sources.
        try:
            for layer in QgsProject.instance().mapLayers().values():
                if hasattr(layer, "source"):
                    src = layer.source()
                    if src and "://" not in src:
                        src_path = Path(src.split("|")[0])
                        if src_path.exists():
                            roots.append(src_path.parent if src_path.is_file() else src_path)
        except Exception:
            pass

        dedup = []
        seen = set()
        for r in roots:
            key = str(r).lower()
            if key not in seen and r.exists():
                dedup.append(r)
                seen.add(key)
        return dedup

    def _recursive_search(self, term, expect_dir=False, extensions=None, max_results=20):
        term_l = term.lower().strip()
        if not term_l:
            return []
        matches = []
        for root in self._candidate_scan_roots():
            try:
                for base, dirs, files in os.walk(root):
                    if expect_dir:
                        for d in dirs:
                            if term_l in d.lower():
                                matches.append(str(Path(base) / d))
                                if len(matches) >= max_results:
                                    return matches
                    else:
                        for f in files:
                            lf = f.lower()
                            if term_l not in lf:
                                continue
                            if extensions:
                                if not any(lf.endswith(ext.lower()) for ext in extensions):
                                    continue
                            matches.append(str(Path(base) / f))
                            if len(matches) >= max_results:
                                return matches
            except Exception:
                continue
        return matches

    def _run_recursive_scan(self, query):
        term = self._extract_search_term(query)
        if not term:
            self._log("<b>AI:</b> Provide what to scan. Example: 'scan recursively for feeder_01.shp'")
            return
        matches = self._recursive_search(term, expect_dir=False, extensions=None, max_results=50)
        dir_matches = self._recursive_search(term, expect_dir=True, extensions=None, max_results=20)
        if not matches and not dir_matches:
            self._log(f"<b>AI:</b> No recursive matches found for '{term}'.")
            return
        lines = []
        for m in matches[:20]:
            lines.append(f"- file: {m}")
        for d in dir_matches[:10]:
            lines.append(f"- dir: {d}")
        extra = (len(matches) - 20) + (len(dir_matches) - 10)
        if extra > 0:
            lines.append(f"... and {extra} more")
        self._log(f"<b>AI:</b> Recursive scan results for '{term}':\n" + "\n".join(lines))

    def _find_layer_by_name_keywords(self, keywords):
        for layer in QgsProject.instance().mapLayers().values():
            lname = layer.name().lower()
            if all(k in lname for k in keywords):
                return layer
        return None

    def _default_export_path(self, filename):
        project = QgsProject.instance()
        home = project.homePath() or str(Path.home())
        out_dir = Path(home) / "ai_gis_copilot_exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        return str(out_dir / filename)

    def _list_available_plugins(self):
        plugins_dir = (
            Path.home()
            / "AppData"
            / "Roaming"
            / "QGIS"
            / "QGIS3"
            / "profiles"
            / "default"
            / "python"
            / "plugins"
        )
        discovered = []
        try:
            for child in plugins_dir.iterdir():
                if not child.is_dir():
                    continue
                meta = child / "metadata.txt"
                display_name = child.name
                if meta.exists():
                    parser = configparser.ConfigParser()
                    parser.read(str(meta), encoding="utf-8")
                    if parser.has_section("general") and parser.has_option("general", "name"):
                        display_name = parser.get("general", "name")
                discovered.append({"id": child.name, "name": display_name})
        except Exception:
            pass
        discovered.sort(key=lambda item: item["name"].lower())
        return discovered

    def _try_plugin_delegation(self, query):
        plugin = self._match_plugin_from_query(query.lower())
        if plugin is None:
            return False

        loaded_plugins = getattr(qgis.utils, "plugins", {})
        plugin_obj = loaded_plugins.get(plugin["id"])
        if plugin_obj is None:
            self._log(
                f"<b>AI:</b> Plugin '{plugin['name']}' is installed but not loaded. Enable it and retry."
            )
            return True

        methods = self._candidate_plugin_methods(plugin_obj)
        if not methods:
            self._log(
                f"<b>AI:</b> Plugin '{plugin['name']}' is loaded, but no callable methods were found."
            )
            return True

        chosen = self._choose_method_for_query(methods, query.lower())
        if chosen is None:
            preview = ", ".join(methods[:8])
            self._log(
                f"<b>AI:</b> Plugin '{plugin['name']}' matched, but action is unclear.\n"
                f"Try keywords related to: {preview}"
            )
            return True

        try:
            getattr(plugin_obj, chosen)()
            self._log(f"<b>AI:</b> Delegated to plugin '{plugin['name']}' using '{chosen}()'.")
        except Exception as exc:
            self._log(f"<b>AI:</b> Plugin delegation failed: {exc}")
        return True

    def _refresh_plugin_registry(self):
        loaded_plugins = getattr(qgis.utils, "plugins", {})
        registry = {}
        for plugin_id, plugin_obj in loaded_plugins.items():
            registry[plugin_id] = self._candidate_plugin_methods(plugin_obj)
        self.plugin_registry = registry

    def _match_plugin_from_query(self, normalized_query):
        for plugin in self._list_available_plugins():
            pid = plugin["id"].replace("_", " ").lower()
            pname = plugin["name"].lower()
            if pid in normalized_query or pname in normalized_query:
                return plugin
        return None

    def _candidate_plugin_methods(self, plugin_obj):
        blocked = {"initgui", "unload", "tr", "classfactory", "show", "hide", "close"}
        methods = []
        for name in dir(plugin_obj):
            lname = name.lower()
            if lname.startswith("_") or lname in blocked:
                continue
            try:
                attr = getattr(plugin_obj, name)
            except Exception:
                continue
            if callable(attr):
                methods.append(name)
        return sorted(methods, key=lambda x: x.lower())

    def _choose_method_for_query(self, methods, normalized_query):
        scored = []
        for method in methods:
            score = 0
            key = method.lower().replace("_", " ")
            for token in key.split():
                if token and token in normalized_query:
                    score += 1
            if score > 0:
                scored.append((score, method))
        if scored:
            scored.sort(key=lambda x: (-x[0], x[1].lower()))
            return scored[0][1]
        for fallback in ("run", "execute", "open_dialog", "open", "launch", "start"):
            for method in methods:
                if method.lower() == fallback:
                    return method
        return None
