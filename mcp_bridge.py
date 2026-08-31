"""
Live binding to the gis-mcp server (https://github.com/mahdin75/gis-mcp).

Unlike agent_tools.py (which selectively reimplements specific gis-mcp
capabilities directly, with no extra process or protocol), this module
does the real thing the user asked for: spawns the actual gis-mcp server
as a subprocess and speaks the MCP protocol to it over HTTP, so every one
of its ~88 tools becomes available to the agent, not just the handful
that were manually ported.

This is entirely optional and opt-in (via /install gis-mcp, /bind gis-mcp
in the chat) -- when not bound, nothing about the plugin's behavior
changes from before this file existed.

Protocol sequence verified against a real running gis-mcp instance before
writing this client (not assumed from documentation):
  1. POST /mcp {"method": "initialize", ...} -> response arrives as
     text/event-stream (SSE-wrapped JSON-RPC, even though we posted plain
     JSON), and the response headers carry an Mcp-Session-Id that must be
     sent on every subsequent request.
  2. POST /mcp {"method": "notifications/initialized"} -- required before
     other calls will work.
  3. POST /mcp {"method": "tools/list"} -> real tool name/description/
     inputSchema for every tool the server exposes.
  4. POST /mcp {"method": "tools/call", "params": {"name", "arguments"}}
     -> real execution result.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def find_real_python_executable():
    """On many Windows QGIS installs, sys.executable inside the plugin's
    Python is QGIS's own application binary (qgis-bin.exe), not a usable
    Python interpreter -- passing it -m/pip arguments makes QGIS try to
    open those arguments as project files instead of running Python,
    which looks like QGIS hanging and opening a second instance of
    itself. Find an actual runnable python3/python executable instead,
    based on the well-known layout of QGIS's Windows packaging (a real
    python3.exe sits alongside qgis-bin.exe, or under sys.prefix)."""
    candidates = []
    exe_dir = os.path.dirname(sys.executable)
    exe_name = os.path.basename(sys.executable).lower()
    if "python" in exe_name and "qgis" not in exe_name:
        candidates.append(sys.executable)
    for name in ("python3.exe", "python.exe", "python3", "python"):
        candidates.append(os.path.join(exe_dir, name))
    for name in ("python3.exe", "python.exe", "python3", "python"):
        candidates.append(os.path.join(sys.prefix, name))
        candidates.append(os.path.join(sys.prefix, "bin", name))
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise McpBridgeError(
        "Could not find a real Python executable distinct from the QGIS application binary. "
        "See the README for manual install instructions via the OSGeo4W Shell."
    )


class McpBridgeError(Exception):
    pass


def _parse_sse_or_json(raw_bytes, content_type):
    text = raw_bytes.decode("utf-8", errors="ignore")
    if "text/event-stream" in (content_type or ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise McpBridgeError(f"No data: line in SSE response: {text[:300]}")
    return json.loads(text)


class GisMcpBridge:
    def __init__(self, host="127.0.0.1", port=8811, startup_timeout=90):
        self.host = host
        self.port = port
        self.startup_timeout = startup_timeout
        self.process = None
        self.session_id = None
        self.tools = []  # raw MCP tool defs: [{"name","description","inputSchema"}, ...]
        self._log_lines = []
        self._log_thread = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def bound(self):
        return self.process is not None and self.process.poll() is None and self.session_id is not None

    def start_and_handshake(self):
        """Spawn the gis-mcp subprocess, wait for it to come up, and
        perform the full MCP handshake + tool discovery. Raises
        McpBridgeError with a clear reason on any failure -- never leaves
        a half-started subprocess running silently."""
        if self.process is not None and self.process.poll() is None:
            raise McpBridgeError("Already running -- call stop() first.")
        env_overrides = {
            "GIS_MCP_TRANSPORT": "http",
            "GIS_MCP_HOST": self.host,
            "GIS_MCP_PORT": str(self.port),
        }
        env = {**os.environ, **env_overrides}
        try:
            python_exe = find_real_python_executable()
        except McpBridgeError as exc:
            raise exc
        try:
            # NOT the bare "gis-mcp" console-script name (PATH problem --
            # see find_real_python_executable's docstring for the other,
            # worse problem this also avoids: sys.executable itself being
            # QGIS's own binary on Windows, not a real Python interpreter).
            self.process = subprocess.Popen(
                [python_exe, "-m", "gis_mcp.main"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError as exc:
            self.process = None
            raise McpBridgeError(
                "gis_mcp module not found in QGIS's Python. Run /install gis-mcp first, then restart QGIS."
            ) from exc

        # Read subprocess output continuously in a background thread rather
        # than only after it exits -- a *slow* first-time startup (common on
        # Windows: rasterio/geopandas/pysal are heavy C-extension imports,
        # and antivirus real-time scanning of freshly-installed site-packages
        # can add real delay) previously gave zero diagnostic information,
        # just a bare "could not reach server" with nothing useful after it.
        import threading

        def _drain_output():
            try:
                for line in self.process.stdout:
                    self._log_lines.append(line.rstrip("\n"))
                    if len(self._log_lines) > 200:
                        self._log_lines.pop(0)
            except Exception:
                pass

        self._log_thread = threading.Thread(target=_drain_output, daemon=True)
        self._log_thread.start()

        deadline = time.time() + self.startup_timeout
        last_error = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise McpBridgeError(
                    f"gis-mcp exited immediately (code {self.process.returncode}): "
                    + "\n".join(self._log_lines[-15:])
                )
            try:
                self._initialize()
                self._send_initialized_notification()
                self._discover_tools()
                return
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        recent_output = "\n".join(self._log_lines[-15:]) or "(no output captured -- it may still be importing dependencies)"
        self.stop()
        raise McpBridgeError(
            f"gis-mcp did not become ready within {self.startup_timeout}s: {last_error}\n"
            f"Recent gis-mcp output:\n{recent_output}"
        )

    def stop(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        self.session_id = None
        self.tools = []

    def _rpc(self, method, params, timeout=30):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "AI-GIS-Copilot/1.0",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.URLError as exc:
            raise McpBridgeError(f"Could not reach gis-mcp server: {exc}") from exc
        new_session = resp.headers.get("mcp-session-id")
        if new_session:
            self.session_id = new_session
        decoded = _parse_sse_or_json(resp.read(), resp.headers.get("content-type", ""))
        if "error" in decoded:
            raise McpBridgeError(f"gis-mcp error: {decoded['error']}")
        return decoded.get("result", {})

    def _initialize(self):
        self._rpc(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "ai-gis-copilot", "version": "1.0"}},
        )

    def _send_initialized_notification(self):
        payload = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": self.session_id or "",
        }
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)

    def _discover_tools(self):
        result = self._rpc("tools/list", {})
        self.tools = result.get("tools", [])

    def call_tool(self, name, arguments):
        if not self.bound:
            raise McpBridgeError("gis-mcp is not bound. Run /bind gis-mcp first.")
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content", [])
        texts = [part.get("text", "") for part in content if part.get("type") == "text"]
        combined = "\n".join(texts)
        try:
            return json.loads(combined)
        except Exception:
            return {"ok": True, "raw": combined}

    def tool_schemas_for_agent(self, prefix="gismcp__"):
        """Convert gis-mcp's raw MCP tool list into this plugin's own
        TOOL_SCHEMAS shape, prefixed to make their origin unambiguous in
        logs and to guarantee no collision with the plugin's own tool
        names. All bridged tools are treated as destructive (confirmation
        required) since gis-mcp's own schemas don't carry that flag and
        several of them write files."""
        out = []
        for t in self.tools:
            out.append(
                {
                    "name": f"{prefix}{t['name']}",
                    "description": f"[gis-mcp] {t.get('description', '')}",
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                    "destructive": True,
                }
            )
        return out


def install_gis_mcp():
    """Runs in the calling thread -- caller (dock) is responsible for not
    blocking the UI for longer than acceptable, same pattern as
    _install_pysal_dependencies. Uses find_real_python_executable(), not
    sys.executable directly -- see that function's docstring for why."""
    python_exe = find_real_python_executable()
    # subprocess.check_call() uses Popen(...).wait() internally, which
    # never drains stdout/stderr -- confirmed by direct reproduction that
    # this deadlocks once the child (pip installing shapely, geopandas,
    # rasterio, pyproj, pysal, fastmcp) writes more than the OS pipe
    # buffer holds, which a multi-package install does easily. This was
    # the actual cause of a real multi-hour hang, not a slow install.
    # subprocess.run() correctly uses communicate() to drain pipes while
    # waiting. A generous but finite timeout means even a genuine network
    # stall surfaces as a clear error instead of hanging forever again.
    result = subprocess.run(
        [python_exe, "-m", "pip", "install", "--user", "gis-mcp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pip exited with code {result.returncode}: {result.stdout[-800:]}")
