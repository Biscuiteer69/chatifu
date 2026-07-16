"""Resolve Boston Scientific IFUs via their Coveo-backed eLabeling search.

BSC publishes IFUs through www.bostonscientific.com/elabeling, backed by a Coveo
Cloud search index. A catalog/model/UDI query with Coveo's exact advanced-query
(`@bsci_model=="X"`) returns the product's own documents with a direct, permanent
PDF URL (`clickUri`) — no presigned expiry, no login. So a match is VERIFIED
(Coveo `==` is exact) and recorded as `exact_catalog`.

The only glue: an anonymous Coveo search token is embedded in the HCP page HTML
(`searchTokenStripped = "eyJ..."`, ~24h TTL); we scrape and cache it, refreshing
on 401. bostonscientific.com needs a browser User-Agent (Akamai 406 otherwise);
the Coveo API needs only the token.

Usage:
    python -m resolvers.boston_resolver M00547100
    python -m resolvers.boston_resolver --batch 200
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

MANUFACTURER_FAMILY = "boston_scientific"
COVEO_ORG = "bostonscientificproductionfv9tfxih"
COVEO_URL = f"https://platform.cloud.coveo.com/rest/search/v2?organizationId={COVEO_ORG}"
HCP_PAGE = "https://www.bostonscientific.com/elabeling/us/en/home/healthcare-professionals.html"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_DELAY_SEC = 1.2
JITTER_SEC = 0.6
TOKEN_TTL_SEC = 20 * 3600
_TOKEN_RE = re.compile(r'searchTokenStripped["\s:=]+(eyJ[A-Za-z0-9._-]+)')
# The Coveo field carrying the English IFU literature type.
_ENGLISH_IFU = "instructions for use"
# Fields worth matching the catalog against (Coveo `==` already exact-matched one).
_MODEL_FIELDS = ("bsci_model", "bsci_udi", "bsci_partnumber")


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener()


class BostonScientificResolver:
    FAMILY = MANUFACTURER_FAMILY

    def __init__(self, db_path: str | Path = SQLITE_PATH, delay_sec: float = DEFAULT_DELAY_SEC) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self._opener = _opener()
        self._token: str | None = None
        self._token_at = 0.0
        self._last_request_at = 0.0

    # -- HTTP ---------------------------------------------------------------

    def _throttle(self) -> None:
        wait = self.delay_sec + random.uniform(0.0, JITTER_SEC)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

    def _token_value(self, force: bool = False) -> str:
        if not force and self._token and (time.monotonic() - self._token_at) < TOKEN_TTL_SEC:
            return self._token
        req = urllib.request.Request(HCP_PAGE, headers={"user-agent": UA, "accept": "text/html"})
        with self._opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
        m = _TOKEN_RE.search(html)
        if not m:
            raise RuntimeError("Could not extract Coveo searchToken from HCP page")
        self._token = m.group(1)
        self._token_at = time.monotonic()
        return self._token

    def _coveo_search(self, aq: str) -> list[dict[str, Any]]:
        self._throttle()
        body = json.dumps({
            "q": "", "aq": aq, "numberOfResults": 50,
            "fieldsToInclude": ["bsci_partnumber", "bsci_model", "bsci_udi",
                                 "bsci_producttradename", "bsci_literaturetype",
                                 "bsci_countrycharacteristics"],
        }).encode()
        for attempt in range(2):
            token = self._token_value(force=(attempt == 1))
            req = urllib.request.Request(
                COVEO_URL, data=body,
                headers={"authorization": f"Bearer {token}", "content-type": "application/json",
                         "user-agent": UA},
            )
            try:
                with self._opener.open(req, timeout=30) as resp:
                    return list(json.loads(resp.read().decode("utf-8")).get("results") or [])
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 419) and attempt == 0:
                    continue  # token expired -> refetch once
                raise
        return []

    @staticmethod
    def _aq_value(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"')

    def search(self, catalog_number: str, model_number: str | None = None,
               primary_di: str | None = None) -> list[dict[str, Any]]:
        """Exact Coveo lookups by model, then UDI, then part number."""
        terms: list[tuple[str, str]] = []
        for term in (catalog_number, model_number):
            if term and term.strip():
                terms.append(("bsci_model", term.strip()))
        if primary_di and primary_di.strip():
            terms.append(("bsci_udi", primary_di.strip()))
        for term in (catalog_number,):
            if term and term.strip():
                terms.append(("bsci_partnumber", term.strip()))
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for field, value in terms:
            key = f"{field}:{value.lower()}"
            if key in seen:
                continue
            seen.add(key)
            hits = self._coveo_search(f'@{field}=="{self._aq_value(value)}"')
            if hits:
                return hits  # first field that matches is authoritative
        return results

    def ifu_documents(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the current English Instructions-for-Use PDFs, deduped by URL."""
        docs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in results:
            url = r.get("clickUri") or ""
            if not url or url in seen:
                continue
            raw = r.get("raw") or {}
            littypes = raw.get("bsci_literaturetype") or []
            if isinstance(littypes, str):
                littypes = [littypes]
            if not any("instructions for use" in str(t).lower() for t in littypes):
                continue  # not an IFU (skip non-IFU literature types)
            name = url.rsplit("/", 1)[-1]
            # BSC labels the *type* "Instructions for Use" in English regardless of
            # the file's language; filter to English by the filename lang code
            # (..._IFU_<LANG>_s.pdf). Keep US/EN/UK; drop ES/FR/ID/etc.
            lang_m = re.search(r"_IFU_([A-Za-z]{2})_", name)
            if lang_m and lang_m.group(1).upper() not in ("US", "EN", "UK"):
                continue
            seen.add(url)
            name = url.rsplit("/", 1)[-1]
            title = (raw.get("bsci_producttradename") or r.get("title") or name)
            if isinstance(title, list):
                title = title[0] if title else name
            docs.append({
                "document_url": url,
                "document_title": str(title),
                "language": "en",
                "revision": None,
                "match_confidence": "exact_catalog",
                "source_file_name": name,
            })
        return docs

    # -- Resolve ------------------------------------------------------------

    def resolve(self, catalog_number: str, model_number: str | None = None,
                device_rowid: int | None = None, primary_di: str | None = None,
                log_to_db: bool = True) -> list[dict[str, Any]]:
        catalog_number = (catalog_number or "").strip()
        if not catalog_number:
            raise ValueError("catalog_number is required.")
        error_type = None
        documents: list[dict[str, Any]] = []
        try:
            results = self.search(catalog_number, model_number=model_number, primary_di=primary_di)
            documents = self.ifu_documents(results)
            status = FOUND_STATUS if documents else NOT_FOUND_STATUS
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            status, error_type = classify_error(exc)
        except Exception as exc:  # noqa: BLE001
            status, error_type = INIT_FAILED_STATUS, type(exc).__name__
        if log_to_db:
            self.log_results(
                DeviceRef(device_rowid, primary_di, catalog_number, model_number),
                HCP_PAGE, documents, status, error_type=error_type,
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


def load_boston_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null and trim(d.catalog_number) != ''
              and lower(d.company_name) like '%boston scientific%'
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
    parser = argparse.ArgumentParser(description="Resolve Boston Scientific eLabeling IFUs.")
    parser.add_argument("catalog_number", nargs="?")
    parser.add_argument("--batch", type=int, help="Resolve N unresolved Boston Scientific devices.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    resolver = BostonScientificResolver()
    if args.batch:
        rows = load_boston_devices(args.batch)
        print(f"Resolving {len(rows)} Boston Scientific devices")
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
