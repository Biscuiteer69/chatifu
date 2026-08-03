# Provably unreachable identifiers

Companies (or slices of them) where IFU coverage cannot be completed from GUDID data, with the
evidence. These satisfy the coverage goal's second branch — *proven unreachable with a stated
reason* — and should NOT be retried by adding scrapers.

Re-check only if the stated cause changes (a portal is rebuilt, a company moves platform, GUDID
starts carrying a field it does not carry today).

---

## Medline — ~246,000 identifiers — LOT NUMBER REQUIRED

`appportal.medline.com/MedlineIFU/` (OutSystems). **Not login-gated** — an earlier note saying
"Invalid Login" was wrong; the portal is public and loads fine.

The blocker is the search form. It takes **Item Number AND Lot Number**, and the Search button
stays *disabled* until both are filled — verified with Playwright: `is_enabled()` is false with
item only, true with both. With a real item and a fabricated lot it returns "Item and/or Lot
Number you entered is not valid or the item does not have Instructions for Use."

A lot number identifies a manufacturing batch and is printed on the physical product label.
**GUDID does not contain it and never will.** GUDID's `lotBatch` field looks promising — 250,288
Medline devices have a value — but the value is the boolean `"true"`, a flag meaning "this device
is lot-tracked", not a lot number.

So the portal is reachable but not *addressable* from our data. Nothing about scraper design
fixes this; it would need lot numbers from an entirely different source (distributor order data,
scanned labels).

Consistent with the other Medline signal: 88% of its GUDID devices are 510(k)-exempt commodity
supplies, which is also why FDA covers only ~1% of it.

## Siemens imaging — ~1,288 identifiers — OWNER-GATED

`doclib.siemens-healthineers.com` serves Siemens Healthcare **Diagnostics** well (see
`resolvers/siemens_resolver.py`). Every imaging catalog tested (Siemens Medical Solutions USA —
transducers, phantoms, scanners) returns zero results: those operator manuals are restricted to
verified equipment owners. The diagnostics/imaging split in our subset is ~3,091 to ~1,288.

## Cardinal Health — ~365,000 identifiers — NO SUBMISSION, NO PORTAL

98% of its GUDID devices are 510(k)-exempt, so there is no FDA document to fall back on, and it
has no IFU portal in `ifu_portal_directory.json`. These are commodity distribution SKUs (gloves,
basins, tubing) that largely have no IFU in existence.

## Philips — ~1,943 identifiers beyond the e-ifu sweep — NO DISCOVERY PATH

Philips IFU PDFs are public and permanent once you have the URL: a known asset at
`documents.philips.com/assets/Instruction%20for%20Use/<date>/<hash>.pdf?feed=ifu_docs_feed`
fetches 200 / 2MB / valid PDF with no auth. The problem is purely discovery — the keys are
opaque hashes and the `ifu_docs_feed` is not enumerable (every feed/sitemap/API path tried
returns 404).

The intended discovery portal, `acc.eifu.philips.com`, is broken for us: 500 to curl AND an
empty page (blank title, blank body, zero XHR) in a real headless browser, so it is not simple
bot-blocking. `eifu.philips.com` does not resolve at all.

The e-ifu sweep already covers 3,184 of Philips' 5,127; the remainder has no route until that
portal works.

## Olympus — ~843 identifiers beyond the e-ifu sweep — REF NOT INDEXED

US IFUs are behind **OlympusConnect.com**, a free but login-required customer portal.

The public EU portal (`olympus.co.uk/medical/en/Contact-and-support/search_page.html`) is
backed by a plain Solr REST endpoint — `olympus-europa.com/SolrRestService/select?query=<q>
&locale=en-gb&fq=IN_SYNC_GROUP:medical` — which responds without auth and does return data
(13 hits for "endoscope"). But its documents are WEB PAGE records (IN_NAME, IN_LINK,
IN_DESCRIPTION, IN_HIERARCHY): it is a site search index, not an IFU catalogue, and it does not
index product REFs. Real Olympus catalogs (e.g. N5367140) return numFound 0 for that reason,
not because the devices are absent.

The e-ifu sweep covers 1,125 of Olympus' 1,968.

## Intuitive Surgical — 9 identifiers — DISPROPORTIONATE

`manuals.intuitivesurgical.com` exists and is not gated. Nine identifiers does not justify a
resolver; revisit only if the count grows.

## BD — 7,523 identifiers — DISABLED ON COST, NOT IMPOSSIBLE

Not strictly unreachable, but deliberately not run: it shares the Qarad WAF with five working
tenants and its multi-unit search prices a miss at one request per unit. Two test batches were
rate-limited, and yield was poor where it did work. See the `bd` entry in
`resolvers/qarad_tenants.py`. The e-ifu sweep already covers 10,144 BD devices on a different
backend.

## Hillrom / Welch Allyn (counted under Baxter) — ~5,700 identifiers — WRONG PORTAL

company_targets.py counts Baxter as %baxter% + %hill-rom% + %hillrom% + %welch allyn%, but
edocs.baxter.com returns **0 items** for Welch Allyn catalogs (008-0002-01, 008-0003-01) — the
acquired brands are not on Baxter's portal. Adding them to the Baxter resolver was tried and
disproved; it wrote 970 false negatives before the monitor's ZERO YIELD check flagged it (rows
since deleted).

Consequence: Baxter's resolver can legitimately report "backlog dry" while ~5,700 catalogs
remain uncovered in the metric. They need their own resolver against Welch Allyn's own portal,
not a wider pattern on Baxter's.
