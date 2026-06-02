from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from resolvers.edwards_resolver import EdwardsResolver


EDWARDS_SEARCH_HTML = """
<html><body>
  <div class="searchResultEntry">
    <div class="row searchResultHeading"><h4>Edwards SAPIEN 3 Transcatheter Heart Valve System</h4></div>
    <div class="row resultMetadata">
      <label>IFU P/N</label>
      <div class="col-xs-7 show-minData">10051068002A_JO</div>
    </div>
    <div class="row resultMetadata">
      <label>Model numbers</label>
      <div class="col-xs-7 show-minData">S3TF320, 9600TFX20, 9600TFX23</div>
    </div>
    <div class="row resultMetadata">
      <label>Document languages</label>
      <div class="col-xs-7 show-minData">English, Swedish - Svenska</div>
    </div>
    <div class="row resultMetadata">
      <label>Effective date (YYYY-MM-DD)</label>
      <div class="col-xs-7 show-minData">2023-08-31</div>
    </div>
    <a class="btn" href="/eifu/pages/viewers/pdf?projectKey=abc&itemKey=def">View document</a>
    <a href="/eifu/abc/DOC-0215896A_JO.pdf" download>Download</a>
  </div>
</body></html>
"""


class FixtureEdwardsResolver(EdwardsResolver):
    def __init__(self, db_path: Path, response: str) -> None:
        super().__init__(db_path=db_path, delay_sec=0)
        self.response = response
        self.requested_urls: list[str] = []

    def _request(self, url: str, delay: bool = True) -> str:
        self.requested_urls.append(url)
        if ".pdf" in url:
            raise AssertionError("resolver attempted to download PDF content")
        return self.response


class EdwardsResolverTests(unittest.TestCase):
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

    def test_resolves_direct_pdf_metadata_without_downloading_pdf(self) -> None:
        resolver = FixtureEdwardsResolver(self.db_path, EDWARDS_SEARCH_HTML)

        documents = resolver.resolve("9600TFX20", model_number="9600TFX")

        self.assertEqual(len(documents), 1)
        self.assertEqual(
            documents[0]["document_url"],
            "https://eifu.edwards.com/eifu/abc/DOC-0215896A_JO.pdf",
        )
        self.assertEqual(documents[0]["match_confidence"], "exact_catalog")
        self.assertTrue(resolver.requested_urls)
        rows = self.rows()
        self.assertEqual(rows[0]["manufacturer_family"], "edwards")
        self.assertEqual(rows[0]["status"], "found")
        self.assertNotIn("PDF body", " ".join(str(value) for value in rows[0]))


if __name__ == "__main__":
    unittest.main()
