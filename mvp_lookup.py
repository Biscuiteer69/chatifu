from __future__ import annotations

import argparse
import json
import re
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
    # Copied from a resolved sibling: same GUDID company + brand, and either the same FDA
    # submission or a brand the portal answered unanimously for (resolvers/sibling_inference.py).
    # A real manufacturer IFU, but one no portal asserted for THIS identifier, so it ranks
    # below every portal-asserted tier and above anything unverified.
    ("found", "sibling_inferred"): 4,
    # FDA-approved PMA labeling (resolvers/fda_resolver.py --pma-labeling): the manufacturer's
    # IFU as approved, i.e. real instructions, warnings and contraindications — but the
    # approval-time revision, so every portal-sourced tier outranks it.
    ("found", "fda_pma_labeling"): 5,
    ("candidate_broad", "search_result"): 6,
    # An FDA 510(k)/PMA summary. Ranked below EVERY manufacturer-sourced tier, including an
    # unverified one, because it is a different kind of document: it carries indications and
    # intended use but no instructions, warnings or contraindications. It is a floor, not a
    # substitute — see resolvers/fda_resolver.py.
    #
    # This was previously implicit: fda_summary matched no key and fell through to the default,
    # which happened to be 5 — the SAME rank as not_found, with ties broken by title string.
    # That is accidental rather than intended ordering, and it made "we hold a regulatory
    # summary" indistinguishable from "we hold nothing" at ~1.09M catalogs.
    ("fda_summary", "fda_submission"): 7,
    ("not_found", None): 8,
    ("not_found", ""): 8,
}
DEFAULT_WARNING = (
    "ChatIFU searches manufacturer IFU sources. "
    "Verify broad matches before use."
)


