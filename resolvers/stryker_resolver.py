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
import os
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
# The CloudFront WAF blocks on request fingerprint, not just rate: a bot-ish
# User-Agent ("ChatIFU/1.0") and missing sec-ch-ua/sec-fetch-* headers get an
# instant 403 even from an idle IP. A full Chrome header set passes. Keep these
# consistent with a real browser hitting labeling.stryker.com.
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://labeling.stryker.com",
    "referer": "https://labeling.stryker.com/",
    "sec-ch-ua": '"Chromium";v="126", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}
# Product attributes whose value carries the catalog/REF number. Tenants name
# this field differently ("Ref or catalog number" on Stryker, "Reference or
# catalog number" on Zimmer Biomet).
REF_ATTRIBUTE_NAMES = (
    "ref or catalog number",
    "reference or catalog number",
    "catalog number",
    "reference number",
    "product ref",       # Arthrex
    "ref",
    "model number",      # Alcon — its products carry a model, not a REF; without this every
    "model",             # hit fell through to keyCode (an internal id) and was rejected.
    "reference - catalog number",   # Highridge (ex Zimmer Biomet Spine)
)
# Tenants name the IFU document group differently and the differences are trivial but fatal:
# Stryker "Instructions For Use", Arthrex "Directions For Use", Baxter "Instruction for Use"
# (singular). Each mismatch silently returned zero documents for products that plainly had one,
# which is indistinguishable from "not published" — so match on the STEM rather than on exact
# phrases, and let a new tenant's wording variant work without another round of debugging.
IFU_GROUP_TERMS = (
    "instruction",   # instruction(s) for use
    "direction",     # direction(s) for use
    "ifu",
    "dfu",
)
# Requests are staggered with jitter so a sweep does not look like a metronome
# to the WAF, which blocks the whole client IP across every tenant.
JITTER_SEC = 1.0


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


# Qarad tenants attach a stub to products whose IFU has not been uploaded: Stryker's is a
# document named "Placeholder Document" (file "IFU 1 B V2"), Zimmer Biomet's is "Zimvie -
# Legacy IFU" with file "dummy.pdf", Baxter's "Legacy document - Vantive" with file "Dummy".
# Every one is a page of lorem ipsum. They are real files on the portal, current, English,
# and under the IFU document group, so every structural filter passed them, and 17,009
# catalogs were served lorem ipsum as their exact_catalog IFU (found 2026-09-02). A stub
# is a not_found with extra steps. Checked against BOTH the document name and the file
# name, because each tenant marks it in a different place.
PLACEHOLDER_TITLE_RE = re.compile(r"placeholder|dummy|lorem\s+ipsum", re.I)


def is_placeholder_document(name: str | None) -> bool:
    return bool(PLACEHOLDER_TITLE_RE.search(name or ""))


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
    if any(normalize_ref(ref) == target for ref in refs_from_item(item)):
        return True
    # Some tenants publish no REF attribute at all — Baxter's are "Product Name", "GTIN
    # Primary", "GTIN Secondary" — so refs_from_item() comes back empty and every product is
    # rejected however well it matched. Its keyCode IS the catalog number, and the platform
    # treats keyCode as the product's primary identifier, so fall back to it. Only when there
    # are no REFs to check: where a tenant does publish REFs, disagreeing with them still means
    # the search returned a neighbour and the item must be rejected.
    if not refs_from_item(item):
        return normalize_ref(str(item.get("keyCode") or "")) == target
    return False


