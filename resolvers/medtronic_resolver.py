"""Resolve Medtronic IFUs from manuals.medtronic.com.

Medtronic devices are keyed by MODEL number, not catalog number: of 96,980
Medtronic/Covidien devices in GUDID, 96,980 carry a model number and only 633
carry a catalog number (their brand_name is usually "NA" too). So this resolver
searches by model, and stores the model as the device identifier in ifu_links.

The portal is a session-based Struts app, not an API:

  1. GET /manuals/main/region?region=na          -> SYNCHRONIZER_TOKEN (CSRF)
  2. GET /manuals/main/country/index             -> country=US & lang=en_US
     (lang MUST be en_US; plain "en" lands on an error page)
  3. GET /manuals/main/en_US/manual/index        -> findby=model&model=<model>

Each page mints a fresh token, so the session is walked in order and the token
carried forward. Results are exact — an unknown model returns "No Results Found"
rather than a fuzzy neighbour — so a hit is Medtronic asserting that the manual
covers that model.

The PDF URL is not in an href (those are "#"); it sits in the row's
openDialogOptIn(...) onclick, pointing at a stable
www.medtronic.com/content/dam/emanuals/... link with no expiry.

Brand path (--by-brand)
-----------------------
A model search only knows the models Medtronic bothered to index; ~35k of its
GUDID devices (Covidien sutures and airway, Sofamor Danek spine, Xomed ENT,
MiniMed, Navigation) come back "No Results Found". The same portal also answers
findby=brand&brand=<name>, which returns every manual filed under a BRAND —
"Polysorb" gives the suture IFU, "Shiley" 29 airway manuals — but the result
rows carry no model list, so a brand hit is family-level: the device has to be
matched to the right row by TITLE, and the link is recorded as
`brand_family_match`, never `exact_catalog`. A device that matches no row is
left pending: absence from a brand search is not evidence of anything.

Usage:
    python -m resolvers.medtronic_resolver 2150014
    python -m resolvers.medtronic_resolver --batch 500
    python -m resolvers.medtronic_resolver --by-brand --dry-run --batch 8
    python -m resolvers.medtronic_resolver --by-brand --apply --batch 40
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
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from company_targets import TOP_DEVICE_TARGETS
from resolvers.eifu_resolver import (
    FOUND_STATUS,
    INIT_FAILED_STATUS,
    NOT_FOUND_STATUS,
    SQLITE_PATH,
    STABLE_STATUSES,
    DeviceRef,
    classify_error,
    ensure_ifu_links_table,
)
from resolvers.stryker_resolver import ensure_source_file_name_column

BASE_URL = "https://manuals.medtronic.com"
MANUFACTURER_FAMILY = "medtronic"
DEFAULT_DELAY_SEC = 1.5
JITTER_SEC = 1.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TOKEN_RE = re.compile(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"')
# openDialogOptIn('<modal>', <id>, '<key>', '<pdf url>', '<part number>')
_OPTIN_RE = re.compile(
    r"openDialogOptIn\(\s*'[^']*'\s*,\s*\d+\s*,\s*'[^']*'\s*,\s*'([^']+)'\s*,\s*'([^']*)'"
)
_TITLE_RE = re.compile(r"<td[^>]*>\s*([^<>{}]{6,120}?)\s*(?:<br|</td)", re.S)

# --- brand path -------------------------------------------------------------
BRAND_MATCH_CONFIDENCE = "brand_family_match"
SEARCH_PATH = "/manuals/main/en_US/manual/index"
MAX_BRAND_CANDIDATES = 4
MIN_BRAND_LEN = 4
MIN_TOKEN_LEN = 4

# A brand-results row: title is the text of the openDialogOptIn anchor; the
# document number, revision and publication date sit in <p class="smalltext">;
# the manual type is the row's second <td>.
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_ANCHOR_TITLE_RE = re.compile(r"openDialogOptIn\([^)]*\)[^>]*>\s*([^<]+?)\s*</a>", re.S)
_DOC_NUMBER_RE = re.compile(
    r"Manual Document Number:\s*([A-Za-z0-9._-]+)(?:\s+REV\.?\s*([A-Z0-9]+))?", re.I
)
_PUBLISHED_RE = re.compile(r"Website Publication Date:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
_MANUAL_TYPE_RE = re.compile(r"<td>\s*<p>\s*([^<]+?)\s*</p>\s*</td>", re.S)
_TRADEMARK_RE = re.compile(r"[™®©℠Ⓡ]")  # ™ ® © ℠ Ⓡ
_SYSTEM_SUFFIX_RE = re.compile(r"\s+(?:spinal\s+system|systems?)\s*$", re.I)
_WAF_RE = re.compile(r"Access Denied|Request blocked|Incapsula|Pardon Our Interruption", re.I)

# Only the manufacturer's usage instructions count. Service/technical manuals,
# quick references and patient booklets are real documents about the family
# but answer a different question than the one the client asked.
_IFU_TYPE_RE = re.compile(
    r"instructions?\s+for\s+use|\bIFU\b|e-?man(?:ual)?\b|package\s+insert|\bimplant\s+manual\b", re.I
)
# Spine files its surgical-technique guides under "Instructions for Use" too;
# they describe an operation, not the device, and the file name says _ST_.
_TECHNIQUE_RE = re.compile(r"surgical[\s_]+techni|\btechnique\b|_ST(?:_|\.pdf$|\d)", re.I)
_SYSTEM_FALLBACK_TYPE_RE = re.compile(r"clinician\s+manual", re.I)
# Document numbers, revisions and file extensions that leak into file-name titles.
_NOISE_TOKEN_RE = re.compile(r"^(?:rev\w*|pdf|st|[a-z]*\d{3,}[a-z0-9]*)$")

# Brands that are just the company, not a family.
COMPANY_TOKENS = frozenset({"medtronic", "covidien", "inc", "llc", "corp", "corporation"})
# First words that are not a family name; useless as a fallback search.
GENERIC_WORDS = frozenset({"custom", "premium", "standard", "adult", "pediatric", "disposable",
                           "reusable", "sterile", "single", "surgical"})
UNUSABLE_BRANDS = frozenset({"na", "n/a", "none", "not applicable", "unknown", "nan"})
# Tokens that appear in almost every title and so match nothing in particular.
STOP_TOKENS = frozenset({
    "with", "without", "system", "systems", "device", "devices", "manual", "manuals",
    "instructions", "instruction", "insert", "package", "guide", "medtronic", "covidien",
    "emanual", "other", "from", "this", "that", "only", "reference", "user", "users",
    "brand", "type", "size", "sizes", "model", "models", "also", "using", "into", "onto",
    "accessories", "accessory", "products", "product", "series", "spinal", "surgical",
    "technique", "procedure", "labeling", "set", "sets", "eifu", "multilanguage", "sterile",
})


class PortalBlocked(RuntimeError):
    """The portal answered with a WAF/403 page. Stop the run; retrying only digs in."""


class MedtronicResolver:
    def __init__(
        self,
        db_path: str | Path = SQLITE_PATH,
        delay_sec: float = DEFAULT_DELAY_SEC,
    ) -> None:
        self.db_path = Path(db_path)
        self.delay_sec = delay_sec
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor()
        )
        self._last_request_at = 0.0
        self._token: str | None = None
        self._brand_cache: dict[str, list[dict[str, Any]]] = {}
        self.request_count = 0

    def _get(self, path: str, params: dict[str, str] | None = None) -> str:
        wait = self.delay_sec + random.uniform(0.0, JITTER_SEC)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=HEADERS)
        self.request_count += 1
        try:
            with self._opener.open(request, timeout=30) as response:
                html = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise PortalBlocked(f"HTTP {exc.code} from {url}") from exc
            raise
        if _WAF_RE.search(html[:4000]) and "SYNCHRONIZER_TOKEN" not in html:
            raise PortalBlocked(f"WAF page from {url}")
        token = _TOKEN_RE.search(html)
        if token:
            self._token = token.group(1)
        return html

    def ensure_session(self) -> None:
        """Walk region -> country so the app will serve search results.

        The country must be committed with lang=en_US: "en" returns an error
        page ("No Nav") because the session never gets a country.
        """
        if self._token:
            return
        self._get("/manuals/main/region", {"region": "na"})
        if not self._token:
            raise RuntimeError("Medtronic portal did not return a CSRF token.")
        self._get(
            "/manuals/main/country/index",
            {
                "SYNCHRONIZER_TOKEN": self._token,
                "SYNCHRONIZER_URI": "/manuals/main/country/index",
                "country": "US",
                "lang": "en_US",
            },
        )

    def search(self, model_number: str) -> list[dict[str, Any]]:
        self.ensure_session()
        html = self._get(
            "/manuals/main/en_US/manual/index",
            {
                "SYNCHRONIZER_TOKEN": self._token or "",
                "SYNCHRONIZER_URI": "/manuals/main/en_US/manual/index",
                "findby": "model",
                "model": model_number,
            },
        )
        if "No Results Found" in html or "Not Found" in html:
            return []
        return documents_from_results(html)

    def search_brand(self, brand: str) -> list[dict[str, Any]]:
        """Every manual the portal files under this brand (one request per brand string).

        Rows: {title, document_number, manual_type, published, revision, pdf_url}.
        Cached for the run: a brand is looked up once, never per device.
        """
        key = " ".join(brand.split()).lower()
        if key in self._brand_cache:
            return self._brand_cache[key]
        self.ensure_session()
        html = self._get(
            SEARCH_PATH,
            {
                "SYNCHRONIZER_TOKEN": self._token or "",
                "SYNCHRONIZER_URI": SEARCH_PATH,
                "findby": "brand",
                "brand": brand,
            },
        )
        rows = [] if "No Results Found" in html else brand_rows_from_results(html)
        self._brand_cache[key] = rows
        return rows

    def log_brand_results(
        self,
        device: DeviceRef,
        source_url: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Attach family documents to a device. Never writes an outcome-only row:
        a device with no matching row stays pending for a later pass."""
        if not rows:
            return
        ensure_ifu_links_table(self.db_path)
        ensure_source_file_name_column(self.db_path)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            with conn:
                for row in rows:
                    conn.execute(
                        """
                        insert or ignore into ifu_links (
                            device_rowid, primary_di, catalog_number,
                            manufacturer_family, source_url, document_url,
                            document_title, language, revision,
                            match_confidence, retrieved_at, status,
                            first_seen_at, last_checked_at, last_success_at,
                            source_file_name
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            device.rowid, device.primary_di, device.catalog_number,
                            MANUFACTURER_FAMILY, source_url, row["pdf_url"],
                            row["title"], "en", row.get("revision"),
                            BRAND_MATCH_CONFIDENCE, checked_at, FOUND_STATUS,
                            checked_at, checked_at, checked_at,
                            row.get("document_number") or None,
                        ),
                    )
                # The model path's not_found verdict is superseded by a document.
                identifiers = {device.catalog_number}
                if device.model_number and device.model_number.strip():
                    identifiers.add(device.model_number.strip())
                for identifier in identifiers:
                    conn.execute(
                        "delete from ifu_links where catalog_number = ? and document_url is null",
                        (identifier,),
                    )
        finally:
            conn.close()

    def resolve(
        self,
        catalog_number: str,
        model_number: str | None = None,
        device_rowid: int | None = None,
        primary_di: str | None = None,
        log_to_db: bool = True,
    ) -> list[dict[str, Any]]:
        # The model is the identifier Medtronic publishes; fall back to whatever
        # the caller passed as the catalog when a device has no model.
        identifier = (model_number or catalog_number or "").strip()
        if not identifier:
            raise ValueError("a model or catalog number is required.")

        source_url = f"{BASE_URL}/manuals/main/en_US/manual/index"
        error_type = None
        documents: list[dict[str, Any]] = []
        try:
            documents = self.search(identifier)
            status = FOUND_STATUS if documents else NOT_FOUND_STATUS
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            status, error_type = classify_error(exc)
        except Exception as exc:  # noqa: BLE001
            status, error_type = INIT_FAILED_STATUS, type(exc).__name__

        if log_to_db:
            self.log_results(
                # Store the model as the device identifier: these devices have no
                # catalog number, so keying ifu_links on one would orphan them.
                DeviceRef(device_rowid, primary_di, identifier, model_number),
                source_url,
                documents,
                status,
                error_type=error_type,
            )
        return documents

    def log_results(
        self,
        device: DeviceRef,
        source_url: str,
        documents: list[dict[str, Any]],
        status: str,
        error_type: str | None = None,
    ) -> None:
        ensure_ifu_links_table(self.db_path)
        ensure_source_file_name_column(self.db_path)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            with conn:
                if documents:
                    for document in documents:
                        document["status"] = FOUND_STATUS
                        conn.execute(
                            """
                            insert into ifu_links (
                                device_rowid, primary_di, catalog_number,
                                manufacturer_family, source_url, document_url,
                                document_title, language, revision,
                                match_confidence, retrieved_at, status,
                                first_seen_at, last_checked_at, last_success_at,
                                source_file_name
                            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            on conflict(catalog_number, document_url)
                              where document_url is not null
                            do update set
                                document_title = excluded.document_title,
                                revision = excluded.revision,
                                match_confidence = excluded.match_confidence,
                                status = excluded.status,
                                last_checked_at = excluded.last_checked_at,
                                last_success_at = excluded.last_success_at
                            """,
                            (
                                device.rowid, device.primary_di, device.catalog_number,
                                MANUFACTURER_FAMILY, source_url, document["document_url"],
                                document["document_title"], document["language"],
                                document["revision"], document["match_confidence"],
                                checked_at, FOUND_STATUS,
                                checked_at, checked_at, checked_at,
                                document.get("source_file_name"),
                            ),
                        )
                    conn.execute(
                        "delete from ifu_links where catalog_number = ? and document_url is null",
                        (device.catalog_number,),
                    )
                else:
                    if status not in STABLE_STATUSES:
                        status = INIT_FAILED_STATUS
                    conn.execute(
                        """
                        insert into ifu_links (
                            device_rowid, primary_di, catalog_number,
                            manufacturer_family, source_url, retrieved_at, status,
                            first_seen_at, last_checked_at, last_success_at,
                            last_error_at, error_type
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        on conflict(catalog_number) where document_url is null
                        do update set
                            status = excluded.status,
                            last_checked_at = excluded.last_checked_at,
                            last_error_at = excluded.last_error_at,
                            error_type = excluded.error_type
                        """,
                        (
                            device.rowid, device.primary_di, device.catalog_number,
                            MANUFACTURER_FAMILY, source_url, checked_at, status,
                            checked_at, checked_at,
                            checked_at if status == NOT_FOUND_STATUS else None,
                            None if status == NOT_FOUND_STATUS else checked_at,
                            error_type,
                        ),
                    )
        finally:
            conn.close()


def documents_from_results(html: str) -> list[dict[str, Any]]:
    """PDF documents on a Medtronic search-results page.

    The result anchors are href="#"; the real link is the 4th argument of the
    row's openDialogOptIn(...) onclick.
    """
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pdf_url, part_number in _OPTIN_RE.findall(html):
        if pdf_url in seen or not pdf_url.lower().endswith(".pdf"):
            continue
        seen.add(pdf_url)
        documents.append({
            "document_url": pdf_url,
            "document_title": title_for(html, part_number) or part_number or "Medtronic manual",
            "language": "en",
            "revision": revision_for(html, part_number),
            # The portal answers an exact model query and returns "No Results
            # Found" for an unknown one, so a hit is Medtronic asserting that
            # this manual covers this model.
            "match_confidence": "exact_catalog",
            "source_file_name": part_number or None,
        })
    return documents


def title_for(html: str, part_number: str) -> str | None:
    """The manual's human title from the results row.

    A row reads "<title> Document Number: <part> REV. <rev>", so the title is
    the text before "Document Number". Without this the UI would label the
    document with its part number (M708348B466E) instead of "Divergence-L
    Anterior Oblique Lumbar Fusion System Manual".
    """
    if not part_number:
        return None
    row = re.search(
        r"<tr[^>]*>(?:(?!</tr>).)*?" + re.escape(part_number) + r"(?:(?!</tr>).)*?</tr>",
        html,
        re.S,
    )
    if not row:
        return None
    text = " ".join(re.sub(r"<[^>]+>", " ", row.group(0)).split())
    text = text.replace("&nbsp;", " ")
    title = re.split(r"Document Number", text, maxsplit=1)[0].strip()
    return title or None


def revision_for(html: str, part_number: str) -> str | None:
    if not part_number:
        return None
    index = html.find(part_number)
    if index == -1:
        return None
    match = re.search(r"REV\.?\s*([A-Z0-9]+)", html[index:index + 200], re.I)
    return match.group(1) if match else None


def brand_rows_from_results(html: str) -> list[dict[str, Any]]:
    """Rows of a findby=brand results page, one per PDF.

    Unlike the model path this keeps the row's manual type and publication date,
    because a brand search returns the family's whole shelf (IFU, technical
    manual, quick reference...) and only the IFU answers the client.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_html in _ROW_RE.findall(html):
        optin = _OPTIN_RE.search(row_html)
        if not optin:
            continue
        pdf_url, part_number = optin.group(1), optin.group(2)
        if pdf_url in seen or not pdf_url.lower().endswith(".pdf"):
            continue
        seen.add(pdf_url)
        title_match = _ANCHOR_TITLE_RE.search(row_html)
        title = " ".join(title_match.group(1).split()) if title_match else ""
        title = title.replace("&amp;", "&").replace("&nbsp;", " ")
        number = _DOC_NUMBER_RE.search(row_html)
        published = _PUBLISHED_RE.search(row_html)
        manual_type = _MANUAL_TYPE_RE.search(row_html)
        rows.append({
            "title": title_from_file_name(title) or part_number or "Medtronic manual",
            "document_number": (number.group(1) if number else part_number) or None,
            "revision": number.group(2) if number and number.group(2) else None,
            "manual_type": " ".join(manual_type.group(1).split()) if manual_type else "",
            "published": published.group(1) if published else None,
            "pdf_url": pdf_url,
        })
    return rows


def normalise_brand(brand_name: str | None) -> str:
    """GUDID brand as the portal would file it: no ™/®, no "Spinal System" tail.

    Empty when the brand names no family: NA, a bare company name, or something
    too short to be a search term (a 2-letter brand would match everything).
    """
    text = _TRADEMARK_RE.sub(" ", brand_name or "")
    text = " ".join(text.replace("&amp;", "&").split()).strip(" -,.")
    if not text or text.lower() in UNUSABLE_BRANDS:
        return ""
    stripped = _SYSTEM_SUFFIX_RE.sub("", text).strip(" -,.")
    text = stripped or text
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens or tokens[0] in COMPANY_TOKENS or all(t in COMPANY_TOKENS for t in tokens):
        return ""
    if len(text) < MIN_BRAND_LEN:
        return ""
    return text


def brand_queries(brand: str) -> list[str]:
    """Search strings in the order to try: the whole brand, then its first word.

    The first word only when it is a real word: at least MIN_BRAND_LEN characters
    and not the company. A two-letter first word ("CD HORIZON", "NC STORMER")
    takes the second word with it.
    """
    queries = [brand]
    words = brand.split()
    if len(words) > 1:
        first = words[0]
        if len(re.sub(r"[^A-Za-z0-9]", "", first)) < MIN_BRAND_LEN and len(words) > 2:
            first = " ".join(words[:2])
        if (
            len(re.sub(r"[^A-Za-z0-9]", "", first)) >= MIN_BRAND_LEN
            and first.lower() not in COMPANY_TOKENS
            and first.lower() not in GENERIC_WORDS
            and first.lower() != brand.lower()
        ):
            queries.append(first)
    return queries


def significant_tokens(text: str | None) -> list[str]:
    """Ordered, de-duplicated comparison tokens: 4+ characters, not a stop-word,
    not a bare number, lightly de-pluralised so "tubes" meets "tube"."""
    out: list[str] = []
    for token in re.findall(r"[a-z0-9]+", _TRADEMARK_RE.sub(" ", text or "").lower()):
        if len(token) < MIN_TOKEN_LEN or token in STOP_TOKENS or _NOISE_TOKEN_RE.match(token):
            continue
        if len(token) > MIN_TOKEN_LEN and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if token not in out:
            out.append(token)
    return out


def is_ifu_row(row: dict[str, Any]) -> bool:
    file_name = (row.get("pdf_url") or "").rsplit("/", 1)[-1]
    if _TECHNIQUE_RE.search(row.get("title") or "") or _TECHNIQUE_RE.search(file_name):
        return False
    return bool(_IFU_TYPE_RE.search(row.get("manual_type") or "")
                or _IFU_TYPE_RE.search(row.get("title") or "")
                or _IFU_TYPE_RE.search(file_name))


def title_from_file_name(title: str) -> str:
    """"M708348B414E_CD_Horizon_eManual_revJ.pdf" -> "CD Horizon eManual revJ".

    Some rows have no title and the portal shows the file name; strip the
    document-number prefix and extension so the UI shows a name."""
    if not title.lower().endswith(".pdf"):
        return title
    words = title[:-4].replace("_", " ").split()
    if len(words) > 1 and _NOISE_TOKEN_RE.match(words[0].lower()):
        words = words[1:]
    return " ".join(words)


def qualifying_rows(rows: list[dict[str, Any]], device_is_system: bool = False) -> list[dict[str, Any]]:
    """The rows that could serve as the device's IFU.

    Usage instructions win; a clinician manual is accepted only when the family
    publishes no IFU at all and the device is itself a system (a console or
    pump is documented by its clinician manual, an implant never is).
    """
    ifu = [row for row in rows if is_ifu_row(row)]
    if ifu or not device_is_system:
        return ifu
    return [row for row in rows if _SYSTEM_FALLBACK_TYPE_RE.search(row.get("manual_type") or "")]


# Attributes a title and a device cannot both carry: a cuffless IFU is not the
# document for a cuffed tube, an adult document not the one for a neonate.
EXCLUSIVE_GROUPS = (
    frozenset({"adult", "pediatric", "paediatric", "neonatal", "neonate", "infant"}),
)


def contradicts(title_tokens: set[str], device_tokens: set[str]) -> bool:
    """True when the title asserts an attribute the device text contradicts.

    "-less" morphology in either direction: "cuffless" against "cuff"/"cuffed",
    plus the mutually exclusive groups above. Only when both sides speak - a
    title that names no age group contradicts nothing.
    """
    for one, other in ((title_tokens, device_tokens), (device_tokens, title_tokens)):
        for token in one:
            if len(token) > 6 and token.endswith("less") and token not in other:
                stem = token[:-4]
                if stem in other or f"{stem}ed" in other:
                    return True
    for group in EXCLUSIVE_GROUPS:
        in_title = title_tokens & group
        in_device = device_tokens & group
        if in_title and in_device and not (in_title & in_device):
            return True
    return False


def match_rows(
    rows: list[dict[str, Any]],
    brand: str,
    device_text: str,
) -> list[dict[str, Any]]:
    """Which of a brand's qualifying rows document this device.

    One qualifying title (even across several PDFs - Polysorb's IFU is two
    files) is the family document and covers every device of the brand. So is
    a row named after nothing but the brand ("CD Horizon eManual").

    Otherwise the brand search has already asserted the brand (only 1 of 27
    Shiley titles even says "Shiley"), so the match is between the part of the
    title BEYOND the brand and the device's brand+description: at least one
    shared significant token, covering at least half of the title's tokens,
    ranked by most shared then fewest unexplained. Ties are kept (capped)
    because the serving layer ranks across a device's documents. No candidate
    means the device stays pending.
    """
    if not rows:
        return []
    brand_tokens = set(significant_tokens(brand))
    device_tokens = set(significant_tokens(device_text)) | brand_tokens

    titles = {row["title"].strip().lower() for row in rows}
    if len(titles) == 1:
        # The family document — unless the brand spans product lines. "Polysorb" is
        # both a suture and a meniscal stapler, and the suture IFU must not be handed
        # to the stapler: when the title says what the product IS beyond the brand
        # name, the device has to share at least one of those words.
        beyond = set(significant_tokens(rows[0]["title"])) - brand_tokens
        if beyond and not (beyond & device_tokens):
            return []
        return list(rows)

    family: list[dict[str, Any]] = []
    scored: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for row in rows:
        title_tokens = set(significant_tokens(row["title"]))
        if not title_tokens:
            continue
        beyond = title_tokens - brand_tokens
        if not beyond:
            family.append(row)
            continue
        shared = beyond & device_tokens
        # The WHOLE title beyond the brand must be explained by the device text.
        # "More than half" was tried first and attached the TaperGuard tracheal
        # tube IFU to a SealGuard tube — the one unexplained token was the one
        # that mattered. A wrong-but-plausible IFU is worse than a pending device.
        if not shared or shared != beyond:
            continue
        if contradicts(beyond, device_tokens):
            continue
        # And the other way round: the title has to say most of what the device is.
        # "Disposable Inner Cannula" is fully explained by "Tracheostomy Tube Cuffless
        # with Disposable Inner Cannula" but it is the accessory's IFU, not the tube's.
        described = set(significant_tokens(device_text)) - brand_tokens
        if len(described) >= 2 and len(shared & described) * 2 <= len(described):
            continue
        scored.append(((len(shared), -len(beyond - shared), row.get("published") or ""), row))
    specific: list[dict[str, Any]] = []
    if scored:
        best = max(key[:2] for key, _ in scored)
        specific = [row for key, row in sorted(scored, key=lambda item: item[0], reverse=True)
                    if key[:2] == best]
    return (family + specific)[:MAX_BRAND_CANDIDATES]


def medtronic_company_names(conn: sqlite3.Connection) -> list[str]:
    """The devices.company_name values behind company_targets' Medtronic patterns.

    Enumerated through the company index and filtered with SQLite's own LIKE, so
    the device query can use the index instead of a lower(company_name) LIKE
    scan of all 5M rows.
    """
    target = next(t for t in TOP_DEVICE_TARGETS if t["key"] == "medtronic")
    patterns = [str(p) for p in target["company_patterns"]]
    names = [r[0] for r in conn.execute("select distinct company_name from devices") if r[0]]
    return [
        name for name in names
        if any(conn.execute("select lower(?) like ?", (name, p)).fetchone()[0] for p in patterns)
    ]


def load_medtronic_brand_devices(db_path: str | Path = SQLITE_PATH) -> list[dict[str, Any]]:
    """Medtronic devices with a usable brand and no `found` row (on either the
    catalog/model identifier or the raw model number). Read-only connection;
    indexed temp tables because a plain join takes minutes on 2.5M ifu_links."""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=120.0)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        names = medtronic_company_names(conn)
        if not names:
            return []
        placeholders = ",".join("?" * len(names))
        conn.execute(
            f"""
            create temp table med as
            select rowid as device_rowid, company_name, brand_name, model_number,
                   catalog_number, device_description, raw_json,
                   case when catalog_number is not null and trim(catalog_number) != ''
                        then trim(catalog_number) else trim(coalesce(model_number, '')) end
                   as identifier
            from devices where company_name in ({placeholders})
            """,
            names,
        )
        conn.execute("create index med_ident on med(identifier)")
        has_index = conn.execute(
            "select 1 from sqlite_master where type='index' and name='idx_ifu_catalog'"
        ).fetchone()
        hint = "indexed by idx_ifu_catalog" if has_index else ""
        conn.execute(
            f"""
            create temp table found_ids as
            select distinct l.catalog_number from med m
              join ifu_links l {hint} on l.catalog_number = m.identifier
             where l.status = '{FOUND_STATUS}'
            union
            select distinct l.catalog_number from med m
              join ifu_links l {hint} on l.catalog_number = trim(coalesce(m.model_number, ''))
             where l.status = '{FOUND_STATUS}'
            """
        )
        conn.execute("create index found_idx on found_ids(catalog_number)")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select * from med m
            where identifier != ''
              and not exists (select 1 from found_ids f where f.catalog_number = m.identifier)
              and not exists (select 1 from found_ids f
                              where f.catalog_number = trim(coalesce(m.model_number, '')))
            order by brand_name, identifier
            """
        ).fetchall()
    finally:
        conn.close()
    devices: list[dict[str, Any]] = []
    for row in rows:
        brand = normalise_brand(row["brand_name"])
        if not brand:
            continue
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        devices.append({
            "rowid": row["device_rowid"],
            "primary_di": raw.get("PrimaryDI"),
            "identifier": row["identifier"],
            "model_number": row["model_number"],
            "brand_name": row["brand_name"],
            "brand": brand,
            "description": row["device_description"] or "",
        })
    return devices


def group_by_brand(devices: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """(brand, devices) groups, biggest first. Grouped case-insensitively so
    "CD HORIZON Spinal System" and "CD HORIZON® Spinal System" are one lookup."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        groups.setdefault(device["brand"].lower(), []).append(device)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), g[0]["brand"].lower()))
    return [(group[0]["brand"], group) for group in ordered]


BRAND_RETRY_DAYS = 30


def ensure_brand_runs_table(db_path: str | Path = SQLITE_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("""create table if not exists medtronic_brand_runs(
            brand_key text primary key, brand text not null, tried_at text not null,
            rows integer not null, devices integer not null, matched integer not null)""")
        conn.commit()
    finally:
        conn.close()


def recently_tried_brands(db_path: str | Path = SQLITE_PATH,
                          days: int = BRAND_RETRY_DAYS) -> set[str]:
    """Brand keys searched within `days`. The loader only returns devices WITHOUT a
    document, so a brand whose search found nothing (Custom Perfusion, ZUMA) or whose
    devices the titles could not explain would otherwise sit at the top of every batch
    and burn the same request forever. Skipped brands come back after `days` — the
    portal does gain manuals."""
    ensure_brand_runs_table(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=30.0)
    try:
        return {row[0] for row in conn.execute(
            "select brand_key from medtronic_brand_runs where tried_at >= ?", (cutoff,))}
    finally:
        conn.close()


def record_brand_run(db_path: str | Path, brand: str, rows: int, devices: int, matched: int) -> None:
    ensure_brand_runs_table(db_path)
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        with conn:
            conn.execute(
                """insert into medtronic_brand_runs(brand_key, brand, tried_at, rows, devices, matched)
                   values (?, ?, ?, ?, ?, ?)
                   on conflict(brand_key) do update set brand=excluded.brand, tried_at=excluded.tried_at,
                   rows=excluded.rows, devices=excluded.devices, matched=excluded.matched""",
                (brand.lower(), brand, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 rows, devices, matched),
            )
    finally:
        conn.close()


def run_by_brand(
    resolver: MedtronicResolver,
    groups: list[tuple[str, list[dict[str, Any]]]],
    apply: bool,
    samples: int = 15,
) -> dict[str, Any]:
    per_brand = max(4, samples // max(1, len(groups)))
    sampled: set[str] = set()
    """Resolve brand groups; writes only when apply=True. Returns run statistics."""
    stats: dict[str, Any] = {
        "brands": 0, "brands_full": 0, "brands_first_word": 0, "brands_no_rows": 0,
        "devices": 0, "devices_matched": 0, "rows_written": 0,
        "manual_types": Counter(), "candidates": Counter(), "samples": [],
    }
    source_url = f"{BASE_URL}{SEARCH_PATH}?findby=brand"
    for brand, devices in groups:
        stats["brands"] += 1
        stats["devices"] += len(devices)
        rows: list[dict[str, Any]] = []
        used = None
        for query in brand_queries(brand):
            rows = resolver.search_brand(query)
            if rows:
                used = query
                break
        if not rows:
            stats["brands_no_rows"] += 1
            print(f"brand {brand!r} ({len(devices)} devices): no results for {brand_queries(brand)}")
            if apply:
                record_brand_run(resolver.db_path, brand, 0, len(devices), 0)
            continue
        stats["brands_full" if used == brand else "brands_first_word"] += 1
        stats["manual_types"].update(row["manual_type"] or "(none)" for row in rows)
        matched = 0
        brand_samples = 0
        brand_source = f"{source_url}&brand={urllib.parse.quote_plus(used or brand)}"
        for device in devices:
            device_text = f"{device['brand_name']} {device['description']}"
            is_system = "system" in device_text.lower()
            candidates = match_rows(qualifying_rows(rows, is_system), brand, device_text)
            stats["candidates"][len(candidates)] += 1
            if not candidates:
                continue
            matched += 1
            if brand_samples < per_brand and device["identifier"] not in sampled:
                sampled.add(device["identifier"])
                brand_samples += 1
                stats["samples"].append((device, candidates))
            if apply:
                resolver.log_brand_results(
                    DeviceRef(device["rowid"], device["primary_di"], device["identifier"],
                              device["model_number"]),
                    brand_source, candidates,
                )
                stats["rows_written"] += len(candidates)
        stats["devices_matched"] += matched
        if apply:
            record_brand_run(resolver.db_path, brand, len(rows), len(devices), matched)
        types = Counter(row["manual_type"] or "(none)" for row in rows)
        print(
            f"brand {brand!r} ({len(devices)} devices): query={used!r} rows={len(rows)} "
            f"ifu={sum(1 for r in rows if is_ifu_row(r))} types={dict(types)} matched={matched}"
        )
    return stats


def load_medtronic_devices(limit: int, db_path: str | Path = SQLITE_PATH) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select d.rowid, d.company_name, d.brand_name, d.model_number,
                   d.catalog_number, d.raw_json
            from devices d
            where d.model_number is not null
              and trim(d.model_number) != ''
              and (lower(d.company_name) like '%medtronic%'
                   or lower(d.company_name) like '%covidien%')
              and not exists (
                select 1 from ifu_links l
                where l.catalog_number = d.model_number
                  and l.status in ('found', 'candidate_broad', 'not_found')
              )
            order by d.model_number
            limit ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve Medtronic manuals by model number.")
    parser.add_argument("model_number", nargs="?")
    parser.add_argument("--batch", type=int,
                        help="Resolve N unresolved Medtronic devices (N BRANDS with --by-brand).")
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--by-brand", action="store_true",
                        help="Brand path: findby=brand for devices the model search cannot reach.")
    parser.add_argument("--dry-run", action="store_true", help="Brand path: print, write nothing.")
    parser.add_argument("--apply", action="store_true", help="Brand path: write ifu_links rows.")
    parser.add_argument("--retry-all", action="store_true",
                        help=f"Brand path: include brands searched within {BRAND_RETRY_DAYS} days.")
    parser.add_argument("--db", default=str(SQLITE_PATH))
    args = parser.parse_args()

    resolver = MedtronicResolver(db_path=args.db)
    if args.by_brand:
        if args.apply and args.dry_run:
            parser.error("--apply and --dry-run are mutually exclusive.")
        if not args.apply:
            print("dry run: nothing will be written (pass --apply to write)")
        devices = load_medtronic_brand_devices(args.db)
        groups = group_by_brand(devices)
        if not args.retry_all:
            tried = recently_tried_brands(args.db)
            groups = [g for g in groups if g[0].lower() not in tried]
        if args.batch:
            groups = groups[:args.batch]
        print(f"Resolving {sum(len(g) for _, g in groups)} medtronic devices ({len(groups)} brands)")
        try:
            stats = run_by_brand(resolver, groups, apply=args.apply)
        except PortalBlocked as exc:
            print(f"STOP: portal blocked the run ({exc}); {resolver.request_count} requests made")
            raise SystemExit(2)
        print("\nmanual_type distribution (rows across brands searched):")
        for manual_type, count in stats["manual_types"].most_common():
            print(f"  {count:4d}  {manual_type}")
        print("candidates per device:", dict(sorted(stats["candidates"].items())))
        print(f"\nsample device -> document pairs ({len(stats['samples'])}):")
        for device, candidates in stats["samples"]:
            print(f"  [{device['identifier']}] {device['brand_name']} | {device['description'][:90]}")
            for row in candidates:
                print(f"      -> {row['title']} ({row['manual_type']}; {row['document_number']}; {row['pdf_url']})")
        print(
            f"\ndone: {stats['devices_matched']}/{stats['devices']} devices matched across "
            f"{stats['brands']} brands (full-brand hits {stats['brands_full']}, first-word hits "
            f"{stats['brands_first_word']}, no rows {stats['brands_no_rows']}); "
            f"{stats['rows_written']} rows written; {resolver.request_count} portal requests"
        )
        return

    if args.batch:
        rows = load_medtronic_devices(args.batch)
        print(f"Resolving {len(rows)} Medtronic devices")
        found = 0
        for index, row in enumerate(rows, 1):
            raw_json = json.loads(row["raw_json"]) if row["raw_json"] else {}
            documents = resolver.resolve(
                catalog_number=row["catalog_number"] or row["model_number"],
                model_number=row["model_number"],
                device_rowid=row["rowid"],
                primary_di=raw_json.get("PrimaryDI"),
            )
            if documents:
                found += 1
            if index % 25 == 0 or documents:
                print(f"[{index}/{len(rows)}] {row['model_number']}: {len(documents)} docs (found {found})")
        print(f"done: {found}/{len(rows)} devices resolved")
        return

    if not args.model_number:
        parser.error("model_number is required unless --batch is used.")
    documents = resolver.resolve(args.model_number, model_number=args.model_number, log_to_db=not args.no_db)
    print(json.dumps(documents, indent=2)[:1400])


if __name__ == "__main__":
    raise SystemExit(main())
