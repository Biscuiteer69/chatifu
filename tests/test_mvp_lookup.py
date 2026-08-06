from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import mvp_lookup


def create_ifu_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE ifu_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_number TEXT,
                status TEXT,
                match_confidence TEXT,
                document_title TEXT,
                document_url TEXT,
                language TEXT,
                revision TEXT,
                retrieved_at TEXT,
                last_checked_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def insert_row(
    db_path: Path,
    catalog: str,
    status: str,
    confidence: str | None = None,
    title: str | None = None,
    url: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ifu_links (
                catalog_number, status, match_confidence, document_title,
                document_url, language, revision, retrieved_at, last_checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                catalog,
                status,
                confidence,
                title,
                url,
                "en" if url else None,
                "A" if url else None,
                "2026-05-26T01:46:09+00:00",
                "2026-05-26T01:46:09+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class FakeResolver:
    calls = 0

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def resolve(self, catalog_number: str) -> list[dict[str, object]]:
        type(self).calls += 1
        insert_row(
            self.db_path,
            catalog_number,
            "found",
            "model_match",
            "Resolved title",
            "https://www.e-ifu.com/viewpdf-iframe/resolved",
        )
        return []


class MvpLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "chatifu.sqlite3"
        create_ifu_table(self.db_path)
        FakeResolver.calls = 0

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_found_model_match_row_is_best_result(self) -> None:
        insert_row(
            self.db_path,
            "GIB00U0340",
            "candidate_broad",
            "search_result",
            "Broad title",
            "https://www.e-ifu.com/viewpdf-iframe/broad",
        )
        insert_row(
            self.db_path,
            "GIB00U0340",
            "found",
            "model_match",
            "TECNIS Eyhance SmartLOAD GIB00",
            "https://www.e-ifu.com/viewpdf-iframe/model",
        )

        result = mvp_lookup.lookup_catalog("GIB00U0340", self.db_path)

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["match_confidence"], "model_match")
        self.assertEqual(result["document_title"], "TECNIS Eyhance SmartLOAD GIB00")

    def test_candidate_broad_rows_include_warning_and_candidates(self) -> None:
        insert_row(self.db_path, "BROAD", "candidate_broad", "search_result", "Candidate 1", "https://example.test/1")
        insert_row(self.db_path, "BROAD", "candidate_broad", "search_result", "Candidate 2", "https://example.test/2")

        result = mvp_lookup.lookup_catalog("BROAD", self.db_path)

        self.assertEqual(result["status"], "candidate_broad")
        self.assertIn("Verify broad matches", result["warning"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_not_found_row_renders_clearly(self) -> None:
        insert_row(self.db_path, "NONE", "not_found")

        result = mvp_lookup.lookup_catalog("NONE", self.db_path)
        text = mvp_lookup.format_human(result)

        self.assertEqual(result["status"], "not_found")
        self.assertIn("Status: not_found", text)
        self.assertNotIn("URL:", text)

    def test_json_output_is_valid_json(self) -> None:
        insert_row(self.db_path, "GIB00U0340", "found", "model_match", "Title", "https://example.test/doc")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = mvp_lookup.main(["--catalog", "GIB00U0340", "--db", str(self.db_path), "--json"])

        self.assertEqual(exit_code, 0)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["catalog_number"], "GIB00U0340")
        self.assertEqual(parsed["status"], "found")

    def test_cached_result_path_does_not_call_resolver(self) -> None:
        insert_row(self.db_path, "GIB00U0340", "found", "model_match", "Cached", "https://example.test/cached")

        result = mvp_lookup.lookup_catalog(
            "GIB00U0340",
            self.db_path,
            resolver_factory=lambda path: FakeResolver(path),
        )

        self.assertEqual(result["document_title"], "Cached")
        self.assertEqual(FakeResolver.calls, 0)

    def test_refresh_path_calls_resolver(self) -> None:
        insert_row(self.db_path, "GIB00U0340", "found", "model_match", "Cached", "https://example.test/cached")

        result = mvp_lookup.lookup_catalog(
            "GIB00U0340",
            self.db_path,
            refresh=True,
            resolver_factory=lambda path: FakeResolver(path),
        )

        self.assertEqual(FakeResolver.calls, 1)
        self.assertEqual(result["status"], "found")


class SearchDevicesTests(unittest.TestCase):
    def setUp(self) -> None:
        import sqlite3
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "chatifu.sqlite3"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                -- Mirrors the real schema, including the columns migrate_search_index.py
                -- adds. The fixture previously described the pre-migration table, so the
                -- ranked query errored, was swallowed, and every assertion here was really
                -- testing the LIKE fallback rather than the search being shipped.
                CREATE TABLE devices (
                    id TEXT PRIMARY KEY,
                    company_name TEXT,
                    brand_name TEXT,
                    model_number TEXT,
                    catalog_number TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    device_description TEXT,
                    parent_company TEXT,
                    has_ifu INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO devices VALUES ('1','ETHICON ENDO-SURGERY, LLC','ECHELON','ECH60S','ECH60S','{}','Surgical stapler','Johnson & Johnson',1);
                INSERT INTO devices VALUES ('2','ETHICON ENDO-SURGERY, LLC','ECHELON ENDOPATH','ECR60T','ECR60T','{}','Endoscopic stapler','Johnson & Johnson',0);
                INSERT INTO devices VALUES ('3','JOHNSON & JOHNSON SURGICAL VISION, INC.','SMARTLOAD','GIB00','GIB00U0340','{}','Lens delivery system','Johnson & Johnson',0);
                CREATE VIRTUAL TABLE devices_fts USING fts5(
                    brand_name, company_name, parent_company, device_description,
                    catalog_number, model_number,
                    content='devices', content_rowid='rowid'
                );
                INSERT INTO devices_fts(devices_fts) VALUES('rebuild');
                """
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_search_returns_matching_devices(self) -> None:
        results = mvp_lookup.search_devices("echelon", db_path=self.db_path)
        brands = [r["brand_name"] for r in results]
        self.assertTrue(any("ECHELON" in b for b in brands))

    def test_search_falls_back_to_or_for_multi_term_no_and_match(self) -> None:
        results = mvp_lookup.search_devices("echelon stapler", db_path=self.db_path)
        self.assertTrue(len(results) >= 1)
        self.assertTrue(any("ECHELON" in r["brand_name"] for r in results))

    def test_search_by_catalog_number(self) -> None:
        results = mvp_lookup.search_devices("GIB00U0340", db_path=self.db_path)
        catalogs = [r["catalog_number"] for r in results]
        self.assertIn("GIB00U0340", catalogs)

    def test_search_returns_empty_for_no_match(self) -> None:
        results = mvp_lookup.search_devices("pacemaker xyz123", db_path=self.db_path)
        self.assertEqual(results, [])

    def test_get_device_returns_known_row(self) -> None:
        result = mvp_lookup.get_device("GIB00U0340", db_path=self.db_path)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["catalog_number"], "GIB00U0340")
        self.assertEqual(result["brand_name"], "SMARTLOAD")

    def test_get_device_returns_none_for_unknown(self) -> None:
        result = mvp_lookup.get_device("UNKNOWN-999", db_path=self.db_path)
        self.assertIsNone(result)
