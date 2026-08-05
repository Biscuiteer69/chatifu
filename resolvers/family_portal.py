"""Shared machinery for manufacturers that publish IFUs by PRODUCT FAMILY.

Most portals we resolve are catalog-keyed: search the identifier, get that device's document.
A second kind exists, and the spine/implant makers are almost all of it — Globus, Alphatec,
and the others behind the 2.1M GUDID records that carry a model number and no catalog number.
They publish one "device insert" per product family and expect the reader to know which family
their implant belongs to. Nothing in the model number says.

Two consequences shape everything here:

1. **Mirror the index, don't search per device.** A family portal's whole catalogue is a few
   hundred documents, so one pass over it covers tens of thousands of devices. Globus is ~520
   requests for 43k devices, Alphatec ~11 for 46k. Per-device searching against these portals
   would be a hundredfold more traffic for strictly less information.

2. **Match on GUDID brandName.** It is the only field that names the family. This is a real
   match, not a guess — the family document IS the IFU for every device in the family, which
   is how the manufacturer publishes — but it is not device-specific, so it is recorded as
   `brand_family_match` and never as `exact_catalog`. A device whose brand names no family is
   left pending rather than marked not_found: the gap is ours, not evidence the maker
   published nothing, and a not_found row would permanently exclude it from a later pass.

Each portal module supplies its own `build_index()` returning a list of document dicts with
label/title/url; everything below is common.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from resolvers.eifu_resolver import SQLITE_PATH

VAULT = Path(__file__).resolve().parent.parent
INDEX_DIR = VAULT / "runs"
INDEX_MAX_AGE = timedelta(days=14)

MATCH_CONFIDENCE = "brand_family_match"

# These portals publish every family in every EU language, and the translations dominate:
# 994 Globus documents are only ~130 English ones plus ~800 translations of them. Matching a
# family without filtering attaches all of them to every device in it, which bloats ifu_links
# and, worse, lets the answerer highlight a passage from the Czech IFU for an English question.
#
# Named languages are dropped; anything unrecognised is KEPT. A title we cannot parse is far
# more likely to be an English document with unusual naming than a translation, and dropping
# it would lose real coverage — Alphatec's titles end "- US" or "- US/NZ" and name no language
# at all.
_TRANSLATION_SUFFIXES = frozenset({
    "czech", "turkish", "swedish", "portuguese", "dutch", "french", "spanish", "greek",
    "german", "danish", "italian", "finnish", "polish", "estonian", "serbian", "norwegian",
    "hungarian", "romanian", "slovak", "slovenian", "slovene", "croatian", "bulgarian",
    "lithuanian", "latvian", "japanese", "chinese", "korean", "russian", "arabic", "hebrew",
    "thai", "vietnamese", "icelandic", "ukrainian", "maltese", "irish", "catalan",
})


def _is_translation(title: str) -> bool:
    tail = title.rsplit("-", 1)[-1].strip().lower() if "-" in title else ""
    return tail in _TRANSLATION_SUFFIXES


def prefer_english(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop translations, and prefer the US edition when the portal marks one.

    Falls back rather than returning nothing: if every match is a translation we keep the
    original list, because some coverage of a device beats none and the caller can still
    tell from the title.
    """
    english = [d for d in docs if not _is_translation(d.get("title", ""))]
    if not english:
        return docs
    us = [d for d in english if d.get("country") == "us"]
    return us or english


