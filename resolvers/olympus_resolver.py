"""Resolve Olympus / Gyrus ACMI IFUs by mirroring the Olympus Europa Solr index.

olympus-europa.com fronts its eIFU library with an unauthenticated Solr endpoint::

    GET /SolrRestService/select?query=*:*&locale=mam&rows=100&start=0
        &fq=document_latestVersion_b:true&fq=document_languageKey_s:EN

The whole latest-English set is ~1,570 documents, so this is a "mirror the index" resolver in
the family_portal.py mould: ~16 requests once a week, and every device is then matched
OFFLINE. Per-device searching would be 7k requests for strictly less information.

Unlike the spine makers, Olympus keys its documents on MODEL NUMBERS, so the join is exact
model equality, not a brand family. Each Solr document carries:

    document_articleNo_s        comma-separated article numbers ("EGNA-403D-2021, NA-403D-2021")
    document_materialNo_s       Olympus material/part number ("PN0009932", "W9211309")
    document_globalModelName_ss list of global model names (absent on many docs)
    IN_NAME / titleAutocomplete the product name, which for most IFUs IS the model
    IN_LINK                     permanent CloudFront PDF URL
    document_fileName_s         the PDF's real file name
    IN_HIERARCHY                "Instructions for Use" / "Quick reference guide" / ...

Only IN_HIERARCHY containing "Instructions for Use" joins; the rest are counted and dropped,
as are the symbol glossaries and access sheets filed there that are not the IFU of anything.

GUDID identifiers meet these on both sides: `model_number` (NM-600L-0521, GIF-H190,
Gyrus's numeric 70338008) against the article/model/name fields, and `catalog_number`
(N5405430, present on a third of Olympus records) against article/material numbers. A
catalog hit is `exact_catalog`; a model hit is `model_portal_match`.

A device the index does not name is left PENDING. The join is ours; absence from a mirror
is not evidence the maker published nothing, and a not_found row would exclude the device
from every later pass.

Usage:
    python -m resolvers.olympus_resolver --dry-run
    python -m resolvers.olympus_resolver --apply [--limit N] [--refresh]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from resolvers.eifu_resolver import SQLITE_PATH, ensure_ifu_links_table

VAULT = Path(__file__).resolve().parent.parent
INDEX_PATH = VAULT / "runs" / "olympus_ifu_index.json"
INDEX_MAX_AGE = timedelta(days=7)

SOLR = "https://www.olympus-europa.com/SolrRestService/select"
PORTAL = "https://www.olympus-europa.com/medical/en/Support/Instructions-for-Use.html"
MANUFACTURER_FAMILY = "olympus"
COMPANY_PATTERNS = ("%olympus%", "%gyrus%")
PAGE_SIZE = 100
REQUEST_DELAY = 1.2
TIMEOUT = 30
IFU_HIERARCHY = "instructions for use"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PORTAL,
}


class OlympusPortalError(RuntimeError):
    pass


# --------------------------------------------------------------------------- fetch

def _solr_url(query: str, start: int = 0, rows: int = PAGE_SIZE) -> str:
    params = urllib.parse.urlencode({"query": query, "locale": "mam",
                                     "rows": rows, "start": start})
    return f"{SOLR}?{params}&fq=document_latestVersion_b:true&fq=document_languageKey_s:EN"


def _get_json(url: str) -> dict[str, Any]:
    """One polite GET. Retries once on 5xx; a 403 aborts the whole job, never retries."""
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise OlympusPortalError(f"HTTP {exc.code} from Olympus Solr; stopping "
                                         f"rather than retrying: {url}") from exc
            if exc.code >= 500 and attempt == 1:
                time.sleep(5.0)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 1:
                time.sleep(5.0)
                continue
            raise OlympusPortalError(f"Olympus Solr unreachable: {exc}") from exc
    raise OlympusPortalError("unreachable")  # pragma: no cover


def _split_codes(value: Any) -> list[str]:
    """Solr stores several article numbers in ONE comma-separated string."""
    if value is None:
        return []
    if isinstance(value, list):
        parts = [p for v in value for p in str(v).split(",")]
    else:
        parts = str(value).split(",")
    return [normalise(p) for p in parts if normalise(p)]


def _slim(doc: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the join and the ifu_links row need."""
    return {
        "id": doc.get("id"),
        "title": doc.get("document_assetTitle_s") or doc.get("IN_NAME") or "",
        "name": doc.get("IN_NAME") or doc.get("titleAutocomplete") or "",
        "url": doc.get("IN_LINK") or "",
        "file_name": doc.get("document_fileName_s") or "",
        "hierarchy": doc.get("IN_HIERARCHY") or "",
        "version": doc.get("document_version_s") or "",
        "article_nos": _split_codes(doc.get("document_articleNo_s")),
        "material_nos": _split_codes(doc.get("document_materialNo_s")),
        "model_names": _split_codes(doc.get("document_globalModelName_ss")),
    }


