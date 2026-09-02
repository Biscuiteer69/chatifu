"""PMA approved labeling (C suffix) as a servable IFU. Every test uses a tmp SQLite file and
a stubbed HEAD; the production database and accessdata.fda.gov are never touched."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mvp_lookup import STATUS_PRIORITY
from resolvers import fda_resolver as fda
from resolvers.eifu_resolver import ensure_ifu_links_table


def test_labeling_url_follows_the_ssed_folder_with_a_c_suffix():
    assert fda.labeling_url("P200045") == "https://www.accessdata.fda.gov/cdrh_docs/pdf20/P200045C.pdf"
    assert fda.labeling_url("P200045", "002") == "https://www.accessdata.fda.gov/cdrh_docs/pdf20/P200045S002C.pdf"
    assert fda.labeling_url("P030016", 35) == "https://www.accessdata.fda.gov/cdrh_docs/pdf3/P030016S035C.pdf"
    assert fda.labeling_url("P200045", "000") == "https://www.accessdata.fda.gov/cdrh_docs/pdf20/P200045C.pdf"
    assert fda.labeling_url("P860019") is None          # pre-1996: no folder
    assert fda.labeling_url("K200188") is None          # 510(k)s have no approved labeling
    assert fda.labeling_url("") is None


def test_labeling_ranks_below_every_portal_tier_and_above_the_summary():
    labeling = STATUS_PRIORITY[("found", "fda_pma_labeling")]
    assert labeling > STATUS_PRIORITY[("found", "sibling_inferred")]
    assert labeling < STATUS_PRIORITY[("candidate_broad", "search_result")]
    assert labeling < STATUS_PRIORITY[("fda_summary", "fda_submission")]
    assert labeling < STATUS_PRIORITY[("not_found", None)]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "pma.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("""create table devices (id integer primary key, company_name text, brand_name text,
        model_number text, catalog_number text, raw_json text, device_description text)""")
    devices = [
        ("BOSTON SCIENTIFIC CORPORATION", "WATCHMAN", "", "M635WFA1", "00802526572449"),
        ("BOSTON SCIENTIFIC CORPORATION", "WATCHMAN", "", "M635WFA2", "00802526572456"),
        ("Edwards Lifesciences LLC", "SAPIEN", "", "9600TFX", "00690103201014"),
        ("Acme", "Old PMA", "", "OLD1", "00000000000001"),
    ]
    for company, brand, model, catalog, di in devices:
        conn.execute("insert into devices (company_name, brand_name, model_number, catalog_number, raw_json)"
                     " values (?,?,?,?,?)", (company, brand, model, catalog, json.dumps({"PrimaryDI": di})))
    conn.commit()
    ensure_ifu_links_table(path)
    fda.ensure_labeling_tables(conn)
    conn.executemany("insert into pma_supplements values (?,?,?)", [
        ("00802526572449", "P130013", "000"),
        ("00802526572456", "P130013", "035"),
        ("00690103201014", "P140031", "007"),
        ("00000000000001", "P860019", "000"),
    ])
    # Already served by the maker's portal: must not be re-linked to FDA labeling.
    conn.execute("""insert into ifu_links (device_rowid, primary_di, catalog_number, manufacturer_family,
        source_url, document_url, document_title, language, match_confidence, retrieved_at, status,
        first_seen_at, last_checked_at, last_success_at)
        values (3, '00690103201014', '9600TFX', 'edwards', 'x', 'https://edwards/sapien.pdf', 'SAPIEN IFU',
                'en', 'exact_catalog', 'now', 'found', 'now', 'now', 'now')""")
    conn.commit()
    conn.close()
    return path


def test_batch_links_labeling_and_falls_back_from_supplement_to_the_pma(db: Path, monkeypatch):
    heads: list[str] = []

    def fake_head(url: str) -> tuple[str, int]:
        heads.append(url)
        return ("not_found", 0) if url.endswith("P130013S035C.pdf") else ("found", 1000)

    monkeypatch.setattr(fda, "_head", fake_head)
    monkeypatch.setattr(fda.time, "sleep", lambda s: None)

    stats = fda.resolve_labeling_batch(10, db)
    # SAPIEN already has a servable IFU -> not pending; the pre-1996 PMA has no URL.
    assert stats == {"documents": 3, "found": 2, "devices_linked": 2}
    assert heads == [
        "https://www.accessdata.fda.gov/cdrh_docs/pdf13/P130013C.pdf",
        "https://www.accessdata.fda.gov/cdrh_docs/pdf13/P130013S035C.pdf",
    ]

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["catalog_number"]: r for r in conn.execute(
        "select * from ifu_links where manufacturer_family = 'fda_pma_labeling'")}
    assert set(rows) == {"M635WFA1", "M635WFA2"}
    assert rows["M635WFA1"]["document_url"].endswith("/P130013C.pdf")
    assert rows["M635WFA1"]["document_title"] == "FDA-approved labeling (PMA P130013)"
    # Supplement 35 has no labeling of its own; its device gets the PMA's, labelled as such.
    assert rows["M635WFA2"]["document_url"].endswith("/P130013C.pdf")
    assert rows["M635WFA2"]["document_title"] == "FDA-approved labeling (PMA P130013 supplement 35)"
    assert all(r["status"] == "found" and r["match_confidence"] == "fda_pma_labeling" for r in rows.values())
    assert rows["M635WFA1"]["source_file_name"] == "P130013C.pdf"
    cached = {r[0]: r[1] for r in conn.execute("select doc_key, status from fda_labeling")}
    assert cached == {"P130013": "found", "P130013S035": "not_found", "P860019": "no_url"}

    # Second run: everything is cached or linked, nothing is fetched again.
    heads.clear()
    assert fda.resolve_labeling_batch(10, db) == {"documents": 0, "found": 0, "devices_linked": 0}
    assert heads == []


def test_company_filter_limits_the_batch(db: Path, monkeypatch):
    monkeypatch.setattr(fda, "_head", lambda url: ("found", 1))
    monkeypatch.setattr(fda.time, "sleep", lambda s: None)
    stats = fda.resolve_labeling_batch(10, db, companies=["%boston scientific%"])
    assert stats["documents"] == 2 and stats["devices_linked"] == 2


def test_throttle_answers_are_not_cached_and_stop_the_batch(db: Path, monkeypatch):
    """accessdata answers 403 when it throttles (verified 2026-09-02: 831 of them, all 200 later).
    A throttled key must stay pending, and three in a row must end the batch."""
    heads: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr(fda, "_head", lambda url: (heads.append(url), ("http_403", 0))[1])
    monkeypatch.setattr(fda.time, "sleep", lambda s: sleeps.append(s))
    conn = sqlite3.connect(db)
    conn.execute("insert into devices (company_name, brand_name, model_number, catalog_number, raw_json)"
                 " values ('Acme', 'Newer', '', 'NEW1', ?)", (json.dumps({"PrimaryDI": "00000000000002"}),))
    conn.execute("insert into pma_supplements values ('00000000000002', 'P150001', '000')")
    conn.execute("insert into device_di select id, '00000000000002', 'NEW1' from devices where catalog_number='NEW1'")
    conn.commit()
    conn.close()

    stats = fda.resolve_labeling_batch(10, db)
    assert stats["found"] == 0
    # First key throttled -> no fallback HEAD for the PMA, backoff, next key; the pre-1996
    # PMA needs no request and does not count as the throttle lifting; third HEAD stops.
    assert len(heads) == 3
    assert sleeps == [fda.THROTTLE_BACKOFF_SEC, 2 * fda.THROTTLE_BACKOFF_SEC]
    conn = sqlite3.connect(db)
    cached = {r[0]: r[1] for r in conn.execute("select doc_key, status from fda_labeling")}
    assert not any(v.startswith("http_") for v in cached.values())
    assert conn.execute("select count(*) from ifu_links where manufacturer_family='fda_pma_labeling'").fetchone()[0] == 0
    # Everything the throttle hid is still pending for the next run.
    assert len(fda._pending_labeling(conn, 10, None)) == 4


def test_404_is_a_verdict_and_other_statuses_are_not():
    assert not fda.is_transient("not_found") and not fda.is_transient("found") and not fda.is_transient("no_url")
    assert fda.is_transient("http_403") and fda.is_transient("http_503") and fda.is_transient("error:URLError")
    assert not fda.is_transient("http_404")
