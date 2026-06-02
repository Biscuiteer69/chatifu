from __future__ import annotations

import argparse
import os
from textwrap import shorten

import requests

from vault import COLLECTION, qdrant


OLLAMA_URL = os.environ.get("CHATIFU_OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embeddings")
EMBED_MODEL = os.environ.get("CHATIFU_EMBED_MODEL", "nomic-embed-text")


def embed(text: str) -> list[float]:
    res = requests.post(OLLAMA_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=60)
    res.raise_for_status()
    vector = res.json().get("embedding")
    if not isinstance(vector, list):
        raise RuntimeError("Ollama embedding response did not include an embedding list.")
    return [float(x) for x in vector]


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local ChatIFU Qdrant vault.")
    parser.add_argument("question")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    client = qdrant()
    response = client.query_points(
        collection_name=COLLECTION,
        query=embed(args.question),
        limit=args.limit,
        with_payload=True,
    )
    for index, point in enumerate(response.points, start=1):
        payload = point.payload or {}
        metadata = payload.get("metadata") or {}
        content = str(payload.get("content") or "")
        print(f"{index}. score={point.score:.4f} sku={metadata.get('sku')} source={metadata.get('source')}")
        print(f"   {shorten(content.replace(chr(10), ' '), width=260, placeholder='...')}")


if __name__ == "__main__":
    main()
