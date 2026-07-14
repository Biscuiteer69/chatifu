"""Promote broad IFU candidates whose document title names the device's brand.

Background — why the obvious tests do not work:

e-ifu.com's /search-document-metadata/{catalog} endpoint substring-matches the
query against document metadata, so it returns a MIX of two kinds of hit:

  * legitimate: the portal's real device->document mapping. Catalog 0030-4864
    (brand STAR S4 IR) returns the STAR S4 IR / iDESIGN booklets. The catalog
    number appears nowhere in the title, the file name, or even the PDF body —
    those documents carry their own part numbers (0030-8814 Rev. B).
  * coincidental: the catalog string happens to sit inside a longer token in an
    unrelated file name. Catalog 00825 (GYNECARE THERMACHOICE) returns MENTOR
    breast implant documents because LAB100825478v3_eIFU.pdf contains "00825".

So the coincidental hits look MORE textually convincing than the real ones, and
no string test against the catalog number — title, file name, or PDF content —
can separate them. Commit bb70780 was right to refuse to serve these.

The signal that does separate them is brand agreement: a legitimate document
titles itself with the device's brand ("... IDESIGN, STAR S4 IR, HYPEROPIA"),
while a coincidental one names a different product family entirely. This script
promotes a candidate only when the manufacturer's own document title contains
the device's GUDID brand name.

Dry-run by default; pass --apply to write.

    python verify_ifu_candidates.py --family johnson_and_johnson
    python verify_ifu_candidates.py --apply
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone

from mvp_lookup import SQLITE_PATH

# The device's brand name appears in the manufacturer's document title.
VERIFIED_CONFIDENCE = "brand_match"

# Short brands ("ECHO", "PS") collide with ordinary words in titles.
MIN_BRAND_LEN = 5


def normalize(text: str) -> str:
    """Uppercase, strip trademark marks and punctuation, collapse whitespace.

    Titles and brands disagree on ™/®, hyphens and spacing ("STAR S4 IR" vs
    "STAR S4-IR"), so compare on a punctuation-free, single-spaced form.
    """
    text = text.replace("™", " ").replace("®", " ")
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().upper()


def brand_in_title(brand: str, title: str) -> bool:
    """True when the full brand phrase appears in the document title.

    The whole phrase must match, not individual tokens: the token "STAR" alone
    would hit unrelated titles, while "STAR S4 IR" identifies the device.
    """
    nb, nt = normalize(brand), normalize(title)
    if len(nb) < MIN_BRAND_LEN:
        return False
    return f" {nb} " in f" {nt} "


def brands_for_catalog(conn: sqlite3.Connection, catalog: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT brand_name FROM devices "
        "WHERE catalog_number = ? AND brand_name IS NOT NULL AND brand_name != ''",
        (catalog,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def fetch_candidates(conn: sqlite3.Connection, family: str | None, limit: int | None) -> list[sqlite3.Row]:
    sql = [
        "SELECT id, catalog_number, manufacturer_family, document_url, document_title",
        "FROM ifu_links",
        "WHERE status = 'candidate_broad'",
        "  AND document_url IS NOT NULL AND document_url != ''",
        "  AND document_title IS NOT NULL AND document_title != ''",
    ]
    params: list[object] = []
    if family:
        sql.append("  AND manufacturer_family = ?")
        params.append(family)
    sql.append("ORDER BY catalog_number, id")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    return conn.execute("\n".join(sql), params).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(SQLITE_PATH))
    parser.add_argument("--family", help="Restrict to one manufacturer_family.")
    parser.add_argument("--limit", type=int, help="Cap rows examined.")
    parser.add_argument("--verbose", action="store_true", help="Also print rejected rows.")
    parser.add_argument("--apply", action="store_true", help="Write promotions (default: dry run).")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = fetch_candidates(conn, args.family, args.limit)
    if not rows:
        print("no candidate_broad rows matched")
        return 0

    brand_cache: dict[str, list[str]] = {}
    promoted = rejected = no_brand = 0
    promoted_catalogs: set[str] = set()
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        catalog = str(row["catalog_number"] or "")
        title = str(row["document_title"] or "")

        if catalog not in brand_cache:
            brand_cache[catalog] = brands_for_catalog(conn, catalog)
        brands = brand_cache[catalog]
        if not brands:
            no_brand += 1
            if args.verbose:
                print(f"NOBRAND  {catalog}: no GUDID brand on file")
            continue

        hit = next((b for b in brands if brand_in_title(b, title)), None)
        if hit:
            print(f"PROMOTE  {catalog} [{hit}] -> {title[:52]}")
            promoted += 1
            promoted_catalogs.add(catalog)
            if args.apply:
                conn.execute(
                    """
                    UPDATE ifu_links
                    SET status = 'found',
                        match_confidence = ?,
                        last_checked_at = ?,
                        last_success_at = ?
                    WHERE id = ?
                    """,
                    (VERIFIED_CONFIDENCE, now, now, int(row["id"])),
                )
        else:
            rejected += 1
            if args.verbose:
                print(f"REJECT   {catalog} [{brands[0]}] != {title[:46]}")

    if args.apply:
        conn.commit()
    conn.close()

    verb = "promoted" if args.apply else "would promote"
    print(
        f"\n{verb}: {promoted} rows across {len(promoted_catalogs)} catalogs | "
        f"brand mismatch: {rejected} | no brand on device: {no_brand} | examined: {len(rows)}"
    )
    if not args.apply and promoted:
        print("dry run — rerun with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
