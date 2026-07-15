from __future__ import annotations

import argparse
import html
import json
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from resolvers.eifu_resolver import (
    CANDIDATE_STATUS,
    FOUND_STATUS,
    HTTP_ERROR_STATUS,
    INIT_FAILED_STATUS,
    NETWORK_ERROR_STATUS,
    NOT_FOUND_STATUS,
    SQLITE_PATH,
    STABLE_STATUSES,
    TIMEOUT_STATUS,
    DeviceRef,
    classify_document_status,
    classify_error,
    ensure_ifu_links_table,
)


BASE_URL = "https://eifu.edwards.com"
SEARCH_PATH = "/eifu/search"
DEFAULT_DELAY_SEC = 1.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass(frozen=True)
class EdwardsDocument:
    document_url: str
    document_title: str
    language: str | None
    revision: str | None
    match_confidence: str
    source_file_name: str | None
    viewer_url: str | None = None


class EdwardsSearchResultParser(HTMLParser):
    def __init__(self, base_url: str = BASE_URL) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.documents: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._entry_depth = 0
        self._capture_title = False
        self._capture_label = False
        self._capture_value = False
        self._pending_label: str | None = None
        self._current_label: list[str] = []
        self._current_value: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())

        if tag == "div" and "searchResultEntry" in classes:
            self._current = {
                "document_title": "",
                "viewer_url": None,
                "document_url": None,
                "metadata": {},
            }
            self._entry_depth = 1
            return

        if self._current is None:
            return

        if tag == "div":
            self._entry_depth += 1
            if "show-minData" in classes:
                self._capture_value = True
                self._current_value = []
            return

        if tag == "h4":
            self._capture_title = True
            return

        if tag == "label":
            self._capture_label = True
            self._current_label = []
            return

        if tag == "a":
            href = attrs_dict.get("href", "")
            if not href:
                return
            url = urllib.parse.urljoin(self.base_url, html.unescape(href))
            if "/eifu/pages/viewers/pdf" in url:
                self._current["viewer_url"] = url
            if ".pdf" in urllib.parse.urlparse(url).path.lower():
                self._current["document_url"] = url

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._capture_title:
            self._current["document_title"] = f"{self._current.get('document_title') or ''} {data}"
        if self._capture_label:
            self._current_label.append(data)
        if self._capture_value:
            self._current_value.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return

        if tag == "h4":
            self._capture_title = False
            self._current["document_title"] = normalize_space(str(self._current.get("document_title") or ""))
            return

        if tag == "label":
            self._capture_label = False
            self._pending_label = normalize_space(" ".join(self._current_label)).lower()
            self._current_label = []
            return

        if tag == "div" and self._capture_value:
            self._capture_value = False
            value = normalize_space(" ".join(self._current_value))
            if self._pending_label and value:
                self._current.setdefault("metadata", {})[self._pending_label] = value
            self._pending_label = None
            self._current_value = []

        if tag == "div":
            self._entry_depth -= 1
            if self._entry_depth <= 0:
                if self._current.get("document_url"):
                    self.documents.append(dict(self._current))
                self._current = None
                self._entry_depth = 0


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


