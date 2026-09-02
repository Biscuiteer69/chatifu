"""Medtronic brand path: findby=brand results matched to devices by title.

Every test builds its own SQLite file under tmp_path and passes db_path
explicitly everywhere; a fresh connection to that file is what gets asserted.
The production database is never opened.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from resolvers.eifu_resolver import DeviceRef, ensure_ifu_links_table
from resolvers.medtronic_resolver import (
    BRAND_MATCH_CONFIDENCE,
    MedtronicResolver,
    brand_queries,
    brand_rows_from_results,
    group_by_brand,
    load_medtronic_brand_devices,
    contradicts,
    match_rows,
    normalise_brand,
    qualifying_rows,
    recently_tried_brands,
    run_by_brand,
    autocomplete_term,
    spelled_as,
    title_from_file_name,
)

DAM = "https://www.medtronic.com/content/dam/emanuals"


def row(title: str, manual_type: str, number: str, published: str = "2024-01-01") -> dict:
    return {
        "title": title,
        "manual_type": manual_type,
        "document_number": number,
        "revision": "A",
        "published": published,
        "pdf_url": f"{DAM}/mitg/{number}.pdf",
    }


POLYSORB = [
    row("Polysorb Coated Braided Synthetic Absorbable Suture", "Instructions for Use", "PT00109678"),
    row("Polysorb Coated Braided Synthetic Absorbable Suture", "Instructions for Use", "PT00180245"),
]
SHILEY = [
    row("Shiley Tracheal Tube Cuffless Reinforced", "Instructions for Use", "PT00200001"),
    row("Shiley Adult Flexible Tracheostomy Tube XLT with TaperGuard Cuff", "Instructions for Use", "PT00200002"),
    row("Shiley Tracheostomy Tube Cuffless Fenestrated", "Instructions for Use", "PT00200003"),
    row("Shiley Tracheal Tube Service Notes", "Technical Manual", "PT00200004"),
]
ZEVO = [
    row("ZEVO Anterior Cervical Plate System", "Technical Manual", "M708348B389A"),
    row("ZEVO Anterior Cervical Plate System Quick Reference", "Quick Reference Guide", "M708348B389B"),
]


class StubResolver(MedtronicResolver):
    """No network: brand -> rows comes from a dict, and every lookup is counted."""

    def __init__(self, db_path: Path, catalogue: dict[str, list[dict]],
                 suggestions: dict[str, list[str]] | None = None) -> None:
        super().__init__(db_path=db_path, delay_sec=0)
        self.catalogue = {k.lower(): v for k, v in catalogue.items()}
        self.suggestions = {k.lower(): v for k, v in (suggestions or {}).items()}
        self.queries: list[str] = []
        self.terms: list[str] = []

    def search_brand(self, brand: str) -> list[dict]:
        self.queries.append(brand)
        return list(self.catalogue.get(brand.lower(), []))

    def suggest_brands(self, term: str) -> list[str]:
        self.terms.append(term)
        return list(self.suggestions.get(term.lower(), []))


def make_db(path: Path, devices: list[tuple[str, str, str, str]]) -> None:
    """devices: (company_name, brand_name, model_number, description)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """create table devices (id integer primary key, company_name text, brand_name text,
           model_number text, catalog_number text, raw_json text, device_description text,
           parent_company text, has_ifu integer)"""
    )
    for company, brand, model, description in devices:
        conn.execute(
            "insert into devices (company_name, brand_name, model_number, catalog_number, raw_json,"
            " device_description) values (?, ?, ?, '', ?, ?)",
            (company, brand, model, json.dumps({"PrimaryDI": f"DI{model}"}), description),
        )
    conn.commit()
    conn.close()
    ensure_ifu_links_table(path)


