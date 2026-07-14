"""
Real-time IFU fetch → parse → keyword search.
Public IFU PDFs may be cached by URL when a document cache is supplied.
"""
from __future__ import annotations

import html as _html
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable

import pypdf

from ifu_cache import IFUDocumentCache


BASE_URL = "https://www.e-ifu.com"
TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "what", "which", "who", "whom", "where", "when", "why", "how",
    "this", "that", "these", "those", "it", "its", "i", "me", "my",
    "you", "your", "he", "him", "his", "she", "her", "we", "our", "they",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "up",
    "about", "into", "and", "but", "or", "not", "no", "so", "if", "as",
    "get", "give", "tell", "show", "explain", "describe", "please", "list",
})

# FIX 1: Common English function/structure words for language detection.
# Multilingual IFUs mix languages per section; pages below 20% density are skipped.
_ENGLISH_WORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those",
    "at", "in", "on", "to", "of", "for", "with", "by", "from", "into", "up", "about",
    "and", "or", "but", "if", "as", "than",
    "is", "are", "be", "been", "was", "were", "have", "has", "had",
    "do", "does", "did", "will", "would", "can", "could", "should", "may", "must",
    "not", "use", "keep", "include",
    "it", "its", "they", "their", "all", "each", "any", "no",
    "only", "also", "such", "more", "when", "where", "which",
})

# FIX 2: Sentence boundary splitting — split after [.!?] + whitespace before uppercase.
_SENTENCE_END = re.compile(r'[.!?]\s+(?=[A-Z•])')
_MIN_SENTENCES = 2
_MAX_SENTENCES = 5

# FIX 5: Page limits to cap parse time on large PDFs.
_WARNING_TERMS = frozenset({
    "warning", "warnings", "caution", "cautions",
    "contraindication", "contraindications", "danger", "hazard",
    "precaution", "precautions", "adverse", "risk",
})
_PAGE_LIMIT = 150
_APPENDIX_PAGES = 30

ParsedPage = str | tuple[int, str]


# Hit strength, used to choose between a device's documents. A device can map to
# several official IFUs (a Synthes implant returns its device-specific IFU plus
# three generic processing procedures), so "which document answers the question"
# has to be decided on evidence. A document whose own section heading matches the
# question beats one that merely contains the words.
SCORE_SECTION_HEADING = 1000.0  # Pass 1: the document has the section asked for
SCORE_STORAGE_PHRASE = 500.0  # Pass 2: storage phrasing found without a heading
# Pass 3 scores by keyword coverage and stays below the tiers above.


@dataclass
class AnswerHit:
    page: int
    snippet: str
    section: str | None = None
    score: float = 0.0


@dataclass
class AnswerResult:
    hits: list[AnswerHit]
    source_url: str
    document_title: str | None
    timing_ms: dict[str, float]
    pdf_url: str | None = None
    document_url: str | None = None
    manufacturer_url: str | None = None
    iframe_url: str | None = None
    open_full_ifu_url: str | None = None
    page_count: int = 0
    error: str | None = None


