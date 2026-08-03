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
from datetime import datetime, timezone
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
        # Deliberately NARROWER than company_targets.py's baxter patterns, which also count
        # Hillrom and Welch Allyn (Baxter acquisitions). Adding them here was tried and
        # DISPROVED: edocs.baxter.com returns 0 items for Welch Allyn catalogs, so the acquired
        # brands are not on this portal and every attempt wrote a false negative. The monitor's
        # ZERO YIELD check caught it within 25 rows, which is what it is for.
        #
        # Consequence to keep in view: Baxter's resolver can legitimately report "backlog dry"
        # while ~5,700 Hillrom/Welch Allyn catalogs remain uncovered in the metric. They need
        # their own portal (Welch Allyn historically ran one), not a wider pattern here.
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
        # DISABLED — do not add to the fleet. Kept because the mapping below is real and took
        # real requests to establish, but the economics do not work on a SHARED WAF:
        #   * Two separate test batches (10 and 12 devices) were both rate-limited part way.
        #   * On 2026-07-31 three fleet WAF flags landed at 16:22 / 19:36 / 21:04, and the last
        #     coincides with a BD test window — i.e. BD probing was spending budget that
        #     Stryker, Zimmer, Arthrex, Baxter and Alcon depend on.
        #   * Yield is poor where it does work: of four companies probed, only Bard Peripheral
        #     Vascular returned an actual document. C.R. Bard found the product but had no IFU
        #     attached, and Bard Access / Becton Dickinson found nothing.
        # 7,523 identifiers is not worth repeatedly endangering five working tenants. Revisit
        # only if BD moves off the shared host, or via the e-ifu sweep which already covers
        # 10,144 of BD's devices on a different backend.
        "enabled": False,
        # The most expensive tenant here, and the reason qarad_unit_hints exists. A MISS costs
        # one request per unit, so before hint caching a 10-device test fired ~130 requests and
        # was rate-limited at device 8. Now the unit that answers is remembered per GUDID company
        # (Bard Peripheral Vascular lives in `pit` and always will), so discovery is paid once
        # per company and the steady state is ~1 request per device.
        #
        # Bounding the unit list would also have capped the cost, but it would silently mark
        # anything in the untried units as not_found — the same false negative that made Arthrex
        # and Baxter look empty for months, and worse than no coverage because it reads as an
        # answer. Keep the full list; let the hints make it cheap.
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
        # BD's resolvable REFs are letter-prefixed (CK001003A, EX051703CS, PW300); the purely
        # numeric values in GUDID do not resolve. Alphabetical order walks the numeric ones
        # first and scores 0, which reads as a dead tenant — the same way Smith+Nephew's first
        # batch did.
        "order_sql": "case when substr(trim(d.catalog_number),1,1) glob '[A-Za-z]' then 0 else 1 end, d.catalog_number",
    },
    "alcon": {
        "origin": "https://ifu.alcon.com",
        # 13 units, each with its own product type (id = bu - 1). Ordered by expected volume:
        # intraocular lenses and cataract consumables dominate Alcon's GUDID footprint.
        "units": [(2, 1), (3, 2), (4, 3), (5, 4), (6, 5), (7, 6), (8, 7),
                  (9, 8), (10, 9), (11, 10), (12, 11), (13, 12), (14, 13)],
        # See company_targets.py: "%alcon%" also matches ALCONOX INC, whose catalog numbers
        # collide with real Alcon products.
        "patterns": ["%alcon laboratories%"],
        "label": "Alcon",
    },
}


