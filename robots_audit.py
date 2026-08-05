"""Check what every portal we scrape actually permits in robots.txt.

Nothing in the fleet has ever read a robots.txt. That is the gap most likely to cost us
access: a manufacturer noticing an unannounced crawler on a path they asked crawlers to
stay off has an easy, uncontestable reason to block us, and we would not even know we had
been asked. Losing a portal is expensive — Qarad alone fronts five tenants and has rate-
banned this IP three times.

This is an AUDIT, not an enforcer. It reports, per host, what robots.txt says about the
paths the resolvers actually request, so the cadence and path decisions can be made against
evidence rather than assumption. Enforcement should follow once the picture is known,
because a naive "obey everything" would silently switch off portals that are fine to read.

One request per host, cached to disk, so running this is itself polite.

Run:  .venv/bin/python robots_audit.py [--refresh]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

VAULT = Path(__file__).resolve().parent
CACHE = VAULT / "runs" / "robots_cache"

# The identity we send. A crawler that names itself and offers a contact gets an email;
# an anonymous one that looks like a browser gets a block.
CHATIFU_UA = "ChatIFU/1.0 (+https://chatifu.com; medical-device IFU index)"

# host -> representative paths the resolvers actually GET/POST. Checking the real paths
# matters: several of these portals disallow /search or /admin while leaving the document
# routes open, and a host-level yes/no would hide that.
TARGETS: dict[str, list[str]] = {
    "https://api-public.qarad.eifu.online": ["/api/v1/business-units", "/api/v1/products"],
    "https://labeling.stryker.com": ["/", "/search"],
    "https://docs.zimmerbiomet.com": ["/", "/search"],
    "https://edfu.arthrex.com": ["/", "/search"],
    "https://edocs.baxter.com": ["/", "/search"],
    "https://ifu.alcon.com": ["/", "/search"],
    "https://ifu.coopersurgical.com": ["/", "/search"],
    "https://eifu.bd.com": ["/", "/search"],
    "https://manuals.medtronic.com": ["/", "/content/manuals/us/en/search.html"],
    "https://services.abbott": ["/api/public/search/sitesearch"],
    "https://manuals.eifu.abbott": ["/"],
    "https://www.e-ifu.com": ["/", "/search", "/fetchPdf"],
    "https://www.bostonscientific.com": ["/elabeling/us/en/home/healthcare-professionals.html"],
    "https://platform.cloud.coveo.com": ["/rest/search/v2"],
    "https://www.aesculapusaifus.com": ["/", "/sites/default/files/ifus/"],
    "https://www.aesculapimplantsystemsifus.com": ["/", "/sites/default/files/ifus/"],
    "https://doclib.siemens-healthineers.com": ["/rest/v1/documents", "/rest/v1/view"],
    "https://ifu.smith-nephew.com": ["/"],
    "https://eifu.fresenius-kabi.com": ["/medtech/search", "/medtech/viewers/pdf"],
    "https://eifu.edwards.com": ["/"],
    "https://www.accessdata.fda.gov": ["/cdrh_docs/pdf24/"],
}


def fetch_robots(base: str, refresh: bool = False) -> tuple[str | None, str]:
    """Return (robots.txt body or None, note). Cached so re-runs cost no requests."""
    CACHE.mkdir(parents=True, exist_ok=True)
    host = urlparse(base).netloc
    path = CACHE / f"{host}.txt"
    if path.exists() and not refresh:
        return path.read_text(), "cached"
    url = f"{base}/robots.txt"
    req = urllib.request.Request(url, headers={"User-Agent": CHATIFU_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        path.write_text(body)
        return body, "fetched"
    except urllib.error.HTTPError as exc:
        # 404 = no robots.txt = nothing asked of us. Distinct from 403, which is the site
        # refusing to talk to us at all and is itself a finding.
        note = "no robots.txt (404)" if exc.code == 404 else f"HTTP {exc.code}"
        if exc.code == 404:
            path.write_text("")
            return "", note
        return None, note
    except Exception as exc:  # noqa: BLE001 - an unreachable host is a finding, not a crash
        return None, f"{type(exc).__name__}: {exc}"


def _is_soft_404(body: str) -> bool:
    """True when the server answered /robots.txt with a web page.

    Most of these portals are SPAs that return their app shell for any unmatched path, so a
    200 is not evidence of a robots.txt. Feeding that HTML to RobotFileParser yields zero
    directives, which parses as "everything allowed" — an absent policy silently reported as
    permission granted. Eight of the twenty-one hosts do exactly this, including every Qarad
    tenant (identical 2454-byte shell) and e-ifu.com, our highest-volume host.
    """
    head = body.lstrip()[:400].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<html" in head


def audit(refresh: bool = False) -> list[dict]:
    rows: list[dict] = []
    for base, paths in TARGETS.items():
        body, note = fetch_robots(base, refresh)
        row: dict = {"host": urlparse(base).netloc, "note": note, "paths": {}}
        if body is None:
            row["verdict"] = "UNKNOWN"
            rows.append(row)
            continue
        if _is_soft_404(body):
            row["verdict"] = "NO-POLICY"
            row["note"] = "no robots.txt (HTML soft-404)"
            rows.append(row)
            continue
        if not body.strip():
            row["verdict"] = "NO-POLICY"
            row["note"] = "no robots.txt (404)"
            rows.append(row)
            continue
        rp = RobotFileParser()
        rp.parse(body.splitlines())
        row["crawl_delay"] = rp.crawl_delay(CHATIFU_UA) or rp.crawl_delay("*")
        for p in paths:
            row["paths"][p] = rp.can_fetch(CHATIFU_UA, base + p)
        allowed = [v for v in row["paths"].values()]
        row["verdict"] = ("ALLOWED" if all(allowed)
                          else "BLOCKED" if not any(allowed) else "MIXED")
        rows.append(row)
        if note == "fetched":
            time.sleep(2)  # one host at a time, unhurried
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit robots.txt for every scraped portal.")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch instead of using cache.")
    ap.add_argument("--json", action="store_true", help="Emit JSON.")
    args = ap.parse_args()

    rows = audit(args.refresh)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    order = {"BLOCKED": 0, "MIXED": 1, "UNKNOWN": 2, "NO-POLICY": 3, "ALLOWED": 4}
    for r in sorted(rows, key=lambda x: (order.get(x["verdict"], 9), x["host"])):
        delay = f"  crawl-delay={r['crawl_delay']}s" if r.get("crawl_delay") else ""
        print(f"{r['verdict']:8} {r['host']:42} ({r['note']}){delay}")
        for p, ok in r["paths"].items():
            if not ok:
                print(f"           DISALLOWED: {p}")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
