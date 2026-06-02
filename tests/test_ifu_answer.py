from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ifu_answer import (
    IFUAnswerer,
    AnswerHit,
    AnswerResult,
    decode_viewer,
    extract_doc_title,
    extract_pdf_url,
    search_pages,
    _is_gate_page,
    _is_english_page,
    _parse_pdf_limited,
)


# ------------------------------------------------------------------
# Helpers for building fake HTTP responses
# ------------------------------------------------------------------

def _viewer_response(
    pdf_path: str = "/fetchPdf/47270/1/0/eifu/test.pdf",
    doc_title: str = "Test IFU Document",
) -> str:
    """Build a fake viewpdf-iframe response in the Drupal AJAX format."""
    inner_html = (
        f'<div class="eifu-pdf-modal">'
        f'<span class="pdf-name">{doc_title}</span>'
        f'<div class="view-pdf">'
        f'<iframe id="pdf-iframe" src="/viewpdf?file={pdf_path}"></iframe>'
        f'</div></div>'
    )
    payload = json.dumps([{
        "command": "openDialog",
        "selector": "#pdf-modal-view",
        "data": inner_html,
    }])
    return f"<textarea>{payload}</textarea>"


def _gate_response(gate_type: str = "welcome") -> str:
    if gate_type == "welcome":
        return (
            '<form id="form"><input name="form_build_id" value="fb1">'
            '<input name="site_user" value="hcp" id="edit-site-user-hcp"></form>'
        )
    return (
        '<form action="/accept-terms-conditions">'
        '<input name="form_build_id" value="fb1">'
        '<input name="acknowledge" id="edit-acknowledge">'
        '</form>'
    )


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    def __init__(self, responses: list[bytes | str | BaseException]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def open(self, req: object, timeout: int) -> FakeResponse:
        url = getattr(req, "full_url", "?")
        self.calls.append(url)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            response = response.encode("utf-8")
        return FakeResponse(response)


def _fake_pdf_parser(pages: list[str]):
    """Returns a pdf_parser callable that ignores bytes and returns fixed pages."""
    def parser(pdf_bytes: bytes) -> list[str]:
        return pages
    return parser


# ------------------------------------------------------------------
# decode_viewer tests
# ------------------------------------------------------------------

class DecodeViewerTests(unittest.TestCase):
    def test_decodes_drupal_ajax_textarea(self) -> None:
        raw = _viewer_response("/fetchPdf/1/1/0/test.pdf", "My IFU")
        inner = decode_viewer(raw)
        self.assertIn("pdf-name", inner)
        self.assertIn("My IFU", inner)

    def test_returns_raw_when_not_ajax_format(self) -> None:
        raw = "<html><body>plain page</body></html>"
        self.assertEqual(decode_viewer(raw), raw)

    def test_returns_empty_string_for_missing_data_field(self) -> None:
        payload = json.dumps([{"command": "openDialog", "data": ""}])
        raw = f"<textarea>{payload}</textarea>"
        self.assertEqual(decode_viewer(raw), "")


# ------------------------------------------------------------------
# extract_pdf_url tests
# ------------------------------------------------------------------

class ExtractPdfUrlTests(unittest.TestCase):
    def test_extracts_fetchpdf_from_viewpdf_iframe(self) -> None:
        inner = (
            '<iframe src="/viewpdf?file=%2FfetchPdf%2F47270%2F1%2F0%2Feifu%2Fdoc.pdf">'
            '</iframe>'
        )
        url = extract_pdf_url(inner)
        self.assertEqual(url, "https://www.e-ifu.com/fetchPdf/47270/1/0/eifu/doc.pdf")

    def test_extracts_direct_iframe_src(self) -> None:
        inner = '<iframe src="/viewpdf?someparam=x"></iframe>'
        url = extract_pdf_url(inner)
        self.assertIsNotNone(url)
        self.assertIn("e-ifu.com", url)

    def test_returns_none_when_no_pdf(self) -> None:
        self.assertIsNone(extract_pdf_url("<div>no pdf here</div>"))


# ------------------------------------------------------------------
# search_pages tests
# ------------------------------------------------------------------

class SearchPagesTests(unittest.TestCase):
    _PAGES = [
        "This is page one about patient selection criteria.",
        "WARNINGS: Do not use in patients with known hypersensitivity.",
        "Contraindications include severe corneal scarring.",
        "Storage: keep at room temperature, shelf life 3 years.",
    ]

    def test_finds_page_with_matching_term(self) -> None:
        hits = search_pages(self._PAGES, "warnings", max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].page, 2)
        self.assertIn("WARNINGS", hits[0].snippet)

    def test_stop_words_are_filtered(self) -> None:
        hits = search_pages(self._PAGES, "what are the warnings", max_hits=5)
        self.assertTrue(len(hits) >= 1)
        self.assertEqual(hits[0].page, 2)

    def test_no_hits_for_missing_term(self) -> None:
        hits = search_pages(self._PAGES, "pacemaker insertion", max_hits=5)
        self.assertEqual(hits, [])

    def test_shelf_life_query_finds_storage_page(self) -> None:
        hits = search_pages(self._PAGES, "shelf life", max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].page, 4)

    def test_multi_term_query_finds_multiple_pages(self) -> None:
        # "patient" matches pages 1 and 2; "contraindications" matches page 3
        hits = search_pages(self._PAGES, "patient contraindications", max_hits=5)
        self.assertGreaterEqual(len(hits), 2)

    def test_pages_are_ranked_by_term_coverage(self) -> None:
        pages = [
            "This page mentions only the device size. It is otherwise generic.",
            "Introduce the instrument through a trocar of the appropriate size.",
        ]
        hits = search_pages(pages, "what size trocar is needed", max_hits=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].page, 2)

    def test_max_hits_is_respected(self) -> None:
        pages = ["pacemaker info"] * 10
        hits = search_pages(pages, "pacemaker", max_hits=3)
        self.assertEqual(len(hits), 3)

    def test_snippet_contains_matched_term(self) -> None:
        hits = search_pages(self._PAGES, "shelf life", max_hits=5)
        self.assertIn("shelf life", hits[0].snippet.lower())

    def test_non_english_page_is_skipped(self) -> None:
        pages = [
            (
                "warning reutilizacion dispositivo paciente esterilizacion "
                "procedimiento fabricante producto medico seguridad uso clinico "
                "instrumento material informacion documento"
            ),
            "WARNINGS: This device is for single use only. Do not reuse it.",
        ]
        hits = search_pages(pages, "warning reuse", max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].page, 2)
        self.assertIn("single use", hits[0].snippet)

    def test_english_page_passes_density_check(self) -> None:
        text = "This is the warning for use with the device and it should not be reused."
        self.assertTrue(_is_english_page(text))

    def test_snippet_uses_complete_sentence_boundaries(self) -> None:
        text = (
            "Before use, inspect the package. "
            "Warnings for reuse are listed here. "
            "Do not reuse this device. "
            "Discard it after use. "
            "Contact the manufacturer for support. "
            "Store it in a clean area."
        )
        hits = search_pages([text], "warnings reuse", max_hits=5)
        self.assertEqual(len(hits), 1)
        snippet = hits[0].snippet
        self.assertTrue(snippet[0].isupper(), snippet)
        self.assertRegex(snippet, r"[.!?]$")
        sentence_count = len([s for s in snippet.split(". ") if s.strip()])
        self.assertGreaterEqual(sentence_count, 2)
        self.assertLessEqual(sentence_count, 5)

    def test_snippet_drops_dangling_list_marker(self) -> None:
        text = (
            "Warnings apply to use of this device. "
            "Patients with inflammation may be at risk. b. "
            "Another complete sentence follows."
        )
        hits = search_pages([text], "warnings inflammation", max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertNotRegex(hits[0].snippet, r"\s[bB]\.$")
        self.assertRegex(hits[0].snippet, r"[.!?]$")

    def test_tuple_pages_preserve_original_page_number(self) -> None:
        pages = [(203, "WARNINGS: This is for use with the device. Do not reuse it.")]
        hits = search_pages(pages, "warning reuse", max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].page, 203)

    def test_section_is_extracted_from_nearby_heading(self) -> None:
        page = (
            "WARNINGS\n"
            "This device is for single use only. Do not reuse it. "
            "Reuse may cause patient injury."
        )
        hits = search_pages([page], "reuse warning", max_hits=5)
        self.assertEqual(hits[0].section, "WARNINGS")


