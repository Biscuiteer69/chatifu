from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


VAULT_DIR = Path(__file__).resolve().parents[1]
SQLITE_PATH = VAULT_DIR / "chatifu.sqlite3"
BASE_URL = "https://www.e-ifu.com"
DEFAULT_DELAY_SEC = 2.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
FOUND_STATUS = "found"
CANDIDATE_STATUS = "candidate_broad"
# Below this length a brand name ("ECHO", "PS") collides with ordinary words.
MIN_BRAND_LEN = 5
# The portal substring-matches the query against document metadata, so a SHORT
# identifier collides by chance: catalog 00825 returns MENTOR documents because
# LAB100825478v3_eIFU.pdf contains "00825". Only trust a portal hit on an exact
# identifier when the identifier is long enough for such a collision to be
# implausible. Synthes models (02.007.026 -> 8 alphanumerics) clear this; the
# 5-digit GYNECARE catalogs that produced the false matches do not.
MIN_PORTAL_TERM_LEN = 6
# Synthes/DePuy model numbers: 02.007.026, 04.535.328S.
_DOTTED_MODEL_RE = re.compile(r"^\d+\.\d+\.\d+")
NOT_FOUND_STATUS = "not_found"
SESSION_GATE_STATUS = "session_gate"
AUTH_FAILED_STATUS = "auth_failed"
INIT_FAILED_STATUS = "init_failed"
HTTP_ERROR_STATUS = "http_error"
NETWORK_ERROR_STATUS = "network_error"
TIMEOUT_STATUS = "timeout"
STABLE_STATUSES = {
    FOUND_STATUS,
    CANDIDATE_STATUS,
    NOT_FOUND_STATUS,
    SESSION_GATE_STATUS,
    AUTH_FAILED_STATUS,
    INIT_FAILED_STATUS,
    HTTP_ERROR_STATUS,
    NETWORK_ERROR_STATUS,
    TIMEOUT_STATUS,
}


@dataclass(frozen=True)
class DeviceRef:
    rowid: int | None
    primary_di: str | None
    catalog_number: str
    model_number: str | None = None


class ResolverFailure(Exception):
    status = INIT_FAILED_STATUS
    error_type = "resolver_failure"


class InitFailure(ResolverFailure):
    status = INIT_FAILED_STATUS
    error_type = "init_failed"


class AuthFailure(ResolverFailure):
    status = AUTH_FAILED_STATUS
    error_type = "auth_failed"


class SessionGateFailure(ResolverFailure):
    status = SESSION_GATE_STATUS
    error_type = "session_gate"


class SearchResultParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.documents: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._current_link: dict[str, Any] | None = None
        self._capture_text: str | None = None
        self._pending_detail_label: str | None = None
        self._doc_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())

        if tag == "div" and "doc-info-row" in classes:
            self._current = {
                "document_url": None,
                "document_title": None,
                "language": "en",
                "revision": None,
                "source_file_name": None,
            }
            self._doc_depth = 1
            return

        if self._current is None:
            return

        if tag == "div":
            self._doc_depth += 1

        if tag == "a":
            href = attrs_dict.get("href", "")
            link_classes = set(attrs_dict.get("class", "").split())
            if "use-ajax" in link_classes and "/viewpdf-iframe/" in href:
                self._current_link = {
                    "href": urllib.parse.urljoin(self.base_url, href),
                    "text": "",
                }
            return

        if tag == "span":
            if "doc-metadata-version" in classes:
                self._capture_text = "revision"
            elif "file-name-label" in classes:
                self._pending_detail_label = "file-name"
            elif "file-name" in classes and self._pending_detail_label == "file-name":
                self._capture_text = "source_file_name"
            elif "language-label" in classes:
                self._pending_detail_label = "language"
            elif "language" in classes and self._pending_detail_label == "language":
                self._capture_text = "language"

    def handle_data(self, data: str) -> None:
        if self._current_link is not None:
            self._current_link["text"] += data
        if self._current is not None and self._capture_text:
            existing = self._current.get(self._capture_text) or ""
            self._current[self._capture_text] = existing + data

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return

        if tag == "a" and self._current_link is not None:
            text = normalize_space(self._current_link["text"])
            if text and not self._current.get("document_title"):
                self._current["document_title"] = text
                self._current["document_url"] = self._current_link["href"]
            self._current_link = None
            return

        if tag == "span" and self._capture_text:
            value = self._current.get(self._capture_text)
            if isinstance(value, str):
                self._current[self._capture_text] = normalize_space(value)
            self._capture_text = None
            return

        if tag == "div":
            self._doc_depth -= 1
            if self._doc_depth <= 0:
                if self._current.get("document_url"):
                    document = dict(self._current)
                    if document.get("revision") in ("", "N/A"):
                        document["revision"] = None
                    if document.get("source_file_name") in ("", "N/A"):
                        document["source_file_name"] = None
                    if document not in self.documents:
                        self.documents.append(document)
                self._current = None
                self._doc_depth = 0


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def extract_form_value(content: str, name: str) -> str:
    pattern = rf'name="{re.escape(name)}"\s+value="([^"]+)"|value="([^"]+)"\s+name="{re.escape(name)}"'
    match = re.search(pattern, content)
    if not match:
        raise ValueError(f"Could not find form field {name!r}.")
    return html.unescape(match.group(1) or match.group(2))


