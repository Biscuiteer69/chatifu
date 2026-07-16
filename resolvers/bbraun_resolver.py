"""Resolve B. Braun / Aesculap (US) IFUs via the Aesculap USA eIFU sites.

Aesculap's US IFU search is a plain Drupal 8 Views page: GET
``/?item=<catalog>&category=All`` returns result rows whose title links point
straight at a static, permanent PDF under ``/sites/default/files/ifus/``. No
API token, no WAF, no login. Two sibling sites cover the catalog:
aesculapusaifus.com (surgical/power/neuro/containers/closure) and
aesculapimplantsystemsifus.com (implants).

Scope: US-marketed Aesculap surgical devices — a subset of the broader B. Braun
GUDID set (infusion/EU IFUs live on a separate captcha-gated global portal, not
covered here), so expect a partial hit rate.

Usage:
    python -m resolvers.bbraun_resolver OP940
    python -m resolvers.bbraun_resolver --batch 200
"""
from __future__ import annotations

import argparse
import html
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

MANUFACTURER_FAMILY = "b_braun"
SEARCH_HOSTS = ("https://www.aesculapusaifus.com", "https://www.aesculapimplantsystemsifus.com")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_DELAY_SEC = 1.0
JITTER_SEC = 0.6
# Result title links point at /sites/default/files/ifus/<file>.pdf
_PDF_LINK_RE = re.compile(
    r'<a\s+[^>]*href="(/sites/default/files/ifus/[^"]+\.pdf)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


class BBraunAesculapResolver:
    FAMILY = MANUFACTURER_FAMILY

    def __init__(self, db_path: str | Path = SQLITE_PATH, delay_sec: float = DEFAULT_DELAY_SEC) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self._opener = urllib.request.build_opener()
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        wait = self.delay_sec + random.uniform(0.0, JITTER_SEC)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, url: str) -> str:
        self._throttle()
        req = urllib.request.Request(url, headers={"user-agent": UA, "accept": "text/html"})
        with self._opener.open(req, timeout=30) as resp:  # urllib follows the /search->/?item redirect
            return resp.read().decode("utf-8", "replace")

    def search(self, catalog_number: str) -> list[dict[str, Any]]:
        """Return {host, pdf_path, title} rows across both Aesculap US sites."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        q = urllib.parse.urlencode({"item": catalog_number, "category": "All"})
        for host in SEARCH_HOSTS:
            try:
                page = self._get(f"{host}/?{q}")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                continue
            for m in _PDF_LINK_RE.finditer(page):
                path = m.group(1)
                if path in seen:
                    continue
                seen.add(path)
                title = html.unescape(_TAG_RE.sub("", m.group(2))).strip()
                rows.append({"host": host, "pdf_path": path, "title": title})
        return rows

    def ifu_documents(self, rows: list[dict[str, Any]], catalog_number: str) -> list[dict[str, Any]]:
        target = _norm(catalog_number)
        docs: list[dict[str, Any]] = []
        for row in rows:
            url = row["host"] + row["pdf_path"]
            name = urllib.parse.unquote(row["pdf_path"].rsplit("/", 1)[-1])
            title = row["title"] or name
            # The catalog usually appears in the file name/title (portal search is
            # a description match, so verify to avoid a neighbouring product).
            hay = _norm(name + " " + title)
            confidence = "exact_catalog" if target and target in hay else "search_result"
            docs.append({
                "document_url": url,
                "document_title": title,
                "language": "en",
                "revision": None,
                "match_confidence": confidence,
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
            rows = self.search(catalog_number)
            if not rows and model_number and model_number.strip() != catalog_number:
                rows = self.search(model_number.strip())
            documents = self.ifu_documents(rows, catalog_number)
            status = FOUND_STATUS if documents else NOT_FOUND_STATUS
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            status, error_type = classify_error(exc)
        except Exception as exc:  # noqa: BLE001
            status, error_type = INIT_FAILED_STATUS, type(exc).__name__
        if log_to_db:
            self.log_results(
                DeviceRef(device_rowid, primary_di, catalog_number, model_number),
                SEARCH_HOSTS[0], documents, status, error_type=error_type,
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


def load_bbraun_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null and trim(d.catalog_number) != ''
              and (lower(d.company_name) like '%aesculap%'
                   or lower(d.company_name) like '%b braun%'
                   or lower(d.company_name) like '%b. braun%'
                   or lower(d.company_name) like '%bbraun%')
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
    parser = argparse.ArgumentParser(description="Resolve B. Braun / Aesculap US IFUs.")
    parser.add_argument("catalog_number", nargs="?")
    parser.add_argument("--batch", type=int, help="Resolve N unresolved B. Braun devices.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    resolver = BBraunAesculapResolver()
    if args.batch:
        rows = load_bbraun_devices(args.batch)
        print(f"Resolving {len(rows)} B. Braun devices")
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
