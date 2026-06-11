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
    _infer_target_sections,
    _is_toc_page,
    _is_body_heading,
    _section_name_matches,
    _extract_section_body,
    _section_aware_search,
    _is_storage_question,
    _find_storage_passage,
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

    def test_direct_pdf_url_preserves_existing_percent_encoding(self) -> None:
        opener = FakeOpener([b"%PDF-1.4 fake"])
        answerer = IFUAnswerer(pdf_parser=_fake_pdf_parser(["Warnings: inspect the device before use."]))
        answerer._opener = opener

        result = answerer.answer(
            "https://manuals.eifu.abbott/content/dam/av/EL2106481%2520Rev.%2520B.pdf",
            "warnings",
        )

        self.assertIsNone(result.error)
        self.assertIn("%2520Rev.%2520B.pdf", opener.calls[0])
        self.assertNotIn("%252520Rev", opener.calls[0])

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


# ------------------------------------------------------------------
# Section-aware extraction unit tests
# ------------------------------------------------------------------

class InferTargetSectionsTests(unittest.TestCase):
    def test_contraindications_maps_correctly(self) -> None:
        targets = _infer_target_sections("what are the contraindications?")
        self.assertEqual(targets[0], "CONTRAINDICATIONS")
        self.assertIn("CONTRAINDICATIONS", targets)

    def test_warnings_maps_correctly(self) -> None:
        targets = _infer_target_sections("list the warnings")
        self.assertIn("WARNINGS", targets)

    def test_shelf_life_phrase_takes_priority_over_shelf(self) -> None:
        targets = _infer_target_sections("what is the shelf life?")
        self.assertIn("STORAGE", targets)

    def test_mri_maps_to_mri_safety(self) -> None:
        targets = _infer_target_sections("is this device MRI safe?")
        self.assertTrue(any("MRI" in t for t in targets))

    def test_unknown_question_returns_empty(self) -> None:
        self.assertEqual(_infer_target_sections("how many units per box?"), [])


class IsTocPageTests(unittest.TestCase):
    def test_dot_leader_page_is_toc(self) -> None:
        text = (
            "Table of Contents\n"
            "CONTRAINDICATIONS ......... 4\n"
            "WARNINGS .................. 5\n"
            "STORAGE ................... 9\n"
        )
        self.assertTrue(_is_toc_page(text))

    def test_body_page_is_not_toc(self) -> None:
        text = (
            "2.0 CONTRAINDICATIONS\n"
            "This device should not be used in patients with known hypersensitivity.\n"
            "Active systemic infection is also a contraindication.\n"
        )
        self.assertFalse(_is_toc_page(text))

    def test_many_toc_ref_lines_flagged(self) -> None:
        text = "WARNINGS 5\nCONTRAINDICATIONS 4\nSTORAGE 9\nINDICATIONS 3\n"
        self.assertTrue(_is_toc_page(text))


class IsBodyHeadingTests(unittest.TestCase):
    def test_all_caps_heading(self) -> None:
        is_h, name = _is_body_heading("CONTRAINDICATIONS")
        self.assertTrue(is_h)
        self.assertEqual(name, "CONTRAINDICATIONS")

    def test_numbered_heading_all_caps(self) -> None:
        is_h, name = _is_body_heading("2.0 CONTRAINDICATIONS")
        self.assertTrue(is_h)
        self.assertIn("CONTRAINDICATIONS", name)

    def test_numbered_heading_mixed_case(self) -> None:
        is_h, name = _is_body_heading("12.1 MRI Safety Information")
        self.assertTrue(is_h)
        self.assertIn("MRI", name)

    def test_sentence_not_a_heading(self) -> None:
        is_h, _ = _is_body_heading("This device is for single use only.")
        self.assertFalse(is_h)

    def test_toc_entry_with_dot_leaders_rejected(self) -> None:
        is_h, _ = _is_body_heading("CONTRAINDICATIONS ......... 4")
        self.assertFalse(is_h)

    def test_toc_entry_with_trailing_page_number_rejected(self) -> None:
        is_h, _ = _is_body_heading("CONTRAINDICATIONS 4")
        self.assertFalse(is_h)

    def test_bare_page_number_rejected(self) -> None:
        is_h, _ = _is_body_heading("42")
        self.assertFalse(is_h)

    def test_warnings_and_precautions(self) -> None:
        is_h, name = _is_body_heading("WARNINGS AND PRECAUTIONS")
        self.assertTrue(is_h)
        self.assertIn("WARNINGS", name)


class SectionNameMatchesTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertTrue(_section_name_matches("CONTRAINDICATIONS", "CONTRAINDICATIONS"))

    def test_target_in_heading(self) -> None:
        self.assertTrue(_section_name_matches("WARNINGS AND PRECAUTIONS", "WARNINGS"))

    def test_key_words_match(self) -> None:
        self.assertTrue(_section_name_matches("MRI SAFETY INFORMATION", "MRI SAFETY"))

    def test_no_match(self) -> None:
        self.assertFalse(_section_name_matches("INDICATIONS FOR USE", "CONTRAINDICATIONS"))


class ExtractSectionBodyTests(unittest.TestCase):
    def test_extracts_until_next_heading(self) -> None:
        pages = [
            "2.0 CONTRAINDICATIONS\nDo not use in infected patients.\nAvoid reuse.\n"
            "3.0 WARNINGS\nWarnings text here.\n"
        ]
        body = _extract_section_body(pages, 0, len("2.0 CONTRAINDICATIONS\n"))
        self.assertIn("infected", body)
        self.assertNotIn("Warnings text", body)

    def test_continues_across_pages(self) -> None:
        pages = [
            "2.0 CONTRAINDICATIONS\nFirst contraindication text.\n",
            "Additional contraindication detail.\n3.0 WARNINGS\nWarning here.\n",
        ]
        body = _extract_section_body(pages, 0, len("2.0 CONTRAINDICATIONS\n"))
        self.assertIn("First contraindication", body)
        self.assertIn("Additional contraindication", body)
        self.assertNotIn("Warning here", body)


