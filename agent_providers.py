"""
Multi-provider LLM client for AI GIS Copilot's agent mode.

Supports three FREE backends, selectable at runtime:
  - "gemini": Google Gemini API free tier (https://ai.google.dev - free API key)
  - "groq":   Groq free tier, OpenAI-compatible (https://console.groq.com - free API key)
  - "ollama": Fully local, no key, no internet (https://ollama.com)

All three are used through plain urllib HTTP calls -- no extra pip packages
required, so the plugin keeps working inside QGIS's bundled Python with zero
install steps for the team.

Every backend is normalized to the same interface so the agent loop never
has to know which provider it is talking to:

    result = provider.chat(system_prompt, messages, tool_schemas)
    result.kind          -> "text" | "tool_calls"
    result.text          -> final answer (kind == "text")
    result.tool_calls     -> [{"id", "name", "arguments": dict}, ...]

`messages` is a normalized list of dicts:
    {"role": "user", "content": "..."}
    {"role": "assistant", "tool_calls": [...], "content": ""}
    {"role": "tool", "name": "...", "content": "<json string result>"}

`tool_schemas` is a list of:
    {"name": str, "description": str, "parameters": <JSON Schema object>}
"""

import json
import urllib.error
import urllib.request


class ProviderError(Exception):
    pass


class ChatResult:
    def __init__(self, kind, text=None, tool_calls=None):
        self.kind = kind  # "text" or "tool_calls"
        self.text = text
        self.tool_calls = tool_calls or []


def _http_post_json(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Providers behind Cloudflare (Groq, etc.) block Python's default
            # "Python-urllib/x.y" User-Agent as bot traffic (HTTP 403, error
            # code 1010) -- a real, browser-like UA avoids that entirely.
            "User-Agent": "Mozilla/5.0 (compatible; AI-GIS-Copilot/1.0)",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise ProviderError(f"HTTP {exc.code}: {detail[:600]}") from exc
    except Exception as exc:
        raise ProviderError(f"Request failed: {exc}") from exc


def _safe_json_loads(text, default=None):
    try:
        return json.loads(text)
    except Exception:
        return default if default is not None else {}


class BaseProvider:
    name = "base"

    def configured(self):
        raise NotImplementedError

    def chat(self, system_prompt, messages, tool_schemas):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenAI-compatible backends (Groq free tier, Ollama local) share one shape.
# ---------------------------------------------------------------------------
class _OpenAICompatibleProvider(BaseProvider):
    def __init__(self, base_url, model, api_key=""):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key or ""

    def configured(self):
        return bool(self.base_url and self.model)

    def _to_oa_tools(self, tool_schemas):
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for t in tool_schemas
        ]

    def _to_oa_messages(self, system_prompt, messages):
        out = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m["role"] == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.get("id", m["name"]),
                        "name": m["name"],
                        "content": m["content"],
                    }
                )
            elif m["role"] == "assistant" and m.get("tool_calls"):
                out.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc.get("id", tc["name"]),
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc.get("arguments", {})),
                                },
                            }
                            for tc in m["tool_calls"]
                        ],
                    }
                )
            else:
                out.append({"role": m["role"], "content": m.get("content", "")})
        return out

    def chat(self, system_prompt, messages, tool_schemas):
        if not self.configured():
            raise ProviderError(f"{self.name} is not configured (missing model/base_url).")
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": self._to_oa_messages(system_prompt, messages),
            "tools": self._to_oa_tools(tool_schemas),
            "tool_choice": "auto",
        }
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        decoded = _http_post_json(f"{self.base_url}/chat/completions", payload, headers)
        try:
            choice = decoded["choices"][0]["message"]
        except Exception as exc:
            raise ProviderError(f"Unexpected {self.name} response: {decoded}") from exc

        raw_calls = choice.get("tool_calls") or []
        if raw_calls:
            calls = []
            for tc in raw_calls:
                fn = tc.get("function", {})
                calls.append(
                    {
                        "id": tc.get("id", fn.get("name")),
                        "name": fn.get("name"),
                        "arguments": _safe_json_loads(fn.get("arguments", "{}"), {}),
                    }
                )
            return ChatResult("tool_calls", tool_calls=calls)
        return ChatResult("text", text=(choice.get("content") or "").strip())


