"""Attenborough Mode: turn analytics facts into a nature-documentary narration.

Fully local. Uses Ollama when reachable, else a built-in template engine.
"""
from __future__ import annotations

import json
import os
import urllib.request

OLLAMA_URL = os.environ.get("TOKMON_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("TOKMON_OLLAMA_MODEL")
DOC_ENGINE = os.environ.get("TOKMON_DOC_ENGINE", "auto")


def ollama_status(url: str | None = None) -> dict:
    """Probe the local Ollama server. Never raises."""
    base = (url or OLLAMA_URL).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m["name"] for m in data.get("models", [])]
    except Exception:
        return {"available": False, "url": base, "models": [], "model": None}
    model = OLLAMA_MODEL if OLLAMA_MODEL in models else (models[0] if models else None)
    return {"available": bool(models), "url": base, "models": models, "model": model}