class EdwardsResolver:
    def __init__(
        self,
        db_path: str | Path = SQLITE_PATH,
        delay_sec: float = DEFAULT_DELAY_SEC,
        timeout_sec: int = 20,
    ) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self.timeout_sec = timeout_sec
        self._last_request_at = 0.0
        self._opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())

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

        search_term = (model_number or catalog_number).strip()
        source_url = self.search_url(search_term)
        error_type = None
        try:
            content = self._search(search_term)
            documents = self._parse_documents(content, catalog_number, model_number)
            status = FOUND_STATUS if any(
                classify_document_status(document.get("match_confidence")) == FOUND_STATUS
                for document in documents
            ) else (CANDIDATE_STATUS if documents else NOT_FOUND_STATUS)
        except (urllib.error.HTTPError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
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
            primary_di=raw_json.get("PrimaryDI") or _primary_di_from_raw(raw_json),
        )

    def search_url(self, term: str) -> str:
        return f"{BASE_URL}{SEARCH_PATH}?{urllib.parse.urlencode({'term': term, 'f.latest_md': 'true'})}"

    def _search(self, term: str) -> str:
        return self._request(self.search_url(term))

    def _request(self, url: str, delay: bool = True) -> str:
        if delay:
            self._rate_limit()
        req = urllib.request.Request(url, headers=HEADERS)
        with self._opener.open(req, timeout=self.timeout_sec) as response:
            return response.read().decode("utf-8", errors="ignore")

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)
        self._last_request_at = time.monotonic()

    def _parse_documents(
        self,
        content: str,
        catalog_number: str,
        model_number: str | None,
    ) -> list[dict[str, Any]]:
        parser = EdwardsSearchResultParser(BASE_URL)
        parser.feed(content)
        documents: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for raw in parser.documents:
            document_url = raw.get("document_url")
            if not document_url or document_url in seen_urls:
                continue
            seen_urls.add(document_url)
            metadata = raw.get("metadata") or {}
            title = raw.get("document_title") or "Untitled Edwards IFU"
            languages = metadata.get("document languages")
            if languages and "english" not in languages.lower():
                continue
            source_file_name = Path(urllib.parse.urlparse(document_url).path).name or None
            revision = metadata.get("effective date (yyyy-mm-dd)")
            documents.append({
                "document_url": document_url,
                "document_title": title,
                "language": "en",
                "revision": revision,
                "match_confidence": edwards_match_confidence(
                    title,
                    metadata.get("model numbers"),
                    catalog_number,
                    model_number,
                ),
                "source_file_name": source_file_name or metadata.get("ifu p/n"),
                "viewer_url": raw.get("viewer_url"),
            })
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
        conn = sqlite3.connect(self.db_path)
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


def edwards_match_confidence(
    title: str,
    model_numbers: str | None,
    catalog_number: str,
    model_number: str | None = None,
) -> str:
    haystack = f"{title} {model_numbers or ''}".lower()
    if catalog_number and catalog_number.lower() in haystack:
        return "exact_catalog"
    if model_number and model_number.lower() in haystack:
        return "model_match"
    return "search_result"


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
        "edwards",
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
        update_document_row(conn, existing[0], device, source_url, document, document_status, checked_at)
        return
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
            "edwards",
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
            "edwards",
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
        "edwards",
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
                    "edwards",
                    source_url,
                    checked_at,
                    status,
                    checked_at,
                    checked_at,
                    success_at,
                    last_error_at,
                    error_type,
                    existing[0],
                ),
            )


def _primary_di_from_raw(raw_json: dict[str, Any]) -> str | None:
    identifiers = raw_json.get("identifiers") or raw_json.get("identifiers".title()) or []
    if isinstance(identifiers, list):
        for item in identifiers:
            if isinstance(item, dict) and str(item.get("type") or "").lower() == "primary":
                return item.get("id")
    return None


def row_get(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]  # type: ignore[index]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def load_edwards_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null
              and trim(d.catalog_number) != ''
              and lower(d.company_name) like '%edwards lifesciences%'
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.catalog_number
                  and l.status in ('found', 'candidate_broad', 'not_found')
              )
            order by d.catalog_number
            limit ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve Edwards Lifesciences IFU metadata.")
    parser.add_argument("--catalog", help="Catalog number to resolve.")
    parser.add_argument("--model", help="Model number to search when different from catalog.")
    parser.add_argument("--limit", type=int, default=0, help="Resolve this many Edwards rows from SQLite.")
    parser.add_argument("--db", default=str(SQLITE_PATH), help="SQLite database path.")
    args = parser.parse_args(argv)

    resolver = EdwardsResolver(db_path=args.db)
    if args.catalog:
        documents = resolver.resolve(args.catalog, model_number=args.model)
        print(json.dumps(documents, indent=2))
        return 0
    for row in load_edwards_devices(args.limit, db_path=args.db):
        documents = resolver.resolve_device_row(row)
        print(f"{row['catalog_number']}: {len(documents)} document(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