class SectionAwareSearchTests(unittest.TestCase):
    def test_finds_contraindications_section(self) -> None:
        pages = [
            "Product description. This is the SGC0101 model, supplied sterile.\n",
            "CONTRAINDICATIONS\nDo not use in patients with active infection.\nAvoid in hypersensitive patients.\n",
        ]
        hits = _section_aware_search(pages, "contraindications", ["CONTRAINDICATIONS"], max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertNotIn("supplied", hits[0].snippet)
        self.assertIn("infection", hits[0].snippet)

    def test_skips_toc_page(self) -> None:
        pages = [
            "CONTRAINDICATIONS ......... 4\nWARNINGS .................. 5\n",
            "CONTRAINDICATIONS\nDo not use in infected patients.\n",
        ]
        hits = _section_aware_search(pages, "contraindications", ["CONTRAINDICATIONS"], max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertIn("infected", hits[0].snippet)

    def test_search_pages_uses_section_aware_path(self) -> None:
        pages = [
            "SGC0101 is supplied sterile and ready for use.\n",
            "CONTRAINDICATIONS\nDo not use in patients with active infection.\n"
            "Also contraindicated in patients with known hypersensitivity.\n",
        ]
        hits = search_pages(pages, "what are the contraindications", max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertNotIn("supplied", hits[0].snippet)
        self.assertIn("infection", hits[0].snippet)

    def test_falls_back_to_keyword_search_when_no_section(self) -> None:
        pages = [
            "The device has a 5-year shelf life. Store at room temperature.",
        ]
        hits = search_pages(pages, "shelf life", max_hits=5)
        self.assertEqual(len(hits), 1)
        self.assertIn("shelf life", hits[0].snippet.lower())


class IsBodyHeadingSynonymTests(unittest.TestCase):
    def test_title_case_storage_is_heading(self) -> None:
        is_h, name = _is_body_heading("Storage")
        self.assertTrue(is_h)
        self.assertEqual(name, "STORAGE")

    def test_title_case_contraindications_is_heading(self) -> None:
        is_h, name = _is_body_heading("Contraindications")
        self.assertTrue(is_h)
        self.assertEqual(name, "CONTRAINDICATIONS")

    def test_storage_with_trailing_page_number_is_not_heading(self) -> None:
        is_h, _ = _is_body_heading("Storage 14")
        self.assertFalse(is_h)

    def test_storage_and_handling_is_heading(self) -> None:
        is_h, name = _is_body_heading("Storage and Handling")
        self.assertTrue(is_h)
        self.assertEqual(name, "STORAGE AND HANDLING")

    def test_shelf_life_is_heading(self) -> None:
        is_h, name = _is_body_heading("Shelf Life")
        self.assertTrue(is_h)
        self.assertEqual(name, "SHELF LIFE")


class StoragePhraseScanTests(unittest.TestCase):
    def test_is_storage_question_detects_storage_keyword(self) -> None:
        self.assertTrue(_is_storage_question("what are the storage conditions and shelf life?"))

    def test_is_storage_question_detects_shelf(self) -> None:
        self.assertTrue(_is_storage_question("what is the shelf life?"))

    def test_is_storage_question_false_for_unrelated(self) -> None:
        self.assertFalse(_is_storage_question("is this device MRI safe?"))

    def test_find_storage_passage_detects_temperature_phrase(self) -> None:
        pages = [
            "SGC0101 is supplied sterile and packaged in a sealed tray.",
            "Store at or below 25°C. Avoid freezing. Shelf life: 3 years from manufacture.",
        ]
        hits = _find_storage_passage(pages)
        self.assertEqual(len(hits), 1)
        self.assertIn("25", hits[0].snippet)

    def test_find_storage_passage_skips_toc_pages(self) -> None:
        pages = [
            "STORAGE ......... 9\nWARNINGS ......... 5\nCONTRAINDICATIONS ......... 4\n",
            "Store at 2–25°C. Do not freeze. Shelf life is 2 years from date of manufacture.",
        ]
        hits = _find_storage_passage(pages)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].page, 2)

    def test_find_storage_passage_ignores_temperature_only_in_mri_section(self) -> None:
        pages = [
            # Only a bare temperature value — no dedicated storage indicator
            "Non-clinical testing shows MitraClip is MR Conditional. "
            "Temperature rise of less than 3°C after 15 minutes of continuous scanning.",
        ]
        hits = _find_storage_passage(pages)
        self.assertEqual(hits, [])


class MinCoverageGuardTests(unittest.TestCase):
    def test_mri_query_returns_empty_when_no_mri_section(self) -> None:
        pages = [
            "This is the Acme IOL implant. It has a catalog number of IOL-001.",
            "CONTRAINDICATIONS\nDo not use in patients with active infection.",
            "STORAGE\nStore below 25°C. Shelf life 3 years.",
        ]
        hits = search_pages(pages, "is this device MRI safe?", max_hits=5)
        self.assertEqual(hits, [])

    def test_section_intent_query_filters_single_term_match(self) -> None:
        # "is this device MRI safe?" → unique terms: ["device", "mri", "safe"]
        # min_coverage = 2. A page with only "MRI" (coverage=1) should be excluded.
        pages = [
            "Acme catalog model MRI-001 is shipped sterile in a sealed package.",
        ]
        hits = search_pages(pages, "is this device MRI safe?", max_hits=5)
        # "mri" matches, but "device" and "safe" do not → coverage=1 < min_coverage=2
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
