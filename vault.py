from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams


VAULT_DIR = Path(os.environ.get("CHATIFU_VAULT_DIR", "/home/biscuited/projects/chatifu_vault"))
QDRANT_PATH = Path(os.environ.get("CHATIFU_QDRANT_PATH", str(VAULT_DIR / "qdrant")))
SQLITE_PATH = Path(os.environ.get("CHATIFU_SQLITE_PATH", str(VAULT_DIR / "chatifu.sqlite3")))
COLLECTION = os.environ.get("CHATIFU_COLLECTION", "chatifu_documents")
VECTOR_SIZE = int(os.environ.get("CHATIFU_VECTOR_SIZE", "768"))


@dataclass(frozen=True)
class DocumentChunk:
    content: str
    embedding: list[float]
    metadata: dict[str, Any]
    source_id: str | None = None


def stable_point_id(source_id: str | None, metadata: dict[str, Any]) -> str:
    raw = source_id or json.dumps(metadata, sort_keys=True, default=str)
    try:
        return str(uuid.UUID(str(raw)))
    except (TypeError, ValueError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw)))


def ensure_dirs() -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    QDRANT_PATH.mkdir(parents=True, exist_ok=True)


def qdrant() -> QdrantClient:
    ensure_dirs()
    client = QdrantClient(path=str(QDRANT_PATH))
    existing = {collection.name for collection in client.get_collections().collections}
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
    return client


def sqlite() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        create table if not exists devices (
            id text primary key,
            company_name text,
            brand_name text,
            model_number text,
            catalog_number text,
            raw_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists processed_skus (
            sku text primary key,
            status text not null,
            source text,
            updated_at text default current_timestamp
        )
        """
    )
    conn.execute(
        """
        create table if not exists scrape_runs (
            id integer primary key autoincrement,
            scraper text not null,
            status text not null,
            details text,
            started_at text default current_timestamp,
            finished_at text
        )
        """
    )
    conn.execute("create index if not exists idx_devices_company on devices(company_name)")
    conn.execute("create index if not exists idx_devices_catalog on devices(catalog_number)")
    conn.commit()
    return conn


def upsert_devices(devices: Iterable[dict[str, Any]]) -> int:
    conn = sqlite()
    count = 0
    with conn:
        for device in devices:
            device_id = str(device.get("id") or stable_point_id(None, device))
            conn.execute(
                """
                insert into devices (id, company_name, brand_name, model_number, catalog_number, raw_json)
                values (?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    company_name=excluded.company_name,
                    brand_name=excluded.brand_name,
                    model_number=excluded.model_number,
                    catalog_number=excluded.catalog_number,
                    raw_json=excluded.raw_json
                """,
                (
                    device_id,
                    device.get("company_name"),
                    device.get("brand_name"),
                    device.get("model_number"),
                    device.get("catalog_number"),
                    json.dumps(device, default=str),
                ),
            )
            count += 1
    conn.close()
    return count


def upsert_chunks(chunks: Iterable[DocumentChunk]) -> int:
    client = qdrant()
    conn = sqlite()
    points: list[tuple[str, list[float], dict[str, Any]]] = []
    count = 0
    for chunk in chunks:
        metadata = dict(chunk.metadata or {})
        sku = metadata.get("sku")
        payload = {
            "content": chunk.content,
            "metadata": metadata,
            "source_id": chunk.source_id,
        }
        points.append((stable_point_id(chunk.source_id, metadata), chunk.embedding, payload))
        if sku:
            conn.execute(
                """
                insert into processed_skus (sku, status, source, updated_at)
                values (?, ?, ?, current_timestamp)
                on conflict(sku) do update set
                    status=excluded.status,
                    source=excluded.source,
                    updated_at=current_timestamp
                """,
                (str(sku), "ingested", str(metadata.get("source") or "local")),
            )
        count += 1

    if points:
        client.upsert(
            collection_name=COLLECTION,
            points=[
                PointStruct(id=point_id, vector=vector, payload=payload)
                for point_id, vector, payload in points
            ],
        )
    conn.commit()
    conn.close()
    return count


def mark_sku(sku: str, status: str, source: str) -> None:
    conn = sqlite()
    with conn:
        conn.execute(
            """
            insert into processed_skus (sku, status, source, updated_at)
            values (?, ?, ?, current_timestamp)
            on conflict(sku) do update set
                status=excluded.status,
                source=excluded.source,
                updated_at=current_timestamp
            """,
            (sku, status, source),
        )
    conn.close()


def sku_processed(sku: str) -> bool:
    conn = sqlite()
    row = conn.execute("select 1 from processed_skus where sku = ? limit 1", (sku,)).fetchone()
    conn.close()
    return row is not None


def device_sku(device: dict[str, Any]) -> str:
    value = (
        device.get("catalog_number")
        or device.get("catalogNumber")
        or device.get("model_number")
        or device.get("versionModelNumber")
        or device.get("PrimaryDI")
    )
    return str(value or "").strip()


def pending_devices(company_like: str, limit: int) -> list[dict[str, Any]]:
    conn = sqlite()
    scan_limit = max(limit * 50, 500)
    rows = conn.execute(
        """
        select raw_json
        from devices
        where lower(company_name) like lower(?)
        order by catalog_number, model_number
        limit ?
        """,
        (company_like, scan_limit),
    ).fetchall()
    conn.close()

    pending: list[dict[str, Any]] = []
    for row in rows:
        device = json.loads(row["raw_json"])
        sku = device_sku(device)
        if sku and not sku_processed(str(sku)):
            pending.append(device)
        if len(pending) >= limit:
            break
    return pending


def pending_devices_any(company_likes: Iterable[str], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    pending: list[dict[str, Any]] = []
    for company_like in company_likes:
        for device in pending_devices(company_like, max(1, limit - len(pending))):
            device_id = str(device.get("id") or stable_point_id(None, device))
            if device_id in seen:
                continue
            seen.add(device_id)
            pending.append(device)
            if len(pending) >= limit:
                return pending
    return pending