def detect_gate_page(content: str) -> str | None:
    text = content.lower()
    if not text.strip():
        return None
    has_document_markers = "doc-info-row" in text or "/viewpdf-iframe/" in text
    if has_document_markers:
        return None
    if "eifu_splash_site_selection_form" in text:
        return "welcome"
    if 'name="site_user"' in text or "edit-site-user-hcp" in text:
        return "welcome"
    if "eifu_splash_site_welcome_form" in text:
        return "terms"
    if 'name="acknowledge"' in text or "edit-acknowledge" in text:
        return "terms"
    if "/welcome" in text and "healthcare professional" in text:
        return "welcome"
    if "accept-terms-conditions" in text and (
        "<form" in text or 'type="submit"' in text or "edit-submit" in text
    ):
        return "terms"
    if "access denied" in text or "not authorized" in text or "forbidden" in text:
        return "auth"
    return None


def classify_document_status(match_confidence: str | None) -> str:
    if match_confidence in {"exact_catalog", "model_match", "brand_match", "model_portal_match"}:
        return FOUND_STATUS
    return CANDIDATE_STATUS


def classify_error(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, ResolverFailure):
        return exc.status, exc.error_type
    if isinstance(exc, urllib.error.HTTPError):
        return HTTP_ERROR_STATUS, f"http_{exc.code}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return TIMEOUT_STATUS, type(exc).__name__
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return TIMEOUT_STATUS, type(reason).__name__
        return NETWORK_ERROR_STATUS, type(reason).__name__
    return INIT_FAILED_STATUS, type(exc).__name__


