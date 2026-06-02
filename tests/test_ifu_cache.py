from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ifu_cache import IFUDocumentCache


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


if __name__ == "__main__":
    unittest.main()
