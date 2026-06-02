from __future__ import annotations

import argparse
import json
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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


API_URL = "https://services.abbott/api/public/search/sitesearch"
DEFAULT_DELAY_SEC = 1.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "x-preferred-language": "en",
    "x-country-code": "US",
    "x-application-id": "manualseifu",
}


class AbbottResolver:
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
            payload = self._search(search_term)
            documents = self._parse_documents(payload, catalog_number, model_number)
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

    def search_url(self, term: str) -> str:
        return f"{API_URL}?{urllib.parse.urlencode({'q': term})}"

    def _search(self, term: str) -> dict[str, Any]:
        payload = {
            "firstresult": 0,
            "q": term,
            "autocorrect": "false",
            "numberofresults": 10,
            "searchtype": "sitesearch",
            "sort": "[]",
        }
        return self._request_json(API_URL, payload)

    def _request_json(self, url: str, payload: dict[str, Any], delay: bool = True) -> dict[str, Any]:
        if delay:
            self._rate_limit()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
        with self._opener.open(req, timeout=self.timeout_sec) as response:
            return json.loads(response.read().decode("utf-8", errors="ignore"))

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)
        self._last_request_at = time.monotonic()

    def _parse_documents(
        self,
        payload: dict[str, Any],
        catalog_number: str,
        model_number: str | None,
    ) -> list[dict[str, Any]]:
        response = payload.get("response") if isinstance(payload, dict) else {}
        results = response.get("results") if isinstance(response, dict) else []
        if not isinstance(results, list):
            return []

        documents: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            document_url = abbott_document_url(result)
            if not document_url or document_url in seen_urls:
                continue
            seen_urls.add(document_url)
            title = normalize_space(str(result.get("title") or result.get("systitle") or "Untitled Abbott IFU"))
            match_confidence = abbott_match_confidence(result, catalog_number, model_number)
            if index == 0 and match_confidence == "search_result":
                # Abbott's response omits matched model fields for some exact model queries,
                # but the first result is the API-ranked match for the exact vault term.
                match_confidence = "model_match"
            documents.append({
                "document_url": document_url,
                "document_title": title,
                "language": "en",
                "revision": result.get("effectivebegindate"),
                "match_confidence": match_confidence,
                "source_file_name": source_file_name(document_url),
                "viewer_url": result.get("uri") or result.get("clickableuri"),
            })
        documents.sort(key=abbott_document_priority)
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
                        delete_raw_encoded_duplicate(conn, device, document)
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


def abbott_document_url(result: dict[str, Any]) -> str | None:
    for key in ("clickableuri", "sysclickableuri", "url", "uri"):
        raw_url = str(result.get(key) or "").strip()
        if not raw_url:
            continue
        url = urllib.parse.urljoin("https://manuals.eifu.abbott", raw_url)
        if ".pdf" in urllib.parse.urlparse(url).path.lower():
            return urllib.parse.unquote(url)
    return None


def abbott_match_confidence(
    result: dict[str, Any],
    catalog_number: str,
    model_number: str | None = None,
) -> str:
    model_list = normalize_space(flatten_value(result.get("sapproductmodelnumberlist"))).lower()
    catalog = catalog_number.lower()
    model = (model_number or "").lower()
    model_tokens = set(split_model_tokens(model_list))
    if catalog and catalog in model_tokens:
        return "exact_catalog"
    if model and model in model_tokens:
        return "model_match"

    haystack = normalize_space(" ".join(
        flatten_value(result.get(key))
        for key in (
            "title",
            "systitle",
            "sapproductdescriptionlist",
            "sapproductmodelnumberlist",
            "url",
            "clickableuri",
            "sysclickableuri",
        )
    )).lower()
    if catalog and catalog in haystack:
        return "exact_catalog"
    if model and model in haystack:
        return "model_match"
    return "search_result"


def abbott_document_priority(document: dict[str, Any]) -> tuple[int, str, str]:
    confidence = str(document.get("match_confidence") or "")
    if confidence == "exact_catalog":
        priority = 0
    elif confidence == "model_match":
        priority = 1
    else:
        priority = 2
    return priority, str(document.get("revision") or ""), str(document.get("document_title") or "")


def flatten_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(flatten_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_value(item) for item in value.values())
    return "" if value is None else str(value)


def split_model_tokens(value: str) -> list[str]:
    return [token.lower() for token in re.split(r"[\s,;|]+", value) if token.strip()]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def source_file_name(document_url: str) -> str | None:
    path = urllib.parse.unquote(urllib.parse.urlparse(document_url).path)
    return Path(path).name or None


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
        "abbott",
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


def delete_raw_encoded_duplicate(
    conn: sqlite3.Connection,
    device: DeviceRef,
    document: dict[str, Any],
) -> None:
    document_url = str(document.get("document_url") or "")
    raw_encoded_url = document_url.replace("%20", "%2520")
    if raw_encoded_url == document_url:
        return
    conn.execute(
        """
        delete from ifu_links
        where catalog_number = ?
          and manufacturer_family = 'abbott'
          and document_url = ?
        """,
        (device.catalog_number, raw_encoded_url),
    )


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
            "abbott",
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
            "abbott",
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
        "abbott",
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
                    "abbott",
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve Abbott eIFU metadata by catalog number.")
    parser.add_argument("--catalog", required=True, help="Catalog number to look up.")
    parser.add_argument("--model", help="Optional model number override.")
    parser.add_argument("--db", default=str(SQLITE_PATH), help="SQLite database path.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human text.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    documents = AbbottResolver(db_path=args.db).resolve(args.catalog, model_number=args.model)
    if args.json:
        print(json.dumps(documents, indent=2))
    else:
        for document in documents:
            print(f"{document.get('match_confidence')}: {document.get('document_title')}")
            print(document.get("document_url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
