"""Resolve Fresenius Kabi IFUs via the medtech eIFU portal (TD Portal SDK).

Two hops, both plain public HTTP, no auth/token:
  1. GET /medtech/search?term=<catalog>&searchMode=productCode&f.lang_md=EN...
     -> server-rendered HTML whose result rows carry a viewer link
        (viewers/pdf?projectKey=<hex>&itemKey=<hex>) and data-docNumber.
  2. GET that viewer page -> the raw, direct, permanent PDF URL
        (/medtech/<project-slug>/<file>.pdf).

searchMode=productCode is an exact lookup, so matches are recorded exact_catalog.

Usage:
    python -m resolvers.fresenius_kabi_resolver P7R8880
    python -m resolvers.fresenius_kabi_resolver --batch 100
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from resolvers.eifu_resolver import (
    FOUND_STATUS,
    INIT_FAILED_STATUS,
    NOT_FOUND_STATUS,
    SQLITE_PATH,
    DeviceRef,
    classify_error,
    ensure_ifu_links_table,
)

MANUFACTURER_FAMILY = "fresenius_kabi"
HOST = "https://eifu.fresenius-kabi.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_DELAY_SEC = 1.5
JITTER_SEC = 0.6
_VIEWER_RE = re.compile(r"viewers/pdf\?projectKey=([a-f0-9]+)&(?:amp;)?itemKey=([a-f0-9]+)", re.I)
_PDF_RE = re.compile(r"/medtech/[A-Za-z0-9_./-]+\.pdf", re.I)


class FreseniusKabiResolver:
    FAMILY = MANUFACTURER_FAMILY

    def __init__(self, db_path: str | Path = SQLITE_PATH, delay_sec: float = DEFAULT_DELAY_SEC) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self._opener = urllib.request.build_opener()
        self._last_request_at = 0.0

    def _get(self, url: str) -> str:
        wait = self.delay_sec + random.uniform(0.0, JITTER_SEC)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()
        req = urllib.request.Request(url, headers={"user-agent": UA, "accept": "text/html"})
        with self._opener.open(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def search(self, catalog_number: str) -> list[tuple[str, str]]:
        params = urllib.parse.urlencode({
            "term": catalog_number, "searchMode": "productCode", "f.lang_md": "EN",
            "f.country": "", "groupBy": "groupDocNumber_md", "sortBy": "effectiveDate_md",
            "sortDirection": "DESC", "maxFacetValues": "100",
        })
        html = self._get(f"{HOST}/medtech/search?{params}")
        seen: set[tuple[str, str]] = set()
        pairs: list[tuple[str, str]] = []
        for m in _VIEWER_RE.finditer(html):
            key = (m.group(1), m.group(2))
            if key not in seen:
                seen.add(key)
                pairs.append(key)
        return pairs

    def ifu_documents(self, viewer_keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for project_key, item_key in viewer_keys:
            try:
                page = self._get(f"{HOST}/medtech/pages/viewers/pdf?projectKey={project_key}&itemKey={item_key}")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                continue
            m = _PDF_RE.search(page)
            if not m:
                continue
            url = HOST + m.group(0)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = url.rsplit("/", 1)[-1]
            docs.append({
                "document_url": url,
                "document_title": name.rsplit(".", 1)[0],
                "language": "en",
                "revision": None,
                "match_confidence": "exact_catalog",
                "source_file_name": name,
            })
        return docs

    def resolve(self, catalog_number: str, model_number: str | None = None,
                device_rowid: int | None = None, primary_di: str | None = None,
                log_to_db: bool = True) -> list[dict[str, Any]]:
        catalog_number = (catalog_number or "").strip()
        if not catalog_number:
            raise ValueError("catalog_number is required.")
        error_type = None
        documents: list[dict[str, Any]] = []
        try:
            keys = self.search(catalog_number)
            documents = self.ifu_documents(keys)
            status = FOUND_STATUS if documents else NOT_FOUND_STATUS
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            status, error_type = classify_error(exc)
        except Exception as exc:  # noqa: BLE001
            status, error_type = INIT_FAILED_STATUS, type(exc).__name__
        if log_to_db:
            self.log_results(
                DeviceRef(device_rowid, primary_di, catalog_number, model_number),
                f"{HOST}/medtech/", documents, status, error_type=error_type,
            )
        return documents

    def log_results(self, device: DeviceRef, source_url: str, documents: list[dict[str, Any]],
                    status: str, error_type: str | None = None) -> None:
        ensure_ifu_links_table(self.db_path)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            with conn:
                if documents:
                    for document in documents:
                        conn.execute(
                            """
                            insert into ifu_links (
                                device_rowid, primary_di, catalog_number,
                                manufacturer_family, source_url, document_url,
                                document_title, language, revision, match_confidence,
                                retrieved_at, status, first_seen_at, last_checked_at,
                                last_success_at, source_file_name
                            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            on conflict(catalog_number, document_url)
                              where document_url is not null
                            do update set
                                document_title = excluded.document_title,
                                match_confidence = excluded.match_confidence,
                                status = excluded.status,
                                last_checked_at = excluded.last_checked_at,
                                last_success_at = excluded.last_success_at
                            """,
                            (device.rowid, device.primary_di, device.catalog_number,
                             MANUFACTURER_FAMILY, source_url, document["document_url"],
                             document["document_title"], document["language"], document["revision"],
                             document["match_confidence"], checked_at, FOUND_STATUS, checked_at,
                             checked_at, checked_at, document["source_file_name"]),
                        )
                else:
                    conn.execute(
                        """
                        insert into ifu_links (
                            device_rowid, primary_di, catalog_number, manufacturer_family,
                            source_url, status, error_type, retrieved_at, first_seen_at,
                            last_checked_at, last_error_at
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        on conflict(catalog_number) where document_url is null
                        do update set
                            status = excluded.status, error_type = excluded.error_type,
                            last_checked_at = excluded.last_checked_at,
                            last_error_at = excluded.last_error_at
                        """,
                        (device.rowid, device.primary_di, device.catalog_number,
                         MANUFACTURER_FAMILY, source_url, status, error_type, checked_at,
                         checked_at, checked_at, checked_at),
                    )
        finally:
            conn.close()


def load_kabi_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null and trim(d.catalog_number) != ''
              and lower(d.company_name) like '%fresenius kabi%'
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve Fresenius Kabi eIFU documents.")
    parser.add_argument("catalog_number", nargs="?")
    parser.add_argument("--batch", type=int, help="Resolve N unresolved Fresenius Kabi devices.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    resolver = FreseniusKabiResolver()
    if args.batch:
        rows = load_kabi_devices(args.batch)
        print(f"Resolving {len(rows)} Fresenius Kabi devices")
        found = 0
        for index, row in enumerate(rows, 1):
            raw_json = json.loads(row["raw_json"]) if row["raw_json"] else {}
            try:
                documents = resolver.resolve(
                    catalog_number=row["catalog_number"], model_number=row["model_number"],
                    device_rowid=row["rowid"], primary_di=raw_json.get("PrimaryDI"),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[{index}/{len(rows)}] {row['catalog_number']}: error {exc}")
                continue
            if documents:
                found += 1
            if index % 25 == 0 or documents:
                print(f"[{index}/{len(rows)}] {row['catalog_number']}: {len(documents)} docs (found {found})")
        print(f"done: {found}/{len(rows)} devices resolved")
        return

    if not args.catalog_number:
        parser.error("catalog_number is required unless --batch is used.")
    documents = resolver.resolve(args.catalog_number, log_to_db=not args.no_db)
    print(json.dumps(documents, indent=2))


if __name__ == "__main__":
    main()
