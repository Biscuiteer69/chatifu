"""Flag company_targets.py patterns that match unrelated manufacturers.

`company_patterns` are SQL LIKE substrings, and an unanchored one silently swallows other
companies. This is not cosmetic — the patterns feed BOTH the goal metric (so the number we
steer by inflates) and resolver targeting (so devices get sent at another maker's portal,
where a catalog-number collision can attach the wrong IFU to a device).

Found this way so far:
    %ge medical%    -> Highridge Medical (20,942), Emerge Medical, Advantage Medical, NXStage
    %ge healthcare% -> CHANGE Healthcare
    %bd%            -> Gembdi Dental-Products, ONE LAMBDA
    %bard%          -> LOMBARD MEDICAL
    %biomet%        -> Precision / Imaging / Bruin Biometrics
    %alcon%         -> ALCONOX INC (whose catalog 1104 collides with a real Alcon product)
    %synthes%       -> Synthesis Health Intelligence

The rule: a match is suspicious when the pattern's text appears INSIDE a word rather than at
a word boundary. "Bard Access" is fine; "LomBARD" is not. Judgement is still required —
legitimate subsidiaries (Covidien under Medtronic, Ethicon under J&J) are word-boundary
matches and pass — so this reports rather than fails.

Run after any change to company_patterns:
    .venv/bin/python audit_company_patterns.py [--all]
"""
from __future__ import annotations

import argparse
import re
import sqlite3

import company_targets as CT
from resolvers.eifu_resolver import SQLITE_PATH


def suspicious(company: str, pattern: str) -> bool:
    """True when the pattern's core text sits inside a word of the company name."""
    core = pattern.strip("%").strip().lower()
    if not core:
        return False
    name = company.lower()
    # Every occurrence must be word-boundary-anchored on its left edge; if any occurrence is,
    # the match is legitimate.
    for match in re.finditer(re.escape(core), name):
        start = match.start()
        if start == 0 or not name[start - 1].isalnum():
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit company_patterns for false matches.")
    ap.add_argument("--all", action="store_true", help="Include targets ranked beyond 20.")
    ap.add_argument("--db", default=str(SQLITE_PATH))
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60.0)
    total_bad = 0
    try:
        for target in CT.TOP_DEVICE_TARGETS:
            if not args.all and target["rank"] > 20:
                continue
            findings: list[tuple[int, str, str]] = []
            for pattern in target["company_patterns"]:
                rows = conn.execute(
                    "select company_name, count(*) from devices "
                    "where lower(company_name) like ? group by 1", (pattern,)).fetchall()
                for name, count in rows:
                    if suspicious(name, pattern):
                        findings.append((count, name, pattern))
            if findings:
                findings.sort(reverse=True)
                bad = sum(c for c, _, _ in findings)
                total_bad += bad
                print(f"\n{target['key']}  —  {bad:,} devices from unrelated companies")
                for count, name, pattern in findings[:6]:
                    print(f"    {count:>8,}  {name[:48]:<50} (pattern {pattern})")
                if len(findings) > 6:
                    print(f"    ... and {len(findings) - 6} more")
    finally:
        conn.close()

    if total_bad:
        print(f"\nTOTAL falsely attributed: {total_bad:,} devices")
        print("Anchor the pattern on the real entity name (e.g. 'bard %' not '%bard%').")
        return 1
    print("No substring false matches found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
