"""Resolve Stryker IFU documents via the official eIFU labeling API.

Stryker publishes IFUs through labeling.stryker.com, backed by the Qarad eIFU
API. That API is far friendlier than a portal scrape: a product search returns
the product's own ``Ref or catalog number``, so a match can be *verified*
rather than inferred. Contrast e-ifu.com, where the portal substring-matches
the query against document metadata and a coincidental file-name hit is
indistinguishable from a real one by text alone (see eifu_resolver).

So this resolver accepts a document only when the product the API returns
carries exactly the catalog number we asked for, and records it as
``exact_catalog`` — the manufacturer's own API asserting the mapping.

The search contract is version-sensitive: the attribute key is ``slug`` (the
older ``name`` form now fails validation with "Incorrect attribute definition
id supplied"), and ``currentDate`` is required. Both were captured from the
live site.

Usage:
    python -m resolvers.stryker_resolver 01-00770
    python -m resolvers.stryker_resolver --batch 500
"""

from __future__ import annotations

import argparse
import json
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

BASE_URL = "https://api-public.qarad.eifu.online/api/v1"
MANUFACTURER_FAMILY = "stryker"
# The API sits behind a CloudFront WAF that blocks the whole client IP once it
# decides you are hammering it — and the block is not per-tenant, it takes out
# every manufacturer on the platform. A 0.5s delay over a few thousand devices
# was enough to trigger it. Stay slow.
DEFAULT_DELAY_SEC = 2.0
# Consecutive WAF blocks before a batch gives up rather than digging in deeper.
MAX_CONSECUTIVE_BLOCKS = 3
BLOCK_BACKOFF_SEC = 60.0
HEADERS = {
    "accept": "*/*",
    "accept-language": "en",
    "content-type": "application/json",
    "origin": "https://labeling.stryker.com",
    "referer": "https://labeling.stryker.com/",
    "user-agent": "Mozilla/5.0 ChatIFU/1.0",
}
# Product attributes whose value carries the catalog/REF number.
REF_ATTRIBUTE_NAMES = ("ref or catalog number", "catalog number", "ref")


class WafBlocked(Exception):
    """The CDN blocked this client IP, not just this request."""


