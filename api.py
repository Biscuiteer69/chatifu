from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import OrderedDict
from hmac import compare_digest
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ollama_client import embed, generate, ollama_health
from vault import VECTOR_SIZE, search_chunks, vault_stats

# In-document highlight stack (ported from mvp_server.py). Imported as
# libraries so the FastAPI service can serve the real product contract:
# device search -> official PDF -> {page, section, snippet} highlight hits.
from ifu_answer import IFUAnswerer, AnswerResult, _is_direct_pdf_url
from ifu_cache import IFUDocumentCache
from mvp_lookup import (
    ensure_ifu_for_catalog,
    get_best_ifu_url,
    get_device,
    search_devices,
)


API_TOKEN = os.environ.get("CHATIFU_API_TOKEN")
ALLOW_UNAUTHENTICATED = os.environ.get("CHATIFU_ALLOW_UNAUTHENTICATED", "0") == "1"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CHATIFU_ALLOWED_ORIGINS", "https://chatifu.com").split(",")
    if origin.strip()
]
DEFAULT_MIN_SCORE = float(os.environ.get("CHATIFU_QUERY_MIN_SCORE", "0.2"))

# Comma-separated invite codes that gate the beta product endpoints
# (/device/*, /ifu/pdf, /answer). Rotate by editing the env value.
BETA_CODES = {
    code.strip()
    for code in os.environ.get("CHATIFU_BETA_CODES", "").split(",")
    if code.strip()
}
# PDF rehosting is OFF by default: we proxy the manufacturer's official PDF
# to the browser and discard it (no stored copy, no rehosting footprint).
# Flip CHATIFU_CACHE_PDFS=1 to persist a disk cache (pending legal sign-off).
CACHE_PDFS = os.environ.get("CHATIFU_CACHE_PDFS", "0") == "1"
ANSWER_CACHE_MAX = int(os.environ.get("CHATIFU_ANSWER_CACHE_MAX", "256"))


# --- Lazy singletons for the highlight stack -------------------------------
_answerer: IFUAnswerer | None = None
_ifu_cache: IFUDocumentCache | None = None
_stack_lock = threading.Lock()


def _get_answerer() -> IFUAnswerer:
    global _answerer
    if _answerer is None:
        with _stack_lock:
            if _answerer is None:
                _answerer = IFUAnswerer()
    return _answerer


def _get_ifu_cache() -> IFUDocumentCache | None:
    """Disk-backed PDF cache, only when CHATIFU_CACHE_PDFS=1 (legal-gated)."""
    global _ifu_cache
    if not CACHE_PDFS:
        return None
    if _ifu_cache is None:
        with _stack_lock:
            if _ifu_cache is None:
                _ifu_cache = IFUDocumentCache()
    return _ifu_cache


# --- Bounded LRU cache for /answer (IFUAnswerer is serial under a lock) -----
_answer_cache: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
_answer_cache_lock = threading.Lock()


def _answer_cache_get(key: tuple[str, str]) -> dict[str, Any] | None:
    with _answer_cache_lock:
        if key in _answer_cache:
            _answer_cache.move_to_end(key)
            return _answer_cache[key]
    return None


def _answer_cache_put(key: tuple[str, str], value: dict[str, Any]) -> None:
    with _answer_cache_lock:
        _answer_cache[key] = value
        _answer_cache.move_to_end(key)
        while len(_answer_cache) > ANSWER_CACHE_MAX:
            _answer_cache.popitem(last=False)


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    limit: int = Field(default=5, ge=1, le=10)
    min_score: float = Field(default=DEFAULT_MIN_SCORE, ge=0.0, le=1.0)
    sku: str | None = Field(default=None, min_length=1, max_length=80)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    auto_sku_filter: bool = True


class AskRequest(QueryRequest):
    max_context_chars: int = Field(default=6000, ge=1000, le=20000)


class AnswerRequest(BaseModel):
    catalog: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=3, max_length=2000)


