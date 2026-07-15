from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from hmac import compare_digest
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
    get_servable_ifu_documents,
    refresh_document_url,
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

# Observability: append one JSON line per request. Gitignored (logs/).
REQUEST_LOG = Path(os.environ.get("CHATIFU_REQUEST_LOG", "logs/requests.jsonl"))
# In-app rate limits (secondary to Cloudflare). Behind the tunnel the socket
# peer is 127.0.0.1, so we key on CF-Connecting-IP. requests per 60s window.
RATE_LIMITS = {"/answer": int(os.environ.get("CHATIFU_RL_ANSWER", "10")),
               "/ask": int(os.environ.get("CHATIFU_RL_ASK", "5"))}
_rl_hits: dict[str, deque] = {}
_rl_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def _rate_limited(path: str, client: str) -> bool:
    limit = RATE_LIMITS.get(path)
    if not limit:
        return False
    now = time.monotonic()
    key = f"{path}|{client}"
    with _rl_lock:
        dq = _rl_hits.setdefault(key, deque())
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(now)
        return False


def _log_request(record: dict[str, Any]) -> None:
    try:
        REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REQUEST_LOG.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # logging must never break a request


def _request_stats(window_s: int = 86400) -> dict[str, Any]:
    """Summarize the last window of the request log for /stats."""
    if not REQUEST_LOG.exists():
        return {"window_hours": window_s // 3600, "requests": 0}
    cutoff = time.time() - window_s
    by_path: dict[str, int] = {}
    statuses: dict[str, int] = {}
    latencies: list[float] = []
    total = 0
    try:
        with REQUEST_LOG.open() as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("ts", 0) < cutoff:
                    continue
                total += 1
                by_path[rec.get("path", "?")] = by_path.get(rec.get("path", "?"), 0) + 1
                sc = str(rec.get("status", "?"))
                statuses[sc] = statuses.get(sc, 0) + 1
                if isinstance(rec.get("latency_ms"), (int, float)):
                    latencies.append(rec["latency_ms"])
    except Exception:
        pass
    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        return round(latencies[min(len(latencies) - 1, int(len(latencies) * p))], 1)

    return {
        "window_hours": window_s // 3600,
        "requests": total,
        "by_path": by_path,
        "by_status": statuses,
        "latency_ms": {"p50": pct(0.5), "p95": pct(0.95)},
    }


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

    @app.middleware("http")
    async def observe_and_limit(request: Request, call_next):
        path = request.url.path
        client = _client_ip(request)
        if _rate_limited(path, client):
            return JSONResponse({"detail": "Rate limit exceeded. Slow down."}, status_code=429)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if path not in ("/healthz",):  # skip health-check noise
                _log_request({
                    "ts": time.time(),
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                    "client": hashlib.sha256(client.encode()).hexdigest()[:12],
                })

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
        return {"vault": vault_stats(), "traffic_24h": _request_stats()}

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
        document_url: str | None = Query(default=None, max_length=2000),
        _: None = Depends(require_beta_access),
    ) -> Response:
        if document_url:
            # Only stream a document that this catalog actually resolves to.
            # Fetching an arbitrary caller-supplied URL would make this endpoint
            # an open proxy into the DGX's network.
            #
            # Compare the stable part of the URL (scheme/host/path) and ignore
            # the query: Stryker's links are presigned and their signature
            # rotates every 6h, so a full-string match would reject a link we
            # ourselves minted an hour ago — including one held in the answer
            # cache. The document is then re-minted below rather than served
            # from the caller's (possibly expired) signature.
            match = next(
                (
                    doc for doc in get_servable_ifu_documents(catalog)
                    if _stable_url_key(str(doc["document_url"])) == _stable_url_key(document_url)
                ),
                None,
            )
            if match is None:
                raise HTTPException(
                    status_code=400,
                    detail="document_url is not an official IFU for this device.",
                )
            doc_url = refresh_document_url(match) or str(match["document_url"])
        else:
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

        result, considered = _best_answer_across_documents(catalog, question, _get_answerer())
        actual_document_url = result.document_url or result.pdf_url
        open_full_ifu_url = result.open_full_ifu_url or actual_document_url
        # Pin the PDF proxy to the document the hits actually came from — otherwise
        # PDF.js would highlight page N of a different IFU. Use result.source_url
        # (the URL the answerer was handed == the SERVABLE/stored document_url that
        # /ifu/pdf validates against), NOT result.document_url — the latter is the
        # *resolved* PDF link (e.g. e-ifu's fetchPdf endpoint) whose path differs
        # from the stored viewpdf-iframe URL, so /ifu/pdf would 400 it. Only pin
        # when servable docs exist (considered non-empty); otherwise the answer came
        # from an on-demand resolve and /ifu/pdf must resolve the URL itself.
        proxy_document_url = result.source_url or actual_document_url
        pdf_proxy_path = f"/ifu/pdf?catalog={urllib.parse.quote(catalog)}"
        if proxy_document_url and considered:
            pdf_proxy_path += f"&document_url={urllib.parse.quote(proxy_document_url, safe='')}"
        payload = {
            "catalog": catalog,
            "document_title": result.document_title,
            "page_count": result.page_count,
            "hits": [
                {"page": h.page, "section": h.section, "snippet": h.snippet}
                for h in result.hits
            ],
            # Path on THIS api to stream the official PDF for PDF.js highlighting.
            "pdf_proxy_path": pdf_proxy_path,
            "document_url": actual_document_url,
            "open_full_ifu_url": open_full_ifu_url,
            # Every official document for this device, so the UI can offer a
            # switcher and show what was searched.
            "documents_considered": considered,
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


# A device can map to several official IFUs. Searching all of them costs a PDF
# fetch and parse each (cached after the first), so bound the fan-out: the list
# is priority-ordered, and beyond a handful the tail is generic boilerplate.
MAX_DOCS_PER_ANSWER = int(os.environ.get("CHATIFU_MAX_DOCS_PER_ANSWER", "5"))


def _stable_url_key(url: str) -> tuple[str, str, str]:
    """The part of a URL that identifies the document, ignoring the query.

    Presigned links carry a rotating signature, so the query string is not part
    of a document's identity.
    """
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme, parts.netloc, parts.path)


