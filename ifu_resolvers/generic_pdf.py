from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from medical_device_vocab import ParsedMedicalDeviceQuery

from .base import ResolvedIFU


class _PdfLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        href = attrs_dict.get("href")
        if href:
            self._current_href = urllib.parse.urljoin(self.base_url, html.unescape(href))
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href:
            text = re.sub(r"\s+", " ", " ".join(self._current_text)).strip()
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text = []


class GenericPdfResolver:
    """Generic fallback resolver for public manufacturer pages with PDF links."""

    def can_handle(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> bool:
        return bool(self._source_url(candidate))

    def resolve(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> ResolvedIFU | None:
        source_url = self._source_url(candidate)
        if not source_url:
            return None

        if self._looks_pdf(source_url):
            return self._resolved(candidate, source_url, source_url, 0.75)

        try:
            req = urllib.request.Request(
                source_url,
                headers={"User-Agent": "ChatIFU/1.0", "Accept": "text/html,application/pdf,*/*"},
            )
            with urllib.request.urlopen(req, timeout=12) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "").lower()
                body = response.read(1_000_000)
        except Exception:
            return None

        if "pdf" in content_type or self._looks_pdf(final_url):
            return self._resolved(candidate, source_url, final_url, 0.7)

        text = body.decode("utf-8", errors="ignore")
        parser = _PdfLinkParser(final_url)
        parser.feed(text)
        best = self._best_link(parser.links)
        if not best:
            return None
        return self._resolved(candidate, source_url, best, 0.55)

    def _source_url(self, candidate: dict[str, Any]) -> str | None:
        for key in ("document_url", "pdf_url", "source_url", "metadata_url", "manufacturer_url"):
            value = candidate.get(key)
            if value:
                return str(value)
        return None

    def _best_link(self, links: list[tuple[str, str]]) -> str | None:
        scored: list[tuple[int, str]] = []
        for href, text in links:
            low = f"{href} {text}".lower()
            score = 0
            if ".pdf" in low or "fetchpdf" in low:
                score += 100
            if any(term in low for term in ("ifu", "instructions for use", "directions for use", "manual")):
                score += 80
            if score:
                scored.append((score, href))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def _looks_pdf(self, url: str) -> bool:
        low = url.lower()
        return ".pdf" in low or "fetchpdf" in low or "/viewpdf" in low

    def _resolved(
        self,
        candidate: dict[str, Any],
        source_url: str,
        document_url: str,
        confidence: float,
    ) -> ResolvedIFU:
        return ResolvedIFU(
            manufacturer=candidate.get("company_name"),
            catalog=candidate.get("catalog_number") or candidate.get("catalog"),
            device_name=candidate.get("brand_name") or candidate.get("device_name"),
            document_url=document_url,
            pdf_url=document_url if self._looks_pdf(document_url) else None,
            iframe_url=document_url,
            open_full_ifu_url=document_url,
            source_url=source_url,
            source_type="generic_pdf",
            confidence=confidence,
        )