class EifuResolver:
    def __init__(
        self,
        db_path: str | Path = SQLITE_PATH,
        delay_sec: float = DEFAULT_DELAY_SEC,
        timeout_sec: int = 15,
    ) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self.timeout_sec = timeout_sec
        self._last_request_at = 0.0
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
            urllib.request.HTTPRedirectHandler(),
        )
        self._session_ready = False

    def resolve(
        self,
        catalog_number: str,
        model_number: str | None = None,
        device_rowid: int | None = None,
        primary_di: str | None = None,
        log_to_db: bool = True,
    ) -> list[dict[str, Any]]:
        catalog_number = catalog_number.strip()
        if not catalog_number:
            raise ValueError("catalog_number is required.")

        search_terms = search_terms_for(catalog_number, model_number)
        source_url = f"{BASE_URL}/search-document-metadata/{urllib.parse.quote(catalog_number)}"
        error_type = None
        try:
            brand_names = self.brands_for_catalog(catalog_number)
            documents = []
            for term in search_terms:
                content = self._search(term)
                gate = detect_gate_page(content)
                if gate:
                    raise SessionGateFailure(f"Search returned {gate} gate page.")
                documents = self._parse_documents(
                    content,
                    catalog_number,
                    model_number,
                    brand_names=brand_names,
                )
                if documents:
                    source_url = f"{BASE_URL}/search-document-metadata/{urllib.parse.quote(term)}"
                    # A hit from the model fallback (the catalog found nothing)
                    # is the portal asserting applicability for that exact
                    # model — trust it, but only if the identifier is long
                    # enough not to collide by chance inside a file name.
                    if term != catalog_number and is_distinctive_identifier(term):
                        promote_portal_model_hits(documents)
                    break
            status = FOUND_STATUS if any(
                classify_document_status(document.get("match_confidence")) == FOUND_STATUS
                for document in documents
            ) else (CANDIDATE_STATUS if documents else NOT_FOUND_STATUS)
        except (ResolverFailure, urllib.error.HTTPError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            documents = []
            status, error_type = classify_error(exc)
        except Exception as exc:
            documents = []
            status, error_type = INIT_FAILED_STATUS, type(exc).__name__

        if log_to_db:
            self.log_results(
                DeviceRef(device_rowid, primary_di, catalog_number, model_number),
                source_url,
                documents,
                status,
                error_type=error_type,
            )
        return documents

    def resolve_device_row(self, row: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
        raw_value = row_get(row, "raw_json")
        raw_json = json.loads(raw_value) if raw_value else {}
        return self.resolve(
            catalog_number=row_get(row, "catalog_number"),
            model_number=row_get(row, "model_number"),
            device_rowid=row_get(row, "rowid"),
            primary_di=raw_json.get("PrimaryDI"),
        )

    def _search(self, catalog_number: str) -> str:
        self._ensure_session()
        url = f"{BASE_URL}/search-document-metadata/{urllib.parse.quote(catalog_number)}"
        return self._request(url)

    def _ensure_session(self) -> None:
        if self._session_ready:
            return

        try:
            welcome = self._request(f"{BASE_URL}/welcome", delay=False)
            form_build_id = extract_form_value(welcome, "form_build_id")
        except (urllib.error.HTTPError, TimeoutError, socket.timeout, urllib.error.URLError):
            raise
        except Exception as exc:
            raise InitFailure(str(exc)) from exc

        self._post(
            f"{BASE_URL}/welcome",
            {
                "site_user": "hcp",
                "eifu_splash_welcome_language": "en",
                "op": "Continue",
                "form_build_id": form_build_id,
                "form_id": "eifu_splash_site_selection_form",
                "url": "",
            },
            delay=False,
        )

        try:
            terms = self._request(f"{BASE_URL}/accept-terms-conditions", delay=False)
            form_build_id = extract_form_value(terms, "form_build_id")
        except (urllib.error.HTTPError, TimeoutError, socket.timeout, urllib.error.URLError):
            raise
        except Exception as exc:
            raise InitFailure(str(exc)) from exc

        response = self._post(
            f"{BASE_URL}/accept-terms-conditions",
            {
                "acknowledge": "1",
                "eifu_splash_welcome_language": "en",
                "op": "Continue",
                "form_build_id": form_build_id,
                "form_id": "eifu_splash_site_welcome_form",
                "url": "",
            },
            delay=False,
        )
        if detect_gate_page(response):
            raise AuthFailure("Terms acknowledgement did not clear the session gate.")
        self._session_ready = True

    def _request(self, url: str, delay: bool = True) -> str:
        if delay:
            self._rate_limit()
        req = urllib.request.Request(url, headers=HEADERS)
        with self._opener.open(req, timeout=self.timeout_sec) as response:
            return response.read().decode("utf-8", errors="ignore")

    def _post(self, url: str, fields: dict[str, str], delay: bool = True) -> str:
        if delay:
            self._rate_limit()
        data = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=data, headers=headers)
        with self._opener.open(req, timeout=self.timeout_sec) as response:
            return response.read().decode("utf-8", errors="ignore")

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)
        self._last_request_at = time.monotonic()

    def brands_for_catalog(self, catalog_number: str) -> list[str]:
        """GUDID brand names registered for this catalog, used to verify titles."""
        if not Path(self.db_path).exists():
            return []
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            rows = conn.execute(
                "SELECT DISTINCT brand_name FROM devices "
                "WHERE catalog_number = ? AND brand_name IS NOT NULL AND brand_name != ''",
                (catalog_number,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [str(row[0]) for row in rows]

    def _parse_documents(
        self,
        content: str,
        catalog_number: str,
        model_number: str | None,
        brand_names: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        parser = SearchResultParser(BASE_URL)
        parser.feed(content)
        documents = []
        seen_urls = set()
        for document in parser.documents:
            document_url = document.get("document_url")
            if not document_url or document_url in seen_urls:
                continue
            seen_urls.add(document_url)
            title = document.get("document_title") or "Untitled e-IFU document"
            documents.append(
                {
                    "document_url": document_url,
                    "document_title": title,
                    "language": document.get("language") or "en",
                    "revision": document.get("revision"),
                    "match_confidence": match_confidence(
                        title,
                        catalog_number,
                        model_number,
                        source_file_name=document.get("source_file_name"),
                        brand_names=brand_names,
                    ),
                    "source_file_name": document.get("source_file_name"),
                }
            )
        return documents

    def log_results(
        self,
        device: DeviceRef,
        source_url: str,
        documents: list[dict[str, Any]],
        status: str,
        error_type: str | None = None,
    ) -> None:
        ensure_ifu_links_table(self.db_path)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            with conn:
                if documents:
                    for document in documents:
                        document_status = classify_document_status(document.get("match_confidence"))
                        document["status"] = document_status
                        upsert_document_row(conn, device, source_url, document, document_status, checked_at)
                    conn.execute(
                        """
                        delete from ifu_links
                        where catalog_number = ?
                          and document_url is null
                        """,
                        (device.catalog_number,),
                    )
                else:
                    if status not in STABLE_STATUSES:
                        status = INIT_FAILED_STATUS
                    is_error = status in {
                        SESSION_GATE_STATUS,
                        AUTH_FAILED_STATUS,
                        INIT_FAILED_STATUS,
                        HTTP_ERROR_STATUS,
                        NETWORK_ERROR_STATUS,
                        TIMEOUT_STATUS,
                    }
                    success_at = checked_at if status == NOT_FOUND_STATUS else None
                    last_error_at = checked_at if is_error else None
                    upsert_outcome_row(
                        conn,
                        device,
                        source_url,
                        checked_at,
                        status,
                        success_at,
                        last_error_at,
                        error_type,
                    )
        finally:
            conn.close()


def upsert_document_row(
    conn: sqlite3.Connection,
    device: DeviceRef,
    source_url: str,
    document: dict[str, Any],
    document_status: str,
    checked_at: str,
) -> None:
    values = (
        device.rowid,
        device.primary_di,
        device.catalog_number,
        "johnson_and_johnson",
        source_url,
        document.get("document_url"),
        document.get("document_title"),
        document.get("language") or "en",
        document.get("revision"),
        document.get("match_confidence"),
        checked_at,
        document_status,
        checked_at,
        checked_at,
        checked_at,
        None,
        None,
    )
    try:
        conn.execute(
            """
            insert into ifu_links (
                device_rowid, primary_di, catalog_number,
                manufacturer_family, source_url, document_url,
                document_title, language, revision,
                match_confidence, retrieved_at, status,
                first_seen_at, last_checked_at, last_success_at,
                last_error_at, error_type
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(catalog_number, document_url) where document_url is not null
            do update set
                device_rowid = excluded.device_rowid,
                primary_di = excluded.primary_di,
                manufacturer_family = excluded.manufacturer_family,
                source_url = excluded.source_url,
                document_title = excluded.document_title,
                language = excluded.language,
                revision = excluded.revision,
                match_confidence = excluded.match_confidence,
                retrieved_at = excluded.retrieved_at,
                status = excluded.status,
                first_seen_at = coalesce(ifu_links.first_seen_at, excluded.first_seen_at),
                last_checked_at = excluded.last_checked_at,
                last_success_at = excluded.last_success_at,
                last_error_at = null,
                error_type = null
            """,
            values,
        )
    except (sqlite3.IntegrityError, sqlite3.OperationalError):
        fallback_upsert_document_row(conn, device, source_url, document, document_status, checked_at)


def fallback_upsert_document_row(
    conn: sqlite3.Connection,
    device: DeviceRef,
    source_url: str,
    document: dict[str, Any],
    document_status: str,
    checked_at: str,
) -> None:
    existing = conn.execute(
        """
        select id from ifu_links
        where catalog_number = ?
          and document_url = ?
        limit 1
        """,
        (device.catalog_number, document.get("document_url")),
    ).fetchone()
    if existing:
        update_document_row(
            conn,
            existing[0],
            device,
            source_url,
            document,
            document_status,
            checked_at,
        )
        return
    try:
        conn.execute(
            """
            insert into ifu_links (
                device_rowid, primary_di, catalog_number,
                manufacturer_family, source_url, document_url,
                document_title, language, revision,
                match_confidence, retrieved_at, status,
                first_seen_at, last_checked_at, last_success_at,
                last_error_at, error_type
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device.rowid,
                device.primary_di,
                device.catalog_number,
                "johnson_and_johnson",
                source_url,
                document.get("document_url"),
                document.get("document_title"),
                document.get("language") or "en",
                document.get("revision"),
                document.get("match_confidence"),
                checked_at,
                document_status,
                checked_at,
                checked_at,
                checked_at,
                None,
                None,
            ),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            """
            select id from ifu_links
            where catalog_number = ?
              and document_url = ?
            limit 1
            """,
            (device.catalog_number, document.get("document_url")),
        ).fetchone()
        if existing is None:
            raise
        update_document_row(
            conn,
            existing[0],
            device,
            source_url,
            document,
            document_status,
            checked_at,
        )


def update_document_row(
    conn: sqlite3.Connection,
    row_id: int,
    device: DeviceRef,
    source_url: str,
    document: dict[str, Any],
    document_status: str,
    checked_at: str,
) -> None:
    conn.execute(
        """
        update ifu_links
        set device_rowid = ?,
            primary_di = ?,
            manufacturer_family = ?,
            source_url = ?,
            document_title = ?,
            language = ?,
            revision = ?,
            match_confidence = ?,
            retrieved_at = ?,
            status = ?,
            first_seen_at = coalesce(first_seen_at, ?),
            last_checked_at = ?,
            last_success_at = ?,
            last_error_at = null,
            error_type = null
        where id = ?
        """,
        (
            device.rowid,
            device.primary_di,
            "johnson_and_johnson",
            source_url,
            document.get("document_title"),
            document.get("language") or "en",
            document.get("revision"),
            document.get("match_confidence"),
            checked_at,
            document_status,
            checked_at,
            checked_at,
            checked_at,
            row_id,
        ),
    )


def upsert_outcome_row(
    conn: sqlite3.Connection,
    device: DeviceRef,
    source_url: str,
    checked_at: str,
    status: str,
    success_at: str | None,
    last_error_at: str | None,
    error_type: str | None,
) -> None:
    values = (
        device.rowid,
        device.primary_di,
        device.catalog_number,
        "johnson_and_johnson",
        source_url,
        checked_at,
        status,
        checked_at,
        checked_at,
        success_at,
        last_error_at,
        error_type,
    )
    try:
        conn.execute(
            """
            insert into ifu_links (
                device_rowid, primary_di, catalog_number,
                manufacturer_family, source_url, retrieved_at, status,
                first_seen_at, last_checked_at, last_success_at,
                last_error_at, error_type
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(catalog_number) where document_url is null
            do update set
                device_rowid = excluded.device_rowid,
                primary_di = excluded.primary_di,
                manufacturer_family = excluded.manufacturer_family,
                source_url = excluded.source_url,
                retrieved_at = excluded.retrieved_at,
                status = excluded.status,
                first_seen_at = coalesce(ifu_links.first_seen_at, excluded.first_seen_at),
                last_checked_at = excluded.last_checked_at,
                last_success_at = coalesce(excluded.last_success_at, ifu_links.last_success_at),
                last_error_at = excluded.last_error_at,
                error_type = excluded.error_type
            """,
            values,
        )
    except (sqlite3.IntegrityError, sqlite3.OperationalError):
        fallback_upsert_outcome_row(
            conn,
            device,
            source_url,
            checked_at,
            status,
            success_at,
            last_error_at,
            error_type,
        )


def fallback_upsert_outcome_row(
    conn: sqlite3.Connection,
    device: DeviceRef,
    source_url: str,
    checked_at: str,
    status: str,
    success_at: str | None,
    last_error_at: str | None,
    error_type: str | None,
) -> None:
    existing = conn.execute(
        """
        select id from ifu_links
        where catalog_number = ?
          and document_url is null
        limit 1
        """,
        (device.catalog_number,),
    ).fetchone()
    if existing:
        update_outcome_row(
            conn,
            existing[0],
            device,
            source_url,
            checked_at,
            status,
            success_at,
            last_error_at,
            error_type,
        )
        return
    try:
        conn.execute(
            """
            insert into ifu_links (
                device_rowid, primary_di, catalog_number,
                manufacturer_family, source_url, retrieved_at, status,
                first_seen_at, last_checked_at, last_success_at,
                last_error_at, error_type
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device.rowid,
                device.primary_di,
                device.catalog_number,
                "johnson_and_johnson",
                source_url,
                checked_at,
                status,
                checked_at,
                checked_at,
                success_at,
                last_error_at,
                error_type,
            ),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            """
            select id from ifu_links
            where catalog_number = ?
              and document_url is null
            limit 1
            """,
            (device.catalog_number,),
        ).fetchone()
        if existing is None:
            raise
        update_outcome_row(
            conn,
            existing[0],
            device,
            source_url,
            checked_at,
            status,
            success_at,
            last_error_at,
            error_type,
        )


def update_outcome_row(
    conn: sqlite3.Connection,
    row_id: int,
    device: DeviceRef,
    source_url: str,
    checked_at: str,
    status: str,
    success_at: str | None,
    last_error_at: str | None,
    error_type: str | None,
) -> None:
    conn.execute(
        """
        update ifu_links
        set device_rowid = ?,
            primary_di = ?,
            manufacturer_family = ?,
            source_url = ?,
            retrieved_at = ?,
            status = ?,
            first_seen_at = coalesce(first_seen_at, ?),
            last_checked_at = ?,
            last_success_at = coalesce(?, last_success_at),
            last_error_at = ?,
            error_type = ?
        where id = ?
        """,
        (
            device.rowid,
            device.primary_di,
            "johnson_and_johnson",
            source_url,
            checked_at,
            status,
            checked_at,
            checked_at,
            success_at,
            last_error_at,
            error_type,
            row_id,
        ),
    )


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _file_name_tokens(file_name: str) -> set[str]:
    """Split an e-IFU file name into identifier tokens.

    Splits on separators that delimit identifiers (space, underscore, etc.)
    but keeps dashes inside tokens, since catalog numbers like 0155-1910
    appear dash-joined in file names.
    """
    stem = re.sub(r"\.[a-z0-9]{2,4}$", "", file_name.lower())
    tokens: set[str] = set()
    for token in re.split(r"[\s_,;()\[\]]+", stem):
        token = token.strip(".-")
        if token:
            tokens.add(token)
            tokens.add(_normalize_identifier(token))
    return tokens


def _identifier_in_file_name(identifier: str, file_name: str | None) -> bool:
    # Substring matching against file names is unsafe: artwork numbers like
    # A001931 contain shorter catalog numbers (01931) by coincidence, which
    # would link a device to the wrong document. Require whole-token equality
    # and a minimum length so short numeric catalogs can't collide.
    if not file_name:
        return False
    normalized = _normalize_identifier(identifier)
    if len(normalized) < 5:
        return False
    tokens = _file_name_tokens(file_name)
    return identifier.lower() in tokens or normalized in tokens


def search_terms_for(catalog_number: str, model_number: str | None) -> list[str]:
    """Terms to try against the portal, in order, stopping at the first that hits.

    The portal indexes documents by the manufacturer's own printed identifier,
    which is not always GUDID's catalog number. Synthes/DePuy devices are the
    clear case: GUDID stores catalog 02007026 while e-ifu.com only knows the
    dotted model 02.007.026 — searching the catalog returns nothing, so every
    one of those devices looked like a genuine miss when it was a punctuation
    mismatch. Fall back to the model number when the catalog finds nothing.
    """
    catalog = (catalog_number or "").strip()
    model = (model_number or "").strip()
    # A dotted model (02.007.026) is the form e-ifu.com indexes, and its catalog
    # counterpart (02007026) never hits — searching the catalog first would burn
    # a request and a rate-limit delay on every one of ~24k such devices.
    order = (model, catalog) if _DOTTED_MODEL_RE.match(model) else (catalog, model)
    terms: list[str] = []
    for term in order:
        if term and term not in terms:
            terms.append(term)
    return terms


def is_distinctive_identifier(term: str) -> bool:
    """True when a portal hit on this exact identifier is safe to trust.

    See MIN_PORTAL_TERM_LEN: short identifiers collide inside longer tokens in
    unrelated file names, which is how catalog 00825 pulled MENTOR documents.
    """
    return len(re.sub(r"[^A-Za-z0-9]", "", term or "")) >= MIN_PORTAL_TERM_LEN


def promote_portal_model_hits(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark documents the portal returned for the device's exact model number.

    Reached only when the catalog number found nothing and the model did. The
    portal answers on its own applicability metadata — the model appears nowhere
    in these titles or file names — so a hit is the manufacturer asserting that
    the document covers this model. Verified: bogus models (02.007.999) return
    nothing, and the device-specific document tracks the model correctly
    (02.007.026 -> OLECRANON OSTEOTOMY NAIL, 04.535.328 -> VOLT Small Fragment).

    This is what makes the Synthes/DePuy family servable at all: 10,447 of those
    devices carry brand_name "NA", so brand agreement can never verify them.
    """
    for document in documents:
        if document.get("match_confidence") == "search_result":
            document["match_confidence"] = "model_portal_match"
    return documents


def _normalize_brand_text(text: str) -> str:
    """Uppercase, drop trademark marks and punctuation, collapse whitespace."""
    text = text.replace("™", " ").replace("®", " ")
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().upper()


def brand_in_title(brand: str, title: str) -> bool:
    """True when the device's full brand phrase appears in the document title.

    The whole phrase must match, not individual tokens: "STAR" alone would hit
    unrelated titles, while "STAR S4 IR" identifies the device. Brands shorter
    than MIN_BRAND_LEN collide with ordinary words and are rejected.
    """
    normalized_brand = _normalize_brand_text(brand or "")
    if len(normalized_brand) < MIN_BRAND_LEN:
        return False
    return f" {normalized_brand} " in f" {_normalize_brand_text(title)} "


def match_confidence(
    title: str,
    catalog_number: str,
    model_number: str | None = None,
    source_file_name: str | None = None,
    brand_names: Iterable[str] | None = None,
) -> str:
    title_lower = title.lower()
    if catalog_number:
        if catalog_number.lower() in title_lower or _identifier_in_file_name(
            catalog_number, source_file_name
        ):
            return "exact_catalog"
    if model_number:
        if model_number.lower() in title_lower or _identifier_in_file_name(
            model_number, source_file_name
        ):
            return "model_match"
    # The portal substring-matches the catalog against document metadata, so a
    # coincidental hit can carry the catalog inside a longer file-name token
    # (LAB100825478v3_eIFU.pdf contains catalog 00825) while a genuine hit
    # carries it nowhere at all — catalog 0030-4864's STAR S4 IR booklets never
    # mention it. Brand agreement, not the catalog string, separates the two.
    for brand in brand_names or ():
        if brand_in_title(brand, title):
            return "brand_match"
    return "search_result"


def ensure_ifu_links_table(db_path: str | Path = SQLITE_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ifu_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_rowid INTEGER REFERENCES devices(rowid),
                    primary_di TEXT,
                    catalog_number TEXT,
                    manufacturer_family TEXT,
                    source_url TEXT,
                    document_url TEXT,
                    document_title TEXT,
                    language TEXT DEFAULT 'en',
                    revision TEXT,
                    match_confidence TEXT,
                    retrieved_at TEXT,
                    status TEXT DEFAULT 'pending'
                );

                CREATE INDEX IF NOT EXISTS idx_ifu_catalog ON ifu_links(catalog_number);
                CREATE INDEX IF NOT EXISTS idx_ifu_primary_di ON ifu_links(primary_di);
                CREATE INDEX IF NOT EXISTS idx_ifu_status ON ifu_links(status);
                """
            )
            existing_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(ifu_links)").fetchall()
            }
            for column_name in (
                "first_seen_at",
                "last_checked_at",
                "last_success_at",
                "last_error_at",
                "error_type",
            ):
                if column_name not in existing_columns:
                    conn.execute(f"ALTER TABLE ifu_links ADD COLUMN {column_name} TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ifu_last_checked ON ifu_links(last_checked_at)"
            )
            create_unique_indexes_if_safe(conn)
    finally:
        conn.close()


def create_unique_indexes_if_safe(conn: sqlite3.Connection) -> None:
    duplicate_document_groups, duplicate_document_rows = duplicate_counts(
        conn,
        """
        select count(*) as row_count
        from ifu_links
        where document_url is not null
        group by catalog_number, document_url
        having row_count > 1
        """,
    )
    if duplicate_document_groups == 0:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ifu_unique_catalog_document
            ON ifu_links(catalog_number, document_url)
            WHERE document_url IS NOT NULL
            """
        )
    else:
        warnings.warn(
            "Skipping idx_ifu_unique_catalog_document because ifu_links has "
            f"{duplicate_document_groups} duplicate document key groups "
            f"covering {duplicate_document_rows} rows.",
            RuntimeWarning,
            stacklevel=2,
        )

    duplicate_outcome_groups, duplicate_outcome_rows = duplicate_counts(
        conn,
        """
        select count(*) as row_count
        from ifu_links
        where document_url is null
        group by catalog_number
        having row_count > 1
        """,
    )
    if duplicate_outcome_groups == 0:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ifu_unique_catalog_outcome
            ON ifu_links(catalog_number)
            WHERE document_url IS NULL
            """
        )
    else:
        warnings.warn(
            "Skipping idx_ifu_unique_catalog_outcome because ifu_links has "
            f"{duplicate_outcome_groups} duplicate outcome key groups "
            f"covering {duplicate_outcome_rows} rows.",
            RuntimeWarning,
            stacklevel=2,
        )


def duplicate_counts(conn: sqlite3.Connection, grouped_count_sql: str) -> tuple[int, int]:
    rows = conn.execute(grouped_count_sql).fetchall()
    return len(rows), sum(int(row[0]) for row in rows)


def load_jnj_devices(
    limit: int,
    db_path: str | Path = SQLITE_PATH,
    dotted_first: bool = False,
) -> list[sqlite3.Row]:
    """Unresolved J&J-family devices to attempt, best prospects first.

    dotted_first prioritises devices whose model number is dotted (02.007.026).
    e-ifu.com indexes that form, not GUDID's catalog number, so those devices
    resolve at close to 100% while the rest mostly return not_found — a sweep
    that ignores the distinction spends most of its time on misses.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number, d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null
              and trim(d.catalog_number) != ''
              and (
                lower(d.company_name) like '%johnson%'
                or lower(d.company_name) like '%depuy%'
                or lower(d.company_name) like '%ethicon%'
                or lower(d.company_name) like '%synthes%'
                or lower(d.company_name) like '%cerenovus%'
                or lower(d.company_name) like '%biosense%'
                or lower(d.company_name) like '%acclarent%'
                or lower(d.company_name) like '%mentor%'
              )
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.catalog_number
                  and l.status in ('found', 'candidate_broad', 'not_found')
              )
            order by
              -- Devices whose model is dotted (02.007.026) resolve at ~100%;
              -- e-ifu.com indexes that form, not the GUDID catalog number.
              case when ? = 1 and d.model_number like '%.%.%' then 0 else 1 end,
              -- e-ifu.com hosts Ethicon/DePuy/Biosense/Mentor families;
              -- Surgical Vision is mostly absent, so try it last.
              case when lower(d.company_name) like '%surgical vision%' then 1 else 0 end,
              d.catalog_number
            limit ?
            """,
            (1 if dotted_first else 0, limit),
        ).fetchall()
    finally:
        conn.close()


def row_get(row: sqlite3.Row, key: str) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else None
    return row.get(key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve J&J e-IFU document metadata links.")
    parser.add_argument("catalog_number", nargs="?", help="Catalog number to resolve.")
    parser.add_argument("--model-number", help="Optional model number for confidence scoring.")
    parser.add_argument("--test-jnj", type=int, help="Resolve N real J&J-family devices from the vault.")
    parser.add_argument(
        "--dotted-first",
        action="store_true",
        help="Attempt dotted-model devices (02.007.026) first; they resolve at ~100%%.",
    )
    parser.add_argument("--no-db", action="store_true", help="Do not log results to ifu_links.")
    args = parser.parse_args()

    resolver = EifuResolver()
    if args.test_jnj:
        rows = load_jnj_devices(args.test_jnj, dotted_first=args.dotted_first)
        print(f"Testing {len(rows)} J&J-family devices")
        for row in rows:
            raw_json = json.loads(row["raw_json"]) if row["raw_json"] else {}
            print(
                f"catalog={row['catalog_number']} model={row['model_number']} "
                f"company={row['company_name']} primary_di={raw_json.get('PrimaryDI')}"
            )
            documents = resolver.resolve(
                catalog_number=row["catalog_number"],
                model_number=row["model_number"],
                device_rowid=row["rowid"],
                primary_di=raw_json.get("PrimaryDI"),
                log_to_db=not args.no_db,
            )
            print(json.dumps(documents, indent=2, ensure_ascii=False))
        return

    if not args.catalog_number:
        parser.error("catalog_number is required unless --test-jnj is used.")
    documents = resolver.resolve(
        args.catalog_number,
        model_number=args.model_number,
        log_to_db=not args.no_db,
    )
    print(json.dumps(documents, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