def ensure_hint_table(db_path: str | Path = SQLITE_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("""create table if not exists qarad_unit_hints(
            tenant text not null, company_name text not null,
            business_unit integer not null, product_type integer not null,
            hits integer default 1, updated_at text,
            primary key (tenant, company_name))""")
        conn.commit()
    finally:
        conn.close()


class QaradTenantResolver(StrykerResolver):
    """StrykerResolver pointed at another tenant, with multi-unit search support.

    Multi-unit tenants are expensive: a MISS costs one request per unit, and BD's 13 units
    turned a 10-device test into ~130 requests, which the WAF rate-limited. The fix is to stop
    re-discovering the same answer — a maker's GUDID company maps to one business unit
    (Bard Peripheral Vascular lives in `pit`, and always will), so the unit that answers is
    remembered per company in qarad_unit_hints and tried first next time. Discovery is paid once
    per company instead of once per device, and the steady state is ~1 request."""

    def __init__(self, tenant: str, db_path: str | Path = SQLITE_PATH, **kwargs):
        cfg = TENANTS[tenant]
        self.ORIGIN = cfg["origin"]
        self.FAMILY = tenant
        self.tenant = tenant
        self.db_path = str(db_path)
        self._units: list[tuple[int, int]] = list(cfg["units"])
        self.SEARCH_BUSINESS_UNIT, self.SEARCH_PRODUCT_TYPE = self._units[0]
        self._hints: dict[str, tuple[int, int]] = {}
        self._confident: set[str] = set()
        # resolve() is inherited and calls search() without the company, so the
        # current device's company is carried here rather than rewriting resolve().
        self.current_company: str | None = None
        super().__init__(**kwargs)
        if len(self._units) > 1:
            ensure_hint_table(self.db_path)
            self._load_hints()

    def _load_hints(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            self._hints = {
                row[0]: (row[1], row[2]) for row in conn.execute(
                    "select company_name, business_unit, product_type from qarad_unit_hints "
                    "where tenant=?", (self.tenant,))
            }
        except sqlite3.OperationalError:
            self._hints = {}
        finally:
            conn.close()

    def _save_hint(self, company: str, bu: int, pt: int) -> None:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute(
                """insert into qarad_unit_hints(tenant, company_name, business_unit,
                   product_type, hits, updated_at) values(?,?,?,?,1,?)
                   on conflict(tenant, company_name) do update set
                     business_unit=excluded.business_unit, product_type=excluded.product_type,
                     hits=hits+1, updated_at=excluded.updated_at""",
                (self.tenant, company, bu, pt, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
        self._hints[company] = (bu, pt)

    def search(self, catalog_number: str, company_name: str | None = None):
        """Try the company's known unit first, then the rest until one answers.

        Single-unit tenants take exactly one request and never touch the hint table."""
        order = list(self._units)
        company_name = company_name or self.current_company
        hint = self._hints.get(company_name or "")
        if hint and hint in order:
            order.insert(0, order.pop(order.index(hint)))
            # Once a company has answered from a unit, trust it: a maker's catalogue lives in ONE
            # business unit, so a miss there is a real miss. Without this, hints only make HITS
            # cheap while every MISS still costs one request per unit — which is how a 12-device
            # BD batch still managed to trip the WAF even with caching in place.
            if company_name in self._confident:
                order = order[:1]

        last_exc = None
        for bu, pt in order:
            self.SEARCH_BUSINESS_UNIT, self.SEARCH_PRODUCT_TYPE = bu, pt
            try:
                items = super().search(catalog_number)
            except WafBlocked:
                raise                            # a block is about the client, not the unit
            except Exception as exc:             # 400/404 = wrong unit for this product
                last_exc = exc
                continue
            if items:
                if company_name:
                    self._save_hint(company_name, bu, pt)
                    self._confident.add(company_name)
                index = self._units.index((bu, pt))
                if index:                        # promote within this process too
                    self._units.insert(0, self._units.pop(index))
                return items
        if last_exc and len(self._units) == 1:
            raise last_exc
        return []


def load_devices(tenant: str, limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    cfg = TENANTS[tenant]
    patterns = cfg["patterns"]
    order_sql = cfg.get("order_sql", "d.catalog_number")
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
            order by {order_sql}
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
            state = "disabled" if cfg.get("enabled") is False else "enabled"
            print(f"{key:<16} {state:<9} {cfg['origin']:<40} units={len(cfg['units'])}")
        return 0
    if not args.tenant:
        ap.error("--tenant is required (or --list)")
    if TENANTS[args.tenant].get("enabled") is False:
        ap.error(f"tenant {args.tenant!r} is disabled — see its entry in TENANTS for why")

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
        resolver.current_company = row["company_name"]
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
