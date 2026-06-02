"""
Option B PoC — real-time IFU fetch, parse, answer, discard.

Speed and accuracy test ONLY. No PDF content is written to disk or stored.
Run:
    python3 option_b_poc.py [--catalog GIB00U0340] [--query "contraindications"]
    python3 option_b_poc.py --url "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701"
"""
from __future__ import annotations

import argparse
import http.cookiejar
import io
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pypdf


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
BASE_URL = "https://www.e-ifu.com"
TIMEOUT = 20

# Viewer page returns <textarea>JSON</textarea> wrapping a Drupal AJAX command array.
# The openDialog command's "data" field contains the inner HTML with the PDF iframe.


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def build_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler(),
    )


def _request(opener: urllib.request.OpenerDirector, url: str, data: bytes | None = None,
             extra_headers: dict[str, str] | None = None) -> tuple[str, str]:
    """Return (body_text, final_url)."""
    h = {**HEADERS, **(extra_headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    with opener.open(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.url


def _extract_form_field(html: str, name: str) -> str | None:
    pattern = rf'name="{re.escape(name)}"\s+value="([^"]*)"'
    m = re.search(pattern, html)
    if m:
        return m.group(1)
    pattern2 = rf'value="([^"]*)"\s+name="{re.escape(name)}"'
    m = re.search(pattern2, html)
    return m.group(1) if m else None


def establish_session(opener: urllib.request.OpenerDirector) -> None:
    """Complete the e-IFU HCP gate and terms flow."""
    welcome_html, _ = _request(opener, f"{BASE_URL}/welcome")
    form_build_id = _extract_form_field(welcome_html, "form_build_id")
    if not form_build_id:
        raise RuntimeError("Could not find form_build_id on welcome page")

    data = urllib.parse.urlencode({
        "site_user": "hcp",
        "eifu_splash_welcome_language": "en",
        "op": "Continue",
        "form_build_id": form_build_id,
        "form_id": "eifu_splash_site_selection_form",
        "url": "",
    }).encode()
    _request(opener, f"{BASE_URL}/welcome", data=data,
             extra_headers={"Content-Type": "application/x-www-form-urlencoded"})

    terms_html, _ = _request(opener, f"{BASE_URL}/accept-terms-conditions")
    form_build_id = _extract_form_field(terms_html, "form_build_id")
    if not form_build_id:
        raise RuntimeError("Could not find form_build_id on terms page")

    data = urllib.parse.urlencode({
        "acknowledge": "1",
        "eifu_splash_welcome_language": "en",
        "op": "Continue",
        "form_build_id": form_build_id,
        "form_id": "eifu_splash_site_welcome_form",
        "url": "",
    }).encode()
    _request(opener, f"{BASE_URL}/accept-terms-conditions", data=data,
             extra_headers={"Content-Type": "application/x-www-form-urlencoded"})


# ---------------------------------------------------------------------------
# PDF URL extraction
# ---------------------------------------------------------------------------

def _decode_viewer_response(raw: str) -> str:
    """
    The viewpdf-iframe endpoint returns <textarea>[JSON]</textarea>.
    The JSON is a Drupal AJAX command array; the openDialog command's
    'data' field contains the inner HTML with the PDF iframe.
    Returns the inner HTML string, or the original raw if parsing fails.
    """
    import html as _html
    import json as _json

    m = re.search(r"<textarea[^>]*>(.*?)</textarea>", raw, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            commands = _json.loads(_html.unescape(m.group(1)))
            for cmd in commands:
                if cmd.get("command") == "openDialog":
                    return cmd.get("data") or ""
        except (ValueError, KeyError):
            pass
    return raw


def extract_pdf_url(viewer_html: str, base_url: str = BASE_URL) -> str | None:
    """
    After decoding the viewer response, look for the iframe with
    /viewpdf?file=%2FfetchPdf%2F... and return the absolute fetchPdf URL.
    Falls back to generic .pdf patterns.
    """
    # Primary: Drupal viewpdf iframe pattern
    # <iframe ... src="/viewpdf?file=%2FfetchPdf%2F47270%2F1%2F0%2Feifu%2Fname.pdf">
    m = re.search(r'<iframe[^>]+src=["\']([^"\']*(?:viewpdf|fetchPdf)[^"\']*)["\']',
                  viewer_html, re.IGNORECASE)
    if m:
        src = m.group(1)
        if "file=" in src:
            # decode the file= query param to get the fetchPdf path
            parsed = urllib.parse.urlparse(src)
            qs = urllib.parse.parse_qs(parsed.query)
            file_paths = qs.get("file") or qs.get("File")
            if file_paths:
                path = file_paths[0]
                if not path.startswith("http"):
                    path = base_url + path
                return path
        # Otherwise use the src directly
        if src.startswith("/"):
            return base_url + src
        return src

    # Generic fallbacks
    patterns = [
        r'<iframe[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'<embed[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'<object[^>]+data=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'"(?:uri|url|file_url|src)"\s*:\s*"([^"]+\.pdf[^"]*)"',
        r'["\'](/(?:sites|files|media|documents)[^"\']+\.pdf[^"\']*)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, viewer_html, re.IGNORECASE)
        if m:
            url = m.group(1)
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = base_url + url
            return url
    return None


# ---------------------------------------------------------------------------
# PDF fetch → in-memory bytes only
# ---------------------------------------------------------------------------

def fetch_pdf_bytes(opener: urllib.request.OpenerDirector, pdf_url: str) -> bytes:
    # URL-encode the path component only (preserve scheme+host+query)
    parsed = urllib.parse.urlparse(pdf_url)
    safe_path = urllib.parse.quote(parsed.path, safe="/")
    safe_url = urllib.parse.urlunparse(parsed._replace(path=safe_path))
    req = urllib.request.Request(safe_url, headers={
        **HEADERS,
        "Accept": "application/pdf,*/*;q=0.8",
    })
    with opener.open(req, timeout=TIMEOUT) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Parse in-memory PDF, no disk touch
# ---------------------------------------------------------------------------

def parse_pdf_text(pdf_bytes: bytes) -> list[str]:
    """Return list of page text strings. Bytes are never written to disk."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return pages


# ---------------------------------------------------------------------------
# Keyword search
# ---------------------------------------------------------------------------

def search_pages(pages: list[str], query: str) -> list[dict[str, Any]]:
    terms = [t.lower() for t in query.split() if t]
    hits = []
    for i, text in enumerate(pages):
        low = text.lower()
        if any(t in low for t in terms):
            # Find a 300-char window around first hit
            idx = next((low.find(t) for t in terms if t in low), 0)
            start = max(0, idx - 100)
            snippet = text[start:start + 300].replace("\n", " ").strip()
            hits.append({"page": i + 1, "snippet": snippet})
    return hits


# ---------------------------------------------------------------------------
# Top-level benchmark
# ---------------------------------------------------------------------------

def run(viewer_url: str, query: str) -> None:
    print(f"\n{'='*60}")
    print(f"Option B PoC — real-time fetch/parse/answer/discard")
    print(f"Viewer URL : {viewer_url}")
    print(f"Query      : {query!r}")
    print(f"{'='*60}\n")

    opener = build_opener()

    # Step 1 — session
    t0 = time.perf_counter()
    print("Step 1: Establishing e-IFU HCP session...")
    try:
        establish_session(opener)
    except Exception as exc:
        print(f"  WARN: Session gate may not have cleared: {exc}")
    t_session = time.perf_counter() - t0
    print(f"  session: {t_session*1000:.0f}ms")

    # Step 2 — viewer page → extract PDF URL
    t1 = time.perf_counter()
    print("Step 2: Fetching viewer page...")
    try:
        viewer_html, final_url = _request(opener, viewer_url)
    except urllib.error.HTTPError as exc:
        print(f"  FAIL: HTTP {exc.code} on viewer page")
        return
    t_viewer = time.perf_counter() - t1
    print(f"  viewer page: {t_viewer*1000:.0f}ms, {len(viewer_html)} chars, final_url={final_url}")

    inner_html = _decode_viewer_response(viewer_html)
    pdf_url = extract_pdf_url(inner_html)
    if not pdf_url:
        print("\n  Could not extract PDF URL from viewer page.")
        print("  Decoded inner HTML (first 2000 chars):")
        print(inner_html[:2000])
        return
    print(f"  PDF URL: {pdf_url}")

    # Step 3 — fetch PDF bytes into memory
    t2 = time.perf_counter()
    print("Step 3: Fetching PDF bytes (in-memory only)...")
    try:
        pdf_bytes = fetch_pdf_bytes(opener, pdf_url)
    except urllib.error.HTTPError as exc:
        print(f"  FAIL: HTTP {exc.code} fetching PDF")
        return
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return
    t_pdf = time.perf_counter() - t2
    print(f"  PDF fetch: {t_pdf*1000:.0f}ms, {len(pdf_bytes)/1024:.1f} KB")

    # Step 4 — parse PDF text in memory
    t3 = time.perf_counter()
    print("Step 4: Parsing PDF text...")
    try:
        pages = parse_pdf_text(pdf_bytes)
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return
    t_parse = time.perf_counter() - t3
    total_chars = sum(len(p) for p in pages)
    print(f"  parse: {t_parse*1000:.0f}ms, {len(pages)} pages, {total_chars} chars")

    # Step 5 — keyword search
    t4 = time.perf_counter()
    print(f"Step 5: Searching for {query!r}...")
    hits = search_pages(pages, query)
    t_search = time.perf_counter() - t4
    print(f"  search: {t_search*1000:.1f}ms, {len(hits)} hit(s)")
    for hit in hits[:3]:
        print(f"\n  -- Page {hit['page']} --")
        print(f"  {hit['snippet']}")

    # Step 6 — discard
    del pdf_bytes
    del pages
    print("\nStep 6: PDF bytes and parsed text discarded (no disk writes).")

    # Summary
    t_total = time.perf_counter() - t0
    print(f"\n{'='*60}")
    print(f"TIMING SUMMARY")
    print(f"  Session gate : {t_session*1000:.0f}ms")
    print(f"  Viewer page  : {t_viewer*1000:.0f}ms")
    print(f"  PDF fetch    : {t_pdf*1000:.0f}ms")
    print(f"  Parse        : {t_parse*1000:.0f}ms")
    print(f"  Search       : {t_search*1000:.1f}ms")
    print(f"  TOTAL        : {t_total*1000:.0f}ms")
    print(f"  Answer found : {'YES' if hits else 'NO (keyword not in doc)'}")
    print(f"{'='*60}\n")


def lookup_viewer_url_from_db(catalog: str) -> str | None:
    import sqlite3
    from pathlib import Path
    db = Path(__file__).parent / "chatifu.sqlite3"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "select document_url from ifu_links where catalog_number=? and document_url is not null limit 1",
            (catalog,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Option B PoC: real-time IFU fetch/parse/discard")
    parser.add_argument("--url", help="e-IFU viewpdf-iframe URL")
    parser.add_argument("--catalog", default="GIB00U0340", help="Catalog number (looks up URL from DB)")
    parser.add_argument("--query", default="contraindications", help="Keyword query to search")
    args = parser.parse_args()

    viewer_url = args.url
    if not viewer_url:
        viewer_url = lookup_viewer_url_from_db(args.catalog)
        if not viewer_url:
            # Use first known URL from ifu_links
            viewer_url = "https://www.e-ifu.com/viewpdf-iframe/24352/1/0/V0G000000000701"
            print(f"No DB hit for catalog {args.catalog!r}, using hardcoded test URL")

    run(viewer_url, args.query)


if __name__ == "__main__":
    main()