def normalise(value: str | None) -> str:
    """Comparison key: letters and digits only, lowercased.

    Deliberately blunt, because the two sides are written for different audiences. GUDID
    carries "Invictus"; the portal calls the same family "Invictus® Bands System - US".
    Dropping punctuation, trademark symbols, spacing and case is what makes those meet.
    """
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def documents_for_brand(brand: str | None, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Portal documents whose family matches this GUDID brand.

    Exact normalised equality wins outright. Only when nothing matches exactly do we accept a
    product whose label STARTS WITH the brand, which is how these makers name variants
    ("REVERE" -> "REVERE Stabilization System"). Never the reverse: allowing a brand that
    starts with a label would let a shorter, unrelated family swallow a longer product name.

    All matches are returned. Some brands legitimately span several documents (REVERE covers
    both "REVERE 4.5 Stabilization System" and "REVERE Stabilization System"), and the
    answerer already searches a device's documents and keeps whichever contains the answer.
    """
    key = normalise(brand)
    if not key or key in ("na", "notapplicable", "none"):
        return []
    exact = [p for p in products if normalise(p["label"]) == key]
    if exact:
        return prefer_english(exact)
    return prefer_english([p for p in products if normalise(p["label"]).startswith(key)])


def load_index(name: str, builder: Callable[[], dict[str, Any]], refresh: bool = False,
               verbose: bool = True) -> dict[str, Any]:
    """Cached portal mirror. Rebuilt only when missing, forced, or older than INDEX_MAX_AGE."""
    path = INDEX_DIR / f"{name}_ifu_index.json"
    if not refresh and path.exists():
        index = json.loads(path.read_text())
        built = datetime.fromisoformat(index.get("built_at", "1970-01-01T00:00:00+00:00"))
        if datetime.now(timezone.utc) - built < INDEX_MAX_AGE:
            return index
        if verbose:
            print("index stale; refetching")
    index = builder()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=1))
    if verbose:
        print(f"index: {len(index['products'])} documents across "
              f"{len({p['label'] for p in index['products']})} families -> {path}")
    return index


def load_family_devices(patterns: tuple[str, ...], limit: int,
                        db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    """Unresolved devices for a family-keyed maker, newest-largest brands first.

    Keyed on the MODEL number: these makers' GUDID records have no catalog number, and
    ifu_links.catalog_number stores whichever identifier was used, the convention the
    Medtronic resolver has always followed.

    Devices with no usable brand are excluded from the batch entirely rather than resolved
    to nothing, so they stay pending for a future pass that can identify their family some
    other way.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    where = " or ".join(["lower(d.company_name) like ?"] * len(patterns))
    try:
        return conn.execute(
            f"""
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.model_number is not null and trim(d.model_number) != ''
              and d.brand_name is not null and trim(d.brand_name) != ''
              and lower(trim(d.brand_name)) not in ('n/a', 'na', 'none')
              and ({where})
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = trim(d.model_number)
                  and l.status in ('found', 'candidate_broad', 'not_found')
              )
            order by d.brand_name, d.model_number
            limit ?
            """,
            (*patterns, limit),
        ).fetchall()
    finally:
        conn.close()


def log(rowid: int, primary_di: str | None, identifier: str, family: str,
        portal: str, docs: list[dict[str, Any]],
        db_path: str | Path = SQLITE_PATH) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        if not docs:
            # Upsert: a document-less row may already exist from another resolver or a
            # transient error being retried, and a plain insert would trip the partial
            # unique index and abort the whole batch.
            conn.execute(
                """insert into ifu_links (device_rowid, primary_di, catalog_number,
                   manufacturer_family, source_url, status, first_seen_at, last_checked_at)
                   values(?,?,?,?,?,?,?,?)
                   on conflict(catalog_number) where document_url is null do update set
                     status='not_found', last_checked_at=excluded.last_checked_at,
                     error_type=null""",
                (rowid, primary_di, identifier, family, portal, "not_found", now, now))
        for doc in docs:
            conn.execute(
                """insert or ignore into ifu_links (device_rowid, primary_di, catalog_number,
                   manufacturer_family, source_url, document_url, document_title, language,
                   match_confidence, retrieved_at, status, first_seen_at, last_checked_at,
                   last_success_at, source_file_name)
                   values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rowid, primary_di, identifier, family, portal, doc["url"], doc["title"],
                 "en", MATCH_CONFIDENCE, now, "found", now, now, now,
                 doc["url"].rsplit("/", 1)[-1][:120]))
        conn.commit()
    finally:
        conn.close()


def run_batch(rows: list[sqlite3.Row], products: list[dict[str, Any]], family: str,
              portal: str, db_path: str | Path = SQLITE_PATH) -> int:
    """Map a batch of devices onto the mirrored index. No network here at all."""
    found = 0
    for i, row in enumerate(rows, 1):
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        identifier = (row["model_number"] or "").strip()
        docs = documents_for_brand(row["brand_name"], products)
        log(row["rowid"], raw.get("PrimaryDI"), identifier, family, portal, docs,
            db_path=db_path)
        if docs:
            found += 1
        if i % 250 == 0:
            print(f"[{i}/{len(rows)}] {row['brand_name']}: {len(docs)} docs (found {found})")
    return found


def coverage_report(products: list[dict[str, Any]], patterns: tuple[str, ...],
                    db_path: str | Path = SQLITE_PATH) -> None:
    """What share of this maker's devices the mirrored index can actually reach.

    Printed before a batch so a portal that mirrored fine but matches nothing is obvious
    immediately, rather than after it has written thousands of not_found rows.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=120000")
    where = " or ".join(["lower(company_name) like ?"] * len(patterns))
    rows = conn.execute(
        f"select brand_name, count(*) n from devices where ({where}) group by brand_name",
        patterns).fetchall()
    conn.close()
    total = sum(n for _, n in rows)
    matched = sum(n for b, n in rows if documents_for_brand(b, products))
    unmatched = sorted(((b, n) for b, n in rows if not documents_for_brand(b, products)),
                       key=lambda x: -x[1])[:6]
    pct = (100 * matched / total) if total else 0.0
    print(f"index reaches {matched:,}/{total:,} devices ({pct:.1f}%)")
    if unmatched:
        print("  largest unmatched brands: "
              + ", ".join(f"{str(b)[:28]} ({n:,})" for b, n in unmatched))
