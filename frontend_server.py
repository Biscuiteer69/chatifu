"""Production static server for the ChatIFU frontend.

Replaces `python3 -m http.server` (single-threaded, dev-only) with an async
Starlette + Uvicorn app: real concurrency, gzip, and sensible cache headers.
Serves frontend/ on 127.0.0.1:8080 behind the Cloudflare Tunnel.

Run:  uvicorn frontend_server:app --host 127.0.0.1 --port 8080 --workers 2
"""
from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

# Long-cache immutable assets; keep index.html fresh so redeploys show up fast.
_IMMUTABLE = (".png", ".ico", ".jpg", ".jpeg", ".svg", ".woff2", ".woff", ".css", ".js")


class CachedStatic(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        lower = path.lower()
        if lower.endswith(_IMMUTABLE):
            response.headers["cache-control"] = "public, max-age=86400"
        elif lower.endswith(".html") or lower in ("", "."):
            response.headers["cache-control"] = "public, max-age=60"
        return response


app = Starlette(
    middleware=[Middleware(GZipMiddleware, minimum_size=512)],
)
app.mount("/", CachedStatic(directory=str(FRONTEND_DIR), html=True), name="static")
