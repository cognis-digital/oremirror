"""Opt-in AI expansion for oremirror (default OFF).

Reuses the Cognis shared-backend pattern: an OpenAI-compatible LOCAL endpoint
named via COGNIS_AI_* env vars. When disabled (the default) every function here
degrades to a deterministic, no-op result so planning stays reproducible and
offline. Standard library only (urllib / json / os / re).

The one capability exposed: expand a high-level application name (e.g.
"a typical kubernetes ingress stack") into a concrete list of image references
the operator can review before mirroring. The model NEVER mirrors anything; it
only proposes a list, which the human vets.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

PRESETS = {
    "uncensored-fleet": {
        "base_url": "http://127.0.0.1:8774/v1",
        "default_model": "Josiefied-Qwen3-8B-abliterated",
    },
    "cognis-code": {
        "base_url": "http://127.0.0.1:11434/v1",
        "default_model": "coder",
    },
}

DEFAULT_TIMEOUT = 60

_SYSTEM_PROMPT = (
    "You are a release engineer who knows the container images that real "
    "applications are composed of. Given a high-level application or stack "
    "name, list the concrete OCI image references (registry/repo:tag) that a "
    "site would need to mirror to run it air-gapped. Prefer pinned, widely "
    "used tags. Respond with a STRICT JSON array of strings (image refs) and "
    "nothing else. If you are unsure, return []."
)


class AIExpander:
    """Off by default; only enabled when a backend endpoint is configured."""

    def __init__(self, backend=None, endpoint=None, model=None, api_key=None,
                 timeout=None):
        self.backend = backend or os.environ.get("COGNIS_AI_BACKEND") or None
        preset = PRESETS.get(self.backend, {}) if self.backend else {}
        self.base_url = (endpoint or os.environ.get("COGNIS_AI_ENDPOINT")
                         or preset.get("base_url") or None)
        if self.base_url:
            self.base_url = self.base_url.rstrip("/")
        self.model = (model or os.environ.get("COGNIS_AI_MODEL")
                      or preset.get("default_model") or None)
        self.api_key = api_key or os.environ.get("COGNIS_AI_KEY") or None
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT

    def is_enabled(self) -> bool:
        return bool(self.base_url and self.model)

    def expand(self, description):
        """Return a list of proposed image-reference strings. Never raises."""
        if not self.is_enabled() or not str(description or "").strip():
            return []
        prompt = (
            "Application / stack to mirror:\n" + str(description).strip()
            + "\n\nReturn the JSON array of image references.")
        try:
            content = self._chat(_SYSTEM_PROMPT, prompt)
        except Exception:
            return []
        return _parse_string_array(content)

    def _chat(self, system_prompt, user_prompt):
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        obj = json.loads(raw)
        choices = obj.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content") or ""


def _strip_think(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _parse_string_array(content):
    """Pull a JSON array of strings out of model text. Never raises."""
    text = _strip_think(content or "")
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [s.strip() for s in parsed if isinstance(s, str) and s.strip()]


_default = None


def is_enabled():
    global _default
    if _default is None:
        _default = AIExpander()
    return _default.is_enabled()


def expand(description):
    global _default
    if _default is None:
        _default = AIExpander()
    return _default.expand(description)
