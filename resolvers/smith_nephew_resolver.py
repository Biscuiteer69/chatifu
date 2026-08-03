"""Resolve Smith+Nephew IFUs by driving ifu.smith-nephew.com with Playwright.

Unlike every other resolver here, S+N has no API to reverse-engineer. The portal is a stateful
ASP.NET WebForms app: results and downloads come from __doPostBack calls against server-side
ViewState, so there is nothing to call directly and no stable document URL to store. The browser
IS the client.

The flow, each step established by driving it and screenshotting:

  1. Three stacked modals, in order: country/language -> healthcare-professional gate -> terms.
    They use a static backdrop, so the cookie banner underneath is NOT clickable until they are
    cleared — clicking it first silently fails with a pointer-interception error.
  2. Cookie ACCEPT.
  3. The REF must be typed into BOTH txt_Search and txt_Search2 ("Repeat number to confirm").
    Filling only the first leaves Search inert, which looks exactly like a broken selector.
  4. Search -> either "SORRY! File cannot be found" (a real not_found) or a gv_ProductDocuments
    grid with one language link per available translation.
  5. Clicking a language link opens a download modal; "Download Only" (btn76) emits the PDF.

Because the download is POST-driven, `document_url` cannot be a fetchable link. The bytes are
put in IFUDocumentCache under a synthetic key and that key is stored instead, so the serving
layer reads from cache rather than trying to re-drive a browser per request.

Identifier note: S+N's REF is the 8-digit form (66020626) that dominates GUDID's catalog_number
for this maker. Longer 11-digit values exist and generally do NOT resolve — they are not REFs.

Cost: ~15-20s per device, which is its own rate limit. The portal has no WAF worth fighting and
is on its own host, so it does not compete with the Qarad or e-ifu budgets.

Usage:
    python -m resolvers.smith_nephew_resolver 66027707
    python -m resolvers.smith_nephew_resolver --batch 20
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from resolvers.eifu_resolver import SQLITE_PATH

PORTAL = "https://ifu.smith-nephew.com/"
MANUFACTURER_FAMILY = "smith_nephew"
# Order matters: the HCP gate must win over "I AM A PATIENT", which otherwise matches first and
# yields a narrower document set.
MODAL_PREFERENCE = ("healthcare", "accept to continue", "continue", "accept", "i agree", "ok")
ENGLISH_HINTS = ("english (us)", "english", "multiple language")
NOT_FOUND_TEXT = "cannot be found"


class SmithNephewResolver:
    def __init__(self, headless: bool = True, db_path: str | Path = SQLITE_PATH):
        self.db_path = str(db_path)
        self._pw = self._browser = self._ctx = self._page = None
        self._headless = headless
        self._cache = None

    # -- browser lifecycle --------------------------------------------------

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._ctx = self._browser.new_context(accept_downloads=True)
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        for closer in (self._ctx, self._browser):
            try:
                closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    def _dismiss_modals(self, rounds: int = 6) -> None:
        """Clear the modal stack. Cookies persist in the context, so after the first page load
        this is usually a no-op — but a re-shown modal silently blocks every later click, so it
        is re-checked on every search rather than assumed gone."""
        page = self._page
        for _ in range(rounds):
            visible = [m for m in page.query_selector_all(".modal") if m.is_visible()]
            if not visible:
                return
            buttons = [e for e in visible[0].query_selector_all("a,button,input[type=submit]")
                       if e.is_visible()]
            pick = None
            for want in MODAL_PREFERENCE:
                for element in buttons:
                    label = (element.inner_text() or element.get_attribute("value") or "").strip().lower()
                    if want in label:
                        pick = element
                        break
                if pick:
                    break
            if pick is None:
                pick = buttons[-1] if buttons else None
            if pick is None:
                return
            pick.click()
            page.wait_for_timeout(1800)

    # -- resolve ------------------------------------------------------------

    def resolve(self, ref: str) -> list[dict]:
        """Documents for one REF. [] means the portal genuinely has none."""
        page = self._page
        page.goto(PORTAL, timeout=60000, wait_until="networkidle")
        page.wait_for_timeout(1000)
        self._dismiss_modals()
        try:
            page.click("#btn43", timeout=2500)          # cookie ACCEPT, once per context
        except Exception:  # noqa: BLE001
            pass

        page.fill("#txt_Search", ref)
        page.fill("#txt_Search2", ref)                   # "Repeat number to confirm"
        page.click("#btn14")
        page.wait_for_timeout(6000)

        if NOT_FOUND_TEXT in " ".join(page.inner_text("body").split()).lower():
            return []

        # VISIBLE links only. A product with several documents renders a language row per
        # document, but only the expanded one is on screen — a catalog like 71358205 offers
        # ("English", False), ("English (US)", False) and ("English", True). Selecting by text
        # alone picks a hidden element, and clicking it hangs for the full 30s timeout, which
        # the batch reports as "0 devices resolved" with no row written. That looked like the
        # portal running out of documents; it was ~4,000 pending devices blocked on a click.
        links = [a for a in page.query_selector_all("a")
                 if "gv_ProductDocuments" in (a.get_attribute("href") or "") and a.is_visible()]
        if not links:
            return []

        # Prefer an English document; fall back to the first offered.
        chosen = None
        for hint in ENGLISH_HINTS:
            for link in links:
                if hint in (link.inner_text() or "").strip().lower():
                    chosen = link
                    break
            if chosen:
                break
        chosen = chosen or links[0]
        language = (chosen.inner_text() or "").strip()

        chosen.click()
        page.wait_for_timeout(4000)
        try:
            with page.expect_download(timeout=60000) as download_info:
                page.click("#btn76")                     # "Download Only"
            download = download_info.value
        except Exception as exc:  # noqa: BLE001
            print(f"  {ref}: download failed ({type(exc).__name__})")
            return []

        filename = download.suggested_filename
        key = f"sn-eifu://{ref}/{filename}"
        data = Path(download.path()).read_bytes()
        if data[:5] != b"%PDF-":
            return []
        self._cache_bytes(key, data)
        return [{
            "document_url": key,
            "document_title": filename,
            "language": language,
            "source_file_name": filename,
            "match_confidence": "exact_catalog",
            "bytes": len(data),
        }]

    def _cache_bytes(self, key: str, data: bytes) -> None:
        try:
            if self._cache is None:
                from ifu_cache import IFUDocumentCache
                self._cache = IFUDocumentCache()
            if self._cache.get(key) is None:
                self._cache.put(key, data)
        except Exception:  # noqa: BLE001 - caching is best-effort
            pass

    # -- persistence --------------------------------------------------------

    def log(self, rowid: int, primary_di: str | None, ref: str, docs: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        try:
            if not docs:
                conn.execute(
                    """insert into ifu_links (device_rowid, primary_di, catalog_number,
                       manufacturer_family, source_url, status, first_seen_at, last_checked_at)
                       values(?,?,?,?,?,?,?,?)""",
                    (rowid, primary_di, ref, MANUFACTURER_FAMILY, PORTAL, "not_found", now, now))
            for doc in docs:
                conn.execute(
                    """insert or ignore into ifu_links (device_rowid, primary_di, catalog_number,
                       manufacturer_family, source_url, document_url, document_title, language,
                       match_confidence, retrieved_at, status, first_seen_at, last_checked_at,
                       last_success_at, source_file_name)
                       values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rowid, primary_di, ref, MANUFACTURER_FAMILY, PORTAL, doc["document_url"],
                     doc["document_title"], doc["language"], doc["match_confidence"], now,
                     "found", now, now, now, doc["source_file_name"]))
            conn.commit()
        finally:
            conn.close()


