"""Resolve Arthrex IFUs — same Qarad/IFUcare eIFU platform as Stryker and Zimmer.

edfu.arthrex.com is a Next.js front end (its footer reads "Concept by IFUcare") over the same
api-public.qarad.eifu.online backend Stryker uses, so this is a tenant config rather than a new
resolver. Note the host is **edfu**, not ifu — ifu.arthrex.com does not resolve at all, which is
why earlier attempts to guess this portal's URL all 404'd.

Tenant specifics, each established by probing rather than assumed:
  * Origin: https://edfu.arthrex.com
  * Business unit: ARX = 8. The shared unit 0 returns 400 for this tenant.
  * Product type: 10. The inherited search path hardcoded product-types/1, which returns a bare
    404 here — indistinguishable from "no results" unless you check product_types(8) first.
  * Its IFU group is named "Directions For Use" (DFU), not "Instructions For Use". The old group
    filter matched only the latter, so products that plainly had a document returned zero.
  * Its REF attribute is "Product REF".
  * One document carries ~197 language variants; is_english_current_file correctly picks the
    single current en-US revision out of them.

Shares the Qarad WAF budget with Stryker and Zimmer, so it MUST be registered in the fleet with
host "qarad" — HOST_LIMITS governs their combined concurrency, not each one's.

Usage:
    python -m resolvers.arthrex_resolver 1002.33A0
    python -m resolvers.arthrex_resolver --batch 25
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

MANUFACTURER_FAMILY = "arthrex"


class ArthrexResolver(StrykerResolver):
    ORIGIN = "https://edfu.arthrex.com"
    SEARCH_BUSINESS_UNIT = 8      # ARX; the shared unit 0 returns 400 for this tenant
    SEARCH_PRODUCT_TYPE = 10      # "products"; the inherited default of 1 is a 404 here
    FAMILY = MANUFACTURER_FAMILY


def load_arthrex_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
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
              and lower(d.company_name) like '%arthrex%'
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
    parser = argparse.ArgumentParser(description="Resolve Arthrex eIFU documents.")
    parser.add_argument("catalog_number", nargs="?")
    parser.add_argument("--batch", type=int, help="Resolve N unresolved Arthrex devices.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    resolver = ArthrexResolver()
    if args.batch:
        rows = load_arthrex_devices(args.batch)
        print(f"Resolving {len(rows)} Arthrex devices")
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