# Filed under "Instructions for Use" but not the IFU of anything: the symbol glossaries list
# ~500 article numbers each, and the Access Information Sheet tells the reader where to find
# the eIFU. Joined, they would be the ONLY "IFU" of ~85 devices and noise on ~450 more, so
# they are dropped from the index and those devices stay pending.
_NOT_DEVICE_IFU = ("SYMBOL SUPPLEMENT", "ACCESS INFORMATION SHEET")


def _is_device_ifu(title: str) -> bool:
    upper = title.upper()
    return not any(p in upper for p in _NOT_DEVICE_IFU)


def filter_page(raw_docs: list[dict[str, Any]]
                ) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """(kept IFU docs, count per IN_HIERARCHY, docs dropped for having no file link).

    Glossaries dropped by `_is_device_ifu` are counted under the pseudo-hierarchy
    "excluded: not a device IFU" so the run summary shows them."""
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    no_link = 0
    for raw in raw_docs:
        d = _slim(raw)
        counts[d["hierarchy"]] = counts.get(d["hierarchy"], 0) + 1
        if IFU_HIERARCHY not in d["hierarchy"].lower():
            continue
        if not d["url"] or d["url"].endswith("/"):
            no_link += 1
            continue
        if not _is_device_ifu(d["title"]):
            key = "excluded: not a device IFU"
            counts[key] = counts.get(key, 0) + 1
            continue
        kept.append(d)
    return kept, counts, no_link


def fetch_index(verbose: bool = True) -> dict[str, Any]:
    """Enumerate the latest-English Solr set, page by page, and keep only the IFUs.

    Returns {"fetched_at", "num_found", "requests", "hierarchy_counts", "docs": [...]}.
    Documents whose IN_LINK is a bare asset directory (no file hash -- the portal has a few)
    are counted under "no_link" and dropped: there is nothing to serve.
    """
    docs: list[dict[str, Any]] = []
    hierarchy_counts: dict[str, int] = {}
    no_link = 0
    start, num_found, requests = 0, None, 0
    while num_found is None or start < num_found:
        if requests:
            time.sleep(REQUEST_DELAY)
        page = _get_json(_solr_url("*:*", start=start))
        requests += 1
        resp = page.get("response") or {}
        num_found = int(resp.get("numFound", 0))
        batch = resp.get("docs") or []
        if not batch:
            break
        kept, counts, dropped = filter_page(batch)
        docs.extend(kept)
        no_link += dropped
        for h, n in counts.items():
            hierarchy_counts[h] = hierarchy_counts.get(h, 0) + n
        start += len(batch)
        if verbose:
            print(f"  page {requests}: {start}/{num_found} docs, {len(docs)} IFUs so far")
    if not docs:
        raise OlympusPortalError("Olympus index mirrored to zero IFU documents")
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "num_found": num_found,
        "requests": requests,
        "hierarchy_counts": hierarchy_counts,
        "no_link": no_link,
        "docs": docs,
    }


def load_index(refresh: bool = False, verbose: bool = True,
               path: Path = INDEX_PATH) -> dict[str, Any]:
    """Cached mirror; refetched only when missing, forced, or older than INDEX_MAX_AGE."""
    if not refresh and path.exists():
        index = json.loads(path.read_text())
        fetched = datetime.fromisoformat(index.get("fetched_at", "1970-01-01T00:00:00+00:00"))
        if datetime.now(timezone.utc) - fetched < INDEX_MAX_AGE:
            # Re-applied on load so a change to the exclusion list needs no refetch.
            index["docs"] = [d for d in index["docs"] if _is_device_ifu(d["title"])]
            return index
        if verbose:
            print("index stale; refetching")
    index = fetch_index(verbose=verbose)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=1))
    if verbose:
        print(f"index: {len(index['docs'])} IFU documents of {index['num_found']} "
              f"({index['requests']} requests) -> {path}")
    return index


