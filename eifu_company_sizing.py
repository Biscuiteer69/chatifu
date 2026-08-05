"""Disciplined e-ifu.com company-sizing sweep.

Instead of blindly resolving millions of devices through e-ifu.com (WAF-risky,
mostly misses), probe ONE representative device per company to learn WHICH of the
~11.5k GUDID companies are actually hosted on e-ifu.com. The result (a coverage
table) then lets a follow-up sweep target only the covered makers — efficient and
gentle on the shared WAF.

Resumable: records every company checked in `eifu_company_coverage`, so re-running
picks up where it left off. Gentle: a per-probe delay + hard backoff on any
WAF/session-gate error. Runs to completion in the background over hours.

Usage: python eifu_company_sizing.py [--delay 4] [--per-batch 40]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from resolvers.eifu_resolver import SQLITE_PATH, EifuResolver
from eifu_sweep import _DEDICATED_PATTERNS

WAF_HINTS = ("gate", "403", "waf", "forbidden", "429")


def ensure_table(db: str) -> None:
    conn = sqlite3.connect(db, timeout=60.0)
    try:
        with conn:
            conn.execute(
                """create table if not exists eifu_company_coverage (
                    company text primary key, covered integer, docs integer,
                    sample_catalog text, checked_at text)"""
            )
    finally:
        conn.close()


def next_companies(db: str, limit: int) -> list[sqlite3.Row]:
    """One probe device per company not yet sized.

    Keyed on whichever identifier the device actually carries. Requiring a catalog number
    silently skipped 5,855 of the 11,585 companies — 1.12M devices whose GUDID records
    supply only a model number — so they were never probed and their coverage was recorded
    nowhere, not even as unknown. The probe identifier is the same shape either way, and
    EifuResolver already accepts a model number, so this costs no extra requests.
    """
    conn = sqlite3.connect(db, timeout=60.0)
    conn.row_factory = sqlite3.Row
    excl = " AND ".join(["lower(company_name) NOT LIKE ?"] * len(_DEDICATED_PATTERNS))
    try:
        # Pick the LONGEST identifier the company has, not the alphabetically first. A
        # company is recorded covered/uncovered from this single probe and never revisited,
        # so a probe that could not have matched becomes a permanent false negative. The
        # resolver refuses to verify a portal hit on fewer than MIN_PORTAL_TERM_LEN
        # alphanumerics because short identifiers collide inside longer tokens, and min()
        # kept handing it exactly those -- '148', 'Rho', '740.RA'. Longest-first also
        # correlates with the distinctive full-length REFs the portals actually index.
        return conn.execute(
            f"""
            select company_name, catalog_number, model_number, raw_json, probe_id from (
                select company_name, catalog_number, model_number, raw_json,
                       coalesce(nullif(trim(catalog_number), ''),
                                nullif(trim(model_number), '')) as probe_id,
                       row_number() over (
                           partition by company_name
                           order by length(coalesce(nullif(trim(catalog_number), ''),
                                                    nullif(trim(model_number), ''))) desc,
                                    coalesce(nullif(trim(catalog_number), ''),
                                             nullif(trim(model_number), ''))
                       ) as rn
                from devices
                where coalesce(nullif(trim(catalog_number), ''),
                               nullif(trim(model_number), '')) is not null
                  and {excl}
                  and company_name not in (select company from eifu_company_coverage)
            ) where rn = 1
            limit ?
            """,
            (*_DEDICATED_PATTERNS, limit),
        ).fetchall()
    finally:
        conn.close()


def record(db: str, company: str, covered: bool, docs: int, catalog: str) -> None:
    conn = sqlite3.connect(db, timeout=60.0)
    try:
        with conn:
            conn.execute(
                "insert or replace into eifu_company_coverage(company,covered,docs,sample_catalog,checked_at) "
                "values (?,?,?,?,?)",
                (company, 1 if covered else 0, docs, catalog,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(SQLITE_PATH))
    ap.add_argument("--delay", type=float, default=4.0, help="seconds between company probes")
    ap.add_argument("--per-batch", type=int, default=40)
    ap.add_argument("--once", action="store_true",
                    help="Run a single batch and exit, for the fleet supervisor to loop. "
                         "e-ifu.com allows ONE fleet worker at a time (DEFAULT_HOST_LIMIT), "
                         "shared with the J&J scrape; running this standalone alongside the "
                         "fleet would double the request rate on the host that has WAF-banned "
                         "us before.")
    args = ap.parse_args()

    ensure_table(args.db)
    resolver = EifuResolver(db_path=args.db)
    total = covered_n = 0
    backoff = 60
    while True:
        rows = next_companies(args.db, args.per_batch)
        if not rows:
            print(f"sizing complete: {total} companies checked, {covered_n} covered")
            return
        # Header the fleet's batch_re parses to tell "worked N" from "backlog dry".
        print(f"Sizing {len(rows)} companies", flush=True)
        for row in rows:
            # probe_id is the catalog number where GUDID supplies one and the model number
            # otherwise; the portal is searched the same way for both.
            company, catalog = row["company_name"], row["probe_id"]
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            try:
                # log_to_db=True so a real hit is captured as coverage, not just measured.
                docs = resolver.resolve(catalog, model_number=row["model_number"],
                                        primary_di=raw.get("PrimaryDI"))
                backoff = 60
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if any(h in msg for h in WAF_HINTS):
                    print(f"WAF/gate ({str(exc)[:40]}); backoff {backoff}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 3600)
                    continue          # retry this company later (not recorded)
                docs = []             # other error -> treat as not-covered for this probe
            record(args.db, company, bool(docs), len(docs), catalog)
            total += 1
            if docs:
                covered_n += 1
                print(f"[{total}] COVERED {company[:40]} ({len(docs)} docs) [{covered_n} covered]")
            elif total % 50 == 0:
                print(f"[{total}] checked… {covered_n} covered so far")
            time.sleep(args.delay)
        if args.once:
            print(f"batch done: {total} companies checked, {covered_n} covered")
            return


if __name__ == "__main__":
    main()
