"""Mirrored Qarad tenants (Highridge). Tmp SQLite + stubbed HTTP; no portal, no production DB."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from resolvers import qarad_tenants as qt
from resolvers.eifu_resolver import ensure_ifu_links_table
from resolvers.stryker_resolver import item_matches_catalog


def test_highridge_ref_attribute_is_recognised():
    item = {"keyCode": "14-590091_00880304797840",
            "attributes": [{"name": "Reference - Catalog number", "value": "14-590091"},
                           {"name": "UDI-DI number", "value": "00880304797840"}]}
    assert item_matches_catalog(item, "14-590091")
    assert not item_matches_catalog(item, "14-590092")


PRODUCTS = [
    {"label": "POLARIS 4.75 INSTRUMENT TRAY A", "id": 11, "keyCode": "14-590091_00880304797840",
     "businessUnit": "Highridge", "productType": "MEDDEV", "ref": "14-590091", "di": "00880304797840",
     "attributes": [{"name": "Reference - Catalog number", "value": "14-590091"}]},
    {"label": "Cannulated Polyaxial Screw", "id": 12, "keyCode": "3505-4530_00889024335561",
     "businessUnit": "Highridge", "productType": "MEDDEV", "ref": "3505-4530", "di": "00889024335561",
     "attributes": [{"name": "Reference - Catalog number", "value": "3505-4530"}]},
    {"label": "Polaris Spinal System Instructions for Use", "id": 13, "keyCode": "060505-02_keycode_060505-02",
     "businessUnit": "Highridge", "productType": "MEDDEV", "ref": "060505-02", "di": None,
     "attributes": [{"name": "Key-Code", "value": "060505-02"}]},
]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "hr.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("""create table devices (id integer primary key, company_name text, brand_name text,
        model_number text, catalog_number text, raw_json text, device_description text)""")
    rows = [
        # matched by DI (catalog differs in punctuation from the portal's)
        ("BIOMET SPINE LLC", "Polaris", "", "14 590091", "00880304797840"),
        # matched by REF only (GUDID has no catalog; model carries the REF)
        ("ZIMMER SPINE, INC.", "PathFinder", "3505-4530", "", "00000000000099"),
        # not on the portal at all
        ("BIOMET SPINE LLC", "Lineum", "", "99-000001", "00000000000001"),
        # already served by another family: not pending
        ("BIOMET SPINE LLC", "Polaris", "", "14-590096", "00880304798052"),
        # other maker: never selected
        ("ZIMMER BIOMET INC", "Persona", "", "14-590091", "00880304797840"),
    ]
    for company, brand, model, catalog, di in rows:
        conn.execute("insert into devices (company_name, brand_name, model_number, catalog_number, raw_json)"
                     " values (?,?,?,?,?)", (company, brand, model, catalog, json.dumps({"PrimaryDI": di})))
    conn.commit()
    ensure_ifu_links_table(path)
    conn.execute("""insert into ifu_links (device_rowid, primary_di, catalog_number, manufacturer_family,
        source_url, document_url, document_title, language, match_confidence, retrieved_at, status,
        first_seen_at, last_checked_at, last_success_at)
        values (4, '00880304798052', '14-590096', 'zimmer_biomet', 'x', 'https://z/p.pdf', 'Polaris',
                'en', 'exact_catalog', 'now', 'found', 'now', 'now', 'now')""")
    # A stub-portal miss from the old family must NOT hide the device from this tenant.
    conn.execute("""insert into ifu_links (device_rowid, primary_di, catalog_number, manufacturer_family,
        source_url, status, first_seen_at, last_checked_at)
        values (1, '00880304797840', '14 590091', 'zimmer_biomet', 'x', 'not_found', 'now', 'now')""")
    conn.execute("""insert into ifu_links (device_rowid, primary_di, catalog_number, manufacturer_family,
        source_url, status, first_seen_at, last_checked_at)
        values (3, '00000000000001', '99-000001', 'zimmer_biomet', 'x', 'not_found', 'now', 'now')""")
    conn.commit()
    conn.close()
    return path


def test_mirror_loader_joins_by_di_then_ref_and_ignores_other_families_misses(db: Path):
    pairs, pending = qt.load_mirror_devices("highridge", 10, PRODUCTS, db_path=db)
    assert pending == 3
    got = {row["ident"]: (product or {}).get("id") for row, product in pairs}
    assert got == {"14 590091": 11, "3505-4530": 12, "99-000001": None}


def test_resolve_mirrored_makes_one_detail_request_per_held_device(db: Path, monkeypatch):
    calls: list[str] = []

    def fake_request(self, url, payload=None):
        calls.append(url)
        if url.endswith("/business-units"):
            return {"items": [{"slug": "Highridge", "id": 1}]}
        if url.endswith("/product-types"):
            return {"items": [{"slug": "MEDDEV", "id": 4}]}
        assert "/products/11?" in url, url
        return {"name": "POLARIS 4.75 INSTRUMENT TRAY A", "documentTypes": [{
            "name": "Instructions for Use",
            "documents": [{"name": "Polaris 4.75 Spinal System Instructions for Use", "files": [{
                "name": "060505-03-US-en Rev07 Polaris 4.75.pdf", "version": "07",
                "languages": [{"isoCode": "en"}], "historical": False, "latestVersion": True,
                "documentUrl": "https://s3/x/060505-03.pdf?X-Amz-Signature=abc"}]}]}]}

    monkeypatch.setattr(qt.QaradTenantResolver, "_request", fake_request)
    monkeypatch.setattr(qt.time, "sleep", lambda s: None)
    resolver = qt.QaradTenantResolver("highridge", db_path=db)
    pairs, _pending = qt.load_mirror_devices("highridge", 10, PRODUCTS, db_path=db)
    by_ident = {row["ident"]: (row, product) for row, product in pairs}

    docs = resolver.resolve_mirrored(*by_ident["14 590091"])
    assert len(docs) == 1 and docs[0]["match_confidence"] == "exact_catalog"
    assert not resolver.resolve_mirrored(*by_ident["99-000001"])   # absent: no request at all
    detail_calls = [c for c in calls if "/products/" in c]
    assert len(detail_calls) == 1

    conn = sqlite3.connect(db)
    rows = conn.execute("select catalog_number, status, document_url from ifu_links "
                        "where manufacturer_family = 'highridge' order by catalog_number").fetchall()
    # The absent device's old zimmer_biomet miss is now Highridge's miss (one row, re-owned),
    # and the found device's stale miss row is gone.
    assert rows == [("14 590091", "found", "https://s3/x/060505-03.pdf"),
                    ("99-000001", "not_found", None)]
    assert conn.execute("select count(*) from ifu_links where manufacturer_family = 'zimmer_biomet' "
                        "and status = 'not_found'").fetchone()[0] == 0
    # Both are now settled for this tenant; only the REF-matched device is still pending.
    pairs, pending = qt.load_mirror_devices("highridge", 10, PRODUCTS, db_path=db)
    assert pending == 1 and pairs[0][0]["ident"] == "3505-4530"


def test_mirror_products_pages_with_the_empty_wildcard(monkeypatch, tmp_path: Path):
    pages = {
        0: {"pageable": {"totalPages": 2}, "items": [{"id": 1, "keyCode": "A_1", "businessUnit": "H",
            "productType": "M", "attributes": [{"name": "Product Description - Name", "value": "Rod"},
                                                {"name": "Reference - Catalog number", "value": "A-1"},
                                                {"name": "UDI-DI number", "value": "1"}]}]},
        1: {"pageable": {"totalPages": 2}, "items": [{"id": 2, "keyCode": "k_keycode_k", "businessUnit": "H",
            "productType": "M", "attributes": [{"name": "Key-Code", "value": "k"}]}]},
    }
    seen: list[dict] = []

    def fake_request(self, url, payload=None):
        seen.append(payload)
        page = int(url.split("page=")[1].split("&")[0])
        return pages[page]

    monkeypatch.setattr(qt.QaradTenantResolver, "_request", fake_request)
    monkeypatch.setattr(qt.time, "sleep", lambda s: None)
    resolver = qt.QaradTenantResolver("highridge", db_path=tmp_path / "x.sqlite3")
    products = qt.mirror_products(resolver, 1, 4)
    assert [p["attributes"][0]["slug"] for p in seen] == ["cross-field-search"] * 2
    assert all(p["attributes"][0]["value"] == "" for p in seen)
    assert [(p["id"], p["ref"], p["di"], p["label"]) for p in products] == [
        (1, "A-1", "1", "Rod"), (2, None, None, "k_keycode_k")]