# --------------------------------------------------------------------------- match

def normalise(value: str | None) -> str:
    """Exact-equality key: upper-cased, all whitespace removed. Nothing blunter -- an
    Olympus model differs from its neighbour by one hyphenated segment."""
    return "".join((value or "").split()).upper()


def build_lookup(docs: list[dict[str, Any]]) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Two maps from normalised code -> doc positions.

    `catalog_keys` holds article + material numbers: what a GUDID catalog_number can equal.
    `model_keys` holds those plus global model names and the product name: what a GUDID
    model_number can equal.
    """
    catalog_keys: dict[str, list[int]] = {}
    model_keys: dict[str, list[int]] = {}
    for i, d in enumerate(docs):
        for code in set(d["article_nos"] + d["material_nos"]):
            if _usable_key(code):
                catalog_keys.setdefault(code, []).append(i)
        name = normalise(d.get("name"))
        for code in set(d["article_nos"] + d["material_nos"] + d["model_names"] + [name]):
            if _usable_key(code):
                model_keys.setdefault(code, []).append(i)
    return catalog_keys, model_keys


def _usable_key(code: str) -> bool:
    """The index carries placeholder codes ("#" names 63 documents); those never join."""
    return len(code) >= 3 and any(ch.isalnum() for ch in code)


def match_device(model_number: str | None, catalog_number: str | None,
                 lookup: tuple[dict[str, list[int]], dict[str, list[int]]],
                 docs: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """(match_confidence, documents) for one device; (None, []) when the index has nothing.

    Catalog equality outranks model equality because it names the exact SKU; a model hit
    is still an exact identifier match, but Olympus models can span several catalog
    variants (D-201-11802 is two catalog numbers), so it is recorded one grade lower.
    """
    catalog_keys, model_keys = lookup
    cat = normalise(catalog_number)
    if cat and cat in catalog_keys:
        return "exact_catalog", [docs[i] for i in catalog_keys[cat]]
    model = normalise(model_number)
    if model and model in model_keys:
        return "model_portal_match", [docs[i] for i in model_keys[model]]
    return None, []


def load_devices(db_path: str | Path, limit: int | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=120000")
    conn.row_factory = sqlite3.Row
    where = " or ".join(["lower(company_name) like ?"] * len(COMPANY_PATTERNS))
    sql = f"""
        select rowid, company_name, brand_name, model_number, catalog_number,
               raw_json, device_description
        from devices
        where ({where})
        order by rowid
    """
    params: list[Any] = list(COMPANY_PATTERNS)
    if limit:
        sql += " limit ?"
        params.append(limit)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def identifier_for(row: sqlite3.Row) -> str:
    """The identifier the client will send: catalog when GUDID has one, else the model."""
    return (row["catalog_number"] or "").strip() or (row["model_number"] or "").strip()


def match_devices(index: dict[str, Any], db_path: str | Path,
                  limit: int | None = None) -> list[dict[str, Any]]:
    """Join every Olympus/Gyrus GUDID device to the mirrored index. No network."""
    docs = index["docs"]
    lookup = build_lookup(docs)
    matches: list[dict[str, Any]] = []
    for row in load_devices(db_path, limit=limit):
        confidence, hits = match_device(row["model_number"], row["catalog_number"],
                                        lookup, docs)
        if not hits:
            continue
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        matches.append({
            "rowid": row["rowid"],
            "primary_di": raw.get("PrimaryDI") or raw.get("primaryDI"),
            "identifier": identifier_for(row),
            "model_number": (row["model_number"] or "").strip(),
            "catalog_number": (row["catalog_number"] or "").strip(),
            "description": row["device_description"] or row["brand_name"] or "",
            "confidence": confidence,
            "docs": hits,
        })
    return matches


# --------------------------------------------------------------------------- write

def ensure_schema(db_path: str | Path) -> None:
    """ifu_links plus the source_file_name column, which the base schema predates
    (the Stryker resolver added it the same way)."""
    ensure_ifu_links_table(db_path)
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(ifu_links)")}
        with conn:
            if "source_file_name" not in columns:
                conn.execute("ALTER TABLE ifu_links ADD COLUMN source_file_name TEXT")
    finally:
        conn.close()


def write_matches(matches: list[dict[str, Any]], db_path: str | Path) -> int:
    """Insert one found row per (device, document). Returns rows inserted."""
    ensure_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=60.0)
    inserted = 0
    try:
        for m in matches:
            source_url = _solr_url(_model_query(normalise(m["model_number"] or m["identifier"])))
            # A resolver that could not find this identifier may have left an outcome-only
            # row; a found document supersedes it.
            conn.execute(
                "delete from ifu_links where catalog_number = ? and document_url is null",
                (m["identifier"],))
            for d in m["docs"]:
                cur = conn.execute(
                    """insert or ignore into ifu_links (device_rowid, primary_di,
                       catalog_number, manufacturer_family, source_url, document_url,
                       document_title, language, revision, match_confidence, retrieved_at,
                       status, first_seen_at, last_checked_at, last_success_at,
                       source_file_name)
                       values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (m["rowid"], m["primary_di"], m["identifier"], MANUFACTURER_FAMILY,
                     source_url, d["url"], d["title"][:200], "en", d.get("version") or None,
                     m["confidence"], now, "found", now, now, now,
                     (d.get("file_name") or d["url"].rsplit("/", 1)[-1])[:120]))
                inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted


def _model_query(model: str) -> str:
    """The query the portal's own search box issues for a model -- recorded as source_url."""
    m = model.replace('"', "")
    return (f"titleAutocomplete:*{m}* OR document_articleNo_s:*{m}* "
            f"OR document_materialNo_s:{m}* OR document_globalModelName_ss:{m}*")


# --------------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description="Olympus / Gyrus ACMI index-mirror IFU resolver.")
    ap.add_argument("--dry-run", action="store_true", help="Match and report; write nothing.")
    ap.add_argument("--apply", action="store_true", help="Write found rows to ifu_links.")
    ap.add_argument("--refresh", action="store_true", help="Re-mirror the Solr index.")
    ap.add_argument("--limit", type=int, help="Only consider the first N devices.")
    ap.add_argument("--db", default=str(SQLITE_PATH))
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        ap.error("choose --dry-run or --apply")

    index = load_index(refresh=args.refresh)
    docs = index["docs"]
    print(f"index: {len(docs)} IFU documents (fetched {index['fetched_at'][:19]}); "
          f"hierarchies: {index.get('hierarchy_counts')}; dropped no-link: {index.get('no_link')}")

    devices = load_devices(args.db, limit=args.limit)
    print(f"Resolving {len(devices)} olympus devices")
    matches = match_devices(index, args.db, limit=args.limit)
    by_conf: dict[str, int] = {}
    for m in matches:
        by_conf[m["confidence"]] = by_conf.get(m["confidence"], 0) + 1
    pairs = sum(len(m["docs"]) for m in matches)
    pct = (100 * len(matches) / len(devices)) if devices else 0.0
    print(f"matched {len(matches)}/{len(devices)} devices ({pct:.1f}%), "
          f"{pairs} device->document pairs; by confidence: {by_conf}")

    if args.dry_run:
        print("sample device -> document:")
        step = max(1, len(matches) // 15)
        for m in matches[::step][:15]:
            d = m["docs"][0]
            print(f"  [{m['confidence']}] {m['identifier']} ({m['model_number']}) "
                  f"{m['description'][:60]!r}\n      -> {d['title'][:80]!r} "
                  f"{'+%d more' % (len(m['docs']) - 1) if len(m['docs']) > 1 else ''}\n"
                  f"         {d['url']}")
        print(f"done: dry-run, {len(matches)}/{len(devices)} devices matchable, 0 rows written")
        return 0

    inserted = write_matches(matches, args.db)
    print(f"done: {len(matches)}/{len(devices)} devices resolved, {inserted} rows inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
