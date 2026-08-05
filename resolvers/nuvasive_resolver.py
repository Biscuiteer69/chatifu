"""Resolve NuVasive IFUs via the nuvasive.com eIFU plugin.

Family-keyed portal — see resolvers/family_portal.py for why these are mirrored rather
than searched per device.

NuVasive is owned by Globus (2023) but publishes on its OWN portal; the Globus eIFU page
merely links here. resolvers/globus_resolver.py therefore does NOT claim these devices, and
must not, or 44k NuVasive records would be marked not_found against a portal that never had
them.

The whole product catalogue is embedded in the eIFU page as a JSON blob on `eifuSettings`
(~630KB, `products` being itself a JSON *string*), so one GET yields every product's title,
slug, country and languages with no enumeration. Documents then come from::

    POST /wp-admin/admin-ajax.php   action=get_eifu_data&product=<slug>&language=<lang>

keyed on the SLUG ("ACP"), while the family we match against is the TITLE ("ACP System").
The response body is a JSON string holding the document array; a plain-text body beginning
"Request Error:" or "Authorization Error:" is the plugin's own failure, not a document set.

Only US and Puerto Rico products are mirrored: 176 of 3,747 entries, 109 distinct families,
which reach 93.1% of NuVasive's 47k devices — the highest of any family portal so far. The
rest of the catalogue is per-country translations of the same families.

Usage:
    python -m resolvers.nuvasive_resolver --refresh-index
    python -m resolvers.nuvasive_resolver --brand Reline
    python -m resolvers.nuvasive_resolver --batch 4000
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from resolvers.eifu_resolver import SQLITE_PATH
from resolvers import family_portal as FP

PORTAL = "https://www.nuvasive.com/resources/electronic-ifu-information/"
AJAX = "https://www.nuvasive.com/wp-admin/admin-ajax.php"
MANUFACTURER_FAMILY = "nuvasive"
COMPANY_PATTERNS = ("%nuvasive%",)

# The eIFU selector offers these as separate "countries"; both are English US labelling.
US_COUNTRIES = ("usa", "puerto-rico")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
REQUEST_DELAY = 2.0
MAX_RETRIES = 3


class NuvasivePortalError(RuntimeError):
    """Portal returned something we could not use."""


def _fetch(url: str, data: bytes | None = None) -> str:
    delay = 5.0
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=data, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise NuvasivePortalError(f"WAF blocked the client (HTTP {exc.code})") from exc
            if attempt == MAX_RETRIES - 1:
                raise NuvasivePortalError(f"HTTP {exc.code} for {url}") from exc
        except Exception as exc:  # noqa: BLE001 - transient network, retry
            if attempt == MAX_RETRIES - 1:
                raise NuvasivePortalError(f"{type(exc).__name__}: {exc}") from exc
        time.sleep(delay)
        delay *= 2
    raise NuvasivePortalError("retries exhausted")


def _catalogue() -> list[dict[str, Any]]:
    """Every product the eIFU page knows about, from its embedded settings blob."""
    page = _fetch(PORTAL)
    marker = page.find("eifuSettings")
    if marker < 0:
        raise NuvasivePortalError("eifuSettings not present on the eIFU page")
    # raw_decode from the opening brace: the blob contains '};' sequences inside its own
    # strings, so any regex that stops at the first one truncates it mid-JSON.
    start = page.index("{", marker)
    settings, _ = json.JSONDecoder().raw_decode(page[start:])
    products = settings.get("products")
    if isinstance(products, str):
        products = json.loads(products)
    if not products:
        raise NuvasivePortalError("settings blob carried no products")
    return products


def _field(doc: dict[str, Any], *names: str) -> str | None:
    """First non-empty value among these keys, matched case-insensitively."""
    lowered = {k.lower(): v for k, v in doc.items()}
    for name in names:
        value = lowered.get(name)
        if value:
            return str(value)
    return None


def _documents(slug: str, language: str) -> list[dict[str, Any]]:
    body = urllib.parse.urlencode({
        "action": "get_eifu_data", "product": slug, "language": language,
    }).encode()
    raw = _fetch(AJAX, data=body).strip()
    if raw.startswith("Request Error:") or raw.startswith("Authorization Error:"):
        raise NuvasivePortalError(raw[:120])
    if not raw or raw == "0":
        return []
    payload = json.loads(raw)
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload if isinstance(payload, list) else []


def build_index(verbose: bool = True) -> dict[str, Any]:
    catalogue = _catalogue()
    wanted: dict[tuple[str, str], str] = {}   # (slug, language) -> title
    for product in catalogue:
        if product.get("country") not in US_COUNTRIES:
            continue
        for lang in product.get("languages") or []:
            if (lang.get("slug") or "").lower() != "en":
                continue
            wanted.setdefault((product["slug"], lang["slug"]), product.get("title") or product["slug"])
    if verbose:
        print(f"  catalogue: {len(catalogue)} entries -> {len(wanted)} US English products")

    index: dict[str, Any] = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "products": [],
    }
    seen: set[str] = set()
    for i, ((slug, language), title) in enumerate(sorted(wanted.items()), 1):
        time.sleep(REQUEST_DELAY + random.uniform(0, 1.0))
        try:
            docs = _documents(slug, language)
        except NuvasivePortalError as exc:
            if verbose:
                print(f"    {title}: {exc}")
            continue
        for doc in docs:
            # Field names are Capitalised -- Url/DocumentDescription/FileName -- unlike every
            # other portal here. Accept both cases rather than assuming: the first pass looked
            # only for lowercase keys, mirrored 109 products to zero documents, and would have
            # marked 44k devices not_found had the empty-index guard not stopped it.
            url = _field(doc, "url", "file", "link")
            if not url or url in seen:
                continue
            seen.add(url)
            index["products"].append({
                "label": title,
                "slug": slug,
                "title": _field(doc, "documentdescription", "title", "name", "filename") or title,
                "url": url,
            })
        if verbose and i % 25 == 0:
            print(f"  [{i}/{len(wanted)}] {len(index['products'])} documents")
    if not index["products"]:
        raise NuvasivePortalError("portal mirrored to zero documents")
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="NuVasive family-keyed IFU resolver.")
    ap.add_argument("--brand", help="Show the documents that match one GUDID brand.")
    ap.add_argument("--batch", type=int, help="Resolve N unresolved NuVasive devices.")
    ap.add_argument("--refresh-index", action="store_true", help="Re-mirror the portal.")
    ap.add_argument("--db", default=str(SQLITE_PATH))
    args = ap.parse_args()

    index = FP.load_index("nuvasive", build_index, refresh=args.refresh_index)
    products = index["products"]

    if args.brand:
        docs = FP.documents_for_brand(args.brand, products)
        print(f"{args.brand}: {len(docs)} document(s)")
        for d in docs:
            print(f"  {d['title']}\n    {d['url']}")
        return 0

    FP.coverage_report(products, COMPANY_PATTERNS, db_path=args.db)
    if not args.batch:
        return 0

    rows = FP.load_family_devices(COMPANY_PATTERNS, args.batch, db_path=args.db)
    print(f"Resolving {len(rows)} NuVasive devices")
    found = FP.run_batch(rows, products, MANUFACTURER_FAMILY, PORTAL, db_path=args.db)
    print(f"done: {found}/{len(rows)} devices resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