class GroqProvider(_OpenAICompatibleProvider):
    """Groq free tier -- https://console.groq.com (free API key, generous rate limits)."""

    name = "groq"

    def __init__(self, api_key="", model="openai/gpt-oss-120b"):
        super().__init__("https://api.groq.com/openai/v1", model, api_key)

    def configured(self):
        return bool(self.api_key and self.model)


class OllamaProvider(_OpenAICompatibleProvider):
    """Fully local, free, no key, no internet -- https://ollama.com."""

    name = "ollama"

    def __init__(self, base_url="http://localhost:11434/v1", model="qwen2.5:7b-instruct"):
        super().__init__(base_url, model, api_key="")


# ---------------------------------------------------------------------------
# Gemini: different request/response shape (functionDeclarations / parts).
# ---------------------------------------------------------------------------
class GeminiProvider(BaseProvider):
    """Google Gemini free tier -- https://ai.google.dev (free API key)."""

    name = "gemini"

    def __init__(self, api_key="", model="gemini-flash-latest"):
        self.api_key = api_key or ""
        self.model = model

    def configured(self):
        return bool(self.api_key and self.model)

    def _to_gemini_tools(self, tool_schemas):
        return [
            {
                "functionDeclarations": [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    }
                    for t in tool_schemas
                ]
            }
        ]

    def _to_gemini_contents(self, messages):
        contents = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m.get("content", "")}]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                parts = []
                for tc in m["tool_calls"]:
                    part = {"functionCall": {"name": tc["name"], "args": tc.get("arguments", {})}}
                    sig = tc.get("thought_signature")
                    if sig:
                        part["thoughtSignature"] = sig
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": m.get("content", "")}]})
            elif m["role"] == "tool":
                # MUST be "user", not "function" -- Gemini's actual API
                # rejects "function" outright (confirmed against Google's
                # own official docs/codelabs, every real example uses
                # role: "user" for a functionResponse part). This was the
                # exact bug breaking every single agent request.
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": m["name"],
                                    "response": {"result": _safe_json_loads(m["content"], {"raw": m["content"]})},
                                }
                            }
                        ],
                    }
                )
        return contents

    def chat(self, system_prompt, messages, tool_schemas):
        if not self.configured():
            raise ProviderError("Gemini is not configured (missing API key).")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": self._to_gemini_contents(messages),
            "tools": self._to_gemini_tools(tool_schemas),
            "generationConfig": {"temperature": 0.1},
        }
        decoded = _http_post_json(url, payload)
        try:
            candidate = decoded["candidates"][0]
            parts = candidate.get("content", {}).get("parts", [])
        except Exception as exc:
            raise ProviderError(f"Unexpected Gemini response: {decoded}") from exc

        calls = []
        text_chunks = []
        for i, part in enumerate(parts):
            if "functionCall" in part:
                fc = part["functionCall"]
                call = {"id": f"call_{i}", "name": fc.get("name"), "arguments": fc.get("args", {}) or {}}
                # Gemini 3.x "thinking" models require this to be echoed
                # back verbatim on the functionCall part when replaying
                # history in the next request, or every follow-up request
                # is rejected with a 400. Confirmed against Google's
                # current docs -- this was previously discarded entirely.
                sig = part.get("thoughtSignature")
                if sig:
                    call["thought_signature"] = sig
                calls.append(call)
            elif "text" in part:
                text_chunks.append(part["text"])

        if calls:
            return ChatResult("tool_calls", tool_calls=calls)
        return ChatResult("text", text="".join(text_chunks).strip())


def build_provider(provider_name, settings):
    """settings: dict with keys gemini_key, groq_key, ollama_base_url, and per-provider model names."""
    provider_name = (provider_name or "").strip().lower()
    if provider_name == "gemini":
        return GeminiProvider(settings.get("gemini_key", ""), settings.get("gemini_model", "gemini-flash-latest"))
    if provider_name == "groq":
        return GroqProvider(settings.get("groq_key", ""), settings.get("groq_model", "openai/gpt-oss-120b"))
    return OllamaProvider(
        settings.get("ollama_base_url", "http://localhost:11434/v1"),
        settings.get("ollama_model", "qwen2.5:7b-instruct"),
    )
