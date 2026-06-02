from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_CACHE_DIR = Path("/home/biscuited/.biscuited/hermes/DGX/cache/ifu_docs")


@dataclass
class CachedDocument:
    cache_key: str
    document_url: str
    path: str
    metadata_path: str
    sha256: str
    content_type: str
    size_bytes: int
    fetched_at: str
    expires_at: str | None
    etag: str | None
    last_modified: str | None


class IFUDocumentCache:
    def __init__(
        self,
        cache_dir: str | Path | None = None,
        ttl_days: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        env_dir = os.environ.get("CHATIFU_IFU_CACHE_DIR")
        env_ttl = os.environ.get("CHATIFU_IFU_CACHE_TTL_DAYS")
        env_max_mb = os.environ.get("CHATIFU_IFU_CACHE_MAX_MB")
        self.cache_dir = Path(cache_dir or env_dir or DEFAULT_CACHE_DIR)
        self.ttl_days = int(ttl_days if ttl_days is not None else (env_ttl or 14))
        self.max_bytes = int(max_bytes if max_bytes is not None else int(env_max_mb or 75) * 1_000_000)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def key_for_url(self, url: str) -> str:
        return hashlib.sha256(_normalize_url(url).encode("utf-8")).hexdigest()

    def get(self, url: str) -> bytes | None:
        doc = self._metadata_for_url(url)
        if not doc or self._is_expired(doc):
            return None
        path = Path(doc.path)
        if not path.exists():
            return None
        return path.read_bytes()

    def put(self, url: str, content: bytes, metadata: dict[str, Any] | None = None) -> CachedDocument:
        metadata = metadata or {}
        content_type = str(metadata.get("content_type") or metadata.get("Content-Type") or "application/pdf")
        self._validate_document(url, content, content_type)
        cache_key = self.key_for_url(url)
        pdf_path = self.cache_dir / f"{cache_key}.pdf"
        meta_path = self.cache_dir / f"{cache_key}.json"
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=self.ttl_days) if self.ttl_days != 0 else None
        content_hash = hashlib.sha256(content).hexdigest()

        _atomic_write(pdf_path, content)
        doc = CachedDocument(
            cache_key=cache_key,
            document_url=url,
            path=str(pdf_path),
            metadata_path=str(meta_path),
            sha256=content_hash,
            content_type=content_type,
            size_bytes=len(content),
            fetched_at=now.isoformat(),
            expires_at=expires.isoformat() if expires else None,
            etag=metadata.get("etag"),
            last_modified=metadata.get("last_modified"),
        )
        _atomic_write(meta_path, json.dumps(asdict(doc), indent=2, sort_keys=True).encode("utf-8"))
        return doc

    def get_or_fetch(
        self,
        url: str,
        fetcher: Callable[[], bytes | tuple[bytes, Any] | tuple[bytes, Any, Any]],
    ) -> tuple[bytes, CachedDocument, bool]:
        cached = self.get(url)
        doc = self._metadata_for_url(url)
        if cached is not None and doc is not None:
            return cached, doc, True

        fetched = fetcher()
        metadata: dict[str, Any] = {}
        if isinstance(fetched, tuple):
            content = fetched[0]
            if len(fetched) > 1 and fetched[1]:
                metadata["final_url"] = str(fetched[1])
            if len(fetched) > 2 and fetched[2]:
                metadata["title"] = str(fetched[2])
        else:
            content = fetched
        doc = self.put(url, content, metadata)
        return content, doc, False

    def purge(self, cache_key: str) -> bool:
        removed = False
        for suffix in (".pdf", ".json"):
            path = self.cache_dir / f"{cache_key}{suffix}"
            if path.exists():
                path.unlink()
                removed = True
        return removed

    def stats(self) -> dict[str, Any]:
        docs = [self._read_metadata(path) for path in self.cache_dir.glob("*.json")]
        docs = [doc for doc in docs if doc is not None]
        total_bytes = sum(doc.size_bytes for doc in docs)
        fetched = sorted([doc.fetched_at for doc in docs if doc.fetched_at])
        return {
            "cache_dir": str(self.cache_dir),
            "total_documents": len(docs),
            "total_bytes": total_bytes,
            "oldest": fetched[0] if fetched else None,
            "newest": fetched[-1] if fetched else None,
            "documents": [asdict(doc) for doc in sorted(docs, key=lambda d: d.size_bytes, reverse=True)[:25]],
        }

    def _metadata_for_url(self, url: str) -> CachedDocument | None:
        return self._read_metadata(self.cache_dir / f"{self.key_for_url(url)}.json")

    def _read_metadata(self, path: Path) -> CachedDocument | None:
        try:
            data = json.loads(path.read_text("utf-8"))
            return CachedDocument(**data)
        except Exception:
            return None

    def _is_expired(self, doc: CachedDocument) -> bool:
        if not doc.expires_at:
            return False
        try:
            return datetime.fromisoformat(doc.expires_at) < datetime.now(timezone.utc)
        except ValueError:
            return True

    def _validate_document(self, url: str, content: bytes, content_type: str) -> None:
        if len(content) < 16:
            raise ValueError("Refusing to cache suspiciously small IFU document")
        if len(content) > self.max_bytes:
            raise ValueError("Refusing to cache IFU document above configured max size")
        low_url = url.lower()
        low_type = content_type.lower()
        pdfish = (
            content.startswith(b"%PDF")
            or "application/pdf" in low_type
            or low_url.endswith(".pdf")
            or "fetchpdf" in low_url
        )
        htmlish = content.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        if not pdfish or htmlish:
            raise ValueError("Refusing to cache non-PDF IFU document")


def _normalize_url(url: str) -> str:
    return " ".join((url or "").strip().split())


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
