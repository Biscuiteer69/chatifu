from __future__ import annotations

import unittest

from medical_device_vocab import (
    parse_medical_device_query,
    problem_categories_for_terms,
    score_device_candidate,
    score_device_candidate_details,
)


class MedicalDeviceQueryParsingTests(unittest.TestCase):
    def test_typo_cleanup_and_structured_parse(self) -> None:
        parsed = parse_medical_device_query(
            "i have a 12mm ethicon trocar asnd its leaking air, "
            "what can I do to trouble shoot it?"
        )

        self.assertIn(" and ", parsed.cleaned_query)
        self.assertNotIn("asnd", parsed.cleaned_query)
        self.assertIn("ethicon", parsed.manufacturer_terms)
        self.assertIn("LAPAROSCOPIC_TROCAR_ACCESS", parsed.detected_concepts)
        self.assertIn("12mm", parsed.size_terms)
        self.assertIn("air leak", parsed.problem_terms)
        self.assertIn("ethicon", parsed.search_terms)
        self.assertIn("12mm", parsed.search_terms)
        self.assertIn("trocar", parsed.search_terms)
        self.assertIn("access", parsed.search_terms)
        self.assertIn("cannula", parsed.search_terms)
        self.assertIn("sleeve", parsed.search_terms)
        self.assertNotIn("have", parsed.search_terms)
        self.assertNotIn("asnd", parsed.search_terms)
        self.assertNotIn("leaking", parsed.search_terms)
        self.assertNotIn("air", parsed.search_terms)
        self.assertNotIn("trouble", parsed.search_terms)
        self.assertNotIn("shoot", parsed.search_terms)

    def test_trocar_query_detects_laparoscopic_access(self) -> None:
        parsed = parse_medical_device_query("12mm trocar leaking air pneumo")
        self.assertEqual(parsed.detected_concepts[0], "LAPAROSCOPIC_TROCAR_ACCESS")

    def test_blake_drain_trocar_query_detects_drain_first(self) -> None:
        parsed = parse_medical_device_query("blake drain trocar spike")
        self.assertEqual(parsed.detected_concepts[0], "DRAIN_TROCAR_OR_SPIKE")

    def test_new_device_concepts_are_detected(self) -> None:
        cases = {
            "laparoscopic grasper clip applier cartridge": "LAPAROSCOPIC_INSTRUMENTS",
            "ethicon harmonic generator error code": "ENERGY_DEVICES",
            "bd catheter balloon wont deflate": "CATHETERS",
            "hip implant pedicle screw": "IMPLANTS",
            "blake silicone drain reservoir": "DRAINS",
            "stryker tibial nail screw guide ifu": "ORTHOPEDIC_SYSTEMS",
            "olympus colonoscope leak test failed": "ENDOSCOPY_DEVICES",
            "echelon stapler reload staple line problem": "STAPLERS_RELOADS",
        }
        for query, concept in cases.items():
            with self.subTest(query=query):
                self.assertIn(concept, parse_medical_device_query(query).detected_concepts)

    def test_problem_terms_stay_out_of_primary_search_terms(self) -> None:
        parsed = parse_medical_device_query("12mm trocar leaking air trouble shoot")
        self.assertIn("air leak", parsed.problem_terms)
        self.assertIn("LEAK_OR_SEAL_PROBLEM", problem_categories_for_terms(parsed.problem_terms))
        self.assertNotIn("air", parsed.search_terms)
        self.assertNotIn("leaking", parsed.search_terms)
        self.assertNotIn("trouble", parsed.search_terms)
        self.assertNotIn("shoot", parsed.search_terms)


class MedicalDeviceScoringTests(unittest.TestCase):
    def test_ethicon_trocar_problem_ranks_access_candidate_first(self) -> None:
        parsed = parse_medical_device_query(
            "i have a 12mm ethicon trocar and its leaking air, "
            "what can I do to troubleshoot it?"
        )
        candidates = [
            {"company_name": "Stryker", "brand_name": "AIR", "catalog_number": "AIR"},
            {"company_name": "ETHICON ENDO-SURGERY, LLC", "brand_name": "BIOPATCH", "catalog_number": "BIO"},
            {
                "company_name": "ETHICON ENDO-SURGERY, LLC",
                "brand_name": "ENDOPATH XCEL Trocar",
                "catalog_number": "TR12",
            },
            {"company_name": "ETHICON ENDO-SURGERY, LLC", "brand_name": "BLAKE", "catalog_number": "BLK"},
        ]

        ranked = sorted(candidates, key=lambda c: score_device_candidate(parsed, c), reverse=True)

        self.assertEqual(ranked[0]["brand_name"], "ENDOPATH XCEL Trocar")
        self.assertNotEqual(ranked[0]["brand_name"], "AIR")
        self.assertLess(
            score_device_candidate(parsed, candidates[1]),
            score_device_candidate(parsed, candidates[2]),
        )
        self.assertLess(
            score_device_candidate(parsed, candidates[3]),
            score_device_candidate(parsed, candidates[2]),
        )

    def test_no_brand_trocar_query_ranks_access_above_air_product(self) -> None:
        parsed = parse_medical_device_query("12mm trocar leaking air")
        air = {"company_name": "Stryker", "brand_name": "AIR", "catalog_number": "AIR"}
        access = {
            "company_name": "ETHICON ENDO-SURGERY, LLC",
            "brand_name": "ENDOPATH XCEL Trocar 12mm",
            "catalog_number": "TR12",
        }
        self.assertGreater(
            score_device_candidate(parsed, access),
            score_device_candidate(parsed, air),
        )

    def test_explicit_stryker_air_query_can_rank_air_first(self) -> None:
        parsed = parse_medical_device_query("stryker air product")
        air = {"company_name": "Stryker", "brand_name": "AIR", "catalog_number": "AIR"}
        access = {
            "company_name": "ETHICON ENDO-SURGERY, LLC",
            "brand_name": "ENDOPATH XCEL Trocar 12mm",
            "catalog_number": "TR12",
        }
        self.assertGreater(
            score_device_candidate(parsed, air),
            score_device_candidate(parsed, access),
        )

    def test_score_explanations_include_positive_and_negative_reasons(self) -> None:
        parsed = parse_medical_device_query(
            "i have a 12mm ethicon trocar and its leaking air, what can I do to troubleshoot it?"
        )
        endopath = {
            "company_name": "ETHICON ENDO-SURGERY, LLC",
            "brand_name": "ENDOPATH XCEL Trocar",
            "catalog_number": "TR12",
            "model_number": "12MM",
        }
        air = {"company_name": "Stryker", "brand_name": "AIR", "catalog_number": "AIR"}

        good = score_device_candidate_details(parsed, endopath)
        bad = score_device_candidate_details(parsed, air)

        self.assertGreater(good.score, bad.score)
        self.assertGreater(good.confidence, bad.confidence)
        self.assertTrue(any("manufacturer match" in r.label for r in good.reasons))
        self.assertTrue(any("device concept match" in r.label for r in good.reasons))
        self.assertTrue(any("manufacturer mismatch" in r.label for r in bad.reasons))
        self.assertTrue(any("AIR matched leak problem" in r.label for r in bad.reasons))


if __name__ == "__main__":
    unittest.main()