def create_app() -> FastAPI:
    app = FastAPI(title="ChatIFU Vault API", version="0.3.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type", "x-api-key", "x-beta-code"],
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "chatifu-vault-api"}

    @app.get("/readyz")
    def readyz(_: None = Depends(require_api_token)) -> dict[str, Any]:
        stats = vault_stats()
        ollama = ollama_health()
        ready = bool(stats["vector_chunks"] > 0 and ollama["ok"] and ollama.get("dimensions") == VECTOR_SIZE)
        return {"status": "ready" if ready else "degraded", "vault": stats, "ollama": ollama}

    @app.get("/stats")
    def stats(_: None = Depends(require_api_token)) -> dict[str, Any]:
        return vault_stats()

    # ------------------------------------------------------------------
    # Beta product endpoints (in-document highlight) — invite-code gated
    # ------------------------------------------------------------------

    @app.get("/device/search")
    def device_search(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=20, ge=1, le=50),
        _: None = Depends(require_beta_access),
    ) -> dict[str, Any]:
        devices = search_devices(q, limit=limit)
        return {"query": q, "count": len(devices), "devices": devices}

    @app.get("/device/lookup")
    def device_lookup(
        catalog: str = Query(min_length=1, max_length=120),
        _: None = Depends(require_beta_access),
    ) -> dict[str, Any]:
        device = get_device(catalog)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        ifu_url = get_best_ifu_url(catalog)
        return {"catalog": catalog, "device": device, "ifu_url": ifu_url, "has_ifu": bool(ifu_url)}

    @app.get("/ifu/pdf")
    def ifu_pdf(
        catalog: str = Query(min_length=1, max_length=120),
        _: None = Depends(require_beta_access),
    ) -> Response:
        doc_url = _resolve_ifu_url(catalog)
        answerer = _get_answerer()
        cache = _get_ifu_cache()
        try:
            if cache is not None and not _is_direct_pdf_url(doc_url):
                pdf_bytes, _doc, cache_hit = cache.get_or_fetch(
                    doc_url, lambda: answerer.fetch_pdf_bytes(doc_url)
                )
            else:
                pdf_bytes, _pdf_url, _title = answerer.fetch_pdf_bytes(doc_url)
                cache_hit = False
        except Exception as exc:  # noqa: BLE001 - surface fetch failures as 502
            raise HTTPException(status_code=502, detail=f"PDF fetch failed: {exc}") from exc
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{catalog}-ifu.pdf"',
                "X-ChatIFU-Cache": "hit" if cache_hit else "miss",
                "Cache-Control": "private, max-age=3600",
            },
        )

    @app.post("/answer")
    def answer(body: AnswerRequest, _: None = Depends(require_beta_access)) -> dict[str, Any]:
        catalog = body.catalog.strip()
        question = body.question.strip()
        cache_key = (catalog, hashlib.sha256(question.lower().encode()).hexdigest())
        cached = _answer_cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

        doc_url = _resolve_ifu_url(catalog)
        result: AnswerResult = _get_answerer().answer(doc_url, question)
        actual_document_url = result.document_url or result.pdf_url
        open_full_ifu_url = result.open_full_ifu_url or actual_document_url or doc_url
        payload = {
            "catalog": catalog,
            "document_title": result.document_title,
            "page_count": result.page_count,
            "hits": [
                {"page": h.page, "section": h.section, "snippet": h.snippet}
                for h in result.hits
            ],
            # Path on THIS api to stream the official PDF for PDF.js highlighting.
            "pdf_proxy_path": f"/ifu/pdf?catalog={catalog}",
            "document_url": actual_document_url,
            "open_full_ifu_url": open_full_ifu_url,
            "source_url": result.source_url,
            "timing_ms": result.timing_ms,
            "error": result.error,
        }
        if not result.error and result.hits:
            _answer_cache_put(cache_key, payload)
        return {**payload, "cached": False}

    # ------------------------------------------------------------------
    # AI-summary endpoints (bearer token) — secondary to the highlight flow
    # ------------------------------------------------------------------

    @app.post("/query")
    def query(body: QueryRequest, _: None = Depends(require_api_token)) -> dict[str, Any]:
        sku = body.sku or (extract_sku(body.question) if body.auto_sku_filter else None)
        vector = embed(body.question)
        matches = search_chunks(vector, limit=body.limit, min_score=body.min_score, sku=sku, source=body.source)
        return {
            "question": body.question,
            "filters": {"sku": sku, "source": body.source},
            "matches": [
                {
                    "score": match.score,
                    "content": match.content,
                    "metadata": match.metadata,
                    "source_id": match.source_id,
                    "point_id": match.point_id,
                }
                for match in matches
            ],
        }

    @app.post("/ask")
    def ask(body: AskRequest, _: None = Depends(require_api_token)) -> dict[str, Any]:
        query_result = query(body)
        context = "\n\n".join(
            f"[{index}] SKU={match['metadata'].get('sku')} SOURCE={match['metadata'].get('source')}\n{match['content']}"
            for index, match in enumerate(query_result["matches"], start=1)
        )[: body.max_context_chars]
        if not context.strip():
            return {"question": body.question, "answer": "I could not find a matching IFU passage.", "matches": []}

        prompt = f"""You are ChatIFU, a careful medical-device IFU assistant for beta testing.
Answer only from the provided IFU context. If the context does not contain the answer, say that.
Do not invent instructions, warnings, indications, contraindications, or device compatibility.
Include SKU/source references from the context when relevant.
Answer in the same language as the user question. Do not mix languages unless directly quoting source text.

Question:
{body.question}

IFU context:
{context}
"""
        return {
            "question": body.question,
            "answer": generate(prompt, timeout=120, temperature=0),
            "matches": query_result["matches"],
        }

    return app


def _resolve_ifu_url(catalog: str) -> str:
    """Return the best official IFU URL for a catalog, resolving on demand."""
    catalog = catalog.strip()
    doc_url = get_best_ifu_url(catalog)
    if not doc_url:
        try:
            ensure_ifu_for_catalog(catalog)
            doc_url = get_best_ifu_url(catalog)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"IFU lookup failed: {exc}") from exc
    if not doc_url:
        raise HTTPException(status_code=404, detail="No official IFU found for this device.")
    return doc_url


def require_api_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    if ALLOW_UNAUTHENTICATED:
        return
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="CHATIFU_API_TOKEN is required before serving protected endpoints.")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    token = bearer or x_api_key or ""
    if not compare_digest(token, API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing API token.")


def require_beta_access(
    x_beta_code: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    """Gate beta product endpoints on a valid invite code OR the admin token."""
    if ALLOW_UNAUTHENTICATED:
        return
    # A valid invite code grants access.
    if x_beta_code and any(compare_digest(x_beta_code.strip(), code) for code in BETA_CODES):
        return
    # The admin bearer/api token also works (for testing + internal tools).
    if API_TOKEN:
        bearer = ""
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization.split(" ", 1)[1].strip()
        token = bearer or x_api_key or ""
        if token and compare_digest(token, API_TOKEN):
            return
    if not BETA_CODES and not API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="CHATIFU_BETA_CODES or CHATIFU_API_TOKEN must be set before serving beta endpoints.",
        )
    raise HTTPException(status_code=401, detail="Valid beta invite code required.")


def extract_sku(question: str) -> str | None:
    for match in re.finditer(r"\b[A-Za-z0-9]{1,12}-[A-Za-z0-9]{2,12}\b", question):
        token = match.group(0).strip()
        if any(char.isdigit() for char in token):
            return token
    return None


app = create_app()
