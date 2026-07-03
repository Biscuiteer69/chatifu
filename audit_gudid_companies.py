"""Audit GUDID company-name coverage for the top-device targets.

Single read-only pass over the AccessGUDID full-release zip building a
Counter of raw companyName values, then for each target reports:

- names/rows matched by the CURRENT company_targets substring patterns
- names/rows additionally matched by PROPOSED aliases (word-boundary)
- suspect matches: names the current substring patterns catch that
  word-boundary matching would not (false-positive candidates, e.g. the
  bare "bd" substring matching unrelated names)
- top unmatched companies by row count (to surface unknown subsidiaries)

Usage:
    python audit_gudid_companies.py --source fda/AccessGUDID_..._.zip \
        [--out logs/gudid_company_audit.json]

Writes a JSON report and prints a human summary. Never touches SQLite.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from company_targets import TOP_DEVICE_TARGETS
from import_accessgudid import iter_device_rows, normalized_patterns

# Proposed additional aliases per target key (word-boundary matched).
# Sources: company_registry.py aliases/subsidiaries + M&A knowledge.
# cordis -> cardinal_health intentionally omitted (divested 2021).
PROPOSED_ALIASES: dict[str, list[str]] = {
    "medtronic": ["covidien", "valleylab", "minimed", "superdimension"],
    "jnj": ["abiomed", "mentor"],
    "abbott": ["st jude", "st. jude"],
    "bd": ["becton", "bard", "carefusion"],
    "philips": ["respironics"],
    "baxter": ["hillrom", "hill-rom", "welch allyn"],
    "b_braun": ["aesculap"],
    "ge_healthcare": ["datex-ohmeda", "ohmeda"],
    "stryker": ["wright medical"],
    "olympus": ["gyrus"],
    "terumo": ["microvention"],
}


def word_boundary_regex(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])")


def scan_companies(source: Path) -> Counter:
    counts: Counter = Counter()
    for i, row in enumerate(iter_device_rows(source)):
        counts[(row.get("companyName") or "").strip()] += 1
        if i % 500_000 == 0:
            print(f"[audit] scanned {i:,} rows...", flush=True)
    return counts


def audit(source: Path) -> dict:
    company_counts = scan_companies(source)
    print(f"[audit] {sum(company_counts.values()):,} rows, {len(company_counts):,} distinct company names")

    target_keys = [str(t["key"]) for t in TOP_DEVICE_TARGETS]
    current = normalized_patterns(target_keys)

    report: dict = {"targets": {}, "unmatched_top": []}
    matched_names: set[str] = set()

    for key in target_keys:
        cur_patterns = current[key]
        proposed = PROPOSED_ALIASES.get(key, [])
        proposed_res = [word_boundary_regex(p) for p in proposed]

        cur_hit: dict[str, int] = {}
        cur_suspect: dict[str, int] = {}
        new_hit: dict[str, int] = {}
        for name, n in company_counts.items():
            low = name.lower()
            cur_sub = any(p in low for p in cur_patterns)
            if cur_sub:
                cur_hit[name] = n
                # substring hit that no word-boundary version of the same
                # patterns would produce -> false-positive candidate
                if not any(word_boundary_regex(p).search(low) for p in cur_patterns):
                    cur_suspect[name] = n
            elif any(r.search(low) for r in proposed_res):
                new_hit[name] = n
        matched_names.update(cur_hit)
        matched_names.update(new_hit)

        report["targets"][key] = {
            "current_rows": sum(cur_hit.values()),
            "current_names": len(cur_hit),
            "proposed_added_rows": sum(new_hit.values()),
            "proposed_added_names": dict(sorted(new_hit.items(), key=lambda kv: -kv[1])),
            "suspect_current_matches": dict(sorted(cur_suspect.items(), key=lambda kv: -kv[1])[:40]),
        }

    unmatched = Counter({n: c for n, c in company_counts.items() if n not in matched_names})
    report["unmatched_top"] = [
        {"company": name, "rows": rows} for name, rows in unmatched.most_common(100)
    ]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("logs/gudid_company_audit.json"))
    args = parser.parse_args()

    report = audit(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"[audit] report -> {args.out}")

    print(f"\n{'target':<18}{'current':>10}{'+proposed':>11}  notable additions / suspects")
    for key, data in report["targets"].items():
        adds = list(data["proposed_added_names"].items())[:3]
        adds_s = ", ".join(f"{n} ({c:,})" for n, c in adds)
        suspects = len(data["suspect_current_matches"])
        line = f"{key:<18}{data['current_rows']:>10,}{data['proposed_added_rows']:>11,}  {adds_s}"
        if suspects:
            line += f"  [!{suspects} suspect current matches]"
        print(line)
    print("\nTop unmatched companies:")
    for item in report["unmatched_top"][:15]:
        print(f"  {item['rows']:>8,}  {item['company']}")


if __name__ == "__main__":
    main()
