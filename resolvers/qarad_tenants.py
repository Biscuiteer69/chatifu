"""Generic runner for Qarad/IFUcare eIFU tenants.

api-public.qarad.eifu.online hosts many manufacturers behind one API; the platform picks the
tenant from the Origin header. Stryker, Zimmer and Arthrex each got their own module before the
pattern was obvious, but a tenant is really just four values — origin, business unit, product
type, and which GUDID companies it covers — so new ones belong in the table below rather than in
another near-identical file.

Detecting a tenant: fetch the portal, look for "IFUcare" in the HTML, and grep its Next.js
_app chunk for `api-public.qarad.eifu.online`. That five-minute check turns what looks like a
scraper build into a config entry.

Finding its ids (never guess — both failure modes are silent):
    business_units()          -> {'BAX': 1}     the shared unit 0 usually 400s for a tenant
    product_types(bu_id)      -> {'MEDDEV': 1}  ids are PER UNIT; a wrong one is a bare 404
A 404 from a wrong product type is indistinguishable from "no results", which is how Arthrex
looked empty for months.

MULTI-UNIT TENANTS: Alcon splits its catalogue across 13 business units (IOL, CAT, REF, VIT...),
each with its own product type. `units` accepts a list of (bu, pt) pairs and they are tried in
order, remembering the unit that answered so the common case costs one request rather than 13.

ALL tenants here share ONE WAF budget with Stryker and Zimmer. In the fleet they must be
registered with host "qarad" so HOST_LIMITS caps their combined concurrency.

Usage:
    python -m resolvers.qarad_tenants --tenant baxter --batch 25
    python -m resolvers.qarad_tenants --tenant baxter --catalog 2B8001
    python -m resolvers.qarad_tenants --list
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

# tenant -> origin, (business_unit, product_type) pairs to try, GUDID company patterns.
TENANTS: dict[str, dict] = {
    "baxter": {
        "origin": "https://edocs.baxter.com",
        "units": [(1, 1)],                      # BAX / MEDDEV
        "patterns": ["%baxter%"],
        "label": "Baxter",
    },
    "coopersurgical": {
        "origin": "https://ifu.coopersurgical.com",
        "units": [(2, 1)],                      # cooper / cooper
        "patterns": ["%coopersurgical%", "%cooper surgical%"],
        "label": "CooperSurgical",
    },
    "bd": {
        # NOT WIRED INTO THE FLEET — do not enable without fixing the cost model first.
        #
        # BD is a valid tenant and the ids below are all confirmed, but multi-unit search makes
        # it uniquely expensive: a MISS costs one request per unit, so 13 units x a 25-device
        # batch is ~325 requests against a WAF shared with Stryker, Zimmer, Arthrex, Baxter and
        # Alcon. A 10-device test was rate-limited at device 8. Bounding the unit list would cap
        # the cost but silently mark anything in the untried units as not_found — the exact
        # false-negative failure that made Arthrex and Baxter look empty for months.
        #
        # The fix is to derive the unit from the device rather than search for it: BD's GUDID
        # companies map to units (Bard Peripheral Vascular -> pit, Bard urology -> ucc, ...),
        # which turns 13 requests into 1. That mapping needs deriving from resolved samples, not
        # from more probing.
        "origin": "https://eifu.bd.com",
        # BD splits across ~16 business units, each with its own product-type ids, and each unit
        # exposes general / lot / softwareversion — only "general" is the searchable catalogue.
        # Ordered by expected GUDID footprint: BD's devices here are mostly Bard (peripheral
        # intervention, urology & critical care, surgery) plus medication delivery/management.
        # Order matters because a miss costs one request per unit until something answers, and
        # the resolver promotes whichever unit hits.
        # Country units (Vietnam/korea/indonesia/taiwan) are omitted — US catalogue only.
        "units": [
            (9, 71),    # pit      — peripheral intervention (Bard Peripheral Vascular)
            (12, 75),   # ucc      — urology & critical care (Bard)
            (11, 74),   # surgery
            (7, 70),    # mds      — medication delivery
            (8, 72),    # mms      — medication management
            (6, 69),    # ids      — integrated diagnostic solutions
            (3, 67),    # globalbd
            (13, 76),   # idssm
            (5, 68),    # Biosciences
            (10, 73),   # pharma
            (18, 83),   # APM
            (20, 84),   # embecta
            (0, 5),     # global   — confirmed working, but thin on its own (~3/8)
        ],
        "patterns": ["%becton%", "%bard%", "%c.r. bard%", "%carefusion%"],
        "label": "BD",
    },
    "alcon": {
        "origin": "https://ifu.alcon.com",
        # 13 units, each with its own product type (id = bu - 1). Ordered by expected volume:
        # intraocular lenses and cataract consumables dominate Alcon's GUDID footprint.
        "units": [(2, 1), (3, 2), (4, 3), (5, 4), (6, 5), (7, 6), (8, 7),
                  (9, 8), (10, 9), (11, 10), (12, 11), (13, 12), (14, 13)],
        "patterns": ["%alcon%"],
        "label": "Alcon",
    },
}


class QaradTenantResolver(StrykerResolver):
    """StrykerResolver pointed at another tenant, with multi-unit search support."""

    def __init__(self, tenant: str, **kwargs):
        cfg = TENANTS[tenant]
        self.ORIGIN = cfg["origin"]
        self.FAMILY = tenant
        self._units: list[tuple[int, int]] = list(cfg["units"])
        self.SEARCH_BUSINESS_UNIT, self.SEARCH_PRODUCT_TYPE = self._units[0]
        super().__init__(**kwargs)

    def search(self, catalog_number: str):
        """Try each (unit, product-type) until one answers, then keep that unit first.

        Single-unit tenants take exactly one request. Multi-unit ones pay the search cost only
        until the right unit is found, after which the promoted unit usually answers first."""
        last_exc = None
        for index, (bu, pt) in enumerate(list(self._units)):
            self.SEARCH_BUSINESS_UNIT, self.SEARCH_PRODUCT_TYPE = bu, pt
            try:
                items = super().search(catalog_number)
            except WafBlocked:
                raise                            # a block is about the client, not the unit
            except Exception as exc:             # 400/404 = wrong unit for this product
                last_exc = exc
                continue
            if items:
                if index:                        # promote the unit that answered
                    self._units.insert(0, self._units.pop(index))
                return items
        if last_exc and len(self._units) == 1:
            raise last_exc
        return []


def load_devices(tenant: str, limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    patterns = TENANTS[tenant]["patterns"]
    where = " or ".join(["lower(d.company_name) like ?"] * len(patterns))
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            f"""
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null
              and trim(d.catalog_number) != ''
              and ({where})
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.catalog_number
                  and l.status in ('found', 'candidate_broad', 'not_found')
              )
            order by d.catalog_number
            limit ?
            """,
            (*patterns, limit),
        ).fetchall()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Qarad/IFUcare tenant eIFU resolver.")
    ap.add_argument("--tenant", choices=sorted(TENANTS), help="Which tenant to resolve.")
    ap.add_argument("--batch", type=int, help="Resolve N unresolved devices.")
    ap.add_argument("--catalog", help="Resolve a single catalog number.")
    ap.add_argument("--list", action="store_true", help="Show configured tenants.")
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()

    if args.list:
        for key, cfg in sorted(TENANTS.items()):
            print(f"{key:<16} {cfg['origin']:<40} units={cfg['units']}")
        return 0
    if not args.tenant:
        ap.error("--tenant is required (or --list)")

    label = TENANTS[args.tenant]["label"]
    resolver = QaradTenantResolver(args.tenant)

    if args.catalog:
        docs = resolver.resolve(args.catalog, log_to_db=not args.no_db)
        print(json.dumps(docs, indent=2)[:1200])
        return 0
    if not args.batch:
        ap.error("--batch or --catalog is required")

    rows = load_devices(args.tenant, args.batch)
    print(f"Resolving {len(rows)} {label} devices")
    found = blocks = 0
    for index, row in enumerate(rows, 1):
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        try:
            docs = resolver.resolve(
                catalog_number=row["catalog_number"], model_number=row["model_number"],
                device_rowid=row["rowid"], primary_di=raw.get("PrimaryDI"),
            )
        except WafBlocked as exc:
            blocks += 1
            if blocks >= MAX_CONSECUTIVE_BLOCKS:
                print(f"blocked {blocks}x — stopping at {index}/{len(rows)}. Resolved {found}. "
                      f"Blocked devices were not marked; retry later.")
                return 0
            wait = BLOCK_BACKOFF_SEC * blocks
            print(f"[{index}/{len(rows)}] {exc} — backing off {wait:.0f}s")
            time.sleep(wait)
            continue
        blocks = 0
        if docs:
            found += 1
        if index % 25 == 0 or docs:
            print(f"[{index}/{len(rows)}] {row['catalog_number']}: {len(docs)} docs (found {found})")
    print(f"done: {found}/{len(rows)} devices resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
