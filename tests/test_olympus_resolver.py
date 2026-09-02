"""Pure-function tests for the Olympus index-mirror resolver.

Every test builds its own SQLite file under tmp_path and passes that path explicitly to
every resolver call; assertions reopen the tmp DB with a fresh connection. Nothing here
may touch chatifu.sqlite3, and there is no network: the index is a literal dict.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from resolvers.eifu_resolver import SQLITE_PATH, ensure_ifu_links_table
from resolvers import olympus_resolver as OR


def _doc(url: str, title: str, article: str = "", material: str = "",
         models: list[str] | None = None, name: str = "",
         file_name: str = "x.pdf") -> dict:
    return {
        "id": url[-4:], "title": title, "name": name, "url": url, "file_name": file_name,
        "hierarchy": "Instructions for Use", "version": "1.0",
        "article_nos": OR._split_codes(article),
        "material_nos": OR._split_codes(material),
        "model_names": OR._split_codes(models),
    }


INDEX = {
    "fetched_at": "2026-09-02T00:00:00+00:00",
    "docs": [
        _doc("https://cdn.example/asset/1/aaa", "GIF-H190 Instructions for Use EN",
             article="GIF-H190, N5405430", models=["GIF-H190"], name="GIF-H190",
             file_name="EN-GIF-H190.pdf"),
        _doc("https://cdn.example/asset/1/bbb", "Suction instruments IFU EN",
             models=["WT000802", "70338008"], name="Suction instruments"),
        _doc("https://cdn.example/asset/1/ccc", "Symbol glossary", article="#"),
    ],
}


def _make_db(tmp_path: Path, devices: list[tuple]) -> Path:
    db = tmp_path / "test.sqlite3"
    assert db.resolve() != Path(SQLITE_PATH).resolve()
    conn = sqlite3.connect(db)
    conn.execute(
        """create table devices (rowid integer primary key, company_name text,
           brand_name text, model_number text, catalog_number text, raw_json text,
           device_description text)""")
    conn.executemany("insert into devices values (?,?,?,?,?,?,?)", devices)
    conn.commit()
    conn.close()
    ensure_ifu_links_table(db)
    return db


def _rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("select * from ifu_links order by id").fetchall()
    finally:
        conn.close()


def _raw(di: str) -> str:
    return json.dumps({"PrimaryDI": di})


def test_model_match_writes_one_row_per_device_keyed_on_model(tmp_path):
    db = _make_db(tmp_path, [
        (1, "OLYMPUS MEDICAL SYSTEMS CORP.", "EVIS", " gif-h190 ", "", _raw("0401"),
         "GASTROVIDEOSCOPE"),
        (2, "Gyrus ACMI, LLC", "Gyrus", "70338008", None, _raw("0402"), "SUCTION TUBE"),
    ])
    matches = OR.match_devices(INDEX, db)
    assert [(m["rowid"], m["identifier"], m["confidence"]) for m in matches] == [
        (1, "gif-h190", "model_portal_match"),
        (2, "70338008", "model_portal_match"),
    ]
    assert OR.write_matches(matches, db) == 2
    rows = _rows(db)
    assert len(rows) == 2
    r = rows[0]
    assert r["catalog_number"] == "gif-h190"          # the identifier the client sends
    assert r["device_rowid"] == 1 and r["primary_di"] == "0401"
    assert r["status"] == "found" and r["manufacturer_family"] == "olympus"
    assert r["document_url"] == "https://cdn.example/asset/1/aaa"
    assert r["document_title"] == "GIF-H190 Instructions for Use EN"
    assert r["source_file_name"] == "EN-GIF-H190.pdf"
    assert r["language"] == "en" and r["match_confidence"] == "model_portal_match"
    assert "olympus-europa.com/SolrRestService/select" in r["source_url"]
    assert "GIF-H190" in r["source_url"]
    for col in ("first_seen_at", "last_checked_at", "last_success_at", "retrieved_at"):
        assert r[col] and r[col].startswith("20")
    # Idempotent: the unique (catalog_number, document_url) index makes a rerun a no-op.
    assert OR.write_matches(matches, db) == 0
    assert len(_rows(db)) == 2


def test_device_with_no_match_gets_no_row(tmp_path):
    db = _make_db(tmp_path, [
        (1, "OLYMPUS Winter & Ibe GmbH", "Olympus", "WA22707S", "WA22707S", _raw("0403"), ""),
        (2, "Gyrus ACMI, LLC", "Gyrus", "#", "", _raw("0404"), "placeholder key"),
    ])
    matches = OR.match_devices(INDEX, db)
    assert matches == []
    OR.write_matches(matches, db)
    assert _rows(db) == []          # never not_found: absence from a mirror is not evidence


def test_catalog_equality_yields_exact_catalog_and_supersedes_outcome_row(tmp_path):
    db = _make_db(tmp_path, [
        (1, "OLYMPUS MEDICAL SYSTEMS CORP.", "EVIS", "GIF-H190", "N5405430", _raw("0405"),
         "GASTROVIDEOSCOPE"),
    ])
    conn = sqlite3.connect(db)
    conn.execute(
        "insert into ifu_links (catalog_number, status, manufacturer_family) values (?,?,?)",
        ("N5405430", "not_found", "eifu_sweep"))
    conn.commit()
    conn.close()
    matches = OR.match_devices(INDEX, db)
    assert len(matches) == 1
    assert matches[0]["confidence"] == "exact_catalog"
    assert matches[0]["identifier"] == "N5405430"
    OR.write_matches(matches, db)
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "found"
    assert rows[0]["match_confidence"] == "exact_catalog"
    assert rows[0]["catalog_number"] == "N5405430"


def test_limit_and_company_filter(tmp_path):
    db = _make_db(tmp_path, [
        (1, "Medtronic", "x", "GIF-H190", "", _raw("1"), ""),
        (2, "OLYMPUS CORPORATION", "x", "GIF-H190", "", _raw("2"), ""),
        (3, "Gyrus Acmi, Llc", "x", "GIF-H190", "", _raw("3"), ""),
    ])
    assert [m["rowid"] for m in OR.match_devices(INDEX, db)] == [2, 3]
    assert [m["rowid"] for m in OR.match_devices(INDEX, db, limit=1)] == [2]


def test_split_codes_and_normalise():
    assert OR._split_codes("EGNA-403D-2021, NA-403D-2021") == ["EGNA-403D-2021", "NA-403D-2021"]
    assert OR._split_codes(["MAJ-2056", "MAJ-2056"]) == ["MAJ-2056", "MAJ-2056"]
    assert OR._split_codes(None) == []
    assert OR.normalise(" gif-h190 ") == "GIF-H190"
    assert not OR._usable_key("#") and OR._usable_key("A20")


def test_filter_page_keeps_only_linked_ifus_and_counts_the_rest():
    raw = [
        {"IN_HIERARCHY": "Instructions for Use", "IN_LINK": "https://cdn.example/asset/1/abc",
         "document_assetTitle_s": "NA-403D-2021 Instructions for Use EN Version 4.0",
         "IN_NAME": "NA-403D-2021", "document_articleNo_s": "EGNA-403D-2021, NA-403D-2021",
         "document_materialNo_s": "PN0009932", "document_fileName_s": "EN-PN0009932.pdf",
         "document_version_s": "4.0"},
        {"IN_HIERARCHY": "Instructions for Use", "IN_LINK": "https://cdn.example/asset/1/",
         "IN_NAME": "INSTRUMENT TRAY"},                       # bare asset dir: no file
        {"IN_HIERARCHY": "Posters / Roll-ups", "IN_LINK": "https://cdn.example/asset/1/def",
         "IN_NAME": "poster"},                                # not an IFU
        {"IN_HIERARCHY": "Instructions for Use", "IN_LINK": "https://cdn.example/asset/1/ghi",
         "document_assetTitle_s": "SYMBOL SUPPLEMENT TO INSTRUCTIONS FOR USE EN Version 02",
         "document_articleNo_s": "WA22351A, A22053A"},       # glossary, not a device IFU
    ]
    kept, counts, no_link = OR.filter_page(raw)
    assert counts == {"Instructions for Use": 3, "Posters / Roll-ups": 1,
                      "excluded: not a device IFU": 1}
    assert no_link == 1
    assert len(kept) == 1
    d = kept[0]
    assert d["article_nos"] == ["EGNA-403D-2021", "NA-403D-2021"]
    assert d["material_nos"] == ["PN0009932"] and d["model_names"] == []
    assert d["title"].startswith("NA-403D-2021") and d["file_name"] == "EN-PN0009932.pdf"
