"""Sibling inference: a device inherits the IFU its resolved siblings hold — under strict rules."""
from __future__ import annotations

import json
import sqlite3

import company_targets
from mvp_lookup import STATUS_PRIORITY, row_priority
from resolvers import sibling_inference as si
from resolvers.eifu_resolver import ensure_ifu_links_table
from resolvers.stryker_resolver import ensure_source_file_name_column

TARGET = {"key": "acme", "company_patterns": ["%acme%"]}


def _db(tmp_path):
    db = tmp_path / "t.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("""create table devices (rowid integer primary key, company_name text, brand_name text,
        model_number text, catalog_number text, raw_json text, device_description text)""")
    conn.execute("create table premarket_submissions (primary_di text, submission_number text)")
    conn.commit()
    conn.close()
    ensure_ifu_links_table(db)
    ensure_source_file_name_column(db)
    return db


def _device(conn, rw, brand, cat, di, descr="Implant", company="Acme Ortho Inc", sub=None):
    conn.execute("insert into devices values (?,?,?,?,?,?,?)",
                 (rw, company, brand, None, cat, json.dumps({"PrimaryDI": di}), descr))
    if sub:
        conn.execute("insert into premarket_submissions values (?,?)", (di, sub))


def _found(conn, rw, cat, doc, title, confidence="exact_catalog"):
    conn.execute("""insert into ifu_links (device_rowid, catalog_number, manufacturer_family, source_url,
        document_url, document_title, match_confidence, status, source_file_name)
        values (?,?,?,?,?,?,?,?,?)""", (rw, cat, "acme", "https://ifu.acme/", doc, title, confidence, "found", "f.pdf"))


def _run(db, apply=False):
    company_targets.TOP_DEVICE_TARGETS.append(TARGET)
    try:
        return si.run(apply=apply, only="acme", db_path=db)["targets"]["acme"]
    finally:
        company_targets.TOP_DEVICE_TARGETS.remove(TARGET)


def test_submission_sibling_inherits_the_document_and_clears_not_found(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _device(conn, 1, "TRILOGY", "00-6105-078-26", "d1", sub="K123456")
    _device(conn, 2, "TRILOGY", "00-6110-080-26", "d2", sub="K123456")
    _found(conn, 1, "00-6105-078-26", "https://s3/trilogy.pdf", "Trilogy Acetabular System")
    conn.execute("insert into ifu_links (catalog_number, status) values ('00-6110-080-26', 'not_found')")
    conn.commit()

    entry = _run(db, apply=True)
    assert entry == {"submission_tier": 1, "brand_tier": 0, "eligible": 1, "written": 1}
    rows = conn.execute("select status, match_confidence, document_url, source_file_name, manufacturer_family "
                        "from ifu_links where catalog_number='00-6110-080-26'").fetchall()
    assert rows == [("found", "sibling_inferred", "https://s3/trilogy.pdf", "f.pdf", "acme")]
    # Idempotent: a second run has nothing left to write.
    assert _run(db, apply=True)["written"] == 0


def test_brand_tier_needs_unanimous_well_covered_siblings(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    for i in range(1, 4):
        _device(conn, i, "NATURAL-KNEE", f"6200-16-00{i}", f"d{i}")
        _found(conn, i, f"6200-16-00{i}", "https://s3/nk.pdf", "Natural-Knee II System")
    _device(conn, 4, "NATURAL-KNEE", "6215-04-301", "d4")          # 3 of 4 resolved, one doc
    _device(conn, 5, "LIGASURE", "LF1212", "d5")                     # siblings disagree
    _device(conn, 6, "LIGASURE", "LF1213", "d6")
    _device(conn, 7, "LIGASURE", "LF1214", "d7")
    _device(conn, 8, "LIGASURE", "LF1215", "d8")
    _found(conn, 5, "LF1212", "https://s3/ls1.pdf", "LigaSure gen 1")
    _found(conn, 6, "LF1213", "https://s3/ls2.pdf", "LigaSure gen 2")
    _found(conn, 7, "LF1214", "https://s3/ls2.pdf", "LigaSure gen 2")
    conn.commit()

    entry = _run(db)
    assert entry["brand_tier"] == 1 and entry["eligible"] == 1


def test_sources_that_are_not_ifus_or_collide_are_never_inherited(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    # Same submission each time, so only the source filter can stop the inheritance.
    _device(conn, 1, "MEDPOR", "9539", "d1", sub="K1")                       # 4-char REF: collision-prone
    _found(conn, 1, "9539", "https://s3/reveal.pdf", "Reveal Clinician Manual")
    _device(conn, 2, "MEDPOR", "9540-A", "d2", sub="K1")
    _device(conn, 3, "AERO-LL", "48-1000-10", "d3", sub="K2")
    _found(conn, 3, "48-1000-10", "https://s3/ph.pdf", "Placeholder Document")
    _device(conn, 4, "AERO-LL", "48-1000-11", "d4", sub="K2")
    _device(conn, 5, "BIORCI", "7208656", "d5", sub="K3", descr="BIORCI TORX DRIVER")
    _found(conn, 5, "7208656", "https://s3/csb.pdf", "IFU_10601461_G_Cleaning-Sterilization_EN.pdf")
    _device(conn, 6, "BIORCI", "7207560", "d6", sub="K3", descr="8 X 25 MM BIORCI SCREW")
    _device(conn, 7, "SIGMA", "158100008", "d7", sub="K4")
    _found(conn, 7, "158100008", "https://s3/echelon.pdf", "ECHELON Linear Cutter", "model_portal_match")
    _device(conn, 8, "SIGMA", "960653", "d8", sub="K4")
    conn.commit()

    assert _run(db)["eligible"] == 0


def test_instrument_document_is_not_handed_to_an_implant(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _device(conn, 1, "UNI-ELBOW", "410-3010", "d1", descr="Radio Capitellum, Trial, Handle", sub="K1")
    _found(conn, 1, "410-3010", "https://s3/sizers.pdf", "Unsterile Instruments and Sizers")
    _device(conn, 2, "UNI-ELBOW", "410-0003", "d2", descr="Radio Capitellum Small, Right", sub="K1")
    _device(conn, 3, "UNI-ELBOW", "410-0004", "d3", descr="Radio Capitellum Sizer, Large", sub="K1")
    conn.commit()

    entry = _run(db)
    assert entry["submission_tier"] == 2 and entry["eligible"] == 1


def test_same_ref_at_another_maker_is_not_a_sibling(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _device(conn, 1, "VARIAX", "940013", "d1", company="Stryker Trauma", sub="K1")
    _found(conn, 1, "940013", "https://s3/variax.pdf", "Variax Clavicle System")
    _device(conn, 2, "SIGMA", "940013", "d2", sub="K1")     # Acme reuses the REF
    _device(conn, 3, "SIGMA", "970481", "d3", sub="K1")
    conn.commit()

    assert _run(db)["eligible"] == 0


def test_inferred_rows_rank_below_every_portal_asserted_tier():
    inferred = row_priority({"status": "found", "match_confidence": "sibling_inferred"})
    for confidence in ("exact_catalog", "model_match", "brand_match", "model_portal_match"):
        assert row_priority({"status": "found", "match_confidence": confidence}) < inferred
    assert inferred < row_priority({"status": "fda_summary", "match_confidence": "fda_submission"})
    assert inferred < row_priority({"status": "not_found", "match_confidence": None})
    assert STATUS_PRIORITY[("found", "sibling_inferred")] < STATUS_PRIORITY[("candidate_broad", "search_result")]
