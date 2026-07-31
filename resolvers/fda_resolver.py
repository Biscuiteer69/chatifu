"""Resolve FDA premarket documents (510(k) summaries / PMA SSEDs) as an IFU FALLBACK.

Every device legally marketed in the US that is not class-I-exempt has a public FDA submission,
and AccessGUDID ships the mapping — `premarketSubmissions.txt` in the delimited full release links
PrimaryDI -> K/P number. So for a large slice of the catalogue we can reach a regulated document
with no manufacturer portal, no WAF, no token and no browser: just a public FDA URL.

WHAT THIS IS NOT: a 510(k) summary is not an Instructions For Use. It carries Indications for Use,
Intended Use, device description and predicate — but no step-by-step instructions and no full
warnings. It is stored under its own manufacturer_family and status so the answerer can label it
honestly, and so it NEVER gets served as if it were the manufacturer's IFU.

Because of that, the status written here (`fda_summary`) is deliberately NOT one of the terminal
statuses the per-maker resolvers exclude on. A device covered by FDA is still attempted by its
manufacturer's resolver later, and the real IFU supersedes this when it arrives. FDA is a floor,
not a ceiling.

URL patterns, established empirically (see tests at the bottom):
    K, year <= 1999   https://www.accessdata.fda.gov/cdrh_docs/pdf/K990993.pdf
    K, 2000-2009      .../cdrh_docs/pdf3/K033594.pdf        (folder = year - 2000)
    K, 2010+          .../cdrh_docs/pdf24/K240049.pdf
    P (PMA SSED)      .../cdrh_docs/pdf24/P240029B.pdf      (note the B suffix)
Pre-1996 submissions predate FDA's digitisation — 4,401 of 36,406 K numbers (12%) have no PDF.

One submission covers many devices (36k documents span ~575k catalog numbers), so documents are
fetched ONCE into `fda_documents` and then linked to every device that shares them. That is the
difference between 36k requests and 575k.

Usage:
    python -m resolvers.fda_resolver --load-submissions        # one-time index build
    python -m resolvers.fda_resolver --batch 200               # resolve a batch
    python -m resolvers.fda_resolver --submission K172521      # single lookup
"""
from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from resolvers.eifu_resolver import SQLITE_PATH

VAULT = Path(__file__).resolve().parent.parent
GUDID_ZIP = VAULT / "fda" / "AccessGUDID_Delimited_Full_Release_20260302.zip"
SUBMISSIONS_MEMBER = "premarketSubmissions.txt"
BASE = "https://www.accessdata.fda.gov/cdrh_docs"
MANUFACTURER_FAMILY = "fda_510k"
STATUS = "fda_summary"          # deliberately NOT terminal — see module docstring
DELAY_SEC = 1.0                 # public .gov bulk endpoint; stay polite regardless
UA = "Mozilla/5.0 (compatible; ChatIFU/1.0; +https://chatifu.com)"


def _doc_url(submission: str) -> str | None:
    """Public AccessGUDID document URL for a submission number, or None if unreachable."""
    sub = (submission or "").strip().upper()
    if len(sub) < 7 or not sub[1:3].isdigit():
        return None
    yy = int(sub[1:3])
    year = 1900 + yy if yy >= 76 else 2000 + yy
    if year < 1996:
        return None                      # predates FDA's PDF digitisation
    folder = "pdf" if year <= 1999 else f"pdf{year - 2000}"
    # PMA/HDE publish the SSED under a B suffix; 510(k) summaries use the bare number.
    name = f"{sub}B" if sub[0] in ("P", "H") else sub
    return f"{BASE}/{folder}/{name}.pdf"


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""create table if not exists premarket_submissions(
        primary_di text not null, submission_number text not null)""")
    conn.execute("create index if not exists idx_psub_di on premarket_submissions(primary_di)")
    conn.execute("create index if not exists idx_psub_k on premarket_submissions(submission_number)")
    # devices.raw_json holds PrimaryDI as JSON; extracting it per query is a full scan of a 7.7GB
    # table, so the DI -> (rowid, identifier) map is materialised and indexed once.
    conn.execute("""create table if not exists device_di as
        select rowid as rw, json_extract(raw_json,'$.PrimaryDI') as di,
               coalesce(nullif(trim(catalog_number),''), trim(model_number)) as ident
        from devices""")
    conn.execute("create index if not exists idx_ddi_di on device_di(di)")
    conn.execute("""create table if not exists fda_documents(
        submission_number text primary key,
        document_url text, status text, bytes integer, checked_at text)""")
    conn.commit()


def load_submissions(conn: sqlite3.Connection, zip_path: Path = GUDID_ZIP) -> int:
    """Stream PrimaryDI -> submission mappings straight out of the GUDID zip."""
    ensure_tables(conn)
    if conn.execute("select count(*) from premarket_submissions").fetchone()[0]:
        return 0                          # already indexed; --reload-submissions to rebuild
    with zipfile.ZipFile(zip_path) as z, z.open(SUBMISSIONS_MEMBER) as fh:
        reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"), delimiter="|")
        next(reader, None)
        conn.executemany("insert into premarket_submissions values(?,?)",
                         ((row[0], row[1]) for row in reader if len(row) >= 2))
    conn.commit()
    return conn.execute("select count(*) from premarket_submissions").fetchone()[0]


def fetch_document(conn: sqlite3.Connection, submission: str) -> dict:
    """Fetch (once) and cache one FDA document's availability. Idempotent."""
    row = conn.execute("select document_url, status, bytes from fda_documents "
                       "where submission_number=?", (submission,)).fetchone()
    if row:
        return {"submission": submission, "document_url": row[0], "status": row[1], "cached": True}

    url = _doc_url(submission)
    now = datetime.now(timezone.utc).isoformat()
    if not url:
        conn.execute("insert or replace into fda_documents values(?,?,?,?,?)",
                     (submission, None, "no_url", 0, now))
        conn.commit()
        return {"submission": submission, "document_url": None, "status": "no_url"}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            ok = resp.status == 200 and "pdf" in ctype
            status = "found" if ok else "not_pdf"
    except urllib.error.HTTPError as exc:
        status, size = ("not_found" if exc.code == 404 else f"http_{exc.code}"), 0
    except Exception as exc:  # noqa: BLE001 - stay fail-soft
        status, size = f"error:{type(exc).__name__}", 0

    conn.execute("insert or replace into fda_documents values(?,?,?,?,?)",
                 (submission, url if status == "found" else None, status, size, now))
    conn.commit()
    return {"submission": submission, "document_url": url if status == "found" else None,
            "status": status, "bytes": size}


