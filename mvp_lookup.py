from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from resolvers.eifu_resolver import EifuResolver, SQLITE_PATH


ERROR_STATUSES = {
    "session_gate",
    "auth_failed",
    "init_failed",
    "http_error",
    "network_error",
    "timeout",
}
STATUS_PRIORITY = {
    ("found", "exact_catalog"): 0,
    ("found", "model_match"): 1,
    # The document title names the device's GUDID brand (verify_ifu_candidates.py).
    # e-ifu.com substring-matches catalogs against document metadata, so a
    # coincidental hit can carry the catalog in its file name while a genuine
    # one carries it nowhere at all; brand agreement is what separates them.
    ("found", "brand_match"): 2,
    # The portal returned this document for the device's exact model number
    # (the catalog found nothing). Ranked last among verified tiers: it rests on
    # the portal's applicability metadata rather than on text we can inspect.
    ("found", "model_portal_match"): 3,
    ("candidate_broad", "search_result"): 4,
    ("not_found", None): 5,
    ("not_found", ""): 5,
}
DEFAULT_WARNING = (
    "ChatIFU searches manufacturer IFU sources. "
    "Verify broad matches before use."
)


def db_connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def fetch_ifu_rows(catalog_number: str, db_path: str | Path = SQLITE_PATH) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    conn = db_connect(db_path)
    try:
        columns = table_columns(conn, "ifu_links")
        if not columns:
            return []
        source_expr = "source_file_name" if "source_file_name" in columns else "NULL AS source_file_name"
        rows = conn.execute(
            f"""
            SELECT
                catalog_number,
                status,
                match_confidence,
                document_title,
                document_url,
                language,
                revision,
                {source_expr},
                retrieved_at,
                last_checked_at
            FROM ifu_links
            WHERE catalog_number = ?
            ORDER BY id
            """,
            (catalog_number,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def row_priority(row: dict[str, Any]) -> tuple[int, str]:
    """Rank a row for document selection.

    A device can map to several equally-ranked documents (catalog 0030-4864 has
    9: patient booklets and professional-use info across four vision
    corrections). Ties are broken by the caller's row order, so fetch_ifu_rows
    orders by id — without it SQLite's row order is unspecified and the IFU we
    serve could change between identical requests.
    """
    status = row.get("status")
    confidence = row.get("match_confidence")
    if (status, confidence) in STATUS_PRIORITY:
        priority = STATUS_PRIORITY[(status, confidence)]
    elif status in ERROR_STATUSES:
        priority = 4
    else:
        priority = 5
    return priority, str(row.get("document_title") or row.get("document_url") or "")


def normalize_result(catalog_number: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "catalog_number": catalog_number,
            "status": "not_found",
            "match_confidence": None,
            "document_title": None,
            "document_url": None,
            "language": None,
            "revision": None,
            "source_file_name": None,
            "retrieved_at": None,
            "last_checked_at": None,
            "warning": "No cached row was found and lookup did not return metadata.",
            "candidates": [],
        }

    sorted_rows = sorted(rows, key=row_priority)
    best = sorted_rows[0]
    candidates = [
        row_to_candidate(row)
        for row in sorted_rows
        if row.get("status") == "candidate_broad"
    ]
    warning = warning_for_row(best, len(candidates))
    return {
        "catalog_number": catalog_number,
        "status": best.get("status"),
        "match_confidence": best.get("match_confidence"),
        "document_title": best.get("document_title"),
        "document_url": best.get("document_url"),
        "language": best.get("language"),
        "revision": best.get("revision"),
        "source_file_name": best.get("source_file_name"),
        "retrieved_at": best.get("retrieved_at"),
        "last_checked_at": best.get("last_checked_at"),
        "warning": warning,
        "candidates": candidates if len(candidates) > 1 or best.get("status") == "candidate_broad" else [],
    }


def row_to_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": row.get("status"),
        "match_confidence": row.get("match_confidence"),
        "document_title": row.get("document_title"),
        "document_url": row.get("document_url"),
        "language": row.get("language"),
        "revision": row.get("revision"),
        "source_file_name": row.get("source_file_name"),
        "retrieved_at": row.get("retrieved_at"),
        "last_checked_at": row.get("last_checked_at"),
    }


def warning_for_row(row: dict[str, Any], candidate_count: int) -> str | None:
    status = row.get("status")
    if status == "candidate_broad":
        if candidate_count > 1:
            return f"{DEFAULT_WARNING} {candidate_count} broad candidates were returned."
        return DEFAULT_WARNING
    if status in ERROR_STATUSES:
        return f"Resolver status is {status}; this is not a not_found result."
    return None


def lookup_catalog(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
    refresh: bool = False,
    resolver_factory: Callable[[str | Path], Any] | None = None,
) -> dict[str, Any]:
    catalog_number = catalog_number.strip()
    if not catalog_number:
        raise ValueError("catalog_number is required.")

    cached_rows = fetch_ifu_rows(catalog_number, db_path)
    if cached_rows and not refresh:
        return normalize_result(catalog_number, cached_rows)

    if resolver_factory is not None:
        resolver = resolver_factory(db_path)
        resolver.resolve(catalog_number)
    else:
        ensure_ifu_for_catalog(catalog_number, db_path=db_path)
    rows = fetch_ifu_rows(catalog_number, db_path)
    return normalize_result(catalog_number, rows)


def ensure_ifu_for_catalog(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
) -> None:
    device = get_device(catalog_number, db_path=db_path)
    company = str((device or {}).get("company_name") or "").lower()
    model_number = (device or {}).get("model_number")
    if "abbott" in company:
        from resolvers.abbott_resolver import AbbottResolver

        AbbottResolver(db_path=db_path).resolve(catalog_number, model_number=model_number)
        return
    if "edwards lifesciences" in company:
        from resolvers.edwards_resolver import EdwardsResolver

        EdwardsResolver(db_path=db_path).resolve(catalog_number, model_number=model_number)
        return
    EifuResolver(db_path=db_path).resolve(catalog_number, model_number=model_number)


def format_human(result: dict[str, Any]) -> str:
    lines = [
        f"Catalog: {result['catalog_number']}",
        f"Status: {result.get('status') or ''}",
        f"Confidence: {result.get('match_confidence') or ''}",
    ]
    if result.get("document_title"):
        lines.append(f"Title: {result['document_title']}")
    if result.get("language"):
        lines.append(f"Language: {result['language']}")
    if result.get("revision"):
        lines.append(f"Revision: {result['revision']}")
    if result.get("source_file_name"):
        lines.append(f"Source file: {result['source_file_name']}")
    if result.get("document_url"):
        lines.append(f"URL: {result['document_url']}")
    if result.get("retrieved_at"):
        lines.append(f"Retrieved: {result['retrieved_at']}")
    if result.get("last_checked_at"):
        lines.append(f"Last checked: {result['last_checked_at']}")
    if result.get("warning"):
        lines.append(f"Warning: {result['warning']}")
    candidates = result.get("candidates") or []
    if len(candidates) > 1:
        lines.append(f"Candidates: {len(candidates)}")
    return "\n".join(lines)


def search_devices(
    query: str,
    db_path: str | Path = SQLITE_PATH,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    FTS5 full-text search over brand_name, company_name, catalog_number.
    Falls back to OR semantics when AND returns nothing, then LIKE.
    """
    query = query.strip()
    if not query or not Path(db_path).exists():
        return []
    conn = db_connect(db_path)
    try:
        terms = [t for t in query.split() if t]
        rows = _fts_query(conn, " ".join(terms), limit)
        if not rows and len(terms) > 1:
            rows = _fts_query(conn, " OR ".join(terms), limit)
        if not rows:
            rows = _like_query(conn, query, limit)
        return rows
    finally:
        conn.close()


def _fts_query(
    conn: sqlite3.Connection, fts_expr: str, limit: int
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT d.brand_name, d.company_name, d.catalog_number, d.model_number
            FROM devices d
            WHERE d.rowid IN (
                SELECT rowid FROM devices_fts WHERE devices_fts MATCH ?
            )
            ORDER BY d.brand_name
            LIMIT ?
            """,
            (fts_expr, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def _like_query(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[dict[str, Any]]:
    pattern = f"%{query}%"
    rows = conn.execute(
        """
        SELECT brand_name, company_name, catalog_number, model_number
        FROM devices
        WHERE brand_name LIKE ?
           OR company_name LIKE ?
           OR catalog_number LIKE ?
        ORDER BY brand_name
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_device(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
) -> dict[str, Any] | None:
    """Return brand_name/company_name/catalog_number/model_number for one device."""
    if not Path(db_path).exists():
        return None
    conn = db_connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT brand_name, company_name, catalog_number, model_number
            FROM devices
            WHERE catalog_number = ?
            LIMIT 1
            """,
            (catalog_number.strip(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_best_ifu_url(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
    include_candidates: bool = False,
) -> str | None:
    """Return the highest-priority cached document_url for a catalog, or None.

    By default only verified matches (status ``found``) are returned: a
    ``candidate_broad`` row can be an unrelated document that happened to
    appear in a manufacturer portal search, and serving it as *the* IFU
    would show a user the wrong device's instructions. Pass
    ``include_candidates=True`` to fall back to broad candidates.
    """
    rows = fetch_ifu_rows(catalog_number, db_path)
    doc_rows = [r for r in rows if r.get("document_url")]
    if not include_candidates:
        doc_rows = [r for r in doc_rows if r.get("status") == "found"]
    if not doc_rows:
        return None
    best = min(doc_rows, key=row_priority)
    return best.get("document_url")


def get_servable_ifu_documents(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every verified document for a catalog, best-ranked first.

    A device legitimately maps to several official IFUs — catalog 0030-4864 has
    9 (patient booklets and professional-use info across four vision
    corrections), and a Synthes implant returns its device-specific IFU plus
    generic processing procedures. Serving whichever one ranks first would show
    an authentic document that may not answer the question, so callers search
    across the set and keep the document that actually contains the answer.
    """
    rows = [r for r in fetch_ifu_rows(catalog_number, db_path) if r.get("document_url")]
    rows = [r for r in rows if r.get("status") == "found"]
    rows.sort(key=row_priority)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row["document_url"])
        if url in seen:
            continue
        seen.add(url)
        deduped.append(row)
    return deduped[:limit] if limit else deduped


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Look up ChatIFU e-IFU metadata by catalog number.")
    parser.add_argument("--catalog", required=True, help="Catalog number to look up.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human text.")
    parser.add_argument("--refresh", action="store_true", help="Refresh by calling the resolver even if cached.")
    parser.add_argument("--db", default=str(SQLITE_PATH), help="SQLite database path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = lookup_catalog(args.catalog, db_path=args.db, refresh=args.refresh)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(format_human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
