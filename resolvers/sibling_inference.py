"""Zero-request coverage: give a device the IFU its resolved siblings already carry.

Makers publish one IFU per product line and GUDID lists every size, power and pack of that
line as its own device. Once the portal has answered for the line, most of the remaining
devices in it are the SAME document under an identifier the portal never indexed (a diopter
suffix, an unhyphenated model, a discontinued pack size). Measured 2026-09-02 across the
top-20 makers: ~120k unresolved devices share a brand with a resolved sibling; ~50k of them
pass the strict rules below. That is a year of polite scraping for zero requests.

The rules are what make this an inference and not a guess:

  * Same GUDID company AND same brandName. Company patterns are too loose ("%medtronic%"
    spans Sofamor Danek and MiniMed); the exact company string is the maker's own grouping.
  * The brand must name a product line, not the company (an "AESCULAP" brand on a Kocher
    clamp says nothing about which IFU it has).
  * Only siblings the portal answered for BY IDENTIFIER OR BY NAME count as sources:
    exact_catalog (REF equality) and brand_match (the title names this very brand).
    model_portal_match is out: e-ifu.com model search collides across product lines (a
    P.F.C. SIGMA knee carrying an ECHELON stapler IFU, an Ender nail carrying an LCP plate).
    Nothing inferred feeds a further inference.
  * The source identifier must be distinctive (>= MIN_PORTAL_TERM_LEN alphanumerics): a
    4-digit Stryker REF "9539" matched a Reveal clinician manual and would have carried it to
    every MEDPOR implant.
  * Sources whose title is a placeholder or a generic processing/safety leaflet are skipped:
    they are not the device's IFU even when the portal filed them under its REF (Stryker's
    tenant holds 6,312 "Placeholder Document" rows).
  * Brand tier: >= MIN_BRAND_SIBLINGS resolved siblings, at least MIN_BRAND_COVERAGE of the
    brand's devices, and ALL of them hold ONE document. A brand whose siblings disagree
    (LigaSure spans generations) or is barely resolved (3 Toric models out of 1,157 Alcon
    models would have made "AcrySof" unanimously Toric) is left alone.
  * Submission tier: the device shares a 510(k)/PMA number AND brand with >= 1 resolved
    sibling, and those siblings hold one document. Same regulatory product, same labeling.
  * A device that already has a `found` row is untouched. A device that fails both tiers is
    left pending, never marked not_found — the gap is ours, not evidence.

Rows are written as status=found, match_confidence=sibling_inferred, carrying the source
row's family/document/file name so serving and re-minting work unchanged. Ranked below every
portal-asserted tier in mvp_lookup.STATUS_PRIORITY.

Usage:
    python -m resolvers.sibling_inference --dry-run           # counts only
    python -m resolvers.sibling_inference --apply
    python -m resolvers.sibling_inference --apply --target zimmer_biomet
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import company_targets
from resolvers.eifu_resolver import MIN_PORTAL_TERM_LEN, SQLITE_PATH, ensure_ifu_links_table
from resolvers.stryker_resolver import ensure_source_file_name_column

MATCH_CONFIDENCE = "sibling_inferred"
SOURCE_CONFIDENCES = ("exact_catalog", "brand_match")
# Titles that are filed under a REF but are not that device's IFU (spot-checked 2026-09-02):
# placeholders, processing/cleaning/care leaflets, MRI cards, S+N CSB_/STG_ bulletins and
# surgical techniques (a REFLECTION shell inherited an R3 liner technique guide).
NON_IFU_TITLE_RE = re.compile(
    r"placeholder|instructions?\s+for\s+(the\s+)?(processing|cleaning|care)|"
    r"cleaning[\s,\-/&]*(and\s+)?steriliz|cleaning\s+instruction|care\s+and\s+steriliz|"
    r"reprocessing|mri\s+safety|"
    r"legacy\s+ifu|safety\s+card|surgical\s+tech|^(CSB|STG)_", re.I)
# An instrument/trial document handed to an implant is the one error the tiers cannot see:
# the brand is shared, the submission is shared, only the description tells them apart.
INSTRUMENT_TITLE_RE = re.compile(r"instrument|trial|sizer|provisional", re.I)
INSTRUMENT_DESC_RE = re.compile(
    r"instrument|trial|sizer|provisional|drill|reamer|handle|guide|cutter|driver|inserter|"
    r"impactor|template|tray|case|broach|rasp|gauge|holder|wrench|screwdriver|awl|tap\b", re.I)
MIN_BRAND_SIBLINGS = 3
MIN_BRAND_COVERAGE = 0.5      # resolved siblings / all devices in the (company, brand) group
STATE_PATH = Path(__file__).resolve().parent.parent / "runs" / "sibling_inference_state.json"
_NO_BRAND = {"", "na", "n/a", "none", "null", "unknown", "-"}


def _brand_names_company(brand: str, company: str) -> bool:
    """True when the brand is just the maker's name (AESCULAP / Aesculap AG, Medtronic / MEDTRONIC, INC.)."""
    b = re.sub(r"[^a-z0-9]", "", brand.lower())
    c = re.sub(r"[^a-z0-9]", "", company.lower())
    return len(b) >= 4 and (b in c or c.startswith(b))


