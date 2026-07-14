"""Tests for the beta serving layer (api.py).

Covers the invite-code auth matrix, the in-document highlight response
contract (/answer), device search/lookup, PDF proxy streaming, and the
/answer LRU cache. The heavy data layer (SQLite, resolvers, PDF fetch) is
patched out so these run offline with no DB.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from ifu_answer import AnswerHit, AnswerResult


def build_client() -> TestClient:
    # Deterministic auth config regardless of ambient env.
    api.ALLOW_UNAUTHENTICATED = False
    api.BETA_CODES = {"beta-good"}
    api.API_TOKEN = "admin-token"
    api._answer_cache.clear()
    return TestClient(api.create_app())


def make_answer() -> AnswerResult:
    return AnswerResult(
        hits=[AnswerHit(page=12, snippet="Sterilize before first use.", section="Cleaning")],
        source_url="https://e-ifu.com/doc/123",
        document_title="Widget IFU",
        timing_ms={"total_ms": 42.0},
        document_url="https://e-ifu.com/doc/123.pdf",
        page_count=30,
    )


class BetaAuthMatrix(unittest.TestCase):
    def setUp(self) -> None:
        self.client = build_client()

    def test_answer_requires_credentials(self) -> None:
        resp = self.client.post("/answer", json={"catalog": "17-0186", "question": "how to clean?"})
        self.assertEqual(resp.status_code, 401)

    def test_answer_rejects_bad_beta_code(self) -> None:
        resp = self.client.post(
            "/answer",
            json={"catalog": "17-0186", "question": "how to clean?"},
            headers={"X-Beta-Code": "nope"},
        )
        self.assertEqual(resp.status_code, 401)

    @patch("api._resolve_ifu_url", return_value="https://e-ifu.com/doc/123")
    def test_answer_accepts_beta_code(self, _resolve) -> None:
        with patch.object(api, "_get_answerer") as get_ans:
            get_ans.return_value.answer.return_value = make_answer()
            resp = self.client.post(
                "/answer",
                json={"catalog": "17-0186", "question": "how to clean?"},
                headers={"X-Beta-Code": "beta-good"},
            )
        self.assertEqual(resp.status_code, 200)

    @patch("api._resolve_ifu_url", return_value="https://e-ifu.com/doc/123")
    def test_answer_accepts_admin_bearer(self, _resolve) -> None:
        with patch.object(api, "_get_answerer") as get_ans:
            get_ans.return_value.answer.return_value = make_answer()
            resp = self.client.post(
                "/answer",
                json={"catalog": "17-0186", "question": "how to clean?"},
                headers={"Authorization": "Bearer admin-token"},
            )
        self.assertEqual(resp.status_code, 200)


class AnswerContract(unittest.TestCase):
    def setUp(self) -> None:
        self.client = build_client()
        self.headers = {"X-Beta-Code": "beta-good"}

    @patch("api._resolve_ifu_url", return_value="https://e-ifu.com/doc/123")
    def test_answer_shape(self, _resolve) -> None:
        with patch.object(api, "_get_answerer") as get_ans:
            get_ans.return_value.answer.return_value = make_answer()
            resp = self.client.post(
                "/answer",
                json={"catalog": "17-0186", "question": "how to clean?"},
                headers=self.headers,
            )
        data = resp.json()
        self.assertEqual(data["catalog"], "17-0186")
        self.assertEqual(data["document_title"], "Widget IFU")
        self.assertEqual(data["page_count"], 30)
        # The proxy path pins the document the hits came from. A device can map
        # to several official IFUs and the answer may not come from the
        # top-ranked one, so without this PDF.js would highlight page N of a
        # different document.
        self.assertTrue(data["pdf_proxy_path"].startswith("/ifu/pdf?catalog=17-0186"))
        self.assertIn("document_url=", data["pdf_proxy_path"])
        self.assertEqual(len(data["hits"]), 1)
        hit = data["hits"][0]
        self.assertEqual({"page", "section", "snippet"}, set(hit.keys()))
        self.assertEqual(hit["page"], 12)
        self.assertFalse(data["cached"])

    @patch("api._resolve_ifu_url", return_value="https://e-ifu.com/doc/123")
    def test_answer_cache_hit(self, _resolve) -> None:
        with patch.object(api, "_get_answerer") as get_ans:
            get_ans.return_value.answer.return_value = make_answer()
            body = {"catalog": "17-0186", "question": "How to CLEAN?"}
            first = self.client.post("/answer", json=body, headers=self.headers).json()
            second = self.client.post("/answer", json=body, headers=self.headers).json()
            # answerer invoked once; second served from cache.
            self.assertEqual(get_ans.return_value.answer.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])


class DeviceEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.client = build_client()
        self.headers = {"X-Beta-Code": "beta-good"}

    @patch("api.search_devices")
    def test_device_search(self, search) -> None:
        search.return_value = [{"catalog_number": "17-0186", "brand_name": "Widget"}]
        resp = self.client.get("/device/search", params={"q": "widget"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["devices"][0]["catalog_number"], "17-0186")

    @patch("api.get_best_ifu_url", return_value="https://e-ifu.com/doc/123")
    @patch("api.get_device", return_value={"catalog_number": "17-0186", "brand_name": "Widget"})
    def test_device_lookup(self, _dev, _url) -> None:
        resp = self.client.get("/device/lookup", params={"catalog": "17-0186"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["has_ifu"])

    @patch("api.get_device", return_value=None)
    def test_device_lookup_404(self, _dev) -> None:
        resp = self.client.get("/device/lookup", params={"catalog": "nope"}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)


class PdfProxy(unittest.TestCase):
    def setUp(self) -> None:
        self.client = build_client()
        self.headers = {"X-Beta-Code": "beta-good"}

    @patch("api._get_ifu_cache", return_value=None)
    @patch("api._resolve_ifu_url", return_value="https://e-ifu.com/doc/123.pdf")
    def test_pdf_stream_no_cache(self, _resolve, _cache) -> None:
        with patch.object(api, "_get_answerer") as get_ans:
            get_ans.return_value.fetch_pdf_bytes.return_value = (b"%PDF-1.7 fake", "u", "t")
            resp = self.client.get("/ifu/pdf", params={"catalog": "17-0186"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/pdf")
        self.assertEqual(resp.headers["x-chatifu-cache"], "miss")
        self.assertTrue(resp.content.startswith(b"%PDF"))


class RateLimitAndLogging(unittest.TestCase):
    def setUp(self) -> None:
        self.client = build_client()
        self.headers = {"X-Beta-Code": "beta-good"}
        api._rl_hits.clear()

    @patch("api._resolve_ifu_url", return_value="https://e-ifu.com/doc/123")
    def test_answer_rate_limited(self, _resolve) -> None:
        api.RATE_LIMITS["/answer"] = 2
        try:
            with patch.object(api, "_get_answerer") as get_ans:
                get_ans.return_value.answer.return_value = make_answer()
                # vary question so the answer cache doesn't short-circuit
                codes = [
                    self.client.post(
                        "/answer",
                        json={"catalog": "17-0186", "question": f"q number {i}?"},
                        headers=self.headers,
                    ).status_code
                    for i in range(4)
                ]
            self.assertEqual(codes[:2], [200, 200])
            self.assertIn(429, codes[2:])
        finally:
            api.RATE_LIMITS["/answer"] = 10

    def test_requests_are_logged(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            api.REQUEST_LOG = Path(d) / "requests.jsonl"
            self.client.get("/device/search", params={"q": "x"}, headers=self.headers)
            self.assertTrue(api.REQUEST_LOG.exists())
            lines = api.REQUEST_LOG.read_text().strip().splitlines()
            self.assertTrue(any('"path": "/device/search"' in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()


class IfuPdfDocumentUrlGuard(unittest.TestCase):
    """document_url must be one of the catalog's own official IFUs.

    Streaming an arbitrary caller-supplied URL would turn this endpoint into an
    open proxy into the DGX's network.
    """

    def setUp(self) -> None:
        self.client = build_client()
        self.headers = {"X-Beta-Code": "beta-good"}

    def test_foreign_document_url_is_rejected(self) -> None:
        resp = self.client.get(
            "/ifu/pdf",
            params={"catalog": "17-0186", "document_url": "http://169.254.169.254/latest/meta-data/"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not an official IFU", resp.json()["detail"])
