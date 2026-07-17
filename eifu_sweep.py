"""Universal e-ifu.com sweep — the multi-manufacturer scale lever.

e-ifu.com is a SHARED UDI eIFU portal hosting hundreds of manufacturers,
searchable by catalog number. Our EifuResolver already resolves against it (it's
how J&J is scraped). This sweep points that resolver at the LONG TAIL — every
company we do NOT have a dedicated resolver for — so one engine captures IFUs
across the whole GUDID universe, not just the top makers.

Why exclude the dedicated makers: a device e-ifu marks `not_found` (because it's
not on e-ifu.com) would be skipped by that maker's own resolver (they all exclude
`not_found`). So we only sweep companies with no dedicated resolver, letting the
per-maker resolvers own their catalogs.

MUST STAY GENTLE: e-ifu.com is WAF-protected and shared with the J&J scrape, so
small batches + long gaps + the resolver's own delay. Run via the fleet pipeline.

Usage: python -m ... (module) / python eifu_sweep.py --batch 60
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from resolvers.eifu_resolver import SQLITE_PATH, EifuResolver

# Companies that already have a dedicated resolver — the sweep skips them.
_DEDICATED_PATTERNS = (
    "%medtronic%", "%covidien%", "%valleylab%",
    "%johnson & johnson%", "%depuy%", "%ethicon%", "%synthes%", "%biosense%",
    "%mentor%", "%abiomed%", "%acclarent%", "%cerenovus%",
    "%stryker%", "%wright medical%",
    "%zimmer%", "%biomet%",
    "%abbott%", "%st jude%", "%st. jude%",
    "%boston scientific%",
    "%edwards lifesciences%",
    "%aesculap%", "%b braun%", "%b. braun%", "%bbraun%",
)


def load_sweep_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    excl = " AND ".join(["lower(company_name) NOT LIKE ?"] * len(_DEDICATED_PATTERNS))
    try:
        # RANDOM order spreads requests across many companies rather than
        # hammering one giant catalog (Cardinal/Medline) first.
        return conn.execute(
            f"""
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null and trim(d.catalog_number) != ''
              and {excl}
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.catalog_number
                  and l.status in ('found', 'candidate_broad', 'not_found')
              )
            order by random()
            limit ?
            """,
            (*_DEDICATED_PATTERNS, limit),
        ).fetchall()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Universal e-ifu.com long-tail IFU sweep.")
    parser.add_argument("--batch", type=int, default=60, help="Resolve N unresolved long-tail devices.")
    parser.add_argument("--db", default=str(SQLITE_PATH))
    args = parser.parse_args()

    rows = load_sweep_devices(args.batch, db_path=args.db)
    print(f"Resolving {len(rows)} e-ifu devices")
    resolver = EifuResolver(db_path=args.db)
    found = 0
    for index, row in enumerate(rows, 1):
        raw_json = json.loads(row["raw_json"]) if row["raw_json"] else {}
        try:
            docs = resolver.resolve(
                row["catalog_number"], model_number=row["model_number"],
                device_rowid=row["rowid"], primary_di=raw_json.get("PrimaryDI"),
            )
        except Exception as exc:  # noqa: BLE001 - WAF/session gate -> surface for fleet backoff
            print(f"[{index}/{len(rows)}] {row['catalog_number']}: error {exc}")
            # A session/WAF failure is worth stopping the batch so the fleet backs off.
            if "gate" in str(exc).lower() or "403" in str(exc) or "waf" in str(exc).lower():
                print("WAF/gate — stopping batch")
                break
            continue
        if docs:
            found += 1
        if index % 25 == 0 or docs:
            print(f"[{index}/{len(rows)}] {row['company_name'][:18]} {row['catalog_number']}: "
                  f"{len(docs)} docs (found {found})")
    print(f"done: {found}/{len(rows)} devices resolved")


if __name__ == "__main__":
    main()