def is_source_ident(ident: str | None) -> bool:
    """Distinctive enough that a portal hit on it is not a chance collision."""
    return len(re.sub(r"[^a-z0-9]", "", (ident or "").lower())) >= MIN_PORTAL_TERM_LEN


def is_ifu_title(title: str | None) -> bool:
    return not NON_IFU_TITLE_RE.search(title or "")


def _targets(only: str | None) -> list[dict]:
    targets = [t for t in company_targets.TOP_DEVICE_TARGETS if not only or t["key"] == only]
    if only and not targets:
        raise SystemExit(f"unknown target {only!r}")
    return targets


def _prepare(conn: sqlite3.Connection) -> None:
    conn.execute("pragma temp_store=memory")
    # device -> ONE source row (lowest id: stable across runs), portal-asserted only. Keyed by
    # the device the portal answered FOR, not by identifier: REF 940013 is a DePuy tibial
    # insert and a Stryker clavicle trial, and only one of them owns the Variax IFU.
    placeholders = ",".join("?" * len(SOURCE_CONFIDENCES))
    conn.create_function("is_source_ident", 1, is_source_ident, deterministic=True)
    conn.create_function("is_ifu_title", 1, is_ifu_title, deterministic=True)
    conn.execute(f"""
        create temp table src as
        select device_rowid rw, min(id) link_id
        from ifu_links
        where status='found' and document_url is not null and document_url != ''
          and device_rowid is not null
          and match_confidence in ({placeholders})
          and is_source_ident(catalog_number) and is_ifu_title(document_title)
        group by device_rowid""", SOURCE_CONFIDENCES)
    conn.execute("create index temp.src_i on src(rw)")
    conn.execute("""
        create temp table anyfound as
        select distinct catalog_number ident from ifu_links
        where status='found' and document_url is not null and document_url != ''""")
    conn.execute("create index temp.anyfound_i on anyfound(ident)")
    conn.execute("create index if not exists premarket_submissions_di on premarket_submissions(primary_di)")


def _maker_table(conn: sqlite3.Connection, target: dict) -> None:
    pats = target["company_patterns"]
    where = " or ".join(["lower(d.company_name) like ?"] * len(pats))
    conn.execute("drop table if exists temp.m")
    conn.execute(f"""
        create temp table m as
        select d.rowid rw, d.company_name company, trim(d.brand_name) brand,
               nullif(trim(d.catalog_number),'') cat, nullif(trim(d.model_number),'') mod,
               json_extract(d.raw_json,'$.PrimaryDI') di, d.device_description descr,
               (select link_id from src where src.rw = d.rowid) src_link,
               (coalesce(d.catalog_number, '') in (select ident from anyfound)
                or coalesce(d.model_number, '') in (select ident from anyfound)) has_found
        from devices d where ({where})""", pats)
    conn.execute("create index temp.m_cb on m(company, brand)")
    # The document each source link holds — inference keys on document identity.
    conn.execute("drop table if exists temp.mdoc")
    conn.execute("""
        create temp table mdoc as
        select m.rw, m.company, m.brand, m.src_link, l.document_url doc
        from m join ifu_links l on l.id = m.src_link""")
    conn.execute("create index temp.mdoc_cb on mdoc(company, brand)")


