from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import create_client


DEFAULT_ENV = "/home/biscuited/projects/chatifu_production/.env"
DEFAULT_EXPORT_DIR = "/home/biscuited/projects/chatifu_vault/export"


def row_count(client: Any, table: str) -> int | None:
    try:
        res = client.table(table).select("id", count="exact").limit(1).execute()
        return res.count
    except Exception:
        return None


def export_table(client: Any, table: str, out_dir: Path, page_size: int) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{table}.jsonl"
    total = row_count(client, table)
    offset = 0
    written = 0
    print(f"[export] {table}: starting, total={total if total is not None else 'unknown'}")
    with path.open("w", encoding="utf-8") as f:
        while True:
            end = offset + page_size - 1
            res = client.table(table).select("*").range(offset, end).execute()
            rows = res.data or []
            if not rows:
                break
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
            written += len(rows)
            print(f"[export] {table}: wrote {written}")
            if len(rows) < page_size:
                break
            offset += page_size
    print(f"[export] {table}: done -> {path}")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Supabase ChatIFU tables to local JSONL.")
    parser.add_argument("--env", default=DEFAULT_ENV)
    parser.add_argument("--out-dir", default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--tables", nargs="+", default=["devices", "documents"])
    parser.add_argument("--count-only", action="store_true")
    args = parser.parse_args()

    load_dotenv(args.env)
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("Missing SUPABASE_URL and SUPABASE_KEY/SUPABASE_SERVICE_ROLE_KEY.")

    client = create_client(url, key)
    out_dir = Path(args.out_dir)
    for table in args.tables:
        if args.count_only:
            print(f"[count] {table}: {row_count(client, table)}")
        else:
            export_table(client, table, out_dir, args.page_size)


if __name__ == "__main__":
    main()
