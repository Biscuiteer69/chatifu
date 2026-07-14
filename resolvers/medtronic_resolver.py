"""Resolve Medtronic IFUs from manuals.medtronic.com.

Medtronic devices are keyed by MODEL number, not catalog number: of 96,980
Medtronic/Covidien devices in GUDID, 96,980 carry a model number and only 633
carry a catalog number (their brand_name is usually "NA" too). So this resolver
searches by model, and stores the model as the device identifier in ifu_links.

The portal is a session-based Struts app, not an API:

  1. GET /manuals/main/region?region=na          -> SYNCHRONIZER_TOKEN (CSRF)
  2. GET /manuals/main/country/index             -> country=US & lang=en_US
     (lang MUST be en_US; plain "en" lands on an error page)
  3. GET /manuals/main/en_US/manual/index        -> findby=model&model=<model>

Each page mints a fresh token, so the session is walked in order and the token
carried forward. Results are exact — an unknown model returns "No Results Found"
rather than a fuzzy neighbour — so a hit is Medtronic asserting that the manual
covers that model.

The PDF URL is not in an href (those are "#"); it sits in the row's
openDialogOptIn(...) onclick, pointing at a stable
www.medtronic.com/content/dam/emanuals/... link with no expiry.

Usage:
    python -m resolvers.medtronic_resolver 2150014
    python -m resolvers.medtronic_resolver --batch 500
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
    STABLE_STATUSES,
    DeviceRef,
    classify_error,
    ensure_ifu_links_table,
)
from resolvers.stryker_resolver import ensure_source_file_name_column

BASE_URL = "https://manuals.medtronic.com"
MANUFACTURER_FAMILY = "medtronic"
DEFAULT_DELAY_SEC = 1.5
JITTER_SEC = 1.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TOKEN_RE = re.compile(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"')
# openDialogOptIn('<modal>', <id>, '<key>', '<pdf url>', '<part number>')
_OPTIN_RE = re.compile(
    r"openDialogOptIn\(\s*'[^']*'\s*,\s*\d+\s*,\s*'[^']*'\s*,\s*'([^']+)'\s*,\s*'([^']*)'"
)
_TITLE_RE = re.compile(r"<td[^>]*>\s*([^<>{}]{6,120}?)\s*(?:<br|</td)", re.S)


class MedtronicResolver:
    def __init__(
        self,
        db_path: str | Path = SQLITE_PATH,
        delay_sec: float = DEFAULT_DELAY_SEC,
    ) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor()
        )
        self._last_request_at = 0.0
        self._token: str | None = None

    def _get(self, path: str, params: dict[str, str] | None = None) -> str:
        wait = self.delay_sec + random.uniform(0.0, JITTER_SEC)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=HEADERS)
        with self._opener.open(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
        token = _TOKEN_RE.search(html)
        if token:
            self._token = token.group(1)
        return html

    def ensure_session(self) -> None:
        """Walk region -> country so the app will serve search results.

        The country must be committed with lang=en_US: "en" returns an error
        page ("No Nav") because the session never gets a country.
        """
        if self._token:
            return
        self._get("/manuals/main/region", {"region": "na"})
        if not self._token:
            raise RuntimeError("Medtronic portal did not return a CSRF token.")
        self._get(
            "/manuals/main/country/index",
            {
                "SYNCHRONIZER_TOKEN": self._token,
                "SYNCHRONIZER_URI": "/manuals/main/country/index",
                "country": "US",
                "lang": "en_US",
            },
        )

    def search(self, model_number: str) -> list[dict[str, Any]]:
        self.ensure_session()
        html = self._get(
            "/manuals/main/en_US/manual/index",
            {
                "SYNCHRONIZER_TOKEN": self._token or "",
                "SYNCHRONIZER_URI": "/manuals/main/en_US/manual/index",
                "findby": "model",
                "model": model_number,
            },
        )
        if "No Results Found" in html or "Not Found" in html:
            return []
        return documents_from_results(html)

    def resolve(
        self,
        catalog_number: str,
        model_number: str | None = None,
        device_rowid: int | None = None,
        primary_di: str | None = None,
        log_to_db: bool = True,
    ) -> list[dict[str, Any]]:
        # The model is the identifier Medtronic publishes; fall back to whatever
        # the caller passed as the catalog when a device has no model.
        identifier = (model_number or catalog_number or "").strip()
        if not identifier:
            raise ValueError("a model or catalog number is required.")

        source_url = f"{BASE_URL}/manuals/main/en_US/manual/index"
        error_type = None
        documents: list[dict[str, Any]] = []
        try:
            documents = self.search(identifier)
            status = FOUND_STATUS if documents else NOT_FOUND_STATUS
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            status, error_type = classify_error(exc)
        except Exception as exc:  # noqa: BLE001
            status, error_type = INIT_FAILED_STATUS, type(exc).__name__

        if log_to_db:
            self.log_results(
                # Store the model as the device identifier: these devices have no
                # catalog number, so keying ifu_links on one would orphan them.
                DeviceRef(device_rowid, primary_di, identifier, model_number),
                source_url,
                documents,
                status,
                error_type=error_type,
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
        ensure_source_file_name_column(self.db_path)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            with conn:
                if documents:
                    for document in documents:
                        document["status"] = FOUND_STATUS
                        conn.execute(
                            """
                            insert into ifu_links (
                                device_rowid, primary_di, catalog_number,
                                manufacturer_family, source_url, document_url,
                                document_title, language, revision,
                                match_confidence, retrieved_at, status,
                                first_seen_at, last_checked_at, last_success_at,
                                source_file_name
                            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            on conflict(catalog_number, document_url)
                              where document_url is not null
                            do update set
                                document_title = excluded.document_title,
                                revision = excluded.revision,
                                match_confidence = excluded.match_confidence,
                                status = excluded.status,
                                last_checked_at = excluded.last_checked_at,
                                last_success_at = excluded.last_success_at
                            """,
                            (
                                device.rowid, device.primary_di, device.catalog_number,
                                MANUFACTURER_FAMILY, source_url, document["document_url"],
                                document["document_title"], document["language"],
                                document["revision"], document["match_confidence"],
                                checked_at, FOUND_STATUS,
                                checked_at, checked_at, checked_at,
                                document.get("source_file_name"),
                            ),
                        )
                    conn.execute(
                        "delete from ifu_links where catalog_number = ? and document_url is null",
                        (device.catalog_number,),
                    )
                else:
                    if status not in STABLE_STATUSES:
                        status = INIT_FAILED_STATUS
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
                            status = excluded.status,
                            last_checked_at = excluded.last_checked_at,
                            last_error_at = excluded.last_error_at,
                            error_type = excluded.error_type
                        """,
                        (
                            device.rowid, device.primary_di, device.catalog_number,
                            MANUFACTURER_FAMILY, source_url, checked_at, status,
                            checked_at, checked_at,
                            checked_at if status == NOT_FOUND_STATUS else None,
                            None if status == NOT_FOUND_STATUS else checked_at,
                            error_type,
                        ),
                    )
        finally:
            conn.close()


