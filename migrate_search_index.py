"""Rebuild the device search index so a clinician's own words can find a device.

A beta tester searched "medtronic gia stapler" and was shown the TSRH Spinal System. Every
part of that failure is in the index rather than the ranking alone:

  * "stapler" is unfindable. devices_fts covers brand/company/catalog/model only, and the
    word lives in GUDID's deviceDescription ("Stapler with DST Series Technology"), which
    was never indexed. No amount of ranking finds a term the index does not hold.
  * "medtronic" cannot match the device. The GIA stapler's company_name is "Covidien LP".
    Medtronic has owned Covidien since 2015 and every clinician calls it a Medtronic
    stapler, but nothing in the row says "Medtronic".

So the search needs two more indexed fields: the device description, and the PARENT company
that people actually name. The parent list is not invented here — it reuses the same
company_targets patterns the scrapers use to decide which maker owns a catalog, so search and
acquisition can never disagree about who owns what.

Both columns are derived, so this is re-runnable: it recomputes and rebuilds from scratch.

Run with the scraper fleet STOPPED. It rewrites all 5M rows, and although WAL keeps readers
going, the fleet's writers would sit on their busy timeout for the duration.

    systemctl --user stop chatifu-scraper-fleet
    .venv/bin/python migrate_search_index.py
    systemctl --user start chatifu-scraper-fleet
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import company_targets as CT

VAULT = Path(__file__).resolve().parent
DB = VAULT / "chatifu.sqlite3"


def add_columns(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    for column, decl in (("device_description", "text"), ("parent_company", "text"),
                         ("has_ifu", "integer not null default 0")):
        if column not in existing:
            conn.execute(f"alter table devices add column {column} {decl}")
            print(f"  added devices.{column}")


def fill_has_ifu(conn: sqlite3.Connection) -> None:
    """Mark devices we can actually answer for, so search can prefer them.

    Finding the right product is only half of a useful search. The GIA query, once it
    reached the staplers, put a variant with no IFU on file at the top -- a tester clicks the
    first result and is told we have nothing, for a device family we cover perfectly well.

    Not a filter: a device with no IFU yet is still the right answer to "do you have this",
    and hiding it would turn a coverage gap into a product that appears not to know the
    device exists. Ranking only.

    Derived from ifu_links, so it goes stale as the fleet resolves more. Refreshed nightly
    by nightly_chatifu.py.
    """
    started = time.monotonic()
    conn.execute("update devices set has_ifu = 0")
    # Materialise the covered identifiers into an indexed temp table first. Left as a
    # subquery against ifu_links, SQLite re-scans 1.7M rows for each of the 5M devices and
    # the statement does not finish in any useful time.
    conn.execute("drop table if exists temp.covered")
    conn.execute(
        "create temp table covered as select distinct catalog_number cc from ifu_links "
        "where status = 'found' and document_url is not null"
    )
    conn.execute("create index temp.covered_cc on covered(cc)")
    cur = conn.execute(
        """
        update devices set has_ifu = 1
        where coalesce(nullif(trim(catalog_number), ''), nullif(trim(model_number), ''))
              in (select cc from covered)
        """
    )
    conn.commit()
    print(f"  has_ifu: {cur.rowcount:,} devices answerable "
          f"in {time.monotonic() - started:.0f}s")


def fill_descriptions(conn: sqlite3.Connection) -> None:
    """Lift deviceDescription out of raw_json into its own column.

    Done in SQL with json_extract rather than by reading 5M rows into Python: the JSON is
    already stored, and a single UPDATE is an order of magnitude faster than a round trip
    per row.
    """
    started = time.monotonic()
    cur = conn.execute(
        "update devices set device_description = "
        "nullif(trim(coalesce(json_extract(raw_json, '$.deviceDescription'), '')), '')"
    )
    conn.commit()
    print(f"  device_description: {cur.rowcount:,} rows in {time.monotonic() - started:.0f}s")


def fill_parent_company(conn: sqlite3.Connection) -> None:
    """Label each device with the parent company a clinician would name.

    Only the makers in company_targets get a parent; everyone else keeps NULL rather than
    being labelled with themselves. Repeating company_name into a second indexed column
    would double its weight in the ranking for no gain, and would make a two-word company
    match look twice as good as a genuine brand match.
    """
    started = time.monotonic()
    conn.execute("update devices set parent_company = null")
    total = 0
    for target in CT.TOP_DEVICE_TARGETS:
        patterns = target["company_patterns"]
        where = " or ".join(["lower(company_name) like ?"] * len(patterns))
        cur = conn.execute(
            f"update devices set parent_company = ? where ({where})",
            (target["name"], *patterns),
        )
        if cur.rowcount:
            print(f"    {target['name'][:34]:34} {cur.rowcount:>9,}")
        total += cur.rowcount

    # Subsidiaries GUDID files under a name that never mentions the parent. Applied after
    # the target patterns and only where nothing was set, so a target pattern always wins.
    for parent, patterns in CT.SEARCH_PARENT_ALIASES:
        where = " or ".join(["lower(company_name) like ?"] * len(patterns))
        cur = conn.execute(
            f"update devices set parent_company = ? "
            f"where parent_company is null and ({where})",
            (parent, *patterns),
        )
        if cur.rowcount:
            print(f"    (alias) {parent[:27]:27} {cur.rowcount:>9,}")
        total += cur.rowcount
    conn.commit()
    print(f"  parent_company: {total:,} rows in {time.monotonic() - started:.0f}s")


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Recreate devices_fts over the wider column set.

    Kept as an external-content table (content='devices'): the column values live once, in
    devices, and FTS stores only the index. On 5M rows that is the difference between a few
    hundred MB and several GB.

    unicode61 with remove_diacritics=2 so an accented brand matches its plain spelling, and
    tokenchars '-' so hyphenated catalog numbers stay one token instead of splitting into
    fragments that match half the database.
    """
    started = time.monotonic()
    conn.execute("drop table if exists devices_fts")
    conn.execute(
        """
        create virtual table devices_fts using fts5(
            brand_name,
            company_name,
            parent_company,
            device_description,
            catalog_number,
            model_number,
            content='devices',
            content_rowid='rowid',
            tokenize="unicode61 remove_diacritics 2 tokenchars '-'"
        )
        """
    )
    conn.execute("insert into devices_fts(devices_fts) values('rebuild')")
    conn.commit()
    print(f"  devices_fts rebuilt in {time.monotonic() - started:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the device search index.")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--skip-fill", action="store_true",
                    help="Only rebuild the FTS table; assume the columns are populated.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=600.0)
    conn.execute("PRAGMA busy_timeout=600000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        print("columns:")
        add_columns(conn)
        if not args.skip_fill:
            print("populating:")
            fill_descriptions(conn)
            fill_parent_company(conn)
            fill_has_ifu(conn)
        print("index:")
        rebuild_fts(conn)
        n = conn.execute("select count(*) from devices_fts").fetchone()[0]
        print(f"done: {n:,} rows indexed")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
