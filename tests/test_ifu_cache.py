from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ifu_cache import IFUDocumentCache, _normalize_url


class IFUDocumentCacheTests(unittest.TestCase):
    def test_put_and_get_pdf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IFUDocumentCache(cache_dir=tmp)
            doc = cache.put("https://example.com/ifu.pdf", b"%PDF-1.7 cache test", {})
            self.assertTrue(Path(doc.path).exists())
            self.assertEqual(cache.get("https://example.com/ifu.pdf"), b"%PDF-1.7 cache test")

    def test_expired_cache_refetches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IFUDocumentCache(cache_dir=tmp, ttl_days=-1)
            calls = 0

            def fetcher() -> bytes:
                nonlocal calls
                calls += 1
                return b"%PDF-1.7 refetch test"

            cache.get_or_fetch("https://example.com/ifu.pdf", fetcher)
            cache.get_or_fetch("https://example.com/ifu.pdf", fetcher)
            self.assertEqual(calls, 2)

    def test_rejects_html_landing_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IFUDocumentCache(cache_dir=tmp)
            with self.assertRaises(ValueError):
                cache.put("https://example.com/product", b"<html>not a pdf</html>", {"content_type": "text/html"})

    def test_cache_hit_used_on_second_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IFUDocumentCache(cache_dir=tmp)
            calls = 0

            def fetcher() -> bytes:
                nonlocal calls
                calls += 1
                return b"%PDF-1.7 only once"

            _body, _doc, hit1 = cache.get_or_fetch("https://example.com/ifu.pdf", fetcher)
            _body, _doc, hit2 = cache.get_or_fetch("https://example.com/ifu.pdf", fetcher)
            self.assertFalse(hit1)
            self.assertTrue(hit2)
            self.assertEqual(calls, 1)


class PresignedURLKeyTests(unittest.TestCase):
    """A rotating signature must not look like a different document — and a query that
    identifies the document must never be thrown away.

    Stryker and Zimmer serve from presigned S3 and the signature rotates every few
    hours. Keying on the whole URL meant a fresh key each re-mint, so the cache never
    hit and every request went back to the network; once the stored link expired the
    tester saw "PDF fetch failed: HTTP Error 403". That was 21 of 33 logged misses.

    The opposite mistake is worse. 405,110 of 743,564 stored documents carry a query
    that IS their identity, so dropping the query wholesale would collapse them onto a
    shared path and serve another device's IFU.
    """

    S3 = ("https://s3-qrd-prd-docs.s3.eu-west-1.amazonaws.com"
          "/stryker/documents/3d2881b5.pdf")

    def _signed(self, sig: str, date: str) -> str:
        return (f"{self.S3}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date={date}"
                f"&X-Amz-Expires=21600&X-Amz-Signature={sig}"
                "&response-content-type=application%2Fpdf")

    def test_rotated_signature_is_the_same_document(self) -> None:
        a = self._signed("aaaa", "20260806T100000Z")
        b = self._signed("zzzz", "20260807T050000Z")
        self.assertEqual(_normalize_url(a), _normalize_url(b))
        self.assertEqual(_normalize_url(a), self.S3)

    def test_cache_hits_across_a_re_mint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IFUDocumentCache(cache_dir=tmp)
            cache.put(self._signed("aaaa", "20260806T100000Z"), b"%PDF-1.7 stryker", {})
            # the same document, fetched again after the link was re-minted
            self.assertEqual(cache.get(self._signed("zzzz", "20260807T050000Z")),
                             b"%PDF-1.7 stryker")

    def test_identifying_query_params_stay_distinct(self) -> None:
        for a, b in (
            ("https://alphatecspine.com/?wpdmdl=111", "https://alphatecspine.com/?wpdmdl=222"),
            ("https://nv.example/d?docId=A&compId=1", "https://nv.example/d?docId=B&compId=1"),
            ("https://sie.example/f?document-id=98", "https://sie.example/f?document-id=99"),
        ):
            self.assertNotEqual(_normalize_url(a), _normalize_url(b),
                                f"{a} and {b} must not share a cache key")

    def test_urls_without_volatile_params_are_untouched(self) -> None:
        # keeps every key already written to the 25,934-entry cache valid
        for url in ("https://nv.example/d?docId=A&compId=1&contRep=X&pVersion=2",
                    "https://example.com/plain.pdf"):
            self.assertEqual(_normalize_url(url), url)


if __name__ == "__main__":
    unittest.main()
