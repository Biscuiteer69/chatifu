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

## Intuitive Surgical — 9 identifiers — DISPROPORTIONATE

`manuals.intuitivesurgical.com` exists and is not gated. Nine identifiers does not justify a
resolver; revisit only if the count grows.

## BD — 7,523 identifiers — DISABLED ON COST, NOT IMPOSSIBLE

Not strictly unreachable, but deliberately not run: it shares the Qarad WAF with five working
tenants and its multi-unit search prices a miss at one request per unit. Two test batches were
rate-limited, and yield was poor where it did work. See the `bd` entry in
`resolvers/qarad_tenants.py`. The e-ifu sweep already covers 10,144 BD devices on a different
backend.
