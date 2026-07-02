from __future__ import annotations

import unittest

from vault import VECTOR_SIZE, stable_point_id, validate_embedding


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
