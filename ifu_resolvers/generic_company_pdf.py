from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from medical_device_vocab import ParsedMedicalDeviceQuery

from .base import ResolvedIFU
from .company_configs import CompanyResolverConfig, config_for_company_name


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        href = attrs_dict.get("href")
        if href:
            self._href = urllib.parse.urljoin(self.base_url, html.unescape(href))
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append((self._href, re.sub(r"\s+", " ", " ".join(self._text)).strip()))
            self._href = None
            self._text = []


class GenericCompanyPdfResolver:
    def __init__(
        self,
        config: CompanyResolverConfig | None = None,
        opener: Any | None = None,
        timeout: int = 12,
    ) -> None:
        self.config = config
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout
        self.last_failure: str | None = None

    def can_handle(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> bool:
        return self._config(candidate) is not None and self._source_url(candidate) is not None

    def resolve(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> ResolvedIFU | None:
        self.last_failure = None
        config = self._config(candidate)
        source_url = self._source_url(candidate)
        if not config or not source_url:
            self.last_failure = "missing company config or source URL"
            return None
        if not _safe_https(source_url):
            self.last_failure = "unsafe or non-HTTPS source URL"
            return None
        allowed_domains = set(config.domains) | {_host(source_url)}
        if not _domain_allowed(source_url, allowed_domains):
            self.last_failure = "source URL outside allowed domains"
            return None
        if _looks_document(source_url):
            return self._resolved(candidate, source_url, source_url, "generic_company_pdf", 0.7)

        try:
            req = urllib.request.Request(source_url, headers={"User-Agent": "ChatIFU/1.0", "Accept": "text/html,application/pdf,*/*"})
            with self.opener.open(req, timeout=self.timeout) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "").lower()
                body = response.read(1_000_000)
        except Exception as exc:
            self.last_failure = f"fetch failed: {exc}"
            return None

        if "pdf" in content_type or _looks_document(final_url):
            return self._resolved(candidate, source_url, final_url, "generic_company_pdf", 0.72)

        parser = _LinkParser(final_url)
        parser.feed(body.decode("utf-8", errors="ignore"))
        best = self._best_link(parser.links, candidate, config, allowed_domains)
        if not best:
            self.last_failure = "no IFU/PDF link found"
            return None
        return self._resolved(candidate, source_url, best, "generic_company_pdf", 0.62)

    def _config(self, candidate: dict[str, Any]) -> CompanyResolverConfig | None:
        return self.config or config_for_company_name(candidate.get("company_name"))

    def _source_url(self, candidate: dict[str, Any]) -> str | None:
        for key in ("document_url", "pdf_url", "source_url", "metadata_url", "manufacturer_url"):
            value = candidate.get(key)
            if value:
                return str(value)
        return None

    def _best_link(
        self,
        links: list[tuple[str, str]],
        candidate: dict[str, Any],
        config: CompanyResolverConfig,
        allowed_domains: set[str],
    ) -> str | None:
        identity = " ".join(str(candidate.get(k) or "").lower() for k in ("catalog_number", "model_number", "brand_name"))
        scored: list[tuple[int, str]] = []
        for href, text in links:
            low = f"{href} {text}".lower()
            if not _safe_https(href) or not _domain_allowed(href, allowed_domains):
                continue
            if any(term in low for term in config.deny_keywords):
                continue
            score = 0
            if any(term in low for term in config.pdf_link_keywords):
                score += 100
            if any(term in low for term in config.ifu_link_keywords):
                score += 80
            for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", identity):
                if token in low:
                    score += 20
            if score:
                scored.append((score, href))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def _resolved(
        self,
        candidate: dict[str, Any],
        source_url: str,
        document_url: str,
        source_type: str,
        confidence: float,
    ) -> ResolvedIFU:
        return ResolvedIFU(
            manufacturer=candidate.get("company_name"),
            catalog=candidate.get("catalog_number") or candidate.get("catalog"),
            device_name=candidate.get("brand_name") or candidate.get("device_name"),
            document_url=document_url,
            pdf_url=document_url if _looks_document(document_url) else None,
            iframe_url=document_url if _looks_document(document_url) else None,
            open_full_ifu_url=document_url,
            source_url=source_url,
            source_type=source_type,
            confidence=confidence,
        )


def _safe_https(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _domain_allowed(url: str, domains: set[str]) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in domains if domain)


def _looks_document(url: str) -> bool:
    low = url.lower()
    return ".pdf" in low or "fetchpdf" in low or "/viewpdf" in low