def _best_answer_across_documents(
    catalog: str,
    question: str,
    answerer: IFUAnswerer,
) -> tuple[AnswerResult, list[dict[str, Any]]]:
    """Answer from the device's document that actually contains the answer.

    Picking by rank alone returns an authentic document that may not address the
    question — a Synthes implant's top-ranked document can be a generic
    sterilization procedure while the olecranon-nail IFU sits below it. Search
    each document and keep the strongest hit: a matching section heading
    (SCORE_SECTION_HEADING) beats mere keyword coverage.
    """
    docs = get_servable_ifu_documents(catalog, limit=MAX_DOCS_PER_ANSWER)
    if not docs:
        # Nothing cached — fall back to the single-document path, which resolves
        # on demand and raises 404/502 with the right detail if that fails.
        return _get_answerer().answer(_resolve_ifu_url(catalog), question), []

    best: AnswerResult | None = None
    best_score = float("-inf")
    considered: list[dict[str, Any]] = []

    for doc in docs:
        # Stryker URLs are presigned and expire after 6h; re-mint before use.
        url = refresh_document_url(doc) or str(doc["document_url"])
        try:
            result = answerer.answer(url, question)
        except Exception as exc:  # noqa: BLE001 - one bad document must not sink the answer
            considered.append({"document_title": doc.get("document_title"), "error": str(exc)})
            continue
        score = max((h.score for h in result.hits), default=float("-inf"))
        considered.append({
            "document_title": doc.get("document_title") or result.document_title,
            "document_url": url,
            "hits": len(result.hits),
            "score": None if score == float("-inf") else score,
            "match_confidence": doc.get("match_confidence"),
        })
        if result.hits and score > best_score:
            # Prefer the manufacturer's document title over one derived from the
            # URL: Stryker's PDFs live at S3 UUID paths, so the derived title is
            # a GUID.
            if doc.get("document_title"):
                result.document_title = str(doc["document_title"])
            best, best_score = result, score

    if best is None:
        # Every document parsed but none contained the answer. Return the
        # top-ranked one so the user still gets the official IFU to read.
        return answerer.answer(str(docs[0]["document_url"]), question), considered
    return best, considered


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
