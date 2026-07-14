"""Resolve Zimmer Biomet IFUs — same Qarad eIFU platform as Stryker.

Zimmer Biomet's portal (docs.zimmerbiomet.com) is served by the same
api-public.qarad.eifu.online backend Stryker uses. The platform picks the
manufacturer from the Origin header, so this is a tenant config rather than a
new resolver:

  * Origin: https://docs.zimmerbiomet.com
  * The tenant reports isGlobalSearch=False, so the shared business-unit 0 that
    Stryker searches returns 403 here; Zimmer's own unit (2) must be used.
  * Its REF attribute is named "Reference or catalog number", where Stryker's is
    "Ref or catalog number" — both are matched.

REF equality still does the verifying, and it earns its keep here: searching
catalog 154379 also returns a product whose UDI is 00887868154379, which merely
*contains* the catalog. That neighbour is rejected.

Usage:
    python -m resolvers.zimmer_resolver 154379
    python -m resolvers.zimmer_resolver --batch 500
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from resolvers.eifu_resolver import SQLITE_PATH
from resolvers.stryker_resolver import (
    BLOCK_BACKOFF_SEC,
    MAX_CONSECUTIVE_BLOCKS,
    StrykerResolver,
    WafBlocked,
)

MANUFACTURER_FAMILY = "zimmer_biomet"


class ZimmerBiometResolver(StrykerResolver):
    ORIGIN = "https://docs.zimmerbiomet.com"
    # isGlobalSearch=False: business-unit 0 is 403 for this tenant.
    SEARCH_BUSINESS_UNIT = 2
    FAMILY = MANUFACTURER_FAMILY


def load_zimmer_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
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
              and (lower(d.company_name) like '%zimmer%'
                   or lower(d.company_name) like '%biomet%')
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
    parser = argparse.ArgumentParser(description="Resolve Zimmer Biomet eIFU documents.")
    parser.add_argument("catalog_number", nargs="?")
    parser.add_argument("--batch", type=int, help="Resolve N unresolved Zimmer Biomet devices.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    resolver = ZimmerBiometResolver()
    if args.batch:
        rows = load_zimmer_devices(args.batch)
        print(f"Resolving {len(rows)} Zimmer Biomet devices")
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
                          f"Resolved {found}. Blocked devices were not marked; retry later.")
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
    print(json.dumps(documents, indent=2)[:1200])


if __name__ == "__main__":
    raise SystemExit(main())
