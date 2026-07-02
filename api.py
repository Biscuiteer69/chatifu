from __future__ import annotations

import os
import re
from hmac import compare_digest
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ollama_client import embed, generate, ollama_health
from vault import VECTOR_SIZE, search_chunks, vault_stats


API_TOKEN = os.environ.get("CHATIFU_API_TOKEN")
ALLOW_UNAUTHENTICATED = os.environ.get("CHATIFU_ALLOW_UNAUTHENTICATED", "0") == "1"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CHATIFU_ALLOWED_ORIGINS", "https://chatifu.com").split(",")
    if origin.strip()
]
DEFAULT_MIN_SCORE = float(os.environ.get("CHATIFU_QUERY_MIN_SCORE", "0.2"))


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    limit: int = Field(default=5, ge=1, le=10)
    min_score: float = Field(default=DEFAULT_MIN_SCORE, ge=0.0, le=1.0)
    sku: str | None = Field(default=None, min_length=1, max_length=80)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    auto_sku_filter: bool = True


class AskRequest(QueryRequest):
    max_context_chars: int = Field(default=6000, ge=1000, le=20000)


def create_app() -> FastAPI:
    app = FastAPI(title="ChatIFU Vault API", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type", "x-api-key"],
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


def extract_sku(question: str) -> str | None:
    for match in re.finditer(r"\b[A-Za-z0-9]{1,12}-[A-Za-z0-9]{2,12}\b", question):
        token = match.group(0).strip()
        if any(char.isdigit() for char in token):
            return token
    return None


app = create_app()