def documents_from_results(html: str) -> list[dict[str, Any]]:
    """PDF documents on a Medtronic search-results page.

    The result anchors are href="#"; the real link is the 4th argument of the
    row's openDialogOptIn(...) onclick.
    """
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pdf_url, part_number in _OPTIN_RE.findall(html):
        if pdf_url in seen or not pdf_url.lower().endswith(".pdf"):
            continue
        seen.add(pdf_url)
        documents.append({
            "document_url": pdf_url,
            "document_title": title_for(html, part_number) or part_number or "Medtronic manual",
            "language": "en",
            "revision": revision_for(html, part_number),
            # The portal answers an exact model query and returns "No Results
            # Found" for an unknown one, so a hit is Medtronic asserting that
            # this manual covers this model.
            "match_confidence": "exact_catalog",
            "source_file_name": part_number or None,
        })
    return documents


def title_for(html: str, part_number: str) -> str | None:
    """The manual's human title from the results row.

    A row reads "<title> Document Number: <part> REV. <rev>", so the title is
    the text before "Document Number". Without this the UI would label the
    document with its part number (M708348B466E) instead of "Divergence-L
    Anterior Oblique Lumbar Fusion System Manual".
    """
    if not part_number:
        return None
    row = re.search(
        r"<tr[^>]*>(?:(?!</tr>).)*?" + re.escape(part_number) + r"(?:(?!</tr>).)*?</tr>",
        html,
        re.S,
    )
    if not row:
        return None
    text = " ".join(re.sub(r"<[^>]+>", " ", row.group(0)).split())
    text = text.replace("&nbsp;", " ")
    title = re.split(r"Document Number", text, maxsplit=1)[0].strip()
    return title or None


def revision_for(html: str, part_number: str) -> str | None:
    if not part_number:
        return None
    index = html.find(part_number)
    if index == -1:
        return None
    match = re.search(r"REV\.?\s*([A-Z0-9]+)", html[index:index + 200], re.I)
    return match.group(1) if match else None


def load_medtronic_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.model_number is not null
              and trim(d.model_number) != ''
              and (lower(d.company_name) like '%medtronic%'
                   or lower(d.company_name) like '%covidien%')
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.model_number
                  and l.status in ('found', 'candidate_broad', 'not_found')
              )
            order by d.model_number
            limit ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve Medtronic manuals by model number.")
    parser.add_argument("model_number", nargs="?")
    parser.add_argument("--batch", type=int, help="Resolve N unresolved Medtronic devices.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    resolver = MedtronicResolver()
    if args.batch:
        rows = load_medtronic_devices(args.batch)
        print(f"Resolving {len(rows)} Medtronic devices")
        found = 0
        for index, row in enumerate(rows, 1):
            raw_json = json.loads(row["raw_json"]) if row["raw_json"] else {}
            documents = resolver.resolve(
                catalog_number=row["catalog_number"] or row["model_number"],
                model_number=row["model_number"],
                device_rowid=row["rowid"],
                primary_di=raw_json.get("PrimaryDI"),
            )
            if documents:
                found += 1
            if index % 25 == 0 or documents:
                print(f"[{index}/{len(rows)}] {row['model_number']}: {len(documents)} docs (found {found})")
        print(f"done: {found}/{len(rows)} devices resolved")
        return

    if not args.model_number:
        parser.error("model_number is required unless --batch is used.")
    documents = resolver.resolve(args.model_number, model_number=args.model_number, log_to_db=not args.no_db)
    print(json.dumps(documents, indent=2)[:1400])


if __name__ == "__main__":
    raise SystemExit(main())