def _brand_tier(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """(device rowid, source link id) for devices whose brand is unanimous.

    Unanimity alone is not enough: with 3 of 1,157 Alcon models resolved — all Toric — the
    "AcrySof" brand was unanimous for a Toric IFU it would have handed every other AcrySof
    lens. The resolved siblings must also be a real share of the brand (MIN_BRAND_COVERAGE),
    so the portal has actually spoken for the line, not for one corner of it."""
    rows = conn.execute("""
        with tot as (
            select company, brand, count(*) n_all from m where brand is not null
            group by company, brand),
        grp as (
            select company, brand, count(*) n, count(distinct doc) nd, min(src_link) link
            from mdoc where brand is not null group by company, brand
            having n >= ? and nd = 1)
        select m.rw, grp.link, m.brand, m.company
        from m join grp on grp.company = m.company and grp.brand = m.brand
        join tot on tot.company = m.company and tot.brand = m.brand
        where m.has_found = 0 and (m.cat is not null or m.mod is not null)
          and grp.n * 1.0 / tot.n_all >= ?""",
        (MIN_BRAND_SIBLINGS, MIN_BRAND_COVERAGE)).fetchall()
    return [(rw, link) for rw, link, brand, company in rows
            if brand.lower() not in _NO_BRAND and not _brand_names_company(brand, company)]


def _submission_tier(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """(device rowid, source link id) for devices sharing brand + FDA submission with a source."""
    rows = conn.execute("""
        with ms as (
            select m.rw, m.company, m.brand, m.has_found, m.cat, m.mod, s.submission_number sub,
                   md.doc, m.src_link
            from m join premarket_submissions s on s.primary_di = m.di
            left join mdoc md on md.rw = m.rw),
        grp as (
            select company, brand, sub, count(distinct doc) nd, min(src_link) link
            from ms where doc is not null and brand is not null
            group by company, brand, sub having nd = 1)
        select distinct ms.rw, grp.link, ms.brand, ms.company
        from ms join grp on grp.company = ms.company and grp.brand = ms.brand and grp.sub = ms.sub
        where ms.has_found = 0 and (ms.cat is not null or ms.mod is not null)""").fetchall()
    return [(rw, link) for rw, link, brand, company in rows
            if brand.lower() not in _NO_BRAND and not _brand_names_company(brand, company)]


def instrument_doc_for_implant(title: str | None, description: str | None) -> bool:
    """True when the document is about instruments and the device visibly is not one."""
    if not INSTRUMENT_TITLE_RE.search(title or ""):
        return False
    return bool(description) and not INSTRUMENT_DESC_RE.search(description)


def _drop_instrument_mismatches(conn: sqlite3.Connection, pairs: dict[int, int]) -> dict[int, int]:
    if not pairs:
        return pairs
    titles = {link: title for link, title in conn.execute(
        f"select id, document_title from ifu_links where id in ({','.join(map(str, set(pairs.values())))})")}
    kept: dict[int, int] = {}
    for rw, link in pairs.items():
        if INSTRUMENT_TITLE_RE.search(titles.get(link) or ""):
            descr = conn.execute("select descr from m where rw = ?", (rw,)).fetchone()
            if instrument_doc_for_implant(titles.get(link), descr[0] if descr else None):
                continue
        kept[rw] = link
    return kept


def _apply(conn: sqlite3.Connection, pairs: dict[int, int]) -> int:
    """Write one found row per device, copied from its source link."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    written = 0
    for rw, link in pairs.items():
        dev = conn.execute("select cat, mod, di from m where rw = ?", (rw,)).fetchone()
        src = conn.execute("""
            select manufacturer_family, source_url, document_url, document_title, language,
                   revision, source_file_name from ifu_links where id = ?""", (link,)).fetchone()
        if not dev or not src:
            continue
        ident = dev[0] or dev[1]           # what the client sends: catalog, else model
        cur = conn.execute("""
            insert or ignore into ifu_links (
                device_rowid, primary_di, catalog_number, manufacturer_family, source_url,
                document_url, document_title, language, revision, match_confidence,
                retrieved_at, status, first_seen_at, last_checked_at, last_success_at,
                source_file_name)
            values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rw, dev[2], ident, src[0], src[1], src[2], src[3], src[4], src[5],
             MATCH_CONFIDENCE, now, "found", now, now, now, src[6]))
        if cur.rowcount:
            written += 1
            # A document now exists; the outcome-only row (not_found) would contradict it.
            conn.execute("delete from ifu_links where catalog_number = ? and document_url is null", (ident,))
    return written


def run(apply: bool, only: str | None = None, db_path: str | Path = SQLITE_PATH) -> dict:
    ensure_ifu_links_table(db_path)
    ensure_source_file_name_column(db_path)
    conn = sqlite3.connect(db_path, timeout=60.0)
    report: dict = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "applied": apply, "targets": {}}
    try:
        t0 = time.time()
        _prepare(conn)
        for target in _targets(only):
            _maker_table(conn, target)
            sub = dict(_submission_tier(conn))
            brand = dict(_brand_tier(conn))
            pairs = _drop_instrument_mismatches(conn, {**brand, **sub})  # submission tier wins: tighter key
            entry = {"submission_tier": len(sub), "brand_tier": len(brand), "eligible": len(pairs)}
            if apply:
                with conn:
                    entry["written"] = _apply(conn, pairs)
            report["targets"][target["key"]] = entry
            print(f'{target["key"]:20} submission {len(sub):6,}  brand {len(brand):6,}  '
                  f'eligible {len(pairs):6,}' + (f'  written {entry.get("written", 0):6,}' if apply else ""),
                  flush=True)
        report["seconds"] = round(time.time() - t0)
    finally:
        conn.close()
    if only is None:
        STATE_PATH.parent.mkdir(exist_ok=True)
        STATE_PATH.write_text(json.dumps(report, indent=1))
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Attach resolved siblings' IFUs to unresolved devices.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--target", help="one company_targets key")
    args = ap.parse_args()
    report = run(apply=args.apply, only=args.target)
    total = sum(t["eligible"] for t in report["targets"].values())
    written = sum(t.get("written", 0) for t in report["targets"].values())
    print(f"done in {report.get('seconds')}s: eligible {total:,}" + (f", written {written:,}" if args.apply else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
