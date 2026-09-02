"""Goal monitor for the top-20 IFU scrape — runs every 6h, alerts on Telegram only when
something is actually wrong.

The goal is expressed as ONE number: how many distinct device identifiers across the top-20
manufacturers still have no terminal ifu_links row. Every check re-measures it, compares to the
previous check, and recomputes the ETA from the OBSERVED rate rather than any original estimate.

Alerts fire on the things that actually stall this goal:

  * fleet down            — the supervisor process is gone
  * stalled               — 6h elapsed, backlog not dry, zero identifiers resolved
  * hot loop              — a target ran many batches but resolved nothing. This is the failure
                            that ran undetected for days: GUDID ships some catalog numbers with
                            trailing whitespace, the pending query compared untrimmed values, and
                            three targets re-hit their vendors every ~25s forever making no
                            progress. Batches-without-writes catches that whole class.
  * zero yield            — a target writes rows but finds NOTHING, i.e. it is rejecting valid
                            matches rather than facing an empty portal. Baxter wrote 577
                            not_found and zero found this way. Distinct from a hot loop, which
                            writes nothing at all, and more dangerous: every row is a permanent
                            false negative that no later pass revisits.
  * WAF blocks            — rate-limit flags in the window; the cadence needs backing off
  * target erroring       — repeated batch failures on one target

Quiet on success by design: a check that finds nothing wrong logs and says nothing, so an alert
always means something needs a decision.

Cron: scrape_goal_monitor_cron.sh every 6h.
Run manually:  .venv/bin/python scrape_goal_monitor.py [--force-alert] [--quiet-hours N]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import company_targets as CT
from healthcheck import telegram_alert

VAULT = Path(__file__).resolve().parent
DB = VAULT / "chatifu.sqlite3"
FLEET_LOG = VAULT / "logs" / "scraper_fleet.log"
STATE = VAULT / "runs" / "scrape_goal_state.json"

# Distributors, not device makers: huge commodity catalogues, worked last by decision.
# Tracked separately so their 614k doesn't swamp the number we actually steer by.
DISTRIBUTORS = {"cardinal_health", "medline"}
MODEL_KEYED = {"medtronic"}          # Medtronic publishes by model, not catalog
WINDOW_HOURS = 6
HOT_LOOP_BATCHES = 20                # batches in-window with zero writes = spinning
ZERO_YIELD_MIN_ROWS = 25             # rows in-window with zero found = rejecting valid matches
WAF_SUSTAINED = 6                    # blocks in-window while still producing = cadence too hot

# The hot-loop check compares a FLEET TARGET's batch count against ifu_links writes, but a target
# key and the manufacturer_family it writes are not always the same string. Where they differ the
# check saw zero writes and reported a hot loop for targets that were in fact the most productive
# in the fleet — FDA had covered 857k catalogs when it was first accused of resolving nothing.
# Map the exceptions; None means "cannot attribute writes to this target, skip the check".
TARGET_FAMILY: dict[str, str | None] = {
    "fda": "fda_510k",
    "jnj": "johnson_and_johnson",
    "eifu_sweep": None,   # sweeps many makers, writing each one's own family
}


def _remaining(conn: sqlite3.Connection, tier: str = "any") -> dict[str, int]:
    """Distinct identifiers not yet covered at `tier`, per company key.

    Two tiers, because conflating them would let us declare victory on documents that are not
    IFUs:
      "any"    — anything at all, including an FDA 510(k) summary. This is what the product can
                 currently answer from.
      "maker"  — a manufacturer-sourced result only. An FDA summary carries Indications for Use
                 but no instructions, so this is the real IFU gap.
      "servable" — a `found` row, the only thing the client can actually be served. The other
                 two tiers count a not_found row as "done": that is the scrapers' backlog, not
                 the product's gap, and on 2026-09-02 it made 65k look like the whole problem
                 when the top-20 servable gap was ~10x that.
    """
    if tier == "servable":
        statuses = "'found'"
    else:
        statuses = "'found','candidate_broad','not_found'"
    extra = ",'fda_summary'" if tier == "any" else ""
    conn.execute("drop table if exists temp.done")
    conn.execute(
        f"create temp table done as select distinct catalog_number c from ifu_links "
        f"where status in ({statuses}{extra})"
    )
    conn.execute("create index temp.tmp_done on done(c)")
    out: dict[str, int] = {}
    for t in CT.TOP_DEVICE_TARGETS:
        if t["rank"] > 20:
            continue
        col = "model_number" if t["key"] in MODEL_KEYED else "catalog_number"
        pats = t["company_patterns"]
        where = " or ".join([f"lower(d.company_name) like ?"] * len(pats))
        out[t["key"]] = conn.execute(
            f"select count(distinct d.{col}) from devices d "
            f"where d.{col} is not null and trim(d.{col}) != '' and ({where}) "
            f"and d.{col} not in (select c from done)",
            pats,
        ).fetchone()[0]
    return out


def _coverage_quality(conn: sqlite3.Connection) -> dict[str, int]:
    """Split what we DO cover by how trustworthy the document is.

    "Covered" hides an enormous quality range, and the split is not close: a verified
    manufacturer IFU, an unverified portal hit, and an FDA 510(k) summary are three different
    products to a clinician. The summary in particular carries indications and intended use but
    NO instructions, warnings or contraindications — so a question about contraindications
    cannot be answered from it, however confident the passage looks.

    Reported every run because the headline coverage number is otherwise flattering: most
    covered catalogs are FDA-summary-only.
    """
    conn.execute("drop table if exists temp.q_ver")
    conn.execute("create temp table q_ver as select distinct catalog_number c from ifu_links "
                 "where status='found'")
    conn.execute("create index temp.qv on q_ver(c)")
    conn.execute("drop table if exists temp.q_broad")
    conn.execute("create temp table q_broad as select distinct catalog_number c from ifu_links "
                 "where status='candidate_broad'")
    conn.execute("create index temp.qb on q_broad(c)")
    verified = conn.execute("select count(*) from q_ver").fetchone()[0]
    unverified = conn.execute(
        "select count(*) from q_broad where c not in (select c from q_ver)").fetchone()[0]
    fda_only = conn.execute(
        "select count(distinct catalog_number) from ifu_links where status='fda_summary' "
        "and catalog_number not in (select c from q_ver) "
        "and catalog_number not in (select c from q_broad)").fetchone()[0]
    return {"verified": verified, "unverified": unverified, "fda_only": fda_only}


def _window_log(hours: int) -> list[str]:
    if not FLEET_LOG.exists():
        return []
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    lines: list[str] = []
    # Only the tail can be in-window; the log grows to hundreds of MB over months.
    tail = subprocess.run(["tail", "-n", "200000", str(FLEET_LOG)],
                          capture_output=True, text=True).stdout.splitlines()
    for line in tail:
        m = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\]", line)
        if m:
            try:
                if datetime.fromisoformat(m.group(1)) >= cutoff:
                    lines.append(line)
            except ValueError:
                continue
    return lines


def _writes_by_family(conn: sqlite3.Connection, hours: int) -> dict[str, int]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return dict(conn.execute(
        "select manufacturer_family, count(distinct catalog_number) from ifu_links "
        "where last_checked_at >= ? group by 1", (since,)).fetchall())


def _issues(conn: sqlite3.Connection, remaining: dict[str, int], prev: dict | None) -> list[str]:
    issues: list[str] = []
    lines = _window_log(WINDOW_HOURS)
    writes = _writes_by_family(conn, WINDOW_HOURS)

    alive = subprocess.run(["pgrep", "-f", "scraper_fleet.py"], capture_output=True).returncode == 0
    if not alive:
        issues.append("FLEET DOWN — scraper_fleet.py is not running")

    # WAF blocks are only worth waking someone for when they STICK. The fleet backs off and a
    # rate-limit self-heals, so an occasional flag on a target that is still completing batches
    # is noise — and noise here is expensive, because the correct response to a real block is to
    # slow down, and being trained to ignore the alert is how you miss the one that matters.
    # Escalate only if a blocked target also stopped producing, or if blocks are sustained.
    waf = [ln for ln in lines if "WAF/rate-limit" in ln]
    if waf:
        hit = sorted({m.group(1) for ln in waf if (m := re.search(r"\[(\w+)\] batch flagged", ln))})
        stuck = [t for t in hit if writes.get(TARGET_FAMILY.get(t, t) or t, 0) == 0]
        if stuck:
            issues.append(f"WAF BLOCKED AND STALLED: {', '.join(stuck)} — {len(waf)} blocks in "
                          f"{WINDOW_HOURS}h and no rows written; back off the cadence")
        elif len(waf) >= WAF_SUSTAINED:
            issues.append(f"WAF blocks sustained: {len(waf)} in {WINDOW_HOURS}h "
                          f"({', '.join(hit)}) — still producing, but the cadence is too hot")

    # Per-target: many batches, nothing resolved => spinning on unmarkable devices.
    batches: dict[str, int] = {}
    errors: dict[str, int] = {}
    for ln in lines:
        if (m := re.search(r"\[(\w+)\] batch ok", ln)):
            batches[m.group(1)] = batches.get(m.group(1), 0) + 1
        elif (m := re.search(r"\[(\w+)\] batch (?:error|timed out)", ln)):
            errors[m.group(1)] = errors.get(m.group(1), 0) + 1
    for target, n in batches.items():
        family = TARGET_FAMILY.get(target, target)
        if family is None:
            continue
        if n >= HOT_LOOP_BATCHES and writes.get(family, 0) == 0:
            issues.append(f"HOT LOOP: {target} ran {n} batches in {WINDOW_HOURS}h and resolved nothing")
    for target, n in errors.items():
        if n >= 5:
            issues.append(f"{target}: {n} failed batches in {WINDOW_HOURS}h")

    # Zero-yield: a resolver that writes rows but finds NOTHING. The hot-loop check above only
    # catches targets writing nothing at all, so this whole class slipped past it — Baxter wrote
    # 577 not_found and zero found because item_matches_catalog() rejected every product (it
    # publishes no REF attribute), and each of those rows is a permanent false negative that
    # nothing revisits. A confident not_found is worse than no coverage.
    since = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()
    for family, found, total in conn.execute(
            "select manufacturer_family, "
            "sum(case when status='found' then 1 else 0 end), count(*) "
            "from ifu_links where last_checked_at >= ? group by 1", (since,)):
        if total >= ZERO_YIELD_MIN_ROWS and not found and family != "fda_510k":
            issues.append(f"ZERO YIELD: {family} wrote {total} rows in {WINDOW_HOURS}h, none found "
                          f"— likely rejecting valid matches, not an empty portal")

    # Whole-goal stall: work outstanding, nothing moved.
    active = sum(v for k, v in remaining.items() if k not in DISTRIBUTORS)
    if prev and active > 0:
        moved = prev.get("active", 0) - active
        if not any("FLEET DOWN" in i for i in issues):
            if moved < 0:
                # The count going UP is a different event from stalling, and saying "0 resolved"
                # about it is simply false. It happens when false-negative rows are deleted so
                # their devices become uncoverable again — which is a deliberate repair, not a
                # regression. Name it so nobody hunts a fault that is not there.
                issues.append(f"COUNT WENT UP by {-moved:,} to {active:,} — expected only after "
                              f"rows were deleted on purpose; otherwise investigate")
            elif moved == 0:
                issues.append(f"STALLED — {active:,} identifiers outstanding, 0 resolved since last check")
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description="Top-20 IFU scrape goal monitor.")
    ap.add_argument("--force-alert", action="store_true", help="Send the status even with no issues.")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    try:
        remaining = _remaining(conn, tier="any")
        maker_only = _remaining(conn, tier="maker")
        servable = _remaining(conn, tier="servable")
        active = sum(v for k, v in remaining.items() if k not in DISTRIBUTORS)
        distrib = sum(v for k, v in remaining.items() if k in DISTRIBUTORS)
        maker_gap = sum(v for k, v in maker_only.items() if k not in DISTRIBUTORS)
        servable_gap = sum(v for k, v in servable.items() if k not in DISTRIBUTORS)
        quality = _coverage_quality(conn)
        prev = json.loads(STATE.read_text()) if STATE.exists() else None
        issues = _issues(conn, remaining, prev)
    finally:
        conn.close()

    now = datetime.now(timezone.utc)

    def _rate(key: str, current: int) -> str:
        if not prev or key not in prev:
            return ""
        hours = max(0.1, (now - datetime.fromisoformat(prev["at"])).total_seconds() / 3600)
        moved = prev[key] - current
        per_day = moved / hours * 24
        eta = f"{current / per_day:.0f}d" if per_day > 0 else "n/a (no progress)"
        return f"{moved:+,} in {hours:.1f}h  ({per_day:,.0f}/day, ETA {eta})"

    servable_rate = _rate("servable_gap", servable_gap)
    pending_rate = _rate("active", active)

    def _top(counts: dict[str, int]) -> str:
        top = sorted(((v, k) for k, v in counts.items() if v > 0 and k not in DISTRIBUTORS),
                     reverse=True)[:5]
        return ", ".join(f"{k} {v:,}" for v, k in top)

    # The servable gap is the headline: it is the number the client experiences. The pending
    # count is the fleet's backlog and stays as a second line so a stall is still visible.
    body = "\n".join([
        f"ChatIFU scrape goal — {now.strftime('%Y-%m-%d %H:%M')}Z",
        f"no SERVABLE IFU:         {servable_gap:,}   (device makers, client-style: status=found)",
        f"  {servable_rate}" if servable_rate else "",
        f"  top: {_top(servable)}",
        f"pending (never tried):   {active:,}   (fleet backlog)",
        f"  {pending_rate}" if pending_rate else "",
        f"  top: {_top(remaining)}",
        f"no MAKER IFU:            {maker_gap:,}   (FDA summary only counts here)",
        f"distributors (last):     {distrib:,}",
        f"COVERED: verified IFU {quality['verified']:,} | unverified "
        f"{quality['unverified']:,} | FDA-summary-only {quality['fda_only']:,}",
    ]).strip()
    body = "\n".join(line for line in body.splitlines() if line.strip())

    print(body)
    if issues:
        print("ISSUES:\n  " + "\n  ".join(issues))
        telegram_alert("⚠️ " + body + "\n\nISSUES:\n- " + "\n- ".join(issues))
    elif args.force_alert:
        telegram_alert("✅ " + body)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "at": now.isoformat(), "active": active, "distributors": distrib,
        "maker_gap": maker_gap, "servable_gap": servable_gap, "quality": quality,
        "remaining": remaining, "servable": servable, "issues": issues,
    }, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
