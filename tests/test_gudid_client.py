from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gudid_client import GUDIDCache, GUDIDClient
from medical_device_vocab import parse_medical_device_query, score_device_candidate


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls = 0

    def open(self, req: object, timeout: int) -> FakeResponse:
        self.calls += 1
        return FakeResponse(self.payloads.pop(0))


class GudidClientTests(unittest.TestCase):
    def test_openfda_response_normalizes_and_scores(self) -> None:
        parsed = parse_medical_device_query("ethicon 12mm trocar leaking air")
        opener = FakeOpener([{
            "results": [{
                "company_name": "ETHICON ENDO-SURGERY, LLC",
                "brand_name": "ENDOPATH XCEL Trocar",
                "catalog_number": "B12LT",
                "version_or_model_number": "12MM",
                "device_description": "Laparoscopic access trocar cannula",
                "gmdn_terms": [{"name": "Laparoscopic trocar"}],
                "product_codes": ["GCJ"],
            }]
        }])
        with tempfile.TemporaryDirectory() as tmp:
            client = GUDIDClient(opener=opener, cache=GUDIDCache(Path(tmp) / "gudid.sqlite3"))
            devices = client.search_openfda_udi(parsed)

        self.assertEqual(len(devices), 1)
        candidate = devices[0].as_candidate()
        self.assertEqual(candidate["brand_name"], "ENDOPATH XCEL Trocar")
        self.assertGreater(score_device_candidate(parsed, candidate), 1000)

    def test_cache_prevents_second_http_call(self) -> None:
        parsed = parse_medical_device_query("ethicon trocar")
        opener = FakeOpener([{"results": []}])
        with tempfile.TemporaryDirectory() as tmp:
            client = GUDIDClient(opener=opener, cache=GUDIDCache(Path(tmp) / "gudid.sqlite3"))
            self.assertEqual(client.search_openfda_udi(parsed), [])
            self.assertEqual(client.search_openfda_udi(parsed), [])
        self.assertEqual(opener.calls, 1)

    def test_lookup_accessgudid_by_di(self) -> None:
        opener = FakeOpener([{
            "gudid": {"device": {
                "deviceIdentifier": "05050474830158",
                "companyName": "JOHNSON & JOHNSON",
                "brandName": "SMARTLOAD",
                "versionModelNumber": "GIB00",
                "gmdnTerms": [{"gmdnPTName": "Intraocular lens injector"}],
            }}
        }])
        with tempfile.TemporaryDirectory() as tmp:
            client = GUDIDClient(opener=opener, cache=GUDIDCache(Path(tmp) / "gudid.sqlite3"))
            device = client.lookup_accessgudid(di="05050474830158")
        self.assertIsNotNone(device)
        self.assertEqual(device.device_identifier, "05050474830158")


if __name__ == "__main__":
    unittest.main()
