"""Resolve Alphatec Spine (ATEC) IFUs via the atecspine.com eIFU archive.

Family-keyed portal — see resolvers/family_portal.py for why these are mirrored rather
than searched per device.

The eIFU page renders nothing server-side; its document list is drawn by the WP Download
Manager archive plugin over AJAX. The call is a POST to the SITE ROOT (wpdm_url.home, not
admin-ajax.php — admin-ajax answers `0`/HTTP 400 for this action) carrying `action=
get_downloads` plus a nonce and an opaque `sc_params` blob, both of which are printed into
the eIFU page and must be scraped from it first, like the Boston Scientific search token.

It must be sent the COMPLETE parameter set the plugin's own JS sends. Omit any of
category/cat_operator/tags/orderby/order/from_date/to_date/date_col and WordPress does not
recognise the request as the AJAX action at all: it returns the ordinary home page with
HTTP 200 and `content-type: text/html`, which reads like a redirect or a block rather than
the missing-argument error it is.

The response is JSON `{html, current, last, params}`. `last` is the page count — the whole
IFU catalogue is 11 pages of 10 cards, so ~110 documents cover 46k Alphatec devices. Card
links point at /download/<slug>/?wpdmdl=<id>, a permanent public route that redirects to
the PDF; the `refresh=` cache-buster in the markup is dropped.

Usage:
    python -m resolvers.atec_resolver --refresh-index
    python -m resolvers.atec_resolver --brand Invictus
    python -m resolvers.atec_resolver --batch 2000
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from resolvers.eifu_resolver import SQLITE_PATH
from resolvers import family_portal as FP

PORTAL = "https://atecspine.com/eifu/"
SITE = "https://atecspine.com/"
MANUFACTURER_FAMILY = "alphatec_spine"
COMPANY_PATTERNS = ("%alphatec%",)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ChatIFU/1.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
REQUEST_DELAY = 2.0
MAX_RETRIES = 3

_NONCE_RE = re.compile(r'wpdmap_ajax\s*=\s*\{"nonce":"([a-f0-9]+)"')
_PARAMS_RE = re.compile(r"wpdmap_params\s*=\s*'([^']+)'")
_CARD_RE = re.compile(
    r'href=["\']([^"\']*?/download/[^"\']*?wpdmdl=\d+[^"\']*)["\'][^>]*>\s*([^<]{3,120}?)\s*</a>')


class AtecPortalError(RuntimeError):
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
                raise AtecPortalError(f"WAF blocked the client (HTTP {exc.code})") from exc
            if attempt == MAX_RETRIES - 1:
                raise AtecPortalError(f"HTTP {exc.code} for {url}") from exc
        except Exception as exc:  # noqa: BLE001 - transient network, retry
            if attempt == MAX_RETRIES - 1:
                raise AtecPortalError(f"{type(exc).__name__}: {exc}") from exc
        time.sleep(delay)
        delay *= 2
    raise AtecPortalError("retries exhausted")


def _resolve_pdf_url(download_url: str) -> str:
    """Follow the /download/ route to the real PDF and store THAT.

    The archive links at /download/<slug>/?wpdmdl=<id>, which 302s to a permanent file under
    /wp-content/uploads/. Storing the redirect would break serving outright: the answerer
    decides how to fetch a document with _is_direct_pdf_url(), whose test is "does the path
    end in .pdf". A /download/ URL fails that and falls into the e-ifu.com VIEWER branch --
    it logs into e-ifu and tries to parse an Alphatec page as an e-ifu viewer, yielding zero
    pages and zero hits for every Alphatec device, while fetching the URL by hand works fine.

    Resolving here rather than teaching the answerer about this one portal keeps the stored
    URL the same shape as every other resolver's, and costs one HEAD per document, once.
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            raise _Redirected(newurl)

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(download_url, headers=HEADERS, method="HEAD")
    try:
        with opener.open(req, timeout=45):
            return download_url          # no redirect: already final
    except _Redirected as hop:
        return urllib.parse.urljoin(download_url, hop.url)
    except Exception:                     # noqa: BLE001 - keep the original on any failure
        return download_url


class _Redirected(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def _session() -> tuple[str, str]:
    """Scrape the nonce and sc_params the archive AJAX requires."""
    page = _fetch(PORTAL)
    nonce = _NONCE_RE.search(page)
    params = _PARAMS_RE.search(page)
    if not nonce or not params:
        raise AtecPortalError("could not find wpdmap nonce/params on the eIFU page")
    return nonce.group(1), params.group(1)


def _page(nonce: str, params: str, cp: int) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "action": "get_downloads", "_wpnonce": nonce, "cp": cp, "init": 1,
        # Every one of these must be present; a missing key silently yields the home page.
        "search": "", "category": "", "cat_operator": "IN", "tags": "",
        "orderby": "date", "order": "desc", "from_date": "", "to_date": "",
        "date_col": "", "sc_params": params,
    }).encode()
    raw = _fetch(SITE, data=body)
    if raw.lstrip().startswith("<"):
        raise AtecPortalError("archive returned HTML, not JSON — parameter set incomplete")
    return json.loads(raw)


def build_index(verbose: bool = True) -> dict[str, Any]:
    nonce, params = _session()
    time.sleep(REQUEST_DELAY)
    index: dict[str, Any] = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "products": [],
    }
    seen: set[str] = set()
    page_no, last = 1, 1
    while page_no <= last:
        payload = _page(nonce, params, page_no)
        last = int(payload.get("last") or 1)
        cards = _CARD_RE.findall(payload.get("html", ""))
        for url, title in cards:
            url = htmllib.unescape(url).split("&refresh=")[0].split("?refresh=")[0]
            title = htmllib.unescape(title).strip()
            if not title or title.lower() == "download" or url in seen:
                continue
            seen.add(url)
            time.sleep(0.7)
            index["products"].append({
                "label": title, "title": title, "url": _resolve_pdf_url(url),
            })
        if verbose:
            print(f"  page {page_no}/{last}: {len(cards)} cards ({len(index['products'])} docs)")
        page_no += 1
        if page_no <= last:
            time.sleep(REQUEST_DELAY + random.uniform(0, 1.0))
    if not index["products"]:
        raise AtecPortalError("archive mirrored to zero documents")
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="Alphatec Spine family-keyed IFU resolver.")
    ap.add_argument("--brand", help="Show the documents that match one GUDID brand.")
    ap.add_argument("--batch", type=int, help="Resolve N unresolved Alphatec devices.")
    ap.add_argument("--refresh-index", action="store_true", help="Re-mirror the portal.")
    ap.add_argument("--db", default=str(SQLITE_PATH))
    args = ap.parse_args()

    index = FP.load_index("atec", build_index, refresh=args.refresh_index)
    products = index["products"]

    if args.brand:
        docs = FP.documents_for_brand(args.brand, products)
        print(f"{args.brand}: {len(docs)} document(s)")
        for d in docs:
            print(f"  {d['title']}\n    {d['url']}")
        return 0

    if not args.batch:
        FP.coverage_report(products, COMPANY_PATTERNS, db_path=args.db)
        return 0

    FP.coverage_report(products, COMPANY_PATTERNS, db_path=args.db)
    rows = FP.load_family_devices(COMPANY_PATTERNS, args.batch, db_path=args.db)
    print(f"Resolving {len(rows)} Alphatec devices")
    found = FP.run_batch(rows, products, MANUFACTURER_FAMILY, PORTAL, db_path=args.db)
    print(f"done: {found}/{len(rows)} devices resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
