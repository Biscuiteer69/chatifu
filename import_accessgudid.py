from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from company_targets import TOP_DEVICE_TARGETS, target_by_key


DEFAULT_SQLITE = Path(os.environ.get("CHATIFU_SQLITE_PATH", "/home/biscuited/projects/chatifu_vault/chatifu.sqlite3"))
OPENFDA_UDI_URL = "https://api.fda.gov/device/udi.json"


def schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists devices (
            id text primary key,
            company_name text,
            brand_name text,
            model_number text,
            catalog_number text,
            raw_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists processed_skus (
            sku text primary key,
            status text not null,
            source text,
            updated_at text default current_timestamp
        )
        """
    )
    conn.execute("create index if not exists idx_devices_company on devices(company_name)")
    conn.execute("create index if not exists idx_devices_catalog on devices(catalog_number)")
    conn.commit()


def normalized_patterns(target_keys: Iterable[str]) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    for key in target_keys:
        target = target_by_key(key)
        patterns = []
        for pattern in target.get("company_patterns", []):
            token = str(pattern).replace("%", "").strip().lower()
            if token:
                patterns.append(token)
        targets[key] = patterns
    return targets


def matched_target(company_name: str, patterns_by_target: dict[str, list[str]]) -> str | None:
    company = company_name.lower()
    for key, patterns in patterns_by_target.items():
        if any(pattern in company for pattern in patterns):
            return key
    return None


def stable_id(row: dict[str, str]) -> str:
    raw = row.get("PrimaryDI") or row.get("publicDeviceRecordKey")
    if raw:
        return raw
    digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()
    return digest


def row_to_device(row: dict[str, str], target: str | None, source_name: str) -> dict[str, str]:
    raw = dict(row)
    raw["_chatifu_target"] = target or "all"
    raw["_fda_source"] = source_name
    return {
        "id": stable_id(row),
        "company_name": row.get("companyName", ""),
        "brand_name": row.get("brandName", ""),
        "model_number": row.get("versionModelNumber", ""),
        "catalog_number": row.get("catalogNumber", ""),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


def upsert_batch(conn: sqlite3.Connection, devices: list[dict[str, str]]) -> None:
    conn.executemany(
        """
        insert into devices (id, company_name, brand_name, model_number, catalog_number, raw_json)
        values (:id, :company_name, :brand_name, :model_number, :catalog_number, :raw_json)
        on conflict(id) do update set
            company_name=excluded.company_name,
            brand_name=excluded.brand_name,
            model_number=excluded.model_number,
            catalog_number=excluded.catalog_number,
            raw_json=excluded.raw_json
        """,
        devices,
    )
    refresh_devices_fts(conn, devices)


def refresh_devices_fts(conn: sqlite3.Connection, devices: list[dict[str, str]]) -> None:
    exists = conn.execute(
        "select 1 from sqlite_master where type='table' and name='devices_fts'"
    ).fetchone()
    if not exists:
        return
    for device in devices:
        row = conn.execute("select rowid from devices where id=?", (device["id"],)).fetchone()
        if not row:
            continue
        conn.execute("delete from devices_fts where rowid=?", (row[0],))
        conn.execute(
            """
            insert into devices_fts(rowid, brand_name, company_name, catalog_number)
            values (?, ?, ?, ?)
            """,
            (row[0], device.get("brand_name"), device.get("company_name"), device.get("catalog_number")),
        )


def iter_device_rows(source: Path):
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            with archive.open("device.txt") as raw:
                wrapper = (line.decode("utf-8", errors="replace") for line in raw)
                yield from csv.DictReader(wrapper, delimiter="|")
    else:
        with source.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            yield from csv.DictReader(handle, delimiter="|")


def import_devices(args: argparse.Namespace) -> Counter:
    if args.openfda_company:
        return import_openfda_company(args)

    target_keys = args.target or [str(target["key"]) for target in TOP_DEVICE_TARGETS]
    patterns_by_target = normalized_patterns(target_keys)
    conn = None
    if not args.dry_run:
        args.sqlite.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(args.sqlite)
        schema(conn)

    counts: Counter = Counter()
    batch: list[dict[str, str]] = []
    for row in iter_device_rows(args.source):
        counts["read"] += 1
        company_name = row.get("companyName", "")
        target = matched_target(company_name, patterns_by_target)
        if target is None and not args.all_companies:
            continue

        counts["matched"] += 1
        if target:
            counts[f"target:{target}"] += 1
        if args.dry_run:
            if args.limit and counts["matched"] >= args.limit:
                break
            continue

        batch.append(row_to_device(row, target, args.source.name))
        if len(batch) >= args.batch_size:
            assert conn is not None
            with conn:
                upsert_batch(conn, batch)
            batch.clear()
            print(f"[import] matched={counts['matched']} read={counts['read']}", flush=True)
        if args.limit and counts["matched"] >= args.limit:
            break

    if batch and not args.dry_run:
        assert conn is not None
        with conn:
            upsert_batch(conn, batch)
    if conn is not None:
        conn.close()
    return counts


def import_openfda_company(args: argparse.Namespace) -> Counter:
    conn = None
    if not args.dry_run:
        args.sqlite.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(args.sqlite)
        schema(conn)

    counts: Counter = Counter()
    batch: list[dict[str, str]] = []
    skip = 0
    limit = min(args.openfda_page_size, 1000)
    total: int | None = None
    while total is None or skip < total:
        if args.limit and counts["matched"] >= args.limit:
            break
        page_limit = limit
        if args.limit:
            page_limit = min(page_limit, args.limit - counts["matched"])
        payload = fetch_openfda_company_page(args.openfda_company, page_limit, skip, args.timeout)
        results = payload.get("results") or []
        total = int(((payload.get("meta") or {}).get("results") or {}).get("total") or len(results))
        counts["read"] += len(results)
        if not results:
            break

        for raw in results:
            row = openfda_row_to_device(raw, args.openfda_company)
            counts["matched"] += 1
            counts["target:edwards"] += 1
            if args.dry_run:
                continue
            batch.append(row)
            if len(batch) >= args.batch_size:
                assert conn is not None
                with conn:
                    upsert_batch(conn, batch)
                batch.clear()
                print(f"[openfda-import] matched={counts['matched']} read={counts['read']}", flush=True)

        skip += len(results)
        if args.openfda_delay:
            time.sleep(args.openfda_delay)

    if batch and not args.dry_run:
        assert conn is not None
        with conn:
            upsert_batch(conn, batch)
    if conn is not None:
        conn.close()
    return counts


def fetch_openfda_company_page(company: str, limit: int, skip: int, timeout: int) -> dict[str, Any]:
    params = {
        "search": f'company_name:"{company}"',
        "limit": str(limit),
        "skip": str(skip),
    }
    url = f"{OPENFDA_UDI_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "ChatIFU/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def openfda_row_to_device(raw: dict[str, Any], target_company: str) -> dict[str, str]:
    mapped = {
        "PrimaryDI": _primary_di(raw),
        "publicDeviceRecordKey": raw.get("public_device_record_key", ""),
        "companyName": raw.get("company_name", ""),
        "brandName": raw.get("brand_name", ""),
        "versionModelNumber": raw.get("version_or_model_number", ""),
        "catalogNumber": raw.get("catalog_number", ""),
        "_chatifu_target": "edwards" if "edwards" in target_company.lower() else "all",
        "_fda_source": "openfda_device_udi",
        "_openfda": raw,
    }
    return row_to_device(mapped, mapped["_chatifu_target"], "openfda_device_udi")


def _primary_di(raw: dict[str, Any]) -> str:
    identifiers = raw.get("identifiers") or []
    for item in identifiers:
        if isinstance(item, dict) and str(item.get("type") or "").lower() == "primary":
            return str(item.get("id") or "")
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Import FDA AccessGUDID device targets into the local ChatIFU SQLite queue.")
    parser.add_argument("--source", type=Path, help="AccessGUDID device.txt or full-release zip.")
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    parser.add_argument("--target", action="append", help="Target key to import. Repeatable. Defaults to all top-20 targets.")
    parser.add_argument("--all-companies", action="store_true", help="Import every company instead of filtering to targets.")
    parser.add_argument("--limit", type=int, help="Stop after this many matched rows.")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--openfda-company", help="Import live OpenFDA UDI records for a company name.")
    parser.add_argument("--openfda-page-size", type=int, default=1000)
    parser.add_argument("--openfda-delay", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    if args.openfda_company:
        args.source = args.source or Path("openfda")
    elif args.source is None:
        raise SystemExit("--source is required unless --openfda-company is supplied")
    elif not args.source.exists():
        raise SystemExit(f"Source not found: {args.source}")
    counts = import_devices(args)
    json.dump(counts, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