class ParsePdfLimitedTests(unittest.TestCase):
    def _reader_factory(self, total: int):
        class FakePage:
            def __init__(self, index: int) -> None:
                self.index = index

            def extract_text(self) -> str:
                return f"Page {self.index + 1}"

        class FakeReader:
            def __init__(self, _stream: object) -> None:
                self.pages = [FakePage(i) for i in range(total)]

        return FakeReader

    def test_normal_question_parses_first_150_pages_only(self) -> None:
        with patch("ifu_answer.pypdf.PdfReader", self._reader_factory(220)):
            pages = _parse_pdf_limited(b"%PDF", "what size trocar is needed")

        self.assertEqual(len(pages), 150)
        self.assertEqual(pages[0], (1, "Page 1"))
        self.assertEqual(pages[-1], (150, "Page 150"))

    def test_warning_question_includes_last_30_pages_with_real_page_numbers(self) -> None:
        with patch("ifu_answer.pypdf.PdfReader", self._reader_factory(220)):
            pages = _parse_pdf_limited(b"%PDF", "what are the warnings for reuse")

        page_numbers = [page_num for page_num, _text in pages]
        self.assertEqual(len(pages), 180)
        self.assertEqual(page_numbers[:2], [1, 2])
        self.assertIn(150, page_numbers)
        self.assertNotIn(151, page_numbers)
        self.assertEqual(page_numbers[-30], 191)
        self.assertEqual(page_numbers[-1], 220)


