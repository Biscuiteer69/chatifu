from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import vault
from vault import VECTOR_SIZE, stable_point_id, validate_embedding


class PendingDevicesTests(unittest.TestCase):
    """Regression: pending_devices must not starve when the first rows by
    catalog_number are all processed (the old limit*50 scan window returned
    0 pending while unprocessed devices sat beyond the window)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_sqlite_path = vault.SQLITE_PATH
        vault.SQLITE_PATH = Path(self._tmp.name) / "test.sqlite3"

    def tearDown(self) -> None:
        vault.SQLITE_PATH = self._old_sqlite_path
        self._tmp.cleanup()

    def _seed(self, processed_count: int, total: int) -> None:
        conn = vault.sqlite()
        with conn:
            for i in range(total):
                catalog = f"CAT-{i:06d}"
                device = {"catalogNumber": catalog, "companyName": "Acme Medical"}
                conn.execute(
                    "insert into devices (id, company_name, brand_name, model_number, catalog_number, raw_json)"
                    " values (?, ?, ?, ?, ?, ?)",
                    (f"DI{i:06d}", "Acme Medical", "Brand", "", catalog, json.dumps(device)),
                )
                if i < processed_count:
                    conn.execute(
                        "insert into processed_skus (sku, status, source) values (?, 'ingested', 'test')",
                        (catalog,),
                    )
        conn.close()

    def test_pending_found_beyond_processed_prefix(self) -> None:
        # 600 processed devices sort first by catalog_number; the old
        # scan_limit=max(limit*50, 500)=500 window saw only processed rows.
        self._seed(processed_count=600, total=610)
        pending = vault.pending_devices("%acme%", limit=10)
        self.assertEqual(len(pending), 10)
        skus = {vault.device_sku(device) for device in pending}
        self.assertTrue(all(sku >= "CAT-000600" for sku in skus), skus)

    def test_all_processed_returns_empty(self) -> None:
        self._seed(processed_count=20, total=20)
        self.assertEqual(vault.pending_devices("%acme%", limit=5), [])

    def test_falls_back_to_model_then_id_for_sku(self) -> None:
        conn = vault.sqlite()
        with conn:
            conn.execute(
                "insert into devices (id, company_name, brand_name, model_number, catalog_number, raw_json)"
                " values ('DI-X', 'Acme Medical', 'Brand', 'MODEL-9', '', ?)",
                (json.dumps({"versionModelNumber": "MODEL-9", "companyName": "Acme Medical"}),),
            )
            conn.execute(
                "insert into processed_skus (sku, status, source) values ('MODEL-9', 'ingested', 'test')"
            )
        conn.close()
        self.assertEqual(vault.pending_devices("%acme%", limit=5), [])


class VaultHelperTests(unittest.TestCase):
    def test_stable_point_id_is_deterministic(self) -> None:
        first = stable_point_id(None, {"sku": "ABC", "chunk_index": 1})
        second = stable_point_id(None, {"chunk_index": 1, "sku": "ABC"})
        self.assertEqual(first, second)

    def test_validate_embedding_accepts_configured_size(self) -> None:
        self.assertEqual(len(validate_embedding([0] * VECTOR_SIZE)), VECTOR_SIZE)

    def test_validate_embedding_rejects_wrong_size(self) -> None:
        with self.assertRaises(ValueError):
            validate_embedding([0], "test-vector")


if __name__ == "__main__":
    unittest.main()
