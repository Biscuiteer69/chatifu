from __future__ import annotations

import socket
import sqlite3
import tempfile
import unittest
import urllib.error
import warnings
from pathlib import Path

from resolvers.eifu_resolver import (
    AUTH_FAILED_STATUS,
    CANDIDATE_STATUS,
    FOUND_STATUS,
    HTTP_ERROR_STATUS,
    NETWORK_ERROR_STATUS,
    NOT_FOUND_STATUS,
    SESSION_GATE_STATUS,
    TIMEOUT_STATUS,
    EifuResolver,
    classify_document_status,
    ensure_ifu_links_table,
    detect_gate_page,
    match_confidence,
)


GIB_SEARCH_HTML = """
<html>
  <body>
    <div class="doc-info-row">
      <a class="use-ajax" href="/viewpdf-iframe/100/1/0/V0GIB00">
        TECNIS Eyhance SmartLOAD GIB00 Directions for Use
      </a>
      <span class="doc-metadata-version">Rev C</span>
      <span class="file-name-label">File Name</span><span class="file-name">gib00.pdf</span>
      <span class="language-label">Language</span><span class="language">en</span>
    </div>
  </body>
</html>
"""

BROAD_SEARCH_HTML = """
<html>
  <body>
    <div class="doc-info-row">
      <a class="use-ajax" href="/viewpdf-iframe/24352/1/0/V0G000000000701">
        PROFESSIONAL USE INFO, STAR S4 IR, IDESIGN
      </a>
    </div>
  </body>
</html>
"""

NO_RESULT_HTML = """
<html><body><h1>Search results</h1><p>Sorry, no result found.</p></body></html>
"""

WELCOME_GATE_HTML = """
<html>
  <body>
    <form id="eifu-splash-site-selection-form">
      <input type="hidden" name="form_id" value="eifu_splash_site_selection_form">
      <input type="radio" name="site_user" value="hcp" id="edit-site-user-hcp">
    </form>
  </body>
</html>
"""

TERMS_GATE_HTML = """
<html>
  <body>
    <form action="/accept-terms-conditions">
      <input type="hidden" name="form_id" value="eifu_splash_site_welcome_form">
      <input type="checkbox" name="acknowledge" id="edit-acknowledge">
      <input id="edit-submit" type="submit" value="Continue">
    </form>
  </body>
</html>
"""

WELCOME_FORM_HTML = """
<html>
  <body>
    <form action="/welcome">
      <input type="hidden" name="form_build_id" value="welcome-build">
      <input type="hidden" name="form_id" value="eifu_splash_site_selection_form">
      <input type="radio" name="site_user" value="hcp" id="edit-site-user-hcp">
    </form>
  </body>
</html>
"""

TERMS_FORM_HTML = """
<html>
  <body>
    <form action="/accept-terms-conditions">
      <input type="hidden" name="form_build_id" value="terms-build">
      <input type="hidden" name="form_id" value="eifu_splash_site_welcome_form">
      <input type="checkbox" name="acknowledge" id="edit-acknowledge">
    </form>
  </body>
</html>
"""

NORMAL_AFTER_TERMS_HTML = "<html><body><main>ready</main></body></html>"
IFU_BODY_TEXT = "This is IFU body content that must never be stored."


class FixtureResolver(EifuResolver):
    def __init__(self, db_path: Path, response: str | BaseException) -> None:
        super().__init__(db_path=db_path, delay_sec=0)
        self.response = response
        self.requested_urls: list[str] = []

    def _ensure_session(self) -> None:
        self._session_ready = True

    def _request(self, url: str, delay: bool = True) -> str:
        self.requested_urls.append(url)
        if "viewpdf" in url or "fetchPdf" in url:
            raise AssertionError("resolver attempted to download PDF content")
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body.encode("utf-8")


class FakeOpener:
    def __init__(self, responses: list[str | BaseException]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, bytes | None]] = []

    def open(self, req: object, timeout: int) -> FakeResponse:
        url = getattr(req, "full_url")
        data = getattr(req, "data", None)
        self.requests.append((url, data))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return FakeResponse(response)


class EifuResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "chatifu.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("select * from ifu_links order by id").fetchall()
        finally:
            conn.close()

    def statuses(self) -> list[str]:
        return [row["status"] for row in self.rows()]

    def test_known_gib_catalog_model_match_is_found(self) -> None:
        resolver = FixtureResolver(self.db_path, GIB_SEARCH_HTML)

        documents = resolver.resolve("GIB00U0340", model_number="GIB00")

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["match_confidence"], "model_match")
        self.assertEqual(documents[0]["status"], FOUND_STATUS)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["catalog_number"], "GIB00U0340")
        self.assertIn("TECNIS Eyhance SmartLOAD GIB00", rows[0]["document_title"])
        self.assertEqual(rows[0]["match_confidence"], "model_match")
        self.assertEqual(rows[0]["status"], FOUND_STATUS)
        self.assertIsNotNone(rows[0]["first_seen_at"])
        self.assertIsNotNone(rows[0]["last_checked_at"])
        self.assertIsNotNone(rows[0]["last_success_at"])
        self.assertIsNone(rows[0]["last_error_at"])
        self.assertIsNone(rows[0]["error_type"])

    def test_rerunning_same_catalog_does_not_duplicate_rows(self) -> None:
        resolver = FixtureResolver(self.db_path, GIB_SEARCH_HTML)

        resolver.resolve("GIB00U0340", model_number="GIB00")
        resolver.resolve("GIB00U0340", model_number="GIB00")

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], FOUND_STATUS)

    def test_later_document_result_deletes_stale_catalog_outcome_row(self) -> None:
        resolver = FixtureResolver(self.db_path, NO_RESULT_HTML)

        resolver.resolve("GIB00U0340", model_number="GIB00")
        resolver.response = GIB_SEARCH_HTML
        resolver.resolve("GIB00U0340", model_number="GIB00")

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["catalog_number"], "GIB00U0340")
        self.assertIsNotNone(rows[0]["document_url"])
        self.assertEqual(rows[0]["status"], FOUND_STATUS)

    def test_http_200_session_gate_is_not_not_found(self) -> None:
        resolver = FixtureResolver(self.db_path, WELCOME_GATE_HTML)

        documents = resolver.resolve("GIB00U0340", model_number="GIB00")

        self.assertEqual(documents, [])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["status"], {SESSION_GATE_STATUS, AUTH_FAILED_STATUS, "init_failed"})
        self.assertNotEqual(rows[0]["status"], NOT_FOUND_STATUS)
        self.assertIsNotNone(rows[0]["last_error_at"])
        self.assertEqual(rows[0]["error_type"], SESSION_GATE_STATUS)

    def test_gate_detection_ignores_legal_footer_on_valid_results(self) -> None:
        html = GIB_SEARCH_HTML + "<footer>terms and conditions</footer>"
        resolver = FixtureResolver(self.db_path, html)

        documents = resolver.resolve("GIB00U0340", model_number="GIB00")

        self.assertIsNone(detect_gate_page(html))
        self.assertEqual(len(documents), 1)
        self.assertEqual(self.rows()[0]["status"], FOUND_STATUS)

    def test_actual_welcome_and_terms_gates_are_detected(self) -> None:
        self.assertEqual(detect_gate_page(WELCOME_GATE_HTML), "welcome")
        self.assertEqual(detect_gate_page(TERMS_GATE_HTML), "terms")

    def test_broad_search_result_is_candidate_not_found(self) -> None:
        resolver = FixtureResolver(self.db_path, BROAD_SEARCH_HTML)

        documents = resolver.resolve("0030-4864", model_number="not-in-title")

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["match_confidence"], "search_result")
        self.assertEqual(documents[0]["status"], CANDIDATE_STATUS)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], CANDIDATE_STATUS)
        self.assertNotEqual(rows[0]["status"], FOUND_STATUS)

    def test_catalog_embedded_in_artwork_number_is_not_exact(self) -> None:
        # A001931 is an artwork number that merely contains catalog 01931;
        # promoting it linked GYNECARE 01931 to a HARMONIC shears document.
        self.assertEqual(
            match_confidence(
                "HARMONIC™ 700, 5 mm Diameter Shears",
                "01931",
                source_file_name="A001931_RevC_HAR723_736_745_eIFU_Links_Clean.pdf",
            ),
            "search_result",
        )

    def test_catalog_as_whole_file_name_token_is_exact(self) -> None:
        self.assertEqual(
            match_confidence(
                "ELITA™ Femtosecond Laser System FLAP Operator Manual",
                "0155-1910",
                source_file_name="0155-1910 Rev B_US EN_Elita.pdf",
            ),
            "exact_catalog",
        )

    def test_short_catalog_never_matches_file_name(self) -> None:
        self.assertEqual(
            match_confidence(
                "Some reload document",
                "736",
                source_file_name="A001931_RevC_HAR723_736_745_eIFU_Links_Clean.pdf",
            ),
            "search_result",
        )

    def test_brand_in_title_promotes_document_the_catalog_never_names(self) -> None:
        # Catalog 0030-4864's STAR S4 IR booklets carry their own part numbers
        # (0030-8814 Rev. B) and never mention the catalog anywhere — title,
        # file name, or PDF body. The brand is the only link.
        self.assertEqual(
            match_confidence(
                "PATIENT INFO BOOKLET, IDESIGN, STAR S4 IR, HYPEROPIA",
                "0030-4864",
                source_file_name="0030-8814_RevB.pdf",
                brand_names=["STAR S4 IR"],
            ),
            "brand_match",
        )

    def test_coincidental_file_name_hit_is_not_promoted_by_brand(self) -> None:
        # The portal returns MENTOR documents for GYNECARE catalog 00825 because
        # LAB100825478v3_eIFU.pdf contains "00825". The brand disagrees, so the
        # document must stay a broad candidate (regression guard for bb70780).
        self.assertEqual(
            match_confidence(
                "MENTOR RESTERILIZABLE GEL BREAST IMPLANT SIZER ePIDS",
                "00825",
                source_file_name="LAB100825478v3_eIFU.pdf",
                brand_names=["GYNECARE THERMACHOICE"],
            ),
            "search_result",
        )

    def test_short_brand_does_not_match(self) -> None:
        self.assertEqual(
            match_confidence(
                "ECHO doppler probe cleaning guide",
                "XY1234",
                brand_names=["ECHO"],
            ),
            "search_result",
        )

    def test_brand_match_counts_as_found(self) -> None:
        self.assertEqual(classify_document_status("brand_match"), "found")

    def test_session_initialization_posts_hcp_language_and_terms_before_search(self) -> None:
        resolver = EifuResolver(db_path=self.db_path, delay_sec=0)
        opener = FakeOpener(
            [
                WELCOME_FORM_HTML,
                TERMS_FORM_HTML,
                TERMS_FORM_HTML,
                NORMAL_AFTER_TERMS_HTML,
                GIB_SEARCH_HTML,
            ]
        )
        resolver._opener = opener

        documents = resolver.resolve("GIB00U0340", model_number="GIB00")

        self.assertEqual(len(documents), 1)
        self.assertEqual([request[0] for request in opener.requests], [
            "https://www.e-ifu.com/welcome",
            "https://www.e-ifu.com/welcome",
            "https://www.e-ifu.com/accept-terms-conditions",
            "https://www.e-ifu.com/accept-terms-conditions",
            "https://www.e-ifu.com/search-document-metadata/GIB00U0340",
        ])
        welcome_post = opener.requests[1][1].decode("utf-8")
        terms_post = opener.requests[3][1].decode("utf-8")
        self.assertIn("site_user=hcp", welcome_post)
        self.assertIn("eifu_splash_welcome_language=en", welcome_post)
        self.assertIn("acknowledge=1", terms_post)
        self.assertEqual(self.rows()[0]["status"], FOUND_STATUS)

    def test_session_initialization_failure_is_not_not_found(self) -> None:
        resolver = EifuResolver(db_path=self.db_path, delay_sec=0)
        resolver._opener = FakeOpener(["<html><body>missing build id</body></html>"])

        documents = resolver.resolve("GIB00U0340", model_number="GIB00")

        self.assertEqual(documents, [])
        row = self.rows()[0]
        self.assertEqual(row["status"], "init_failed")
        self.assertNotEqual(row["status"], NOT_FOUND_STATUS)

    def test_terms_post_gate_failure_is_auth_failed_not_not_found(self) -> None:
        resolver = EifuResolver(db_path=self.db_path, delay_sec=0)
        resolver._opener = FakeOpener(
            [
                WELCOME_FORM_HTML,
                TERMS_FORM_HTML,
                TERMS_FORM_HTML,
                TERMS_GATE_HTML,
            ]
        )

        documents = resolver.resolve("GIB00U0340", model_number="GIB00")

        self.assertEqual(documents, [])
        row = self.rows()[0]
        self.assertEqual(row["status"], AUTH_FAILED_STATUS)
        self.assertNotEqual(row["status"], NOT_FOUND_STATUS)

    def test_no_result_catalog_creates_not_found_with_freshness(self) -> None:
        resolver = FixtureResolver(self.db_path, NO_RESULT_HTML)

        documents = resolver.resolve("NO-SUCH-CATALOG")

        self.assertEqual(documents, [])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], NOT_FOUND_STATUS)
        self.assertIsNotNone(rows[0]["first_seen_at"])
        self.assertIsNotNone(rows[0]["last_checked_at"])
        self.assertIsNotNone(rows[0]["last_success_at"])
        self.assertIsNone(rows[0]["last_error_at"])
        self.assertIsNone(rows[0]["error_type"])

    def test_transient_errors_do_not_create_not_found_rows(self) -> None:
        cases = [
            (
                "timeout",
                TimeoutError("timed out"),
                TIMEOUT_STATUS,
            ),
            (
                "network",
                urllib.error.URLError(OSError("temporary DNS failure")),
                NETWORK_ERROR_STATUS,
            ),
            (
                "http_429",
                urllib.error.HTTPError("https://www.e-ifu.com/search", 429, "rate limited", {}, None),
                HTTP_ERROR_STATUS,
            ),
            (
                "http_500",
                urllib.error.HTTPError("https://www.e-ifu.com/search", 500, "server error", {}, None),
                HTTP_ERROR_STATUS,
            ),
            (
                "socket_timeout",
                urllib.error.URLError(socket.timeout("timed out")),
                TIMEOUT_STATUS,
            ),
        ]
        for name, exc, expected_status in cases:
            with self.subTest(name=name):
                db_path = Path(self.tempdir.name) / f"{name}.sqlite3"
                resolver = FixtureResolver(db_path, exc)

                documents = resolver.resolve(f"ERR-{name}")

                self.assertEqual(documents, [])
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute("select * from ifu_links").fetchone()
                finally:
                    conn.close()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row["status"], expected_status)
                self.assertNotEqual(row["status"], NOT_FOUND_STATUS)
                self.assertIsNotNone(row["last_error_at"])
                self.assertIsNotNone(row["error_type"])

    def test_resolver_does_not_download_pdfs_or_store_body_content(self) -> None:
        resolver = FixtureResolver(self.db_path, GIB_SEARCH_HTML + IFU_BODY_TEXT)

        resolver.resolve("GIB00U0340", model_number="GIB00")

        self.assertTrue(resolver.requested_urls)
        self.assertFalse(any("viewpdf" in url or "fetchPdf" in url for url in resolver.requested_urls))
        rows = self.rows()
        serialized_row_values = " ".join(str(value) for value in rows[0])
        self.assertIn("viewpdf-iframe", rows[0]["document_url"])
        self.assertNotIn(IFU_BODY_TEXT, serialized_row_values)

    def test_duplicate_dirty_db_warns_when_unique_indexes_are_skipped(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                create table if not exists ifu_links (
                    id integer primary key autoincrement,
                    device_rowid integer,
                    primary_di text,
                    catalog_number text,
                    manufacturer_family text,
                    source_url text,
                    document_url text,
                    document_title text,
                    language text default 'en',
                    revision text,
                    match_confidence text,
                    retrieved_at text,
                    status text
                )
                """
            )
            conn.executemany(
                "insert into ifu_links (catalog_number, document_url, status) values (?, ?, ?)",
                [
                    ("DUP-DOC", "https://example.test/doc", FOUND_STATUS),
                    ("DUP-DOC", "https://example.test/doc", FOUND_STATUS),
                    ("DUP-OUTCOME", None, NOT_FOUND_STATUS),
                    ("DUP-OUTCOME", None, NOT_FOUND_STATUS),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ensure_ifu_links_table(self.db_path)

        messages = [str(item.message) for item in caught]
        self.assertTrue(any("idx_ifu_unique_catalog_document" in message for message in messages))
        self.assertTrue(any("idx_ifu_unique_catalog_outcome" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
