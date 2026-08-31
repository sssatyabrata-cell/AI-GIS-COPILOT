import json
import os
import urllib.error
import urllib.request


class LLMClient:
    def __init__(self):
        self.api_key = os.environ.get("AI_GIS_COPILOT_API_KEY", "").strip()
        self.base_url = os.environ.get("AI_GIS_COPILOT_BASE_URL", "http://localhost:11434/v1").strip()
        self.model = os.environ.get("AI_GIS_COPILOT_MODEL", "qwen2.5:7b-instruct").strip()

    def configured(self):
        return bool(self.base_url and self.model and (self._is_local_base_url() or self.api_key))

    def set_api_key(self, api_key):
        self.api_key = (api_key or "").strip()

    def set_base_url(self, base_url):
        self.base_url = (base_url or "").strip().rstrip("/")

    def set_model(self, model):
        self.model = (model or "").strip()

    def _is_local_base_url(self):
        b = (self.base_url or "").lower()
        return ("localhost" in b) or ("127.0.0.1" in b)

    def set_free_defaults(self):
        # Free-first local defaults (Ollama-compatible OpenAI API).
        self.base_url = "http://localhost:11434/v1"
        self.model = "qwen2.5:7b-instruct"
        self.api_key = ""

    def chat_json(self, system_prompt, user_prompt, temperature=0.0):
        if not self.configured():
            raise RuntimeError("LLM is not configured. Set API key/base URL/model first.")

        payload = {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}/chat/completions"

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        decoded = json.loads(body)
        text = decoded["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except Exception:
            # Some local models ignore JSON mode; extract first object-like block.
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(text[start : end + 1])
            raise RuntimeError(f"LLM did not return valid JSON: {text[:400]}")
