"""Resolve Globus Medical IFUs via the globusmedical.com eIFU selector API.

Globus publishes by PRODUCT FAMILY, not by catalog number. Its eIFU page drives three
public WordPress REST routes with no auth, token or WAF::

    GET /wp-json/wp/v2/globus/ifu/countries
    GET /wp-json/wp/v2/globus/ifu/products?country=<c>&subtype=<s>
    GET /wp-json/wp/v2/globus/ifu/documents?country=<c>&product=<p>&subtype=<s>

(countries: us, ous; subtypes: device_insert, patient_implant_card. Each response is a
JSON *string* containing the JSON payload -- decode twice.) `documents` returns direct,
permanent PDF URLs under /wp-content/uploads/, with no presigned expiry.

So this resolver is shaped differently from the catalog-search ones, and much cheaper.
The whole portal is only a few hundred requests, so we MIRROR ITS INDEX ONCE and then map
devices to families entirely offline -- 43k Globus devices resolved for ~520 requests,
against ~43k for a per-catalog portal. The index is cached and only refetched when stale.

Devices are matched by GUDID brandName against the portal's product label, because Globus
GUDID records carry a model number and no catalog number, and nothing in the model number
identifies the family. 121 GUDID brands against 129 portal products covers 78.7% of the
devices; the rest are brands with no published US device insert (knee/hip lines acquired
with StelKast, and 4.7k records whose brand is literally "N/A").

A family document is the real IFU for every device in that family -- that is how Globus
publishes -- but it is NOT device-specific, so it is recorded as `brand_family_match` and
never as `exact_catalog`. Six brands legitimately map to more than one product (REVERE ->
"REVERE 4.5 Stabilization System" and "REVERE Stabilization System"); all matches are
attached, and the answerer already selects whichever document contains the answer.

Usage:
    python -m resolvers.globus_resolver --refresh-index
    python -m resolvers.globus_resolver --brand CREO
    python -m resolvers.globus_resolver --batch 2000
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from resolvers.eifu_resolver import SQLITE_PATH
from resolvers import family_portal as FP

VAULT = Path(__file__).resolve().parent.parent
BASE = "https://www.globusmedical.com/wp-json/wp/v2/globus/ifu"
PORTAL = "https://www.globusmedical.com/eifu/"
MANUFACTURER_FAMILY = "globus_medical"

COUNTRIES = ("us", "ous")
SUBTYPES = ("device_insert", "patient_implant_card")

# Plain public WordPress JSON. Identify normally; there is no WAF here to work around.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "application/json, text/plain, */*",
}
REQUEST_DELAY = 2.0          # seconds between requests, plus jitter
MAX_RETRIES = 3

# Company patterns. Globus acquired NuVasive in 2023 but NuVasive IFUs are on a SEPARATE
# portal (nuvasive.com/resources/electronic-ifu-information), which this resolver does not
# reach -- the Globus eIFU page links out to it rather than serving it. Do not add
# %nuvasive% here on the strength of the corporate merger; it would mark 44k NuVasive
# devices not_found against a portal that never had them.
COMPANY_PATTERNS = ("%globus medical%",)


class GlobusPortalError(RuntimeError):
    """Portal returned something we could not use."""


def _get(url: str) -> Any:
    """GET and decode. The API double-encodes: a JSON string holding JSON."""
    delay = 5.0
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise GlobusPortalError(f"WAF blocked the client (HTTP {exc.code})") from exc
            if attempt == MAX_RETRIES - 1:
                raise GlobusPortalError(f"HTTP {exc.code} for {url}") from exc
        except Exception as exc:  # noqa: BLE001 - transient network, retry
            if attempt == MAX_RETRIES - 1:
                raise GlobusPortalError(f"{type(exc).__name__}: {exc}") from exc
        time.sleep(delay)
        delay *= 2
    payload = json.loads(raw)
    if isinstance(payload, str):        # the double encoding
        payload = json.loads(payload)
    if isinstance(payload, dict) and not payload.get("success", True):
        raise GlobusPortalError(str(payload.get("message", "portal reported failure")))
    return payload


def _sleep() -> None:
    time.sleep(REQUEST_DELAY + random.uniform(0, 1.0))


def build_index(verbose: bool = True) -> dict[str, Any]:
    """Mirror the whole portal: every product's documents, per country and subtype."""
    index: dict[str, Any] = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "products": [],
    }
    seen_docs: set[tuple[str, str]] = set()
    for country in COUNTRIES:
        for subtype in SUBTYPES:
            url = (f"{BASE}/products?country={urllib.parse.quote(country)}"
                   f"&subtype={urllib.parse.quote(subtype)}")
            try:
                products = _get(url).get("products", [])
            except GlobusPortalError as exc:
                if verbose:
                    print(f"  products {country}/{subtype}: {exc}")
                continue
            _sleep()
            if verbose:
                print(f"  {country}/{subtype}: {len(products)} products")
            for product in products:
                value, label = product.get("value"), product.get("label")
                if not value or not label:
                    continue
                durl = (f"{BASE}/documents?country={urllib.parse.quote(country)}"
                        f"&product={urllib.parse.quote(value)}"
                        f"&subtype={urllib.parse.quote(subtype)}")
                try:
                    documents = _get(durl).get("documents", [])
                except GlobusPortalError as exc:
                    if verbose:
                        print(f"    {label}: {exc}")
                    continue
                _sleep()
                for doc in documents:
                    key = (label, doc.get("url", ""))
                    if not doc.get("url") or key in seen_docs:
                        continue
                    seen_docs.add(key)
                    index["products"].append({
                        "label": label,
                        "value": value,
                        "country": country,
                        "subtype": subtype,
                        "title": doc.get("title") or label,
                        "url": doc["url"],
                    })
    if not index["products"]:
        raise GlobusPortalError("portal mirrored to zero documents")
    return index


def load_index(refresh: bool = False, verbose: bool = True) -> dict[str, Any]:
    return FP.load_index("globus", lambda: build_index(verbose=verbose),
                         refresh=refresh, verbose=verbose)


def main() -> int:
    ap = argparse.ArgumentParser(description="Globus Medical family-keyed IFU resolver.")
    ap.add_argument("--brand", help="Show the documents that match one GUDID brand.")
    ap.add_argument("--batch", type=int, help="Resolve N unresolved Globus devices.")
    ap.add_argument("--refresh-index", action="store_true", help="Re-mirror the portal.")
    ap.add_argument("--db", default=str(SQLITE_PATH))
    args = ap.parse_args()

    index = load_index(refresh=args.refresh_index)
    products = index["products"]

    if args.brand:
        docs = FP.documents_for_brand(args.brand, products)
        print(f"{args.brand}: {len(docs)} document(s)")
        for d in docs:
            print(f"  [{d.get('country')}/{d.get('subtype')}] {d['title']}\n    {d['url']}")
        return 0

    FP.coverage_report(products, COMPANY_PATTERNS, db_path=args.db)
    if not args.batch:
        return 0

    rows = FP.load_family_devices(COMPANY_PATTERNS, args.batch, db_path=args.db)
    print(f"Resolving {len(rows)} Globus devices")
    found = FP.run_batch(rows, products, MANUFACTURER_FAMILY, PORTAL, db_path=args.db)
    print(f"done: {found}/{len(rows)} devices resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