def normalize_ref(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def strip_presigned_query(url: str) -> str:
    """The stable object path of a presigned URL, without its signature."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def refs_from_item(item: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for attribute in item.get("attributes") or []:
        name = str(attribute.get("name") or "").strip().lower()
        if name in REF_ATTRIBUTE_NAMES:
            value = str(attribute.get("value") or "")
            # One product can list several REFs ("01-00770, 01-00771").
            refs.extend(part for part in re.split(r"[,;/]", value) if part.strip())
    return refs


def is_english_current_file(file_info: dict[str, Any]) -> bool:
    """Keep only the current English IFU.

    A product lists the same IFU in every market language (90-01951_AB_IFU_CS,
    _TR, _SV ...) plus superseded revisions; serving a Czech or withdrawn
    document would be authentic and useless.
    """
    if file_info.get("historical"):
        return False
    if file_info.get("latestVersion") is False:
        return False
    codes = {
        str(language.get("isoCode") or "").lower()
        for language in file_info.get("languages") or []
    }
    return "en" in codes


def item_matches_catalog(item: dict[str, Any], catalog_number: str) -> bool:
    """True when the API's product carries exactly the catalog we searched for.

    The search is a cross-field query, so it can return a neighbouring product.
    Requiring REF equality is what makes this resolver's matches verified rather
    than guessed.
    """
    target = normalize_ref(catalog_number)
    if not target:
        return False
    return any(normalize_ref(ref) == target for ref in refs_from_item(item))


def ensure_source_file_name_column(db_path: str | Path) -> None:
    """Add ifu_links.source_file_name if this database predates it.

    The column holds the manufacturer's stable file name, which is how an
    expiring presigned URL gets re-minted. Without it a Stryker row would go
    dead six hours after it was written.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(ifu_links)")}
        if "source_file_name" not in columns:
            with conn:
                conn.execute("ALTER TABLE ifu_links ADD COLUMN source_file_name TEXT")
    finally:
        conn.close()


class StrykerResolver:
    def __init__(
        self,
        db_path: str | Path = SQLITE_PATH,
        delay_sec: float = DEFAULT_DELAY_SEC,
        country: str = "US",
    ) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self.country = country
        self._last_request_at = 0.0
        self._business_units: dict[str, int] | None = None
        self._product_types: dict[int, dict[str, int]] = {}

    # -- HTTP ---------------------------------------------------------------

    def _request(self, url: str, payload: dict[str, Any] | None = None) -> Any:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)
        self._last_request_at = time.monotonic()

        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # CloudFront blocks the client IP (403/429) rather than the request,
            # so every subsequent call fails too. Surface it as its own error so
            # a batch can stop instead of burning through the work list marking
            # thousands of devices as failures.
            if exc.code in (403, 429):
                raise WafBlocked(f"WAF blocked the client (HTTP {exc.code})") from exc
            raise

    def business_units(self) -> dict[str, int]:
        if self._business_units is None:
            data = self._request(f"{BASE_URL}/business-units")
            self._business_units = {
                str(item["slug"]): int(item["id"]) for item in data.get("items", [])
            }
        return self._business_units

    def search(self, catalog_number: str) -> list[dict[str, Any]]:
        payload = {
            # Required by the API; it filters documents effective on this date.
            "currentDate": datetime.now(timezone.utc).date().isoformat(),
            "attributes": [{"slug": "cross-field-search", "value": catalog_number}],
            "country": self.country,
        }
        data = self._request(
            f"{BASE_URL}/business-units/0/product-types/1/products?audience=HCP&page=0",
            payload=payload,
        )
        return list(data.get("items") or [])

    def product_types(self, bu_id: int) -> dict[str, int]:
        """slug -> id for a business unit, cached.

        This was re-fetched for every device, which is a wasted API call each
        time against a WAF-protected endpoint.
        """
        if bu_id not in self._product_types:
            data = self._request(f"{BASE_URL}/business-units/{bu_id}/product-types")
            self._product_types[bu_id] = {
                str(pt["slug"]): int(pt["id"]) for pt in data.get("items", [])
            }
        return self._product_types[bu_id]

    def _product_files(self, item: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """IFU files the API lists for a product, plus the product name."""
        bu_id = self.business_units().get(str(item.get("businessUnit")))
        if bu_id is None:
            return [], None
        pt_id = self.product_types(bu_id).get(str(item.get("productType")))
        if pt_id is None:
            return [], None

        product = self._request(
            f"{BASE_URL}/business-units/{bu_id}/product-types/{pt_id}"
            f"/products/{item.get('id')}?audience=HCP"
        )
        files: list[dict[str, Any]] = []
        for group in product.get("documentTypes") or []:
            group_name = str(group.get("name") or "").lower()
            if "instructions for use" not in group_name and "ifu" not in group_name:
                continue
            for document in group.get("documents") or []:
                for file_info in document.get("files") or []:
                    if not is_english_current_file(file_info):
                        continue
                    files.append({**file_info, "_document_name": document.get("name")})
        return files, product.get("name")

    def ifu_documents(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        files, product_name = self._product_files(item)
        documents: list[dict[str, Any]] = []
        for file_info in files:
            url = file_info.get("documentUrl")
            if not url:
                continue
            documents.append({
                # Store the S3 object path WITHOUT the presigned query. The
                # signature rotates on every resolve, so storing the full URL
                # would defeat the (catalog_number, document_url) uniqueness
                # index and insert a duplicate row each run — besides going dead
                # in 6h. The signature is minted at serve time instead.
                "document_url": strip_presigned_query(str(url)),
                "document_title": file_info.get("_document_name") or product_name or "Stryker IFU",
                "language": "en",
                "revision": file_info.get("version"),
                # The API returned this product for exactly this catalog number
                # and the product's own REF confirms it.
                "match_confidence": "exact_catalog",
                # The file name is the only STABLE handle: documentUrl is a
                # presigned S3 link that expires (X-Amz-Expires=21600, 6h), so a
                # stored URL would 403 by the end of the day. Keep the name and
                # re-mint the URL at serve time (see fresh_document_url).
                "source_file_name": file_info.get("name"),
            })
        return documents

    def fresh_document_url(self, catalog_number: str, source_file_name: str) -> str | None:
        """Mint a new presigned URL for a document we resolved earlier.

        Stored Stryker document URLs expire after 6 hours, so the serving layer
        re-mints one on demand rather than handing the user a dead link.
        """
        for item in self.search(catalog_number):
            if not item_matches_catalog(item, catalog_number):
                continue
            files, _name = self._product_files(item)
            for file_info in files:
                if file_info.get("name") == source_file_name and file_info.get("documentUrl"):
                    return str(file_info["documentUrl"])
        return None

    # -- Resolve ------------------------------------------------------------

    def resolve(
        self,
        catalog_number: str,
        model_number: str | None = None,
        device_rowid: int | None = None,
        primary_di: str | None = None,
        log_to_db: bool = True,
    ) -> list[dict[str, Any]]:
        catalog_number = (catalog_number or "").strip()
        if not catalog_number:
            raise ValueError("catalog_number is required.")

        source_url = f"https://labeling.stryker.com/hcp/{self.country}"
        error_type = None
        documents: list[dict[str, Any]] = []
        try:
            matches = [
                item for item in self.search(catalog_number)
                if item_matches_catalog(item, catalog_number)
            ]
            for item in matches:
                documents.extend(self.ifu_documents(item))
            status = FOUND_STATUS if documents else NOT_FOUND_STATUS
        except WafBlocked:
            # Not this device's fault, and writing a failure row for it would
            # mislabel a device we never actually got to check.
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            status, error_type = classify_error(exc)
        except Exception as exc:  # noqa: BLE001
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
                                last_success_at = excluded.last_success_at,
                                document_url = excluded.document_url,
                                source_file_name = excluded.source_file_name
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
                    # A document now exists, so drop any stale outcome-only row.
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
                            last_success_at = excluded.last_success_at,
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


def load_stryker_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null
              and trim(d.catalog_number) != ''
              and (lower(d.company_name) like '%stryker%'
                   or lower(d.company_name) like '%wright medical%')
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
    parser = argparse.ArgumentParser(description="Resolve Stryker eIFU documents.")
    parser.add_argument("catalog_number", nargs="?")
    parser.add_argument("--batch", type=int, help="Resolve N unresolved Stryker devices.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    resolver = StrykerResolver()
    if args.batch:
        rows = load_stryker_devices(args.batch)
        print(f"Resolving {len(rows)} Stryker devices")
        found = 0
        blocks = 0
        for index, row in enumerate(rows, 1):
            raw_json = json.loads(row["raw_json"]) if row["raw_json"] else {}
            try:
                documents = resolver.resolve(
                    catalog_number=row["catalog_number"],
                    model_number=row["model_number"],
                    device_rowid=row["rowid"],
                    primary_di=raw_json.get("PrimaryDI"),
                )
            except WafBlocked as exc:
                blocks += 1
                if blocks >= MAX_CONSECUTIVE_BLOCKS:
                    print(f"blocked {blocks}x — stopping at {index}/{len(rows)}. "
                          f"Resolved {found}. Retry later; blocked devices were not marked.")
                    return
                wait = BLOCK_BACKOFF_SEC * blocks
                print(f"[{index}/{len(rows)}] {exc} — backing off {wait:.0f}s")
                time.sleep(wait)
                continue
            blocks = 0
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
    raise SystemExit(main())