class IFUAnswerer:
    """
    Session-cached real-time IFU fetch → parse → keyword search → discard.
    Thread-safe. PDF bytes are never written to disk.
    """

    def __init__(
        self,
        pdf_parser: Callable[[bytes], list[ParsedPage]] | None = None,
        document_cache: IFUDocumentCache | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._opener: urllib.request.OpenerDirector = _build_opener()
        self._session_ready = False
        # None → use built-in _parse_pdf_limited (respects page cap).
        # Tests inject a callable to skip real PDF parsing.
        self._pdf_parser = pdf_parser
        self._document_cache = document_cache

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def answer(
        self,
        document_url: str,
        question: str,
        max_hits: int = 5,
    ) -> AnswerResult:
        """Fetch, parse, search, discard. Thread-safe; no disk writes."""
        with self._lock:
            return self._answer_locked(document_url, question, max_hits)

    def fetch_pdf_bytes(self, document_url: str) -> tuple[bytes, str | None, str | None]:
        """Fetch the actual resolved IFU PDF bytes without writing them to disk."""
        with self._lock:
            return self._fetch_pdf_bytes_locked(document_url)

    # ------------------------------------------------------------------
    # Session management (called with lock held)
    # ------------------------------------------------------------------

    def _reset_session(self) -> None:
        self._opener = _build_opener()
        self._session_ready = False

    def _ensure_session(self) -> None:
        if self._session_ready:
            return
        welcome = self._http_get(f"{BASE_URL}/welcome")
        fb = _form_field(welcome, "form_build_id")
        if not fb:
            raise RuntimeError("form_build_id missing on welcome page")
        self._http_post(f"{BASE_URL}/welcome", {
            "site_user": "hcp",
            "eifu_splash_welcome_language": "en",
            "op": "Continue",
            "form_build_id": fb,
            "form_id": "eifu_splash_site_selection_form",
            "url": "",
        })
        terms = self._http_get(f"{BASE_URL}/accept-terms-conditions")
        fb = _form_field(terms, "form_build_id")
        if not fb:
            raise RuntimeError("form_build_id missing on terms page")
        post_resp = self._http_post(f"{BASE_URL}/accept-terms-conditions", {
            "acknowledge": "1",
            "eifu_splash_welcome_language": "en",
            "op": "Continue",
            "form_build_id": fb,
            "form_id": "eifu_splash_site_welcome_form",
            "url": "",
        })
        if _is_gate_page(post_resp):
            raise RuntimeError("Terms POST did not clear the session gate")
        self._session_ready = True

    # ------------------------------------------------------------------
    # HTTP helpers (called with lock held; no lock re-acquisition)
    # ------------------------------------------------------------------

    def _http_get(self, url: str) -> str:
        req = urllib.request.Request(url, headers=HEADERS)
        with self._opener.open(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _http_post(self, url: str, fields: dict[str, str]) -> str:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with self._opener.open(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _http_bytes(self, url: str) -> bytes:
        body, _final_url = self._http_bytes_with_url(url)
        return body

    def _http_bytes_with_url(self, url: str) -> tuple[bytes, str]:
        parsed = urllib.parse.urlparse(url)
        safe = urllib.parse.urlunparse(
            parsed._replace(path=urllib.parse.quote(parsed.path, safe="/%"))
        )
        req = urllib.request.Request(safe, headers={**HEADERS, "Accept": "application/pdf,*/*"})
        with self._opener.open(req, timeout=TIMEOUT) as resp:
            final_url = resp.geturl() if hasattr(resp, "geturl") else safe
            return resp.read(), final_url

    def _fetch_pdf_bytes_locked(self, document_url: str) -> tuple[bytes, str | None, str | None]:
        if _is_direct_pdf_url(document_url):
            pdf_bytes, final_pdf_url = self._http_bytes_with_url(document_url)
            return pdf_bytes, final_pdf_url or document_url, _title_from_url(final_pdf_url or document_url)

        self._ensure_session()
        viewer_raw = self._http_get(document_url)
        inner = decode_viewer(viewer_raw)
        if _is_gate_page(inner) or _is_gate_page(viewer_raw):
            self._reset_session()
            self._ensure_session()
            viewer_raw = self._http_get(document_url)
            inner = decode_viewer(viewer_raw)

        doc_title = extract_doc_title(inner)
        pdf_url = extract_pdf_url(inner)
        if not pdf_url:
            raise RuntimeError("Could not extract PDF URL from viewer page")
        if self._document_cache is not None:
            pdf_bytes, _doc, _cache_hit = self._document_cache.get_or_fetch(
                pdf_url,
                lambda: self._http_bytes_with_url(pdf_url),
            )
            final_pdf_url = pdf_url
        else:
            pdf_bytes, final_pdf_url = self._http_bytes_with_url(pdf_url)
        return pdf_bytes, final_pdf_url or pdf_url, doc_title

    # ------------------------------------------------------------------
    # Answer pipeline (called with lock held)
    # ------------------------------------------------------------------

    def _answer_locked(
        self,
        document_url: str,
        question: str,
        max_hits: int,
    ) -> AnswerResult:
        timing: dict[str, float] = {}
        t_total = time.perf_counter()
        direct_pdf = _is_direct_pdf_url(document_url)

        # Step 1 — session
        t0 = time.perf_counter()
        if direct_pdf:
            timing["session_ms"] = 0.0
        else:
            try:
                self._ensure_session()
            except Exception as exc:
                self._reset_session()
                return AnswerResult(
                    hits=[], source_url=document_url, document_title=None,
                    timing_ms={"session_ms": _ms(t0), "total_ms": _ms(t_total)},
                    error=f"Session error: {exc}",
                )
            timing["session_ms"] = _ms(t0)

        # Step 2 — viewer page → inner HTML → PDF URL
        t1 = time.perf_counter()
        if direct_pdf:
            doc_title = _title_from_url(document_url)
            pdf_url = document_url
            timing["viewer_ms"] = 0.0
        else:
            try:
                viewer_raw = self._http_get(document_url)
            except Exception as exc:
                if _is_auth_exc(exc):
                    self._reset_session()
                timing["viewer_ms"] = _ms(t1)
                return AnswerResult(
                    hits=[], source_url=document_url, document_title=None,
                    timing_ms={**timing, "total_ms": _ms(t_total)},
                    error=f"Viewer page fetch failed: {exc}",
                )

            inner = decode_viewer(viewer_raw)

            # Reset and retry once if the viewer returned a gate page
            if _is_gate_page(inner) or _is_gate_page(viewer_raw):
                self._reset_session()
                try:
                    self._ensure_session()
                    viewer_raw = self._http_get(document_url)
                    inner = decode_viewer(viewer_raw)
                except Exception as exc:
                    timing["viewer_ms"] = _ms(t1)
                    return AnswerResult(
                        hits=[], source_url=document_url, document_title=None,
                        timing_ms={**timing, "total_ms": _ms(t_total)},
                        error=f"Session retry failed: {exc}",
                    )

            timing["viewer_ms"] = _ms(t1)
            doc_title = extract_doc_title(inner)
            pdf_url = extract_pdf_url(inner)
            if not pdf_url:
                return AnswerResult(
                    hits=[], source_url=document_url, document_title=doc_title,
                    timing_ms={**timing, "total_ms": _ms(t_total)},
                    error="Could not extract PDF URL from viewer page",
                )

        # Step 3 — fetch PDF bytes into memory only
        t2 = time.perf_counter()
        try:
            cache_hit = False
            if self._document_cache is not None and not direct_pdf:
                pdf_bytes, _doc, cache_hit = self._document_cache.get_or_fetch(
                    pdf_url,
                    lambda: self._http_bytes_with_url(pdf_url),
                )
                final_pdf_url = pdf_url
            else:
                pdf_bytes, final_pdf_url = self._http_bytes_with_url(pdf_url)
        except Exception as exc:
            if _is_auth_exc(exc):
                self._reset_session()
            timing["fetch_ms"] = _ms(t2)
            return AnswerResult(
                hits=[], source_url=document_url, document_title=doc_title,
                timing_ms={**timing, "total_ms": _ms(t_total)},
                error=f"PDF fetch failed: {exc}",
                pdf_url=pdf_url,
                document_url=pdf_url,
                manufacturer_url=document_url,
                open_full_ifu_url=pdf_url,
            )
        timing["fetch_ms"] = _ms(t2)
        timing["cache_hit"] = 1.0 if cache_hit else 0.0
        pdf_url = final_pdf_url or pdf_url

        # Step 4 — parse (pdf_bytes never leaves memory; deleted after parse)
        t3 = time.perf_counter()
        try:
            if self._pdf_parser is not None:
                pages = self._pdf_parser(pdf_bytes)
            else:
                pages = _parse_pdf_limited(pdf_bytes, question)
        except Exception as exc:
            timing["parse_ms"] = _ms(t3)
            return AnswerResult(
                hits=[], source_url=document_url, document_title=doc_title,
                timing_ms={**timing, "total_ms": _ms(t_total)},
                error=f"PDF parse failed: {exc}",
                pdf_url=pdf_url,
                document_url=pdf_url,
                manufacturer_url=document_url,
                open_full_ifu_url=pdf_url,
            )
        finally:
            del pdf_bytes
        timing["parse_ms"] = _ms(t3)
        page_count = len(pages)

        # Step 5 — keyword search; text discarded immediately after
        t4 = time.perf_counter()
        hits = search_pages(pages, question, max_hits)
        del pages
        timing["search_ms"] = round(_ms(t4), 2)
        timing["total_ms"] = _ms(t_total)

        return AnswerResult(
            hits=hits,
            source_url=document_url,
            document_title=doc_title,
            timing_ms=timing,
            pdf_url=pdf_url,
            document_url=pdf_url,
            manufacturer_url=document_url,
            open_full_ifu_url=pdf_url,
            page_count=page_count,
        )


# ------------------------------------------------------------------
# Module-level helpers (also used by tests)
# ------------------------------------------------------------------

def _build_opener() -> urllib.request.OpenerDirector:
    jar = CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler(),
    )


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _form_field(html_str: str, name: str) -> str | None:
    for pattern in (
        rf'name="{re.escape(name)}"\s+value="([^"]*)"',
        rf'value="([^"]*)"\s+name="{re.escape(name)}"',
    ):
        m = re.search(pattern, html_str)
        if m:
            return _html.unescape(m.group(1))
    return None


def _is_gate_page(content: str) -> bool:
    low = content.lower()
    if "doc-info-row" in low or "/viewpdf-iframe/" in low or "/fetchpdf/" in low:
        return False
    if (
        "eifu_splash_site_selection_form" in low
        or 'name="site_user"' in low
        or "edit-site-user-hcp" in low
        or "accept-terms-conditions" in low
    ):
        return True
    if "access denied" in low or "forbidden" in low:
        return True
    return False


def _is_auth_exc(exc: BaseException) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)


def _is_direct_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    return path.endswith(".pdf") or "/fetchpdf/" in path


def _title_from_url(url: str) -> str | None:
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return name or None


def decode_viewer(raw: str) -> str:
    """
    The viewpdf-iframe endpoint returns <textarea>[Drupal AJAX JSON]</textarea>.
    The openDialog command's 'data' field holds the inner HTML with the PDF iframe.
    Exported so tests can call it directly.
    """
    m = re.search(r"<textarea[^>]*>(.*?)</textarea>", raw, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            commands = json.loads(_html.unescape(m.group(1)))
            for cmd in commands:
                if cmd.get("command") == "openDialog":
                    return cmd.get("data") or ""
        except (ValueError, KeyError, TypeError):
            pass
    return raw


def extract_doc_title(inner_html: str) -> str | None:
    m = re.search(r'class="pdf-name"[^>]*>(.*?)</span>', inner_html, re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
        return title or None
    return None


def extract_pdf_url(inner_html: str, base_url: str = BASE_URL) -> str | None:
    """
    Locate the fetchPdf URL from the viewer's iframe src.
    The pattern: <iframe src="/viewpdf?file=%2FfetchPdf%2F...pdf">
    """
    m = re.search(
        r'<iframe[^>]+src=["\']([^"\']*(?:viewpdf|fetchPdf)[^"\']*)["\']',
        inner_html, re.IGNORECASE,
    )
    if m:
        src = m.group(1)
        if "file=" in src:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(src).query)
            paths = qs.get("file") or qs.get("File")
            if paths:
                path = paths[0]
                return path if path.startswith("http") else base_url + path
        return src if src.startswith("http") else base_url + src

    for pattern in (
        r'<embed[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'<object[^>]+data=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'"(?:uri|url|src)"\s*:\s*"([^"]+\.pdf[^"]*)"',
        r'["\'](/(?:sites|files|media)[^"\']+\.pdf[^"\']*)["\']',
    ):
        m = re.search(pattern, inner_html, re.IGNORECASE)
        if m:
            url = m.group(1)
            if url.startswith("//"):
                return "https:" + url
            if url.startswith("/"):
                return base_url + url
            return url
    return None


def _parse_pdf(pdf_bytes: bytes) -> list[str]:
    """Parse all pages — kept for backward compatibility; production uses _parse_pdf_limited."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return pages


def _is_warning_question(question: str) -> bool:
    words = set(re.split(r"\W+", question.lower()))
    return bool(words & _WARNING_TERMS)


def _parse_pdf_limited(pdf_bytes: bytes, question: str) -> list[tuple[int, str]]:
    """Parse at most _PAGE_LIMIT pages; for warning questions also parse the last _APPENDIX_PAGES."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    if total <= _PAGE_LIMIT:
        indices: list[int] = list(range(total))
    elif _is_warning_question(question):
        front = list(range(_PAGE_LIMIT))
        back_start = max(_PAGE_LIMIT, total - _APPENDIX_PAGES)
        indices = front + list(range(back_start, total))
    else:
        indices = list(range(_PAGE_LIMIT))
    pages: list[tuple[int, str]] = []
    for i in indices:
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            text = ""
        pages.append((i + 1, text))
    return pages


def _is_english_page(text: str) -> bool:
    """Return True if >= 20% of words are common English function words."""
    words = [w for w in re.split(r"\W+", text.lower()) if w]
    if not words:
        return False
    if len(words) < 6:
        return True
    english_count = sum(1 for w in words if w in _ENGLISH_WORDS)
    return (english_count / len(words)) >= 0.20


def _split_sentences(text: str) -> list[tuple[int, int]]:
    """Return (start, end) character spans of sentences in text."""
    starts = [0]
    for m in _SENTENCE_END.finditer(text):
        starts.append(m.end())
    return [
        (starts[i], starts[i + 1] if i + 1 < len(starts) else len(text))
        for i in range(len(starts))
    ]


def search_pages(
    pages: list[ParsedPage],
    question: str,
    max_hits: int = 5,
) -> list[AnswerHit]:
    """
    Section-aware IFU extraction.  Maps the question to a target IFU section,
    locates its body in non-TOC pages, and returns a snippet from that body.

    Pass 1 — section heading search: find a matching section heading in non-TOC
              pages, extract its body, return a snippet.
    Pass 2 — storage phrase scan: if storage intent is detected but no heading
              was found, scan for storage-condition phrases (temperature, humidity,
              'store at', etc.) and return the densest match.
    Pass 3 — keyword fallback: score pages by term coverage.  For section-intent
              queries, require at least half the unique query terms to be present
              to avoid returning irrelevant pages when the section truly does not
              exist in the document.
    """
    # Pass 1 — section-aware heading search
    target_sections = _infer_target_sections(question)
    if target_sections:
        section_hits = _section_aware_search(pages, question, target_sections, max_hits)
        if section_hits:
            return section_hits

        # Pass 2 — storage phrase scan (only when Pass 1 found nothing)
        if _is_storage_question(question):
            storage_hits = _find_storage_passage(pages)
            if storage_hits:
                return storage_hits

    # Pass 3 — keyword page-scoring fallback (also skips TOC pages).
    # For section-intent queries, require at least half the unique terms to match
    # so that pages containing only a generic term like "device" are rejected when
    # the section simply does not exist in the document.
    raw_terms = [t.lower() for t in re.split(r"\W+", question) if t and len(t) >= 2]
    terms = [t for t in raw_terms if t not in _STOP_WORDS]
    if not terms:
        terms = raw_terms

    unique_terms = list(dict.fromkeys(terms))
    min_coverage = max(1, (len(unique_terms) + 1) // 2) if target_sections else 1

    scored_hits: list[tuple[int, int, int, AnswerHit]] = []
    for i, page in enumerate(pages):
        page_num, text = _page_number_and_text(i, page)
        if not _is_english_page(text):
            continue
        if _is_toc_page(text):
            continue
        low = text.lower()
        if not any(t in low for t in terms):
            continue
        coverage = sum(1 for t in unique_terms if t in low)
        if coverage < min_coverage:
            continue
        occurrences = sum(low.count(t) for t in unique_terms)
        snippet, anchor = _best_snippet_and_anchor(text, low, terms)
        section = _extract_section(text, anchor) or "Relevant IFU passage"
        scored_hits.append((
            coverage,
            occurrences,
            page_num,
            AnswerHit(page=page_num, snippet=snippet, section=section, score=float(coverage)),
        ))

    scored_hits.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [hit for _coverage, _occurrences, _page_num, hit in scored_hits[:max_hits]]


def _page_number_and_text(i: int, page: ParsedPage) -> tuple[int, str]:
    if isinstance(page, tuple):
        return page
    return i + 1, page


def _best_snippet(text: str, low: str, terms: list[str]) -> str:
    snippet, _ = _best_snippet_and_anchor(text, low, terms)
    return snippet


def _best_snippet_and_anchor(text: str, low: str, terms: list[str]) -> tuple[str, int]:
    """Return 2–5 complete sentences centered on the densest term cluster."""
    positions: list[int] = []
    for t in terms:
        idx = 0
        while True:
            pos = low.find(t, idx)
            if pos == -1:
                break
            positions.append(pos)
            idx = pos + 1

    anchor = positions[0] if positions else 0
    if len(positions) > 1:
        positions.sort()
        best_count = 0
        for pos in positions:
            start = max(0, pos - 80)
            end = start + 300
            count = sum(1 for p in positions if start <= p < end)
            if count > best_count:
                best_count = count
                anchor = pos

    spans = _split_sentences(text)
    if not spans:
        return _clean_snippet(text[:400]), anchor

    # Locate the sentence containing the anchor character
    anchor_idx = len(spans) - 1
    for i, (s, e) in enumerate(spans):
        if s <= anchor < e:
            anchor_idx = i
            break

    # Build a 2–5 sentence window centered on the anchor sentence
    start_idx = max(0, anchor_idx - 1)
    end_idx = min(len(spans), start_idx + _MAX_SENTENCES)
    if end_idx - start_idx < _MIN_SENTENCES:
        end_idx = min(len(spans), start_idx + _MIN_SENTENCES)

    raw = text[spans[start_idx][0]:spans[end_idx - 1][1]]
    return _clean_snippet(raw), anchor


def _clean_snippet(raw: str) -> str:
    snippet = re.sub(r"\s+", " ", raw).strip()
    # PDF extraction often leaves a dangling list marker at the edge of a
    # sentence window, for example "a." or "b.". Do not treat that as content.
    snippet = re.sub(r"\s+(?:[a-z]|[ivx]+)\.$", "", snippet, flags=re.IGNORECASE).strip()
    if snippet and not re.search(r"[.!?]$", snippet):
        m = list(re.finditer(r"[.!?](?=\s|$)", snippet))
        if m:
            snippet = snippet[:m[-1].end()].strip()
    return snippet


_SECTION_KEYWORDS = frozenset({
    "warning", "warnings", "caution", "cautions", "contraindication",
    "contraindications", "indication", "indications", "instruction",
    "instructions", "precaution", "precautions", "adverse", "storage",
    "sterile", "reuse", "single use", "description", "implantation",
})

# ------------------------------------------------------------------ #
# Section-aware extraction — editable question→section mapping        #
# ------------------------------------------------------------------ #

# Edit this table to add/adjust question keyword → IFU section mappings.
# Keys are lower-cased; multi-word phrases are matched before single words.
QUESTION_SECTION_MAP: dict[str, list[str]] = {
    "contraindication":   ["CONTRAINDICATIONS", "WARNINGS AND PRECAUTIONS", "WARNINGS", "PRECAUTIONS"],
    "contraindications":  ["CONTRAINDICATIONS", "WARNINGS AND PRECAUTIONS", "WARNINGS", "PRECAUTIONS"],
    "warning":            ["WARNINGS", "WARNINGS AND PRECAUTIONS", "CAUTIONS"],
    "warnings":           ["WARNINGS", "WARNINGS AND PRECAUTIONS", "CAUTIONS"],
    "precaution":         ["PRECAUTIONS", "WARNINGS AND PRECAUTIONS", "WARNINGS"],
    "precautions":        ["PRECAUTIONS", "WARNINGS AND PRECAUTIONS", "WARNINGS"],
    "shelf life":         ["STORAGE", "STORAGE AND HANDLING", "SHELF LIFE", "HOW SUPPLIED"],
    "storage":            ["STORAGE", "STORAGE AND HANDLING", "STORAGE CONDITIONS", "HOW SUPPLIED"],
    "shelf":              ["STORAGE", "STORAGE AND HANDLING", "HOW SUPPLIED"],
    "store":              ["STORAGE", "STORAGE AND HANDLING", "HOW SUPPLIED"],
    "temperature":        ["STORAGE", "STORAGE AND HANDLING", "STORAGE CONDITIONS", "HOW SUPPLIED"],
    "magnetic resonance": ["MRI SAFETY", "MRI SAFETY INFORMATION", "MAGNETIC RESONANCE IMAGING"],
    "mr safe":            ["MRI SAFETY", "MRI SAFETY INFORMATION", "MRI INFORMATION"],
    "mr conditional":     ["MRI SAFETY", "MRI SAFETY INFORMATION", "MRI INFORMATION"],
    "mri":                ["MRI SAFETY", "MRI SAFETY INFORMATION", "MRI INFORMATION",
                           "MAGNETIC RESONANCE", "MAGNETIC RESONANCE IMAGING"],
    "indication":         ["INDICATIONS", "INDICATIONS FOR USE", "INTENDED USE"],
    "indications":        ["INDICATIONS", "INDICATIONS FOR USE", "INTENDED USE"],
    "intended use":       ["INTENDED USE", "INDICATIONS FOR USE", "INDICATIONS"],
    "cleaning":           ["CLEANING", "CLEANING AND STERILIZATION", "REPROCESSING"],
    "steriliz":           ["STERILIZATION", "CLEANING AND STERILIZATION", "REPROCESSING"],
    "adverse":            ["ADVERSE EVENTS", "ADVERSE EFFECTS", "COMPLICATIONS"],
    "complication":       ["COMPLICATIONS", "ADVERSE EVENTS"],
}

# Numbered section heading: "2.0 SECTION NAME" or "12.1 MRI Safety Information"
_NUMBERED_HEADING_RE = re.compile(
    r'^(\d{1,2}(?:\.\d{1,2})*)\s+([A-Z]\S+(?:\s+\S+){0,6})\s*$'
)

# Known IFU section names (lowercase) for title-case heading detection.
_HEADING_SYNONYMS: frozenset[str] = frozenset({
    "contraindications", "contraindication", "when not to use", "do not use if",
    "warnings", "warning", "precautions", "precaution", "cautions", "caution",
    "warnings and precautions", "precautions and warnings",
    "storage", "storage and handling", "storage conditions", "storage information",
    "how supplied", "how to store", "shelf life",
    "indications", "indication", "indications for use", "intended use",
    "cleaning", "sterilization", "cleaning and sterilization", "reprocessing",
    "mri safety", "mri safety information", "mri information", "mr safety",
    "magnetic resonance imaging",
    "adverse events", "adverse effects", "complications",
    "device description", "description", "implantation",
    "directions for use", "instructions for use", "specifications",
})


def _infer_target_sections(question: str) -> list[str]:
    """Map question text to priority-ordered IFU section names."""
    q = question.lower()
    for phrase in sorted(QUESTION_SECTION_MAP, key=len, reverse=True):
        if phrase in q:
            return QUESTION_SECTION_MAP[phrase]
    return []


def _is_toc_page(text: str) -> bool:
    """
    True when this page is primarily a table-of-contents / index page.
    Uses fraction-based thresholds so a mixed page (e.g. multilingual directory
    embedded on the same page as real content) is NOT misclassified.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    # A line qualifies as a TOC line if it has a dot-leader pattern
    toc_lines = sum(1 for ln in lines if re.search(r'\.{3,}', ln))
    if toc_lines >= 3 and toc_lines / len(lines) >= 0.40:
        return True
    # Also catch TOCs without dot leaders: section-name + trailing page number
    ref_lines = sum(1 for ln in lines if re.search(r'[A-Z]{3,}.*\s\d{1,3}\s*$', ln))
    return ref_lines >= 3 and ref_lines / len(lines) >= 0.40


def _is_body_heading(line: str) -> tuple[bool, str]:
    """
    Return (is_heading, normalised_section_name).
    Accepts numbered headings and short all-caps lines; rejects TOC entries and
    sentence-like lines (ending with '.').
    """
    clean = re.sub(r'\s+', ' ', line).strip()
    if not clean or len(clean) > 120:
        return False, ""
    if re.match(r'^\d+\s*$', clean):
        return False, ""
    # Reject dot-leader TOC entries
    if re.search(r'\.{3,}', clean):
        return False, ""
    # Reject heading text + trailing bare page number (TOC without dot leaders)
    if re.search(r'\s\d{1,3}\s*$', clean):
        text_part = re.sub(r'\s+\d{1,3}\s*$', '', clean).strip()
        letters = [c for c in text_part if c.isalpha()]
        if letters and sum(1 for c in letters if c.isupper()) / len(letters) >= 0.80:
            return False, ""
        if text_part.lower() in _HEADING_SYNONYMS:
            return False, ""

    # Numbered heading: "2.0 CONTRAINDICATIONS" / "12.1 MRI Safety Information"
    m = _NUMBERED_HEADING_RE.match(clean)
    if m:
        section_text = m.group(2).strip()
        if not section_text.endswith('.') and len(section_text.split()) <= 7:
            return True, section_text.upper()

    # Short all-caps heading line (no trailing period)
    if not clean.endswith('.'):
        letters = [c for c in clean if c.isalpha()]
        if not letters:
            return False, ""
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio >= 0.85 and 4 <= len(clean) <= 80 and len(clean.split()) <= 7:
            return True, clean.upper()

    # Title-case heading matching a known IFU section name (e.g. "Storage", "Contraindications")
    if not clean.endswith('.'):
        candidate = clean.rstrip(':').strip()
        if candidate.lower() in _HEADING_SYNONYMS and 1 <= len(candidate.split()) <= 6:
            return True, candidate.upper()

    return False, ""


# Storage-condition phrase patterns for phrase-based fallback detection.
# Split into "dedicated" indicators (unambiguous storage context) and
# "contextual" indicators (temperature values that could appear in MRI/clinical text).
# _find_storage_passage requires at least one dedicated indicator per page.
_STORAGE_INDICATORS_RE = re.compile(
    r'(?:'
    r'store(?:d)?\s+(?:at|below|in\s+a\s+cool)'
    r'|storage\s+(?:temperature|condition|information)'
    r'|cool[,\s]+dry\s+place'
    r'|do\s+not\s+freeze'
    r'|avoid\s+freezing'
    r'|shelf\s+life'
    r'|expir(?:y|ation)\s+date'
    r'|protected?\s+from\s+(?:light|heat)'
    r'|\d+\s*°\s*[CF]'
    r'|relative\s+humidity'
    r'|temperature[:\s]+\d'
    r')',
    re.IGNORECASE,
)

# Patterns that unambiguously indicate a storage context (not MRI/clinical).
_STORAGE_DEDICATED_RE = re.compile(
    r'(?:'
    r'store(?:d)?\s+(?:at|below|in\s+a\s+cool)'
    r'|storage\s+(?:temperature|condition|information)'
    r'|cool[,\s]+dry\s+place'
    r'|do\s+not\s+freeze'
    r'|avoid\s+freezing'
    r'|shelf\s+life'
    r'|expir(?:y|ation)\s+date'
    r'|relative\s+humidity'
    r')',
    re.IGNORECASE,
)

_STORAGE_ANCHOR_TERMS = [
    "temperature", "humidity", "storage", "store", "freeze",
    "cool", "shelf", "celsius", "fahrenheit", "dry",
]


def _is_storage_question(question: str) -> bool:
    q = question.lower()
    return any(w in q for w in ("storage", "shelf life", "shelf", "store", "temperature", "cool"))


def _find_storage_passage(pages: list[ParsedPage]) -> list[AnswerHit]:
    """
    Phrase-based storage passage finder used when no STORAGE section heading exists.
    Scans non-TOC English pages for storage-condition patterns (temperature ranges,
    'store at', 'do not freeze', shelf life, etc.) and returns the best matching passage.
    """
    best_score = 0
    best_hit: AnswerHit | None = None

    for i, page in enumerate(pages):
        page_num, text = _page_number_and_text(i, page)
        if not _is_english_page(text):
            continue
        if _is_toc_page(text):
            continue
        matches = _STORAGE_INDICATORS_RE.findall(text)
        if not matches:
            continue
        # Require at least one dedicated storage indicator so that temperature
        # values in MRI safety or clinical sections (e.g. "3°C after scanning")
        # do not masquerade as storage pages.
        if not _STORAGE_DEDICATED_RE.search(text):
            continue
        score = len(matches)
        if score > best_score:
            best_score = score
            low = text.lower()
            snippet, _ = _best_snippet_and_anchor(text, low, _STORAGE_ANCHOR_TERMS)
            if snippet:
                best_hit = AnswerHit(
                    page=page_num,
                    snippet=snippet,
                    section="Storage Conditions",
                    score=SCORE_STORAGE_PHRASE,
                )

    return [best_hit] if best_hit else []


def _section_name_matches(heading: str, target: str) -> bool:
    """True when a detected heading name corresponds to a target section."""
    h = heading.upper().strip()
    t = target.upper().strip()
    if t in h or h in t:
        return True
    fill = {"AND", "FOR", "OF", "THE", "IN", "TO", "A", "AN", "WITH", "OR"}
    t_words = set(t.split()) - fill
    h_words = set(h.split())
    return bool(t_words) and t_words <= h_words


def _extract_section_body(
    pages: list[ParsedPage],
    start_page_idx: int,
    heading_end_pos: int,
    max_chars: int = 3000,
) -> str:
    """
    Extract the section body starting just after the heading at
    (start_page_idx, heading_end_pos), continuing across pages until the next
    section heading or max_chars is reached.
    """
    parts: list[str] = []
    total = 0
    first_segment = True

    for i in range(start_page_idx, len(pages)):
        _, text = _page_number_and_text(i, pages[i])
        segment = text[heading_end_pos:] if i == start_page_idx else text
        heading_end_pos = 0

        stop = False
        for raw_line in segment.splitlines(True):
            stripped = raw_line.strip()
            if first_segment and not stripped:
                continue  # skip blank lines immediately after the heading
            first_segment = False
            if stripped:
                is_h, _ = _is_body_heading(stripped)
                if is_h:
                    stop = True
                    break
            parts.append(raw_line)
            total += len(raw_line)
            if total >= max_chars:
                stop = True
                break

        if stop:
            break

    return re.sub(r'\s+', ' ', ''.join(parts)).strip()


def _section_aware_search(
    pages: list[ParsedPage],
    question: str,
    target_sections: list[str],
    max_hits: int,
) -> list[AnswerHit]:
    """
    Locate target section headings in non-TOC pages and extract their bodies.
    """
    terms = [t.lower() for t in re.split(r'\W+', question) if t and len(t) >= 2]
    terms = [t for t in terms if t not in _STOP_WORDS] or terms
    hits: list[AnswerHit] = []

    for i, page in enumerate(pages):
        page_num, text = _page_number_and_text(i, page)
        if not _is_english_page(text):
            continue
        if _is_toc_page(text):
            continue

        pos = 0
        for raw_line in text.splitlines(True):
            stripped = raw_line.strip()
            if stripped:
                is_h, section_name = _is_body_heading(stripped)
                if is_h:
                    for target in target_sections:
                        if _section_name_matches(section_name, target):
                            heading_end = pos + len(raw_line)
                            body = _extract_section_body(pages, i, heading_end)
                            if body:
                                low = body.lower()
                                snippet, _ = _best_snippet_and_anchor(body, low, terms)
                                if not snippet:
                                    snippet = _clean_snippet(body[:400])
                                hits.append(AnswerHit(
                                    page=page_num,
                                    snippet=snippet,
                                    section=section_name,
                                    score=SCORE_SECTION_HEADING,
                                ))
                            break
            pos += len(raw_line)

        if len(hits) >= max_hits:
            break

    return hits


def _extract_section(text: str, anchor: int) -> str | None:
    """Find a nearby heading-like line before the matched passage."""
    lines: list[tuple[int, int, str]] = []
    pos = 0
    for raw in text.splitlines(True):
        stripped = raw.strip()
        start = pos + raw.find(stripped) if stripped else pos
        end = pos + len(raw)
        if stripped:
            lines.append((start, end, stripped))
        pos = end

    if not lines:
        return None

    line_idx = 0
    for idx, (_start, end, _line) in enumerate(lines):
        if anchor < end:
            line_idx = idx
            break

    for _start, _end, line in reversed(lines[max(0, line_idx - 12):line_idx + 1]):
        section = _section_from_heading_line(line)
        if section:
            return section
    return None


def _section_from_heading_line(line: str) -> str | None:
    clean = re.sub(r"\s+", " ", line).strip(" :-")
    if len(clean) < 3 or len(clean) > 100:
        return None

    before_colon = clean.split(":", 1)[0].strip()
    candidate = before_colon if len(before_colon) <= 60 else clean
    lower = candidate.lower()
    if any(keyword in lower for keyword in _SECTION_KEYWORDS):
        return candidate[:80]

    letters = [c for c in candidate if c.isalpha()]
    if not letters:
        return None
    uppercase_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if uppercase_ratio >= 0.70 and not candidate.endswith("."):
        return candidate[:80]
    return None
