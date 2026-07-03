from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import zipfile
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from company_targets import TOP_DEVICE_TARGETS, target_by_key


DEFAULT_SQLITE = Path(os.environ.get("CHATIFU_SQLITE_PATH", "/home/biscuited/projects/chatifu_vault/chatifu.sqlite3"))


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


@lru_cache(maxsize=4096)
def _pattern_regex(token: str) -> re.Pattern[str]:
    # Word-boundary matching: bare substring matching mis-tagged e.g.
    # "Highridge Medical" as ge_healthcare ("...ridGE MEDICAL") and
    # "Gebdi Dental" / "One Lambda" as bd. A token must not be embedded
    # inside a longer alphanumeric run.
    return re.compile(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])")


def matched_target(company_name: str, patterns_by_target: dict[str, list[str]]) -> str | None:
    company = company_name.lower()
    for key, patterns in patterns_by_target.items():
        if any(_pattern_regex(pattern).search(company) for pattern in patterns):
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


def prune_stale(sqlite_path: Path, dry_run: bool = False) -> Counter:
    """Re-evaluate every stored device against the CURRENT target patterns.

    Deletes rows that no longer match any target (imported by an earlier,
    looser pattern — e.g. substring false positives) and retags rows whose
    matching target changed. Keeps devices_fts in sync for deletions.
    """
    patterns_by_target = normalized_patterns([str(t["key"]) for t in TOP_DEVICE_TARGETS])
    conn = sqlite3.connect(sqlite_path)
    counts: Counter = Counter()
    has_fts = conn.execute(
        "select 1 from sqlite_master where type='table' and name='devices_fts'"
    ).fetchone() is not None

    rows = conn.execute("select rowid, id, company_name, raw_json from devices")
    to_delete: list[tuple[int, str]] = []
    to_retag: list[tuple[str, str, str]] = []  # (new_raw_json, new_target, id)
    for rowid, device_id, company_name, raw_json in rows:
        counts["scanned"] += 1
        new_target = matched_target(company_name or "", patterns_by_target)
        if new_target is None:
            to_delete.append((rowid, device_id))
            continue
        raw = json.loads(raw_json)
        if raw.get("_chatifu_target") != new_target:
            raw["_chatifu_target"] = new_target
            to_retag.append((json.dumps(raw, ensure_ascii=False), new_target, device_id))

    counts["deleted"] = len(to_delete)
    counts["retagged"] = len(to_retag)
    if dry_run:
        conn.close()
        return counts

    with conn:
        for rowid, device_id in to_delete:
            conn.execute("delete from devices where rowid=?", (rowid,))
            if has_fts:
                conn.execute("delete from devices_fts where rowid=?", (rowid,))
        conn.executemany("update devices set raw_json=? where id=?", [(r, i) for r, _, i in to_retag])
    conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Import FDA AccessGUDID device targets into the local ChatIFU SQLite queue.")
    parser.add_argument("--source", type=Path, required=True, help="AccessGUDID device.txt or full-release zip.")
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    parser.add_argument("--target", action="append", help="Target key to import. Repeatable. Defaults to all top-20 targets.")
    parser.add_argument("--all-companies", action="store_true", help="Import every company instead of filtering to targets.")
    parser.add_argument("--limit", type=int, help="Stop after this many matched rows.")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prune-stale",
        action="store_true",
        help="After import, delete/retag stored devices that no longer match the current target patterns.",
    )
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Source not found: {args.source}")
    counts = import_devices(args)
    json.dump(counts, sys.stdout, indent=2)
    print()
    if args.prune_stale:
        prune_counts = prune_stale(args.sqlite, dry_run=args.dry_run)
        print("[prune]", json.dumps(prune_counts))


if __name__ == "__main__":
    main()