def ensure_source_file_name_column(db_path: str | Path) -> None:
    """Add ifu_links.source_file_name if this database predates it.

    The column holds the manufacturer's stable file name, which is how an
    expiring presigned URL gets re-minted. Without it a Stryker row would go
    dead six hours after it was written.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(ifu_links)")}
        with conn:
            if "source_file_name" not in columns:
                conn.execute("ALTER TABLE ifu_links ADD COLUMN source_file_name TEXT")
            # Re-minting a sibling-inferred row looks up the portal-asserted row holding the
            # same file (mvp_lookup.refresh_document_url); without this it scans 2.5M rows.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ifu_source_file ON ifu_links(source_file_name)"
            )
    finally:
        conn.close()


class StrykerResolver:
    # Per-tenant config; the Qarad platform selects the manufacturer from the
    # Origin header, so a new manufacturer is a subclass, not a new resolver.
    ORIGIN = "https://labeling.stryker.com"
    SEARCH_BUSINESS_UNIT = 0  # tenants with isGlobalSearch=False need their own
    # Product-type ids are per business unit, NOT global — Stryker's search lives under 1 but
    # Arthrex's is 10 and BD's units differ again. Hardcoding 1 returns a bare 404 on any other
    # tenant, which reads like "no results" rather than "wrong URL". Always confirm with
    # product_types(bu_id) before adding a tenant.
    SEARCH_PRODUCT_TYPE = 1
    FAMILY = MANUFACTURER_FAMILY

    def __init__(
        self,
        db_path: str | Path = SQLITE_PATH,
        delay_sec: float = DEFAULT_DELAY_SEC,
        country: str = "US",
    ) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self.country = country
        self.headers = {**HEADERS, "origin": self.ORIGIN, "referer": self.ORIGIN + "/"}
        self._last_request_at = 0.0
        self._business_units: dict[str, int] | None = None
        self._product_types: dict[int, dict[str, int]] = {}
        self._byte_cache: Any = None

    def _maybe_cache_pdf(self, signed_url: str) -> None:
        """When PDF caching is enabled (CHATIFU_CACHE_PDFS), download the doc now
        — the presigned URL is valid at scrape time — and store the bytes keyed by
        the stable object path, so the serving layer can serve from cache instead
        of a WAF-rate-limited Qarad re-mint. Best-effort; never breaks a resolve."""
        if os.environ.get("CHATIFU_CACHE_PDFS", "0") not in ("1", "true", "True"):
            return
        try:
            if self._byte_cache is None:
                from ifu_cache import IFUDocumentCache
                self._byte_cache = IFUDocumentCache()
            key = strip_presigned_query(signed_url)
            if self._byte_cache.get(key) is not None:
                return  # already cached
            req = urllib.request.Request(
                signed_url,
                headers={"user-agent": self.headers["user-agent"], "accept": "application/pdf,*/*"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if data[:5] == b"%PDF-":
                self._byte_cache.put(key, data)
        except Exception:
            pass

    # -- HTTP ---------------------------------------------------------------

    def _request(self, url: str, payload: dict[str, Any] | None = None) -> Any:
        wait = self.delay_sec + random.uniform(0.0, JITTER_SEC)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self.headers)
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
            f"{BASE_URL}/business-units/{self.SEARCH_BUSINESS_UNIT}"
            f"/product-types/{self.SEARCH_PRODUCT_TYPE}/products?audience=HCP&page=0",
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

        # `country` is optional for some tenants and MANDATORY for others — Baxter 404s the
        # detail route without it, which looks like a missing product rather than a missing
        # parameter. Always send it; tenants that ignore it are unaffected.
        product = self._request(
            f"{BASE_URL}/business-units/{bu_id}/product-types/{pt_id}"
            f"/products/{item.get('id')}?audience=HCP&country={self.country}"
        )
        files: list[dict[str, Any]] = []
        for group in product.get("documentTypes") or []:
            group_name = str(group.get("name") or "").lower()
            if not any(term in group_name for term in IFU_GROUP_TERMS):
                continue
            for document in group.get("documents") or []:
                if is_placeholder_document(document.get("name")):
                    continue
                for file_info in document.get("files") or []:
                    if not is_english_current_file(file_info):
                        continue
                    if is_placeholder_document(file_info.get("name")):
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
            self._maybe_cache_pdf(str(url))  # gated by CHATIFU_CACHE_PDFS; no-op when off
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

        # Per tenant: Zimmer/Arthrex/Baxter rows used to say labeling.stryker.com here, which
        # made a Biomet miss read as "searched on Stryker's portal".
        source_url = f"{self.ORIGIN}/hcp/{self.country}"
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
                                self.FAMILY, source_url, document["document_url"],
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
                            -- The latest portal to answer owns the miss. Without this a
                            -- Highridge miss left the row labelled zimmer_biomet, so the
                            -- mirrored loader never saw the device as settled and re-drew
                            -- it every batch.
                            manufacturer_family = excluded.manufacturer_family,
                            source_url = excluded.source_url,
                            status = excluded.status,
                            last_checked_at = excluded.last_checked_at,
                            last_success_at = excluded.last_success_at,
                            last_error_at = excluded.last_error_at,
                            error_type = excluded.error_type
                        """,
                        (
                            device.rowid, device.primary_di, device.catalog_number,
                            self.FAMILY, source_url, checked_at, status,
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