def _pending_submissions(conn: sqlite3.Connection, limit: int) -> list[tuple[str, int]]:
    """Submissions covering still-unlinked devices, most-devices-first so each fetch pays off."""
    return conn.execute("""
        select s.submission_number, count(distinct d.rw) n
        from device_di d
        join premarket_submissions s on s.primary_di = d.di
        where not exists (select 1 from fda_documents f where f.submission_number = s.submission_number)
        group by s.submission_number
        order by n desc
        limit ?""", (limit,)).fetchall()


def link_devices(conn: sqlite3.Connection, submission: str, url: str) -> int:
    """Write one ifu_links row per device sharing this submission."""
    rows = conn.execute("""
        select d.rw, d.di, d.ident
        from device_di d
        join premarket_submissions s on s.primary_di = d.di
        where s.submission_number = ?""", (submission,)).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    title = f"FDA {'PMA summary' if submission[0] in 'PH' else '510(k) summary'} {submission}"
    n = 0
    for rowid, di, ident in rows:
        if not ident:
            continue
        conn.execute("""insert or ignore into ifu_links
            (device_rowid, primary_di, catalog_number, manufacturer_family, source_url,
             document_url, document_title, language, match_confidence, retrieved_at, status,
             first_seen_at, last_checked_at, last_success_at, source_file_name)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rowid, di, ident, MANUFACTURER_FAMILY, url, url, title, "en",
             "fda_submission", now, STATUS, now, now, now, submission))
        n += 1
    conn.commit()
    return n


def resolve_batch(limit: int, db_path: str | Path = SQLITE_PATH) -> dict:
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        ensure_tables(conn)
        pending = _pending_submissions(conn, limit)
        print(f"Resolving {len(pending)} FDA submissions")
        found = linked = 0
        for i, (sub, devices) in enumerate(pending, 1):
            res = fetch_document(conn, sub)
            if res["status"] == "found":
                found += 1
                linked += link_devices(conn, sub, res["document_url"])
            print(f"[{i}/{len(pending)}] {sub}: {res['status']} ({devices} devices)")
            if not res.get("cached"):
                time.sleep(DELAY_SEC)
        return {"submissions": len(pending), "found": found, "devices_linked": linked}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="FDA premarket document resolver (IFU fallback).")
    ap.add_argument("--load-submissions", action="store_true", help="Build the DI->submission index.")
    ap.add_argument("--batch", type=int, help="Resolve N submissions (most-devices-first).")
    ap.add_argument("--submission", help="Look up one submission number.")
    ap.add_argument("--db", default=str(SQLITE_PATH))
    args = ap.parse_args()

    if args.load_submissions:
        conn = sqlite3.connect(args.db, timeout=120.0)
        try:
            n = load_submissions(conn)
            print(f"premarket_submissions rows: {n or conn.execute('select count(*) from premarket_submissions').fetchone()[0]:,}")
        finally:
            conn.close()
        return 0
    if args.submission:
        print(_doc_url(args.submission))
        return 0
    if args.batch:
        print(resolve_batch(args.batch, args.db))
        return 0
    ap.error("one of --load-submissions / --batch / --submission is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