def links(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("select * from ifu_links order by catalog_number, document_url").fetchall()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "brand.sqlite3"
    make_db(path, [
        ("Covidien LP", "Polysorb", "170096", "Absorbable Single Stitch Reload"),
        ("Covidien LP", "Polysorb", "160511", "Meniscal Stapler Up Curved Loading Unit"),
        ("Covidien LP", "Shiley", "86546", "Tracheal Tube Cuffless Reinforced"),
        ("Covidien LP", "Shiley", "50XLTCP", "Adult Flexible Tracheostomy Tube XLT Cuffed"),
        ("Covidien LP", "Shiley", "ZZ-999", "Humidification Filter"),
        ("MEDTRONIC SOFAMOR DANEK, INC.", "ZEVO™ Anterior Cervical Plate System", "G7714010",
         "SCREW G7714010 ZEVO VAR SD 4.0MM X 10MM"),
        ("MEDTRONIC, INC.", "MEDTRONIC, INC.", "0009900", "Reusable instrument"),
        ("MEDTRONIC, INC.", "NA", "0009901", "Custom tubing pack"),
        ("Acme Devices", "Polysorb", "ACME1", "Not a Medtronic device"),
    ])
    return path


def test_brand_rows_parse_real_page_structure():
    html = """
    <table><tr><th>Title</th><th>Manual Type</th></tr>
    <tr><td><p><a href="#" onclick="openDialogOptIn('/m', 1238394, 'PT00109678_x',
      'https://www.medtronic.com/content/dam/emanuals/mitg/PT00109678_x.pdf', 'PT00109678');">
      Polysorb Coated Braided Synthetic Absorbable Suture</a></p>
      <p class="smalltext">Manual Document Number: PT00109678 REV. A | <a href="#">Applicable to</a>
      <p class="smalltext">Website Publication Date:\n      2024-04-05</p></td>
      <td><p>Instructions for Use</p></td>
      <td class="actions"><a href="#" onclick="openDialogOptIn('/m', 1238394, 'PT00109678_x',
      'https://www.medtronic.com/content/dam/emanuals/mitg/PT00109678_x.pdf', 'PT00109678');">View</a></td></tr>
    </table>"""
    rows = brand_rows_from_results(html)
    assert rows == [{
        "title": "Polysorb Coated Braided Synthetic Absorbable Suture",
        "document_number": "PT00109678",
        "revision": "A",
        "manual_type": "Instructions for Use",
        "published": "2024-04-05",
        "pdf_url": "https://www.medtronic.com/content/dam/emanuals/mitg/PT00109678_x.pdf",
    }]


def test_brand_derivation_strips_marks_and_skips_company_names():
    assert normalise_brand("ZEVO™ Anterior Cervical Plate System") == "ZEVO Anterior Cervical Plate"
    assert normalise_brand("CD HORIZON® Spinal System") == "CD HORIZON"
    assert normalise_brand("Shiley") == "Shiley"
    assert normalise_brand("Medtronic") == ""
    assert normalise_brand("MEDTRONIC, INC.") == ""
    assert normalise_brand("Covidien") == ""
    assert normalise_brand("Medtronic Reusable Instruments") == ""
    assert normalise_brand("NA") == ""
    assert normalise_brand("n/a") == ""
    assert normalise_brand("Z2") == ""
    assert normalise_brand("DLP®") == ""
    assert normalise_brand(None) == ""
    assert brand_queries("ZEVO Anterior Cervical Plate") == ["ZEVO Anterior Cervical Plate", "ZEVO"]
    assert brand_queries("CD HORIZON") == ["CD HORIZON"]
    assert brand_queries("NC STORMER OTW") == ["NC STORMER OTW", "NC STORMER"]
    assert brand_queries("Custom Perfusion") == ["Custom Perfusion"]
    assert brand_queries("Shiley") == ["Shiley"]


def test_single_ifu_title_fans_out_only_where_the_device_shares_a_product_word():
    rows = qualifying_rows(POLYSORB)
    assert match_rows(rows, "Polysorb", "Polysorb Absorbable Single Stitch Reload") == POLYSORB
    # Polysorb is a suture AND a meniscal stapler; the suture IFU says "suture" and the
    # stapler shares no product word with it, so the stapler stays pending.
    assert match_rows(rows, "Polysorb", "Polysorb Meniscal Stapler Loading Unit") == []
    # A title that is nothing but the brand is the family document for everything.
    family = [row("CD Horizon eManual", "Instructions for Use", "M708348B414E")]
    assert match_rows(family, "CD HORIZON", "TRAY 1665000 UNIVERSAL INSTRUMENT UPPER") == family


def test_multi_row_brand_picks_by_shared_title_tokens():
    rows = qualifying_rows(SHILEY)
    picked = match_rows(rows, "Shiley", "Shiley Tracheal Tube Cuffless Reinforced")
    assert [r["document_number"] for r in picked] == ["PT00200001"]
    # "Cuffed" does not say WHICH cuff: the TaperGuard IFU is the closest row, and closest
    # is how a SealGuard tube was handed the TaperGuard IFU. Every title token beyond the
    # brand must be explained by the device, so this one stays pending.
    assert match_rows(rows, "Shiley", "Shiley Adult Flexible Tracheostomy Tube XLT Cuffed") == []
    picked = match_rows(rows, "Shiley", "Shiley Adult Flexible Tracheostomy Tube XLT with TaperGuard Cuff")
    assert [r["document_number"] for r in picked] == ["PT00200002"]
    # Nothing shared beyond the brand -> no candidate, device stays pending.
    assert match_rows(rows, "Shiley", "Shiley Humidification Filter") == []
    # One generic shared token ("Tube") is not a match, and contradicted
    # attributes (cuffed vs cuffless, pediatric vs adult) rule a row out.
    assert match_rows(rows, "Shiley", "Shiley Nasopharyngeal Tube") == []
    assert match_rows(rows, "Shiley", "Shiley Pediatric Flexible Tracheostomy Tube XLT TaperGuard Cuff") == []
    assert match_rows(rows, "Shiley", "Shiley Tracheal Tube Cuffed Reinforced") == []
    # The accessory's IFU is fully explained by the tube's description, but it says
    # only half of what the tube is: the whole device stays pending rather than
    # being served its inner cannula's leaflet.
    with_accessory = rows + qualifying_rows([row("Disposable Inner Cannula", "Instructions for Use", "PT00148008")])
    assert match_rows(with_accessory, "Shiley",
                      "Tracheostomy Tube Cuffless with Disposable Inner Cannula") == []
    # More than four ties are capped.
    many = [row(f"Shiley Tracheal Tube Cuffless Reinforced {i}", "Instructions for Use", f"X{i}")
            for i in range(6)]
    assert len(match_rows(many, "Shiley", "Shiley Tracheal Tube Cuffless Reinforced")) == 4


def test_contradictions():
    assert contradicts({"cuffless", "tube"}, {"cuffed", "tube"})
    assert contradicts({"cuff", "tube"}, {"cuffless", "tube"})
    assert not contradicts({"cuffless", "tube"}, {"cuffless", "tube"})
    assert contradicts({"adult", "tube"}, {"neonatal", "tube"})
    assert not contradicts({"tube"}, {"neonatal", "tube"})


def test_non_ifu_manual_types_are_excluded():
    assert qualifying_rows(SHILEY) == SHILEY[:3]
    assert qualifying_rows(ZEVO) == []
    # A clinician manual is accepted only for a system device with no IFU at all.
    console = [row("InterStim Clinician Programmer", "Clinician Manual", "M1"),
               row("InterStim Patient Programmer", "Patient Manual", "M2")]
    assert qualifying_rows(console, device_is_system=False) == []
    assert qualifying_rows(console, device_is_system=True) == console[:1]
    ifu_and_clinician = console + [row("InterStim System IFU", "Instructions for Use", "M3")]
    assert qualifying_rows(ifu_and_clinician, device_is_system=True) == ifu_and_clinician[2:]


def test_family_document_covers_brand_and_technique_guides_are_not_ifus():
    shelf = [
        row("CD Horizon™ MultiLine™ Connector Spinal System Surgical Technique", "Instructions for Use", "W791"),
        row("M708348B414E_CD_Horizon_eManual_revJ.pdf", "Instructions for Use", "M708348B414E"),
        row("CD Horizon Growth Rod Conversion Set", "Instructions for Use", "B444E"),
        row("CD Horizon ModuLeX Spinal System Driver Surgical Technique", "Technical Manual", "W682"),
    ]
    # Mazor technique guide: the title does not say so, the file name (_ST_) does.
    shelf.append(dict(row("MAZOR ROBOTIC GUIDANCE TLIF PROCEDURE with CD HORIZON MODULEX 5.5",
                          "Instructions for Use", "W282"),
                      pdf_url=f"{DAM}/spinal/M333023W282_Mazor_Robotic_Guidance_TLIF_ST_revA.pdf"))
    shelf = [dict(r, title=title_from_file_name(r["title"])) for r in shelf]
    assert [r["document_number"] for r in qualifying_rows(shelf)] == ["M708348B414E", "B444E"]
    assert shelf[1]["title"] == "CD Horizon eManual revJ"
    q = qualifying_rows(shelf)
    # The eManual names only the brand: every CD HORIZON device gets it...
    assert [r["document_number"] for r in
            match_rows(q, "CD HORIZON", "CD HORIZON Spinal System TRAY 1665000 UNIVERSAL INSTRUMENT")] == ["M708348B414E"]
    # ...and a growth-rod device also gets the growth-rod insert.
    assert [r["document_number"] for r in
            match_rows(q, "CD HORIZON", "CD HORIZON Spinal System GROWTH ROD CONVERSION 5.5")] == ["M708348B414E", "B444E"]


def test_loader_uses_target_patterns_and_skips_unusable_brands(db: Path):
    devices = load_medtronic_brand_devices(db)
    identifiers = {d["identifier"] for d in devices}
    assert identifiers == {"170096", "160511", "86546", "50XLTCP", "ZZ-999", "G7714010"}
    assert "ACME1" not in identifiers  # not a Medtronic company
    assert {d["brand"] for d in devices} == {"Polysorb", "Shiley", "ZEVO Anterior Cervical Plate"}
    groups = group_by_brand(devices)
    assert [(b, len(g)) for b, g in groups] == [("Shiley", 3), ("Polysorb", 2),
                                                ("ZEVO Anterior Cervical Plate", 1)]


def test_run_writes_family_matches_and_never_not_found(db: Path):
    resolver = StubResolver(db, {"Polysorb": POLYSORB, "Shiley": SHILEY, "ZEVO": ZEVO})
    # A stale model-path verdict for a device that is about to get a document.
    resolver.log_results(DeviceRef(1, "DI170096", "170096", "170096"),
                         "https://manuals.medtronic.com/x", [], "not_found")
    assert [r["status"] for r in links(db)] == ["not_found"]

    groups = group_by_brand(load_medtronic_brand_devices(db))
    stats = run_by_brand(resolver, groups, apply=True)

    # One lookup per brand string, never per device; ZEVO fell back to its first word.
    assert resolver.queries == ["Shiley", "Polysorb", "ZEVO Anterior Cervical Plate", "ZEVO"]
    assert stats["brands_full"] == 2 and stats["brands_first_word"] == 1
    assert stats["devices_matched"] == 2

    rows = links(db)
    by_device = {}
    for r in rows:
        by_device.setdefault(r["catalog_number"], []).append(r)
    # Polysorb: the suture reload gets both pages, brand_family_match, not_found row gone;
    # the meniscal stapler shares no product word with the suture IFU and stays pending.
    assert sorted(by_device["170096"], key=lambda r: r["document_url"])[0]["status"] == "found"
    assert {r["document_url"] for r in by_device["170096"]} == {p["pdf_url"] for p in POLYSORB}
    assert "160511" not in by_device
    assert all(r["match_confidence"] == BRAND_MATCH_CONFIDENCE for r in rows)
    assert all(r["status"] == "found" and r["document_url"] for r in rows)
    assert all(r["manufacturer_family"] == "medtronic" for r in rows)
    assert all("findby=brand" in r["source_url"] for r in rows)
    assert by_device["170096"][0]["primary_di"] == "DI170096"
    assert by_device["170096"][0]["source_file_name"] == "PT00109678"
    # Shiley: the fully-explained title is picked; "XLT Cuffed" does not name the
    # TaperGuard cuff so that device stays pending; the filter gets nothing.
    assert [r["document_url"] for r in by_device["86546"]] == [SHILEY[0]["pdf_url"]]
    assert "50XLTCP" not in by_device
    assert "ZZ-999" not in by_device
    # ZEVO has only technical/quick-reference manuals: nothing written, nothing not_found.
    assert "G7714010" not in by_device
    assert not any(r["status"] == "not_found" for r in rows)

    # Re-running is idempotent (insert or ignore).
    run_by_brand(resolver, groups, apply=True)
    assert len(links(db)) == len(rows)

    # Every brand searched is remembered so the next batch skips it for 30 days —
    # including the ones whose devices stayed pending, or they would head every batch.
    assert recently_tried_brands(db) == {"polysorb", "shiley", "zevo anterior cervical plate"}
    assert recently_tried_brands(db, days=-1) == set()


def test_dry_run_does_not_record_brands(db: Path):
    resolver = StubResolver(db, {"Polysorb": POLYSORB})
    run_by_brand(resolver, group_by_brand(load_medtronic_brand_devices(db)), apply=False)
    assert recently_tried_brands(db) == set()


def test_dry_run_writes_nothing(db: Path):
    resolver = StubResolver(db, {"Polysorb": POLYSORB, "Shiley": SHILEY})
    groups = group_by_brand(load_medtronic_brand_devices(db))
    stats = run_by_brand(resolver, groups, apply=False)
    assert stats["devices_matched"] == 2
    assert stats["rows_written"] == 0
    assert links(db) == []


def test_autocomplete_rescues_a_respelled_brand_only_when_the_letters_agree():
    assert autocomplete_term("Ti-Cron") == "cron"          # "ti" is too short to type
    assert autocomplete_term("Paradigm REAL-Time Revel") == "paradigm"
    assert autocomplete_term("CD HORIZON") == "horizon"
    assert autocomplete_term("X2 4.5") is None
    assert spelled_as("Ti-Cron", ["TI CRON", "TICRON PLUS"]) == "TI CRON"
    assert spelled_as("ARTIC-L", ["ARTiC-L 3D Ti", "ARTiC-XL 3D Ti"]) is None
    assert spelled_as("Shiley", []) is None


def test_dead_brand_gets_one_autocomplete_request_then_is_recorded(tmp_path: Path):
    db = tmp_path / "ticron.sqlite3"
    make_db(db, [("Covidien LP", "Ti-Cron", "88863", "Coated Polyester Suture"),
                 ("Covidien LP", "ZUMA", "88864", "Bipolar Forceps")])
    ticron = [row("Ti-Cron Coated Polyester Suture", "Instructions for Use", "PT00300001")]
    resolver = StubResolver(db, {"TI CRON": ticron}, suggestions={"cron": ["TI CRON"], "zuma": []})
    groups = group_by_brand(load_medtronic_brand_devices(db))
    stats = run_by_brand(resolver, groups, apply=True)
    assert resolver.terms == ["cron", "zuma"]
    assert resolver.queries == ["Ti-Cron", "TI CRON", "ZUMA"]
    assert stats["brands_respelled"] == 1 and stats["brands_no_rows"] == 1
    assert [r["document_url"] for r in links(db)] == [ticron[0]["pdf_url"]]
    # Both outcomes are remembered: the respelled hit and the brand that is not here.
    assert recently_tried_brands(db) == {"ti-cron", "zuma"}
