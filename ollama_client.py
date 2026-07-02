from __future__ import annotations

import os
from typing import Any

import requests


EMBED_URL = os.environ.get("CHATIFU_OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embeddings")
GENERATE_URL = os.environ.get("CHATIFU_DOC_GENERATE_URL", "http://127.0.0.1:11434/api/generate")
EMBED_MODEL = os.environ.get("CHATIFU_EMBED_MODEL", "nomic-embed-text")
GENERATE_MODEL = os.environ.get("CHATIFU_DOC_MODEL", "qwen3:14b")


def embed(text: str, timeout: int = 60) -> list[float]:
    res = requests.post(EMBED_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=timeout)
    res.raise_for_status()
    vector = res.json().get("embedding")
    if not isinstance(vector, list):
        raise RuntimeError("Ollama embedding response did not include an embedding list.")
    return [float(value) for value in vector]


def generate(prompt: str, model: str | None = None, timeout: int = 90, **options: Any) -> str:
    payload: dict[str, Any] = {"model": model or GENERATE_MODEL, "prompt": prompt, "stream": False}
    if options:
        payload["options"] = options
    res = requests.post(GENERATE_URL, json=payload, timeout=timeout)
    res.raise_for_status()
    return str(res.json().get("response", ""))


def ollama_health(timeout: int = 10) -> dict[str, Any]:
    try:
        vector = embed("health check", timeout=timeout)
        return {"ok": True, "embed_model": EMBED_MODEL, "dimensions": len(vector)}
    except Exception as exc:
        return {"ok": False, "embed_model": EMBED_MODEL, "error": str(exc)}
