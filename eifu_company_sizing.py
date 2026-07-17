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
    conn = sqlite3.connect(db, timeout=60.0)
    conn.row_factory = sqlite3.Row
    excl = " AND ".join(["lower(company_name) NOT LIKE ?"] * len(_DEDICATED_PATTERNS))
    try:
        return conn.execute(
            f"""
            select company_name, catalog_number, model_number, raw_json,
                   min(catalog_number) as _mincat
            from devices
            where catalog_number is not null and trim(catalog_number) != ''
              and {excl}
              and company_name not in (select company from eifu_company_coverage)
            group by company_name
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
        for row in rows:
            company, catalog = row["company_name"], row["catalog_number"]
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


if __name__ == "__main__":
    main()
