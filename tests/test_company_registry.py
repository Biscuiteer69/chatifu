from __future__ import annotations

import unittest

from company_registry import match_company_terms


class CompanyRegistryTests(unittest.TestCase):
    def test_alias_matching_works(self) -> None:
        matches = match_company_terms("st jude medical balloon catheter")
        self.assertEqual(matches[0].canonical_name, "Abbott")

    def test_parent_subbrand_matching_works(self) -> None:
        matches = match_company_terms("ethicon endopath xcel trocar")
        self.assertEqual(matches[0].canonical_name, "Johnson & Johnson MedTech")

    def test_unknown_manufacturer_does_not_block_generic_fallback(self) -> None:
        self.assertEqual(match_company_terms("unknownco 12mm trocar"), [])


if __name__ == "__main__":
    unittest.main()
