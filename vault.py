from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


VAULT_DIR = Path(os.environ.get("CHATIFU_VAULT_DIR", "/home/biscuited/projects/chatifu_vault"))
QDRANT_PATH = Path(os.environ.get("CHATIFU_QDRANT_PATH", str(VAULT_DIR / "qdrant")))
# When set (e.g. http://127.0.0.1:6333), use a Qdrant server instead of the
# embedded file store. Embedded mode is brute-force (no HNSW index) and is
# designed for <=20k points; the production collection is far past that.
QDRANT_URL = os.environ.get("CHATIFU_QDRANT_URL", "").strip()
SQLITE_PATH = Path(os.environ.get("CHATIFU_SQLITE_PATH", str(VAULT_DIR / "chatifu.sqlite3")))
COLLECTION = os.environ.get("CHATIFU_COLLECTION", "chatifu_documents")
VECTOR_SIZE = int(os.environ.get("CHATIFU_VECTOR_SIZE", "768"))
_QDRANT_CLIENT: Any | None = None


@dataclass(frozen=True)
class DocumentChunk:
    content: str
    embedding: list[float]
    metadata: dict[str, Any]
    source_id: str | None = None


@dataclass(frozen=True)
class SearchMatch:
    score: float
    content: str
    metadata: dict[str, Any]
    source_id: str | None
    point_id: str


def _qdrant_types() -> tuple[Any, Any, Any, Any]:
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http.models import Distance, PointStruct, VectorParams
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: install qdrant-client to use the ChatIFU vector vault.") from exc
    return QdrantClient, Distance, PointStruct, VectorParams


def stable_point_id(source_id: str | None, metadata: dict[str, Any]) -> str:
    raw = source_id or json.dumps(metadata, sort_keys=True, default=str)
    try:
        return str(uuid.UUID(str(raw)))
    except (TypeError, ValueError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw)))


def validate_embedding(embedding: Iterable[Any], source: str = "embedding") -> list[float]:
    vector = [float(value) for value in embedding]
    if len(vector) != VECTOR_SIZE:
        raise ValueError(f"{source} has {len(vector)} dimensions; expected {VECTOR_SIZE}.")
    return vector


def ensure_dirs() -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    QDRANT_PATH.mkdir(parents=True, exist_ok=True)


def qdrant() -> Any:
    global _QDRANT_CLIENT
    if _QDRANT_CLIENT is not None:
        return _QDRANT_CLIENT
    QdrantClient, Distance, _, VectorParams = _qdrant_types()
    ensure_dirs()
    if QDRANT_URL:
        client = QdrantClient(url=QDRANT_URL)
    else:
        client = QdrantClient(path=str(QDRANT_PATH))
    existing = {collection.name for collection in client.get_collections().collections}
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        ensure_payload_indexes(client)
    _QDRANT_CLIENT = client
    return client


def ensure_payload_indexes(client: Any) -> None:
    """Create keyword indexes for every payload field used in filters.

    Payload indexes should exist before points are ingested so Qdrant builds
    filter-aware HNSW edges; unindexed filter fields push the query planner
    onto slow or low-recall strategies. Only applies in server mode.
    """
    if not QDRANT_URL:
        return
    for field in ("metadata.sku", "metadata.source"):
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name=field,
            field_schema="keyword",
        )


def sqlite() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(SQLITE_PATH, timeout=30.0)
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
    _, _, PointStruct, _ = _qdrant_types()
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
        point_id = stable_point_id(chunk.source_id, metadata)
        points.append((point_id, validate_embedding(chunk.embedding, point_id), payload))
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


def _query_filter(sku: str | None = None, source: str | None = None) -> Any | None:
    if not sku and not source:
        return None
    try:
        from qdrant_client.http.models import FieldCondition, Filter, MatchValue
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: install qdrant-client to use the ChatIFU vector vault.") from exc

    conditions = []
    if sku:
        conditions.append(FieldCondition(key="metadata.sku", match=MatchValue(value=str(sku))))
    if source:
        conditions.append(FieldCondition(key="metadata.source", match=MatchValue(value=str(source))))
    return Filter(must=conditions)


def search_chunks(
    query_embedding: Iterable[Any],
    limit: int = 5,
    min_score: float = 0.0,
    sku: str | None = None,
    source: str | None = None,
) -> list[SearchMatch]:
    client = qdrant()
    response = client.query_points(
        collection_name=COLLECTION,
        query=validate_embedding(query_embedding, "query"),
        query_filter=_query_filter(sku=sku, source=source),
        limit=max(1, min(limit, 25)),
        with_payload=True,
    )
    matches: list[SearchMatch] = []
    for point in response.points:
        score = float(point.score)
        if score < min_score:
            continue
        payload = point.payload or {}
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        matches.append(
            SearchMatch(
                score=score,
                content=str(payload.get("content") or ""),
                metadata=metadata,
                source_id=payload.get("source_id"),
                point_id=str(point.id),
            )
        )
    return matches


def sqlite_counts() -> dict[str, int]:
    conn = sqlite()
    try:
        devices = int(conn.execute("select count(*) from devices").fetchone()[0])
        processed = int(conn.execute("select count(*) from processed_skus").fetchone()[0])
        return {"devices": devices, "processed_skus": processed}
    finally:
        conn.close()


def processed_status_counts() -> dict[str, int]:
    conn = sqlite()
    try:
        rows = conn.execute(
            """
            select status, count(*) as count
            from processed_skus
            group by status
            order by count desc, status
            """
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}
    finally:
        conn.close()


def vector_count(exact: bool = True) -> int:
    client = qdrant()
    return int(client.count(collection_name=COLLECTION, exact=exact).count)


def vault_stats(exact_vectors: bool = False) -> dict[str, Any]:
    return {
        "vault_dir": str(VAULT_DIR),
        "sqlite_path": str(SQLITE_PATH),
        "qdrant_path": str(QDRANT_PATH),
        "collection": COLLECTION,
        "vector_size": VECTOR_SIZE,
        "counts": sqlite_counts(),
        "processed_statuses": processed_status_counts(),
        "vector_chunks": vector_count(exact=exact_vectors),
    }


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
    # Anti-join against processed_skus so already-processed rows never consume
    # the candidate window (the old limit*50 scan starved once the first rows
    # by catalog_number were all processed, silently returning 0 pending).
    # The sku expression mirrors device_sku(): catalog -> model -> PrimaryDI(id).
    conn = sqlite()
    rows = conn.execute(
        """
        select d.raw_json
        from devices d
        left join processed_skus p
            on p.sku = coalesce(
                nullif(trim(d.catalog_number), ''),
                nullif(trim(d.model_number), ''),
                d.id
            )
        where lower(d.company_name) like lower(?)
          and p.sku is null
        order by d.catalog_number, d.model_number
        limit ?
        """,
        (company_like, limit),
    ).fetchall()
    conn.close()

    pending: list[dict[str, Any]] = []
    for row in rows:
        device = json.loads(row["raw_json"])
        if device_sku(device):
            pending.append(device)
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