# ------------------------------------------------------------------
# IFUAnswerer unit tests (session mocked)
# ------------------------------------------------------------------

class IFUAnswererTests(unittest.TestCase):
    def _make_answerer(
        self,
        viewer_raw: str,
        pdf_pages: list[str] | BaseException,
        session_ready: bool = True,
    ) -> tuple[IFUAnswerer, FakeOpener]:
        if isinstance(pdf_pages, BaseException):
            pdf_response: bytes | BaseException = pdf_pages
        else:
            pdf_response = b"%PDF-1.4 fake"

        opener = FakeOpener([viewer_raw, pdf_response])
        if isinstance(pdf_pages, list):
            parser = _fake_pdf_parser(pdf_pages)
        else:
            parser = None

        answerer = IFUAnswerer(pdf_parser=parser)
        answerer._opener = opener
        answerer._session_ready = session_ready
        return answerer, opener

    def test_returns_hits_when_term_found(self) -> None:
        pages = ["WARNINGS: Do not reuse. Check sterilization label."]
        answerer, _ = self._make_answerer(_viewer_response(), pages)

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "what are the warnings for reuse",
        )

        self.assertIsNone(result.error)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].page, 1)
        self.assertIn("WARNINGS", result.hits[0].snippet)

    def test_direct_pdf_url_skips_viewer_and_returns_hits(self) -> None:
        opener = FakeOpener([b"%PDF-1.4 fake"])
        answerer = IFUAnswerer(pdf_parser=_fake_pdf_parser(["Contraindications: do not use if package is damaged."]))
        answerer._opener = opener

        result = answerer.answer(
            "https://eifu.edwards.com/eifu/abc/DOC-0215896A_JO.pdf",
            "contraindications",
        )

        self.assertIsNone(result.error)
        self.assertEqual(len(opener.calls), 1)
        self.assertIn("DOC-0215896A_JO.pdf", result.document_title or "")
        self.assertEqual(result.hits[0].page, 1)

    def test_returns_empty_hits_when_no_match(self) -> None:
        pages = ["Storage: keep below 25°C. Sterile until package is opened."]
        answerer, _ = self._make_answerer(_viewer_response(), pages)

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "pacemaker insertion battery",
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.hits, [])

    def test_error_on_no_pdf_url_in_viewer(self) -> None:
        answerer, _ = self._make_answerer("<div>no iframe here</div>", [])

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "warnings",
        )

        self.assertIsNotNone(result.error)
        self.assertIn("PDF URL", result.error)
        self.assertEqual(result.hits, [])

    def test_error_on_pdf_fetch_failure(self) -> None:
        import urllib.error
        exc = urllib.error.HTTPError("url", 403, "forbidden", {}, None)
        answerer, _ = self._make_answerer(_viewer_response(), exc)

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "warnings",
        )

        self.assertIsNotNone(result.error)
        self.assertIn("403", result.error)

    def test_session_not_re_established_when_ready(self) -> None:
        pages = ["Contraindications: none known."]
        answerer, opener = self._make_answerer(_viewer_response(), pages, session_ready=True)

        answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "contraindications",
        )

        # No session gate calls; only viewer + PDF
        self.assertEqual(len(opener.calls), 2)
        self.assertNotIn("welcome", opener.calls[0])
        self.assertNotIn("accept-terms", opener.calls[0])

    def test_result_contains_no_pdf_bytes(self) -> None:
        pages = ["Shelf life: 5 years from manufacture date."]
        answerer, _ = self._make_answerer(_viewer_response(), pages)

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "shelf life",
        )

        # Verify AnswerResult holds only metadata + text snippets, not bytes
        result_dict = {
            "hits": [{"page": h.page, "snippet": h.snippet} for h in result.hits],
            "pdf_url": result.pdf_url,
            "source_url": result.source_url,
            "document_title": result.document_title,
            "timing_ms": result.timing_ms,
            "error": result.error,
        }
        serialized = str(result_dict)
        self.assertNotIn("%PDF", serialized)
        self.assertNotIn("pdf_bytes", serialized)

    def test_gate_page_in_viewer_triggers_session_reset(self) -> None:
        welcome_html = (
            '<form id="f"><input name="form_build_id" value="fb1">'
            '<input name="site_user" value="hcp" id="edit-site-user-hcp"></form>'
        )
        terms_html = (
            '<form action="/accept-terms-conditions">'
            '<input name="form_build_id" value="fb2">'
            '<input name="acknowledge" id="edit-acknowledge"></form>'
        )
        pages = ["Reuse warning: device is single use only."]
        viewer = _viewer_response()

        opener = FakeOpener([
            # First viewer call returns gate page
            _gate_response("welcome"),
            # Session re-init: welcome GET, welcome POST, terms GET, terms POST
            welcome_html,
            "<html><body>ok</body></html>",
            terms_html,
            "<html><body>ok</body></html>",
            # Second viewer call succeeds
            viewer,
            # PDF fetch
            b"%PDF-1.4 fake",
        ])

        # Subclass that preserves the injected opener across _reset_session calls
        class InjectableAnswerer(IFUAnswerer):
            def _reset_session(self) -> None:
                self._session_ready = False

        answerer = InjectableAnswerer(pdf_parser=_fake_pdf_parser(pages))
        answerer._opener = opener
        answerer._session_ready = True  # appears ready, but viewer returns gate

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "reuse warning",
        )

        self.assertIsNone(result.error, f"Expected no error, got: {result.error}")
        self.assertTrue(len(result.hits) >= 1)

    def test_timing_keys_present(self) -> None:
        pages = ["Precautions: inspect before use."]
        answerer, _ = self._make_answerer(_viewer_response(), pages)

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "precautions",
        )

        for key in ("session_ms", "viewer_ms", "fetch_ms", "parse_ms", "search_ms", "total_ms"):
            self.assertIn(key, result.timing_ms, f"Missing timing key: {key}")

    def test_document_title_extracted_from_viewer(self) -> None:
        viewer = _viewer_response(doc_title="TECNIS Eyhance GIB00 IFU")
        pages = ["Indications for use: aphakic correction."]
        answerer, _ = self._make_answerer(viewer, pages)

        result = answerer.answer(
            "https://www.e-ifu.com/viewpdf-iframe/1/1/0/X",
            "indications",
        )

        self.assertEqual(result.document_title, "TECNIS Eyhance GIB00 IFU")


class GatePageDetectionTests(unittest.TestCase):
    def test_valid_result_page_is_not_gate(self) -> None:
        html = '<div class="doc-info-row"><a href="/viewpdf-iframe/1/1/0/X">IFU</a></div>'
        self.assertFalse(_is_gate_page(html))

    def test_welcome_form_is_gate(self) -> None:
        html = '<form><input name="site_user" value="hcp" id="edit-site-user-hcp"></form>'
        self.assertTrue(_is_gate_page(html))

    def test_terms_form_is_gate(self) -> None:
        html = '<form action="/accept-terms-conditions"><input name="acknowledge"></form>'
        self.assertTrue(_is_gate_page(html))

    def test_fetchpdf_url_presence_overrides_gate_detection(self) -> None:
        html = (
            '<form action="/accept-terms-conditions">'
            '<iframe src="/fetchPdf/1/1/0/eifu/test.pdf"></iframe></form>'
        )
        self.assertFalse(_is_gate_page(html))


if __name__ == "__main__":
    unittest.main()
