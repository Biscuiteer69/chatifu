"""Resolve Siemens Healthineers IFUs from the public document library.

doclib.siemens-healthineers.com exposes a clean JSON API with no auth, no token and no WAF:

    GET /rest/v1/documents?product-code=<catalog>   -> {"data":[...], "meta":{"total":N}}
    GET /rest/v1/view?document-id=<id>              -> the PDF itself

IMPORTANT — only the DIAGNOSTICS half of Siemens is reachable. Imaging (Siemens Medical
Solutions USA: transducers, phantoms, scanners) returns zero results for every catalog tested;
those operator manuals are owner-gated. Siemens Healthcare Diagnostics (reagents, calibrators,
assays) resolves well. In our GUDID subset that split is roughly 3,091 diagnostics devices to
1,288 imaging, so most of Siemens is reachable and the imaging remainder is a documented
dead end rather than a gap to keep retrying.

Two per-document filters, both learned by inspecting payloads:

  * `accessible` — false means /view returns 401, so it is not a document we can serve. Some
    entries are also fileFormat DOCX rather than PDF. The very first document sampled happened
    to be both, which briefly made the whole source look unusable; across a full result set it
    is 9 of 10 accessible PDFs.
  * Language lives in the TITLE, not in a field. intOnlyIncludedCountries/Excluded are null
    throughout; instead one document per product carries no country parenthetical (the base
    English revision) while the rest are "(Europe)", "(Greece)", "(Peru)", "(Spain)"... So the
    untagged title is preferred, then an explicit US/English tag, and known non-English country
    variants are dropped.

Usage:
    python -m resolvers.siemens_resolver OPGL07
    python -m resolvers.siemens_resolver --batch 50
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

from resolvers.eifu_resolver import SQLITE_PATH

BASE = "https://doclib.siemens-healthineers.com/rest/v1"
MANUFACTURER_FAMILY = "siemens"
DELAY_SEC = 1.5
UA = "Mozilla/5.0 (compatible; ChatIFU/1.0; +https://chatifu.com)"

# Country parentheticals that indicate a non-English localisation. English-speaking markets and
# the untagged base document are what we want.
_ENGLISH_MARKERS = ("usa", "us", "united states", "uk", "united kingdom", "english",
                    "canada", "australia", "global", "international")
_TITLE_COUNTRY = re.compile(r"\(([^)]{2,40})\)\s*$")


def _request(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - fail-soft; caller treats None as "no result"
        return None


def _language_rank(title: str) -> int:
    """0 = best. Untagged titles are the base English revision; explicit English markets next;
    anything else is a localisation we do not want to serve to a US audience."""
    match = _TITLE_COUNTRY.search(title or "")
    if not match:
        return 0
    tag = match.group(1).strip().lower()
    return 1 if any(marker == tag or marker in tag for marker in _ENGLISH_MARKERS) else 9


def _revision_key(attrs: dict) -> float:
    """Higher revision wins among otherwise equal candidates."""
    try:
        return float(re.sub(r"[^0-9.]", "", str(attrs.get("revision") or "0")) or 0)
    except ValueError:
        return 0.0


def resolve(catalog: str) -> list[dict]:
    """Serviceable English PDFs for one product code, best first."""
    data = _request(f"{BASE}/documents?product-code={urllib.parse.quote(catalog)}")
    if not data or not data.get("data"):
        return []
    candidates = []
    for row in data["data"]:
        attrs = row.get("attributes") or {}
        if not attrs.get("accessible"):
            continue                              # /view would 401
        if str(attrs.get("fileFormat") or "").upper() != "PDF":
            continue
        if attrs.get("blockedNotOwner"):
            continue                              # owner-gated (imaging manuals)
        title = str(attrs.get("title") or "")
        rank = _language_rank(title)
        if rank >= 9:
            continue                              # non-English localisation
        # Verify the catalog really is one of this document's product codes; the search is
        # exact but a document can list several codes and we want that recorded.
        codes = [str(c).strip().upper() for c in (attrs.get("productCodes") or [])]
        exact = catalog.strip().upper() in codes
        candidates.append((rank, -_revision_key(attrs), {
            "document_url": f"{BASE}/view?document-id={row.get('id')}",
            "document_title": title,
            "language": "en",
            "revision": attrs.get("revision"),
            "source_file_name": str(row.get("id")),
            "match_confidence": "exact_catalog" if exact else "search_result",
        }))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return [c[2] for c in candidates]


def load_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    """Unresolved Siemens devices, DIAGNOSTICS first — imaging is owner-gated and will always
    come back empty, so working it first would burn the batch on guaranteed misses."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null and trim(d.catalog_number) != ''
              and lower(d.company_name) like '%siemens%'
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.catalog_number
                  and l.status in ('found','candidate_broad','not_found')
              )
            order by case when lower(d.company_name) like '%diagnos%' then 0 else 1 end,
                     d.catalog_number
            limit ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def log(rowid: int, primary_di: str | None, catalog: str, docs: list[dict],
        db_path: str | Path = SQLITE_PATH) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        if not docs:
            conn.execute(
                """insert into ifu_links (device_rowid, primary_di, catalog_number,
                   manufacturer_family, source_url, status, first_seen_at, last_checked_at)
                   values(?,?,?,?,?,?,?,?)""",
                (rowid, primary_di, catalog, MANUFACTURER_FAMILY, BASE, "not_found", now, now))
        for doc in docs:
            conn.execute(
                """insert or ignore into ifu_links (device_rowid, primary_di, catalog_number,
                   manufacturer_family, source_url, document_url, document_title, language,
                   revision, match_confidence, retrieved_at, status, first_seen_at,
                   last_checked_at, last_success_at, source_file_name)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rowid, primary_di, catalog, MANUFACTURER_FAMILY, BASE, doc["document_url"],
                 doc["document_title"], doc["language"], doc.get("revision"),
                 doc["match_confidence"], now, "found", now, now, now, doc["source_file_name"]))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Siemens Healthineers document-library resolver.")
    ap.add_argument("catalog", nargs="?")
    ap.add_argument("--batch", type=int, help="Resolve N unresolved Siemens devices.")
    args = ap.parse_args()

    if args.catalog:
        print(json.dumps(resolve(args.catalog), indent=2)[:1500])
        return 0
    if not args.batch:
        ap.error("catalog or --batch is required")

    rows = load_devices(args.batch)
    print(f"Resolving {len(rows)} Siemens devices")
    found = 0
    for index, row in enumerate(rows, 1):
        catalog = row["catalog_number"].strip()
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        docs = resolve(catalog)
        log(row["rowid"], raw.get("PrimaryDI"), catalog, docs)
        if docs:
            found += 1
        if index % 25 == 0 or docs:
            print(f"[{index}/{len(rows)}] {catalog}: {len(docs)} docs (found {found})")
        time.sleep(DELAY_SEC)
    print(f"done: {found}/{len(rows)} devices resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
