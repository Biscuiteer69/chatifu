from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from resolvers.abbott_resolver import AbbottResolver


ABBOTT_SEARCH_JSON = {
    "status": True,
    "response": {
        "results": [
            {
                "title": "EL2144114 Rev. D - Abbott - MitraClip G5 System",
                "clickableuri": "https://manuals.eifu.abbott/content/dam/av/manuals-eifu/vascular/EL2144114%2520Artwork_rev%2520D.pdf",
                "sapproductmodelnumberlist": "CDS0701-XTW CDS0701-XT SGC0701",
                "sapproductdescriptionlist": "MitraClip G5 System",
                "effectivebegindate": "2025-05-09T00:00:00Z",
            }
        ],
        "totalCount": 1,
    },
}


class FixtureAbbottResolver(AbbottResolver):
    def __init__(self, db_path: Path, response: dict) -> None:
        super().__init__(db_path=db_path, delay_sec=0)
        self.response = response
        self.requests: list[tuple[str, dict]] = []

    def _request_json(self, url: str, payload: dict, delay: bool = True) -> dict:
        self.requests.append((url, payload))
        return self.response


class AbbottResolverTests(unittest.TestCase):
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
        resolver = FixtureAbbottResolver(self.db_path, ABBOTT_SEARCH_JSON)

        documents = resolver.resolve("CDS0701-XTW", model_number="CDS0701-XTW")

        self.assertEqual(len(documents), 1)
        self.assertEqual(
            documents[0]["document_url"],
            "https://manuals.eifu.abbott/content/dam/av/manuals-eifu/vascular/EL2144114%20Artwork_rev%20D.pdf",
        )
        self.assertEqual(documents[0]["match_confidence"], "exact_catalog")
        self.assertEqual(resolver.requests[0][1]["q"], "CDS0701-XTW")
        rows = self.rows()
        self.assertEqual(rows[0]["manufacturer_family"], "abbott")
        self.assertEqual(rows[0]["status"], "found")
        self.assertNotIn("PDF body", " ".join(str(value) for value in rows[0]))

    def test_top_ranked_exact_query_is_model_match_when_abbott_omits_model_fields(self) -> None:
        response = {
            "status": True,
            "response": {
                "results": [{
                    "title": "Abbott Vascular MITRACLIP NT Clip Delivery System - U.S.",
                    "clickableuri": "https://manuals.eifu.abbott/content/dam/av/manuals-eifu/vascular/EL2106481%2520Rev.%2520B.pdf",
                }],
                "totalCount": 1,
            },
        }
        resolver = FixtureAbbottResolver(self.db_path, response)

        documents = resolver.resolve("SGC0101", model_number="SGC0101")

        self.assertEqual(documents[0]["match_confidence"], "model_match")
        self.assertEqual(self.rows()[0]["status"], "found")


if __name__ == "__main__":
    unittest.main()