def load_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    """Unresolved S+N devices, most-resolvable form first.

    Length alone is not enough. GUDID holds ~31k 8-character values for S+N but they are not one
    family: the portal indexes the 7x and 66 series (71xxxxxx orthopaedic implants — 20k of them
    — plus 72/74 instruments and 66 wound care), while 0x/1x 8-character values are some other
    identifier and return "file cannot be found" every time. Ordering alphabetically walks the
    unresolvable ones first and makes a working resolver look completely broken, which is exactly
    what the first batch did: 0/6, all of them 0x/1x."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.catalog_number, d.raw_json
            from devices d
            where d.catalog_number is not null and trim(d.catalog_number) != ''
              and lower(d.company_name) like '%smith%nephew%'
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.catalog_number
                  and l.status in ('found','candidate_broad','not_found')
              )
            order by case
                       when length(trim(d.catalog_number)) = 8
                            and substr(trim(d.catalog_number),1,1) in ('7','6') then 0
                       when length(trim(d.catalog_number)) = 8 then 1
                       else 2
                     end,
                     d.catalog_number
            limit ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Smith+Nephew eIFU resolver (Playwright).")
    ap.add_argument("ref", nargs="?", help="Single REF to resolve.")
    ap.add_argument("--batch", type=int, help="Resolve N unresolved S+N devices.")
    ap.add_argument("--headed", action="store_true", help="Show the browser (debugging).")
    args = ap.parse_args()

    with SmithNephewResolver(headless=not args.headed) as resolver:
        if args.ref:
            print(json.dumps(resolver.resolve(args.ref), indent=2))
            return 0
        if not args.batch:
            ap.error("ref or --batch is required")

        rows = load_devices(args.batch)
        print(f"Resolving {len(rows)} Smith+Nephew devices")
        found = 0
        for index, row in enumerate(rows, 1):
            ref = row["catalog_number"].strip()
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            try:
                docs = resolver.resolve(ref)
            except Exception as exc:  # noqa: BLE001 - one bad device must not kill the batch
                print(f"[{index}/{len(rows)}] {ref}: error {type(exc).__name__}")
                continue
            resolver.log(row["rowid"], raw.get("PrimaryDI"), ref, docs)
            if docs:
                found += 1
            print(f"[{index}/{len(rows)}] {ref}: {len(docs)} docs (found {found})")
        print(f"done: {found}/{len(rows)} devices resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