def db_connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def table_columns_all(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Including GENERATED columns — `table_info` silently omits them, so checking a
    generated column's existence with it reports False for a column that is there."""
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_xinfo({table_name})")}
    except sqlite3.Error:
        return table_columns(conn, table_name)


# ─────────────────────────────────────────────────────────────────────────────
# Catalog-format matching
#
# GUDID and the manufacturer portals punctuate the same part number differently:
# GUDID holds Stryker `45-20004` where the portal indexes `4520004`, and Synthes
# `02007026` where e-ifu.com indexes `02.007.026`. Matching the raw string alone
# leaves ~249k devices whose IFU we already hold unreachable.
#
# Stripping punctuation to compare is only safe WITH a manufacturer check. Real
# example: GUDID device `62-00620` is a Stryker Leibinger part, and `62006-20` in
# our own table is Alphatec's Zodiac spine implant — same digits, different maker,
# and serving one for the other would put the wrong surgical instructions in front
# of a clinician. 180 normalized keys are claimed by more than one maker family.
# So a punctuation-insensitive match is accepted ONLY when the device's company
# resolves to the same manufacturer family as the document.
# ─────────────────────────────────────────────────────────────────────────────

MIN_CATALOG_KEY_LEN = 6   # short keys collide by chance; mirrors MIN_PORTAL_TERM_LEN

# manufacturer_family -> the GUDID company-name fragments that belong to it.
# Subsidiaries matter: Synthes/DePuy file under J&J, Wright Medical under Stryker,
# St. Jude under Abbott, Aesculap under B. Braun.
FAMILY_COMPANY_HINTS: dict[str, tuple[str, ...]] = {
    # ev3 came in with Covidien and is genuinely Medtronic's neurovascular line.
    # Almost every other unmapped company that collides with a stored number is a
    # coincidence, not a missing subsidiary (Oticon hearing aids landing on a Globus
    # spine number, Sklar/Boss instruments on Stryker), so this list stays conservative.
    "medtronic": ("medtronic", "covidien", "ev3"),
    "johnson_and_johnson": ("johnson", "depuy", "synthes", "ethicon", "mentor",
                            "cerenovus", "biosense", "acclarent", "gynecare"),
    "stryker": ("stryker", "wright medical", "howmedica", "k2m", "leibinger"),
    "zimmer_biomet": ("zimmer", "biomet"),
    "abbott": ("abbott", "st. jude", "st jude"),
    "boston_scientific": ("boston scientific",),
    "b_braun": ("b. braun", "b.braun", "braun melsungen", "aesculap"),
    "smith_nephew": ("smith & nephew", "smith and nephew", "smith+nephew"),
    "siemens": ("siemens",),
    "edwards": ("edwards lifesciences",),
    "arthrex": ("arthrex",),
    "alphatec_spine": ("alphatec", "atec spine"),
    "nuvasive": ("nuvasive",),
    "globus_medical": ("globus medical",),
    "fresenius_kabi": ("fresenius",),
    "baxter": ("baxter", "hill-rom", "hillrom", "welch allyn"),
    "coopersurgical": ("coopersurgical", "cooper surgical"),
}


def catalog_key(value: str | None) -> str:
    """Punctuation-insensitive form of a catalog number. Must stay in step with the
    `catalog_key` generated column on ifu_links (same separators, same casing)."""
    if not value:
        return ""
    out = str(value).upper()
    for ch in ("-", ".", "/", " ", "_"):
        out = out.replace(ch, "")
    return out


def family_for_company(company_name: str | None) -> str | None:
    """Which manufacturer family a GUDID company belongs to, or None if unknown.
    None means we cannot vouch for a punctuation-insensitive match."""
    if not company_name:
        return None
    name = str(company_name).lower()
    for family, hints in FAMILY_COMPANY_HINTS.items():
        if any(h in name for h in hints):
            return family
    return None


def _select_rows(conn: sqlite3.Connection, where: str, param: str) -> list[dict[str, Any]]:
    columns = table_columns(conn, "ifu_links")
    if not columns:
        return []
    source_expr = "source_file_name" if "source_file_name" in columns else "NULL AS source_file_name"
    family_expr = "manufacturer_family" if "manufacturer_family" in columns else "NULL AS manufacturer_family"
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
            {family_expr},
            retrieved_at,
            last_checked_at
        FROM ifu_links
        WHERE {where}
        ORDER BY id
        """,
        (param,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_ifu_rows(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
    company_name: str | None = None,
    model_number: str | None = None,
) -> list[dict[str, Any]]:
    """Rows for a device. Exact catalog match first — that is always safe. If that
    yields no servable document, retry punctuation-insensitively on the catalog and
    then on the model number, in both cases only for documents belonging to this
    device's own manufacturer.

    The model fallback is the one that matters at scale. The spine makers publish
    against the model number, so their documents are stored under it: 169,615
    devices — most of Nuvasive, Alphatec, Globus and Medtronic Sofamor Danek — hold
    an IFU we already have but were unreachable through a catalog-only lookup.
    """
    if not Path(db_path).exists():
        return []
    conn = db_connect(db_path)
    try:
        rows = _select_rows(conn, "catalog_number = ?", catalog_number)
        family = family_for_company(company_name)
        if family:
            # An exact string match is only "always safe" within one maker. Catalog
            # numbers are not globally unique: `11111` is a J&J HEALIX anchor REF and a
            # Stryker GmbH device asking for it must not be handed that IFU. Rows from
            # another KNOWN maker are dropped; unattributed rows (FDA summaries,
            # legacy family labels) are kept so nothing already servable goes dark.
            rows = [r for r in rows
                    if (r.get("manufacturer_family") or "") == family
                    or (r.get("manufacturer_family") or "") not in FAMILY_COMPANY_HINTS]
        # Fall through on a `not_found` outcome row, not merely on no row at all.
        # The common shape is exactly that: a resolver probed the GUDID catalog
        # `45-20004`, failed, and wrote not_found — while the document had been
        # stored under the portal's `4520004`. Returning the not_found row as the
        # last word is what kept ~249k devices dark.
        if any(r.get("status") == "found" and r.get("document_url") for r in rows):
            return rows

        if not family:
            return rows   # unknown maker — cannot vouch for anything but an exact hit
        if "catalog_key" not in table_columns_all(conn, "ifu_links"):
            return rows   # migration not applied; exact matching only

        seen = {(r.get("catalog_number"), r.get("document_url")) for r in rows}
        extra: list[dict[str, Any]] = []
        for candidate in (catalog_number, model_number):
            key = catalog_key(candidate)
            if len(key) < MIN_CATALOG_KEY_LEN:
                continue
            for row in _select_rows(conn, "catalog_key = ?", key):
                ident = (row.get("catalog_number"), row.get("document_url"))
                if ident in seen or (row.get("manufacturer_family") or "") != family:
                    continue
                seen.add(ident)
                extra.append(row)
            if any(r.get("status") == "found" and r.get("document_url") for r in extra):
                break   # the catalog answered; no need to widen to the model
        return rows + extra
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
        # Ranked LAST. An error row records a failed attempt and carries no document —
        # measured: all 2,128 of them have an empty document_url, so one can never be served.
        # When it once tied candidate_broad and outranked fda_summary, 864 catalogs returned
        # nothing while a usable document sat one row lower.
        priority = 10
    else:
        priority = 9
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
    if "stryker" in company or "wright medical" in company:
        from resolvers.stryker_resolver import StrykerResolver

        StrykerResolver(db_path=db_path).resolve(catalog_number, model_number=model_number)
        return
    if "zimmer" in company or "biomet" in company:
        from resolvers.zimmer_resolver import ZimmerBiometResolver

        ZimmerBiometResolver(db_path=db_path).resolve(catalog_number, model_number=model_number)
        return
    if "medtronic" in company or "covidien" in company:
        from resolvers.medtronic_resolver import MedtronicResolver

        MedtronicResolver(db_path=db_path).resolve(catalog_number, model_number=model_number)
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


# Words a clinician types around the device, never to identify it. Left in the query they
# are actively harmful under OR semantics: "medtronic gia stapler isnt working can you help
# me troubleshoot it" matched Curbell products on the token "it", because several of their
# model numbers contain ",IT,".
_SEARCH_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "am", "im", "i",
    "isnt", "arent", "wasnt", "doesnt", "dont", "cant", "wont",
    "have", "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "can", "may", "might", "shall", "must", "need", "want",
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "this", "that", "these", "those", "it", "its", "they", "them", "their",
    "you", "your", "we", "our", "us", "me", "my", "mine", "he", "she", "his", "her",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "up", "out", "off",
    "and", "or", "but", "if", "then", "than", "so", "as", "not", "no",
    "help", "please", "troubleshoot", "trouble", "shoot", "shooting", "fix", "fixing",
    "working", "work", "works", "broken", "issue", "issues", "problem", "problems",
    "error", "errors", "question", "about", "regarding", "using", "use", "used",
    "get", "getting", "got", "show", "find", "looking", "look", "tell", "know",
})

# bm25 weights, one per devices_fts column, in declared order. Higher counts for more.
#
# The identifiers dominate because a clinician typing one has named exactly one device.
# company_name and parent_company are deliberately the LOWEST: matching the maker alone is
# the weakest possible evidence, and treating it as strong is precisely what surfaced an
# arbitrary Medtronic spinal system for a stapler query. Description sits in the middle --
# "stapler" is real evidence, but weaker than naming the brand.
_BM25_WEIGHTS = (
    10.0,   # brand_name
    8.0,    # company_name
    8.0,    # parent_company
    2.0,    # device_description
    12.0,   # catalog_number
    12.0,   # model_number
)
# Tuned against the 14 cross-maker queries in tests/test_device_search.py, not chosen by
# feel: 1.0/4.0 and 3.0/4.0 and 6.0/2.5 all scored 13/14, and only 8.0/2.0 gets "stryker hip
# stem" right. The case that decides it is instructive — a competitor's extraction tool whose
# GUDID description reads "Tip, Modular Stem, Stryker" was beating Stryker's own hip stems,
# because a maker's name mentioned in a DESCRIPTION counted for more than the maker actually
# being that company. Naming the manufacturer has to outweigh being mentioned by one.
#
# Weighting the company highly is safe here in a way it was not before, because ranking now
# exists at all. The original failure was not that company matching was strong; it was that
# every Medtronic device tied on company alone and the winner fell out of ORDER BY brand_name.
# bm25 still scores a device matching brand+description+company far above one matching only
# the company, so "medtronic gia stapler" cannot degenerate into "any Medtronic device".


# Demotes devices with no IFU on file. bm25() scores are NEGATIVE and sorted ascending, so
# scaling toward zero pushes a row DOWN the list; 0.6 is enough to lose a tie without letting
# a covered but poorly-matching device outrank a clearly-correct one.
#
# A penalty rather than a filter, deliberately. A device we hold no IFU for is still the right
# answer to "do you have this?", and hiding it would turn a coverage gap into a product that
# looks like it has never heard of the device.
_NO_IFU_PENALTY = 0.6


def search_terms(query: str) -> list[str]:
    """The parts of a query that identify a device.

    Punctuation is stripped so "stapler?" and "stapler" are one term, and single characters
    are dropped as too unselective to be worth an OR arm.
    """
    cleaned = re.sub(r"[^\w\s-]", " ", query.lower())
    terms = [t.strip("-") for t in cleaned.split()]
    kept = [t for t in terms if len(t) > 1 and t not in _SEARCH_STOP_WORDS]
    # A query made entirely of stop words is more likely to be an odd device name than a
    # sentence with nothing in it, so fall back to the raw tokens rather than returning none.
    return kept or [t for t in terms if t]


def search_devices(
    query: str,
    db_path: str | Path = SQLITE_PATH,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """FTS5 search over brand, company, parent company, description and identifiers.

    Tries to satisfy every meaningful term first, then relaxes to ANY term -- but the relaxed
    pass is ranked by bm25, so a device matching all three of "medtronic gia stapler" still
    beats one matching only "medtronic". The old code relaxed the same way and then ordered
    alphabetically, which threw that information away: every Medtronic device tied, and the
    winner was whichever brand sorted first (" TSRH® Spinal System", on a leading space).
    """
    query = query.strip()
    if not query or not Path(db_path).exists():
        return []
    conn = db_connect(db_path)
    try:
        terms = [fts_phrase(t) for t in search_terms(query)]
        if not terms:
            return []
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
    """Matching rows, best first.

    Queries devices_fts directly and joins back, rather than the previous `rowid IN
    (subquery)`: bm25() is only available on the row being scanned, so a subquery cannot
    rank, which is why the old version had to fall back to ordering by brand name.
    """
    try:
        rows = conn.execute(
            """
            SELECT d.brand_name, d.company_name, d.catalog_number, d.model_number
            FROM devices_fts f
            JOIN devices d ON d.rowid = f.rowid
            WHERE devices_fts MATCH ?
            ORDER BY bm25(devices_fts, ?, ?, ?, ?, ?, ?)
                     * (CASE WHEN d.has_ifu = 1 THEN 1.0 ELSE ? END)
            LIMIT ?
            """,
            (fts_expr, *_BM25_WEIGHTS, _NO_IFU_PENALTY, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def fts_phrase(term: str) -> str:
    """Quote a term so FTS5 treats it as a literal phrase.

    Device identifiers are full of characters FTS5 reads as syntax. An
    unquoted "MI-001F" is parsed as a column filter and raises
    "no such column: 001F"; the caller then silently falls back to a LIKE scan
    over 1.26M rows. Since most catalog numbers are hyphenated, the index was
    being bypassed for nearly every identifier search.
    """
    return '"' + term.replace('"', '""') + '"'


def _like_query(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[dict[str, Any]]:
    """Last-resort scan when FTS returns nothing.

    Matches the most selective TERM, not the whole query string. As a literal substring a
    multi-word query can essentially never match a field -- "echelon stapler" is not a
    substring of any brand name -- so this fallback silently returned nothing for exactly
    the multi-word queries that needed it. The longest term is used because it is the most
    specific and the cheapest to exclude rows with.
    """
    terms = sorted(search_terms(query), key=len, reverse=True)
    pattern = f"%{terms[0] if terms else query}%"
    rows = conn.execute(
        """
        SELECT brand_name, company_name, catalog_number, model_number
        FROM devices
        WHERE brand_name LIKE ?
           OR company_name LIKE ?
           OR catalog_number LIKE ?
           OR model_number LIKE ?
        ORDER BY brand_name
        LIMIT ?
        """,
        (pattern, pattern, pattern, pattern, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_device(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
) -> dict[str, Any] | None:
    """Return brand_name/company_name/catalog_number/model_number for one device.

    The identifier may be a catalog number OR a model number. Whole
    manufacturers publish neither consistently: of 96,980 Medtronic devices only
    633 carry a catalog number, so a catalog-only lookup cannot find them at all.
    """
    if not Path(db_path).exists():
        return None
    conn = db_connect(db_path)
    try:
        identifier = catalog_number.strip()
        row = conn.execute(
            """
            SELECT brand_name, company_name, catalog_number, model_number
            FROM devices
            WHERE catalog_number = ?
            LIMIT 1
            """,
            (identifier,),
        ).fetchone()
        if row is None:
            # Fall back to the model number — the only identifier Medtronic and
            # other model-keyed manufacturers publish.
            row = conn.execute(
                """
                SELECT brand_name, company_name, catalog_number, model_number
                FROM devices
                WHERE model_number = ?
                LIMIT 1
                """,
                (identifier,),
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
    return refresh_document_url(best, db_path)


def needs_presigned_url(url: str | None) -> bool:
    """True for documents whose URL must be signed before it can be fetched.

    Stryker serves IFUs from S3 behind a presigned link that expires after 6h,
    so we store the bare object path and mint a signature at serve time. A
    stored full URL (from an older run) is also treated as needing a refresh.
    """
    text = str(url or "")
    return "amazonaws.com" in text


def refresh_document_url(row: dict[str, Any], db_path: str | Path = SQLITE_PATH) -> str | None:
    """Return a currently-valid document URL for a resolved row.

    Non-expiring URLs are returned as-is. For an expiring one, ask the
    manufacturer's API for a fresh link using the stable file name we stored,
    and persist it so concurrent readers benefit.
    """
    url = row.get("document_url")
    if not needs_presigned_url(url):
        return url
    catalog = str(row.get("catalog_number") or "")
    source_file_name = row.get("source_file_name")
    if not catalog or not source_file_name:
        return url

    # Re-mint with the resolver for THIS manufacturer's Qarad tenant. Both Stryker
    # and Zimmer Biomet serve from the same S3 backend, but the presigned link is
    # minted per tenant (selected by Origin / business unit), so using the Stryker
    # resolver for a Zimmer doc finds nothing and the stored (expired) URL leaks
    # through -> 403 at fetch time.
    family = str(row.get("manufacturer_family") or "").lower()
    from resolvers.qarad_tenants import TENANTS, QaradTenantResolver

    try:
        if "zimmer" in family or "biomet" in family:
            from resolvers.zimmer_resolver import ZimmerBiometResolver
            resolver = ZimmerBiometResolver(db_path=db_path)
        elif family in TENANTS:
            # Arthrex/Baxter/Alcon/CooperSurgical mint per tenant too; asking Stryker's
            # tenant for their file found nothing and leaked the expired URL through.
            resolver = QaradTenantResolver(family, db_path=db_path)
        else:
            from resolvers.stryker_resolver import StrykerResolver
            resolver = StrykerResolver(db_path=db_path)
        terms = [catalog]
        if TENANTS.get(family, {}).get("search_key") == "model":
            # Model-keyed tenants (Alcon) were resolved by the device's model, and the
            # catalog is a sub-variant the portal cannot find.
            conn = db_connect(db_path)
            try:
                hit = conn.execute(
                    "select model_number from devices where catalog_number = ? "
                    "and model_number is not null and trim(model_number) != '' limit 1",
                    (catalog,),
                ).fetchone()
            finally:
                conn.close()
            if hit and hit[0]:
                terms.insert(0, str(hit[0]).strip())
        if row.get("match_confidence") == "sibling_inferred":
            # The portal never indexed THIS identifier; the file was found under a sibling's.
            # Ask with the sibling's REF (a portal-asserted row holding the same file).
            conn = db_connect(db_path)
            try:
                sibling = conn.execute(
                    "select catalog_number from ifu_links where source_file_name = ? "
                    "and manufacturer_family = ? and status = 'found' "
                    "and match_confidence in ('exact_catalog', 'brand_match') "
                    "order by id limit 1",
                    (str(source_file_name), family),
                ).fetchone()
            finally:
                conn.close()
            if sibling and sibling[0]:
                terms.insert(0, str(sibling[0]))
        fresh = None
        for term in terms:
            fresh = resolver.fresh_document_url(term, str(source_file_name))
            if fresh:
                break
    except Exception:
        # Re-mint can fail (WAF block, network): never let it crash a request.
        # The stored URL will 403 at fetch, which the caller handles as a miss.
        return url
    if not fresh:
        return url
    conn = db_connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE ifu_links SET document_url = ? WHERE catalog_number = ? AND source_file_name = ?",
                (fresh, catalog, source_file_name),
            )
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return fresh


def get_servable_ifu_documents(
    catalog_number: str,
    db_path: str | Path = SQLITE_PATH,
    limit: int | None = None,
    company_name: str | None = None,
    model_number: str | None = None,
) -> list[dict[str, Any]]:
    """Every verified document for a catalog, best-ranked first.

    A device legitimately maps to several official IFUs — catalog 0030-4864 has
    9 (patient booklets and professional-use info across four vision
    corrections), and a Synthes implant returns its device-specific IFU plus
    generic processing procedures. Serving whichever one ranks first would show
    an authentic document that may not answer the question, so callers search
    across the set and keep the document that actually contains the answer.
    """
    rows = [r for r in fetch_ifu_rows(catalog_number, db_path, company_name, model_number)
            if r.get("document_url")]
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
