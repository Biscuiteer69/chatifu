"""Model-keyed Qarad tenants (Alcon): one search per model, written to every catalog."""
from __future__ import annotations

import json
import sqlite3

from resolvers.eifu_resolver import ensure_ifu_links_table
from resolvers.qarad_tenants import QaradTenantResolver, load_model_groups
from resolvers.stryker_resolver import item_matches_catalog


def test_model_number_attribute_counts_as_the_products_ref():
    # Alcon's tenant carries "Model Number", not a REF; before it was recognised every hit
    # fell through to keyCode and was rejected (3 models -> 274 false negatives).
    item = {"keyCode": "106008535",
            "attributes": [{"name": "Model Number", "value": "SN6AT8"},
                           {"name": "Product Name", "value": "AcrySof IQ Toric"}]}
    assert item_matches_catalog(item, "SN6AT8")
    assert not item_matches_catalog(item, "SN6AT8.060")


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""create table devices (rowid integer primary key, company_name text, brand_name text,
        model_number text, catalog_number text, raw_json text)""")
    rows = [("Alcon Laboratories, Inc.", "AcrySof", "SN6AT8", f"SN6AT8.{p:03d}",
             json.dumps({"PrimaryDI": f"0038000{p:04d}"})) for p in (60, 65, 80)]
    rows.append(("Alcon Laboratories, Inc.", "Clareon", "CCAET0", "CCAET0.170", json.dumps({"PrimaryDI": "1"})))
    conn.executemany("insert into devices(company_name, brand_name, model_number, catalog_number, raw_json) "
                     "values(?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    ensure_ifu_links_table(db_path)


def test_groups_are_biggest_first_and_skip_resolved(tmp_path):
    db = tmp_path / "t.sqlite3"
    _seed(db)
    groups = load_model_groups("alcon", 10, db_path=db)
    assert [(m, len(members)) for _c, m, members in groups] == [("SN6AT8", 3), ("CCAET0", 1)]


def test_one_hit_is_written_to_every_catalog_of_the_model(tmp_path):
    db = tmp_path / "t.sqlite3"
    _seed(db)
    resolver = QaradTenantResolver("alcon", db_path=db)
    resolver.search = lambda term, company=None: [  # type: ignore[method-assign]
        {"keyCode": "x", "attributes": [{"name": "Model Number", "value": term}]}]
    resolver.ifu_documents = lambda item: [{  # type: ignore[method-assign]
        "document_url": "https://s3/alcon/doc.pdf", "document_title": "AcrySof IQ Toric",
        "language": "en", "revision": "3", "match_confidence": "exact_catalog",
        "source_file_name": "toric.pdf"}]
    company, model, members = load_model_groups("alcon", 1, db_path=db)[0]
    docs = resolver.resolve_model_group(company, model, members)
    assert len(docs) == 1

    conn = sqlite3.connect(db)
    rows = conn.execute("select catalog_number, status, match_confidence, source_url from ifu_links "
                        "order by catalog_number").fetchall()
    assert [r[0] for r in rows] == ["SN6AT8.060", "SN6AT8.065", "SN6AT8.080"]
    # Never exact_catalog: the portal asserted the model, GUDID supplied the catalog link.
    assert {r[1:3] for r in rows} == {("found", "model_portal_match")}
    assert rows[0][3].startswith("https://ifu.alcon.com/")
    # The group is settled, so the next batch moves on to the next model.
    assert [m for _c, m, _ in load_model_groups("alcon", 10, db_path=db)] == ["CCAET0"]
