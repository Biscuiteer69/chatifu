from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

from vault import DocumentChunk, upsert_chunks, upsert_devices


DEFAULT_EXPORT_DIR = "/home/biscuited/projects/chatifu_vault/export"


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def ingest_devices(path: Path, batch_size: int) -> int:
    total = 0
    batch: list[dict[str, Any]] = []
    for row in read_jsonl(path):
        batch.append(row)
        if len(batch) >= batch_size:
            total += upsert_devices(batch)
            print(f"[ingest] devices: {total}")
            batch = []
    if batch:
        total += upsert_devices(batch)
    print(f"[ingest] devices: done {total}")
    return total


def ingest_documents(path: Path, batch_size: int) -> int:
    total = 0
    batch: list[DocumentChunk] = []
    for row in read_jsonl(path):
        embedding = row.get("embedding")
        content = row.get("content")
        if not isinstance(embedding, list) or not content:
            continue
        batch.append(
            DocumentChunk(
                content=str(content),
                embedding=[float(x) for x in embedding],
                metadata=dict(row.get("metadata") or {}),
                source_id=str(row.get("id") or ""),
            )
        )
        if len(batch) >= batch_size:
            total += upsert_chunks(batch)
            print(f"[ingest] documents: {total}")
            batch = []
    if batch:
        total += upsert_chunks(batch)
    print(f"[ingest] documents: done {total}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Supabase JSONL export into local ChatIFU vault.")
    parser.add_argument("--export-dir", default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args()

    export_dir = Path(args.export_dir)
    devices = export_dir / "devices.jsonl"
    documents = export_dir / "documents.jsonl"
    if devices.exists():
        ingest_devices(devices, args.batch_size)
    else:
        print(f"[ingest] missing {devices}")
    if documents.exists():
        ingest_documents(documents, args.batch_size)
    else:
        print(f"[ingest] missing {documents}")


if __name__ == "__main__":
    main()

