from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mvp_server import (
    MvpHandler,
    _clean_user_query,
    _extract_device_terms,
    _looks_like_ifu_question,
    render_device_page,
    render_page,
)


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


def insert_row(db_path: Path, catalog: str, title: str, url: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ifu_links (
                catalog_number, status, match_confidence, document_title,
                document_url, language, revision, retrieved_at, last_checked_at
            ) VALUES (?, 'found', 'model_match', ?, ?, 'en', 'A', 'now', 'now')
            """,
            (catalog, title, url),
        )
        conn.commit()
    finally:
        conn.close()


class TestHandler(MvpHandler):
    pass


class MvpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "chatifu.sqlite3"
        create_ifu_table(self.db_path)
        TestHandler.db_path = self.db_path

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_html_escapes_catalog_title_and_url_values(self) -> None:
        result = {
            "catalog_number": '<script>alert("catalog")</script>',
            "status": "found",
            "match_confidence": "model_match",
            "document_title": '<img src=x onerror=alert("title")>',
            "document_url": 'https://example.test/doc" onclick="alert(1)',
            "language": "en",
            "revision": "A",
            "source_file_name": None,
            "retrieved_at": "now",
            "last_checked_at": "now",
            "warning": None,
            "candidates": [],
        }

        html = render_page(catalog=result["catalog_number"], result=result)

        self.assertNotIn(result["catalog_number"], html)
        self.assertNotIn(result["document_title"], html)
        self.assertNotIn(result["document_url"], html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
        self.assertIn("&quot; onclick=&quot;", html)

    def test_api_route_returns_json_without_downloading_pdf(self) -> None:
        insert_row(
            self.db_path,
            "GIB00U0340",
            "TECNIS Eyhance",
            "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            with urllib.request.urlopen(
                f"http://{host}:{port}/api/lookup?catalog=GIB00U0340",
                timeout=5,
            ) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        parsed = json.loads(body)
        self.assertEqual(parsed["status"], "found")
        self.assertEqual(parsed["document_url"], "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701")
        self.assertNotIn("IFU body", body)

    def test_result_page_includes_safety_note_and_open_link(self) -> None:
        insert_row(
            self.db_path,
            "GIB00U0340",
            "TECNIS Eyhance",
            "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            with urllib.request.urlopen(
                f"http://{host}:{port}/lookup?catalog=GIB00U0340",
                timeout=5,
            ) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertIn("Open manufacturer e-IFU", body)
        self.assertIn("answers using cited IFU passages", body)
        self.assertNotIn("PDF bytes", body)

    def test_stale_metadata_only_copy_is_not_rendered(self) -> None:
        body = render_page()
        self.assertNotIn("metadata" + " links only", body)
        self.assertNotIn("does not download PDFs or store IFU body content", body)


# ------------------------------------------------------------------
# New route tests
# ------------------------------------------------------------------

import sqlite3 as _sqlite3

from ifu_answer import IFUAnswerer, AnswerHit, AnswerResult
from ifu_cache import IFUDocumentCache


class FakeAnswerer:
    """Stub IFUAnswerer that returns a canned result without network calls."""

    def __init__(
        self,
        hits: list[AnswerHit] | None = None,
        error: str | None = None,
        source_url: str | None = None,
        document_url: str | None = None,
        pdf_url: str | None = None,
        pdf_bytes: bytes = b"%PDF-1.4 fake pdf",
    ) -> None:
        self.hits = hits or []
        self.error = error
        self.source_url = source_url
        self.document_url = document_url
        self.pdf_url = pdf_url
        self.pdf_bytes = pdf_bytes
        self.calls: list[tuple[str, str]] = []
        self.fetch_calls: list[str] = []

    def answer(self, document_url: str, question: str, max_hits: int = 5) -> AnswerResult:
        self.calls.append((document_url, question))
        return AnswerResult(
            hits=self.hits,
            source_url=self.source_url or document_url,
            document_title="Test IFU",
            timing_ms={"total_ms": 123.0},
            pdf_url=self.pdf_url,
            document_url=self.document_url,
            manufacturer_url=document_url,
            open_full_ifu_url=self.document_url or self.pdf_url,
            error=self.error,
        )

    def fetch_pdf_bytes(self, document_url: str) -> tuple[bytes, str | None, str | None]:
        self.fetch_calls.append(document_url)
        return self.pdf_bytes, self.pdf_url or self.document_url, "Test IFU"


def _create_devices_table(db_path: Path) -> None:
    conn = _sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                company_name TEXT,
                brand_name TEXT,
                model_number TEXT,
                catalog_number TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );
            INSERT OR IGNORE INTO devices VALUES
                ('D1','ETHICON ENDO-SURGERY, LLC','ECHELON','ECH60S','ECH60S','{}');
            INSERT OR IGNORE INTO devices VALUES
                ('D2','JOHNSON & JOHNSON','SMARTLOAD','GIB00','GIB00U0340','{}');
            CREATE VIRTUAL TABLE IF NOT EXISTS devices_fts USING fts5(
                brand_name, company_name, catalog_number,
                content='devices', content_rowid='rowid'
            );
            INSERT INTO devices_fts(rowid, brand_name, company_name, catalog_number)
                SELECT rowid, brand_name, company_name, catalog_number FROM devices;
            """
        )
        conn.commit()
    finally:
        conn.close()


class NewRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "chatifu.sqlite3"
        create_ifu_table(self.db_path)
        _create_devices_table(self.db_path)
        TestHandler.db_path = self.db_path

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _start_server(self) -> tuple[ThreadingHTTPServer, threading.Thread, tuple]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, server.server_address

    def _stop_server(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def _get(self, addr: tuple, path: str) -> tuple[int, str]:
        host, port = addr
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def _post_json(self, addr: tuple, path: str, payload: dict) -> tuple[int, str]:
        host, port = addr
        req = urllib.request.Request(
            f"http://{host}:{port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def test_home_page_returns_search_form(self) -> None:
        server, thread, addr = self._start_server()
        try:
            status, body = self._get(addr, "/")
        finally:
            self._stop_server(server, thread)
        self.assertEqual(status, 200)
        self.assertIn('action="/search"', body)
        self.assertIn("ChatIFU", body)

    def test_search_route_returns_device_cards(self) -> None:
        server, thread, addr = self._start_server()
        try:
            status, body = self._get(addr, "/search?q=echelon")
        finally:
            self._stop_server(server, thread)
        self.assertEqual(status, 200)
        self.assertIn("ECHELON", body)
        self.assertIn("/device?catalog=", body)

    def test_dirty_search_heading_does_not_double_wrap_results_for(self) -> None:
        dirty = urllib.parse.quote('Results for “unknown catheter wont open?”')
        server, thread, addr = self._start_server()
        try:
            status, body = self._get(addr, f"/search?q={dirty}")
        finally:
            self._stop_server(server, thread)
        self.assertEqual(status, 200)
        self.assertIn("Results for “unknown catheter wont open?”", body)
        self.assertNotIn("Results for “Results for", body)

    def test_device_page_shows_question_form(self) -> None:
        insert_row(self.db_path, "GIB00U0340", "SMARTLOAD IFU",
                   "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701")
        server, thread, addr = self._start_server()
        try:
            status, body = self._get(addr, "/device?catalog=GIB00U0340")
        finally:
            self._stop_server(server, thread)
        self.assertEqual(status, 200)
        self.assertIn("ask-form", body)
        self.assertIn("GIB00U0340", body)

    def test_device_page_with_q_prefills_and_auto_runs_question(self) -> None:
        question = "what size trocar is needed?"
        body = render_device_page(
            "ECH60S",
            {
                "brand_name": "ECHELON",
                "company_name": "ETHICON ENDO-SURGERY, LLC",
                "catalog_number": "ECH60S",
                "model_number": "ECH60S",
            },
            "ECHELON IFU",
            "https://www.e-ifu.com/viewpdf-iframe/11006/5/1/V0G000000000211",
            initial_question=question,
        )
        self.assertIn(f'value="{question}"', body)
        self.assertIn("requestSubmit", body)
        self.assertIn("initialQuestion", body)

    def test_api_ask_returns_json_hits(self) -> None:
        insert_row(self.db_path, "GIB00U0340", "SMARTLOAD IFU",
                   "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701")
        fake = FakeAnswerer(
            hits=[AnswerHit(page=3, section="WARNINGS", snippet="WARNINGS: do not reuse.")]
        )
        TestHandler.answerer = fake
        server, thread, addr = self._start_server()
        try:
            status, body = self._get(addr, "/api/ask?catalog=GIB00U0340&q=warnings")
        finally:
            self._stop_server(server, thread)
            TestHandler.answerer = IFUAnswerer()
        parsed = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(parsed["hits"]), 1)
        self.assertEqual(parsed["hits"][0]["page"], 3)
        self.assertEqual(parsed["hits"][0]["section"], "WARNINGS")
        self.assertIn("WARNINGS", parsed["hits"][0]["snippet"])

    def test_api_ask_separates_source_document_iframe_and_open_urls(self) -> None:
        insert_row(self.db_path, "ECH60S", "ECHELON IFU",
                   "https://www.e-ifu.com/viewpdf-iframe/11006/5/1/V0G000000000211")
        actual_pdf = "https://example.com/actual-ifu.pdf"
        fake = FakeAnswerer(
            hits=[AnswerHit(page=9, section="Instructions", snippet="The IFU passage says test.")],
            source_url="https://www.jnjmedtech.com/some-product-page",
            document_url=actual_pdf,
            pdf_url=actual_pdf,
        )
        TestHandler.answerer = fake
        server, thread, addr = self._start_server()
        try:
            status, body = self._get(addr, "/api/ask?catalog=ECH60S&q=wont%20fire")
        finally:
            self._stop_server(server, thread)
            TestHandler.answerer = IFUAnswerer()
        parsed = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(parsed["source_url"], "https://www.jnjmedtech.com/some-product-page")
        self.assertEqual(parsed["document_url"], actual_pdf)
        self.assertEqual(parsed["open_full_ifu_url"], actual_pdf)
        self.assertEqual(parsed["iframe_url"], "/ifu/pdf?catalog=ECH60S")
        self.assertNotIn("jnjmedtech.com", parsed["iframe_url"])

    def test_pdf_proxy_route_returns_inline_pdf_bytes(self) -> None:
        insert_row(self.db_path, "ECH60S", "ECHELON IFU",
                   "https://www.e-ifu.com/viewpdf-iframe/11006/5/1/V0G000000000211")
        fake = FakeAnswerer(pdf_bytes=b"%PDF-1.7 proxy-test")
        TestHandler.answerer = fake
        server, thread, addr = self._start_server()
        try:
            host, port = addr
            with urllib.request.urlopen(
                f"http://{host}:{port}/ifu/pdf?catalog=ECH60S",
                timeout=5,
            ) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                disposition = response.headers.get("Content-Disposition", "")
                body = response.read()
        finally:
            self._stop_server(server, thread)
            TestHandler.answerer = IFUAnswerer()
        self.assertEqual(status, 200)
        self.assertIn("application/pdf", content_type)
        self.assertIn("inline", disposition)
        self.assertTrue(body.startswith(b"%PDF-1.7 proxy-test"))
        self.assertEqual(fake.fetch_calls, ["https://www.e-ifu.com/viewpdf-iframe/11006/5/1/V0G000000000211"])

    def test_pdf_proxy_uses_cache_on_second_call_when_enabled(self) -> None:
        insert_row(self.db_path, "ECH60S", "ECHELON IFU",
                   "https://www.e-ifu.com/viewpdf-iframe/11006/5/1/0")
        fake = FakeAnswerer(pdf_bytes=b"%PDF-1.7 cache-proxy")
        TestHandler.answerer = fake
        TestHandler.ifu_cache = IFUDocumentCache(cache_dir=Path(self.tempdir.name) / "ifu_cache")
        server, thread, addr = self._start_server()
        try:
            host, port = addr
            with urllib.request.urlopen(f"http://{host}:{port}/ifu/pdf?catalog=ECH60S", timeout=5) as r1:
                first_header = r1.headers.get("X-ChatIFU-Cache")
                r1.read()
            with urllib.request.urlopen(f"http://{host}:{port}/ifu/pdf?catalog=ECH60S", timeout=5) as r2:
                second_header = r2.headers.get("X-ChatIFU-Cache")
                r2.read()
        finally:
            self._stop_server(server, thread)
            TestHandler.answerer = IFUAnswerer()
            TestHandler.ifu_cache = None
        self.assertEqual(first_header, "miss")
        self.assertEqual(second_header, "hit")
        self.assertEqual(len(fake.fetch_calls), 1)

    def test_api_ask_returns_error_for_missing_params(self) -> None:
        server, thread, addr = self._start_server()
        try:
            import urllib.error
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(addr, "/api/ask?catalog=GIB00U0340")
        finally:
            self._stop_server(server, thread)
        self.assertEqual(ctx.exception.code, 400)

    def test_api_ask_xss_safe_in_hits(self) -> None:
        insert_row(self.db_path, "GIB00U0340", "SMARTLOAD IFU",
                   "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701")
        xss_snippet = '<script>alert("xss")</script>'
        fake = FakeAnswerer(hits=[AnswerHit(page=1, snippet=xss_snippet)])
        TestHandler.answerer = fake
        server, thread, addr = self._start_server()
        try:
            _, body = self._get(addr, "/api/ask?catalog=GIB00U0340&q=xss")
        finally:
            self._stop_server(server, thread)
            TestHandler.answerer = IFUAnswerer()
        # JSON is returned as-is; XSS escaping is done client-side by escHtml()
        parsed = json.loads(body)
        self.assertEqual(parsed["hits"][0]["snippet"], xss_snippet)

    def test_answer_ui_includes_split_view_page_jump_and_disclaimer(self) -> None:
        device = {
            "brand_name": "SMARTLOAD",
            "company_name": "JOHNSON & JOHNSON",
            "catalog_number": "GIB00U0340",
            "model_number": "GIB00",
        }
        body = render_device_page(
            "GIB00U0340",
            device,
            "SMARTLOAD IFU",
            "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701",
        )

        self.assertIn("Open Full IFU", body)
        self.assertIn("answer-split", body)
        self.assertIn('id="ifu-frame"', body)
        self.assertIn('data-base-src="', body)
        self.assertIn("Go to page ", body)
        self.assertIn("goToPage", body)
        self.assertIn("<mark>", body)
        self.assertIn("The IFU passage says:", body)
        self.assertIn("AI-assisted. Verify all information in the full IFU", body)
        self.assertIn("const candidates = [data.iframe_url, data.document_url, data.pdf_url]", body)
        self.assertIn("isGenericLandingUrl(data.source_url)", body)
        self.assertIn("The IFU answer was generated from the document", body)

    def test_natural_language_search_fallback_preserves_question(self) -> None:
        question = (
            "echelon 3000 stapler wont fire and its on the tissue and wont open, "
            "what should I do next?"
        )
        device = {
            "brand_name": "ECHELON",
            "company_name": "ETHICON ENDO-SURGERY, LLC",
            "catalog_number": "ECH60S",
            "model_number": "ECH60S",
        }

        def fake_search(query: str, db_path: Path, limit: int = 20) -> list[dict[str, str]]:
            if query == question:
                return []
            if query == "echelon 3000 stapler ech cutter reload":
                return [device]
            return []

        server, thread, addr = self._start_server()
        try:
            encoded = urllib.parse.quote(question)
            with patch("mvp_server.search_devices", side_effect=fake_search):
                status, body = self._get(addr, f"/search?q={encoded}")
        finally:
            self._stop_server(server, thread)
        self.assertEqual(status, 200)
        self.assertIn("Ask a question from this device&#x27;s IFU", body)
        self.assertIn("ECH60S", body)
        self.assertIn(html_escape(question), body)

    def test_medical_device_query_search_groups_identity_above_problem_terms(self) -> None:
        question = (
            "i have a 12mm ethicon trocar asnd its leaking air, "
            "what can I do to trouble shoot it?"
        )
        stryker_air = {
            "brand_name": "AIR",
            "company_name": "Stryker",
            "catalog_number": "AIR001",
            "model_number": "AIR",
        }
        biopatch = {
            "brand_name": "BIOPATCH",
            "company_name": "ETHICON ENDO-SURGERY, LLC",
            "catalog_number": "BIO001",
            "model_number": "BIO",
        }
        endopath = {
            "brand_name": "ENDOPATH XCEL Trocar",
            "company_name": "ETHICON ENDO-SURGERY, LLC",
            "catalog_number": "TR12",
            "model_number": "12MM",
        }
        blake = {
            "brand_name": "BLAKE",
            "company_name": "ETHICON ENDO-SURGERY, LLC",
            "catalog_number": "BLK001",
            "model_number": "BLK",
        }

        def fake_search(query: str, db_path: Path, limit: int = 20) -> list[dict[str, str]]:
            low = query.lower()
            if "ethicon" in low or "trocar" in low or "air" in low:
                return [stryker_air, biopatch, endopath, blake]
            return []

        server, thread, addr = self._start_server()
        try:
            encoded = urllib.parse.quote(question)
            with patch("mvp_server.search_devices", side_effect=fake_search):
                status, body = self._get(addr, f"/search?q={encoded}")
        finally:
            self._stop_server(server, thread)

        self.assertEqual(status, 200)
        self.assertIn("Detected", body)
        self.assertIn("Manufacturer</dt><dd>Ethicon", body)
        self.assertIn("Device concept</dt><dd>laparoscopic trocar", body)
        self.assertIn("Size</dt><dd>12mm", body)
        self.assertIn("Problem</dt><dd>air leak", body)
        self.assertIn("Use this IFU", body)
        self.assertIn(urllib.parse.quote(question), body)
        self.assertLess(body.index("Best matches"), body.index("Other possible matches"))
        self.assertLess(body.index("ENDOPATH XCEL Trocar"), body.index("AIR"))
        self.assertIn("Why this match?", body)
        self.assertIn("Wrong IFU?", body)

    def test_feedback_post_stores_safe_jsonl_without_raw_question(self) -> None:
        feedback_path = Path(self.tempdir.name) / "feedback.jsonl"
        os.environ["CHATIFU_FEEDBACK_PATH"] = str(feedback_path)
        server, thread, addr = self._start_server()
        try:
            status, body = self._post_json(addr, "/api/feedback", {
                "feedback_type": "wrong_ifu",
                "catalog": "TR12",
                "question": "patient identifying raw question should not be stored",
                "comment": "<script>x</script>",
            })
        finally:
            self._stop_server(server, thread)
            os.environ.pop("CHATIFU_FEEDBACK_PATH", None)
        self.assertEqual(status, 200)
        self.assertIn('"ok": true', body)
        stored = feedback_path.read_text("utf-8")
        self.assertIn("wrong_ifu", stored)
        self.assertIn("<script>x</script>", stored)
        self.assertNotIn("patient identifying raw question should not be stored", stored)

    def test_admin_search_debug_json_contains_score_reasons(self) -> None:
        question = "i have a 12mm ethicon trocar asnd its leaking air, what can I do to trouble shoot it?"
        server, thread, addr = self._start_server()
        try:
            encoded = urllib.parse.quote(question)
            status, body = self._get(addr, f"/api/admin/search_debug?q={encoded}")
        finally:
            self._stop_server(server, thread)
        parsed = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("parsed_query", parsed)
        self.assertIn("candidates", parsed)
        self.assertIn("generated_search_strings", parsed)

    def test_admin_token_required_when_env_set(self) -> None:
        os.environ["CHATIFU_ADMIN_TOKEN"] = "secret"
        server, thread, addr = self._start_server()
        try:
            import urllib.error
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(addr, "/api/admin/search_debug?q=trocar")
        finally:
            self._stop_server(server, thread)
            os.environ.pop("CHATIFU_ADMIN_TOKEN", None)
        self.assertEqual(ctx.exception.code, 403)

    def test_admin_cache_page_works(self) -> None:
        TestHandler.ifu_cache = IFUDocumentCache(cache_dir=Path(self.tempdir.name) / "ifu_cache")
        server, thread, addr = self._start_server()
        try:
            status, body = self._get(addr, "/admin/cache")
        finally:
            self._stop_server(server, thread)
            TestHandler.ifu_cache = None
        self.assertEqual(status, 200)
        self.assertIn("IFU cache", body)


class NaturalLanguageQueryTests(unittest.TestCase):
    def test_clean_user_query_strips_results_for_wrappers(self) -> None:
        q = (
            "Results for “Results for \"echelon 3000 stapler wont fire "
            "and its on the tissue and wont open, what should I do next?\"”"
        )
        self.assertEqual(
            _clean_user_query(q),
            "echelon 3000 stapler wont fire and its on the tissue and wont open, what should I do next?",
        )

    def test_looks_like_ifu_question_examples(self) -> None:
        examples = [
            "echelon 3000 stapler wont fire and its on the tissue and wont open, what should I do next?",
            "what size trocar is needed",
            "how many times can this be fired",
            "what are the warnings for reuse",
        ]
        for example in examples:
            with self.subTest(example=example):
                self.assertTrue(_looks_like_ifu_question(example))

    def test_extract_device_terms_from_mixed_question(self) -> None:
        terms = _extract_device_terms(
            "echelon 3000 stapler wont fire and its on the tissue and wont open, "
            "what should I do next?"
        )
        self.assertIn("echelon", terms)
        self.assertIn("3000", terms)
        self.assertIn("stapler", terms)
        self.assertNotIn("Results for", terms)
        self.assertNotIn("what should i do", terms)
        self.assertNotIn("wont", terms)
        self.assertNotIn("tissue", terms)
        self.assertNotIn("open", terms)


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
