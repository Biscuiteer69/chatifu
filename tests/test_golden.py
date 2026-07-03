"""Golden-query pre-deploy gate (integration).

Exercises the real highlight extraction against live manufacturer IFUs for
known devices across all beta-relevant manufacturers. Requires the vault API
running on 127.0.0.1:8123 with a valid beta code, plus network egress — so
it is marked `integration` and skipped by the default unit run.

Run before opening beta traffic:
    CHATIFU_BETA_CODE=<code> pytest -m integration tests/test_golden.py -v
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

API = os.environ.get("CHATIFU_API_URL", "http://127.0.0.1:8123")
BETA = os.environ.get("CHATIFU_BETA_CODE", "")
GOLDEN = json.loads((Path(__file__).parent / "golden_queries.json").read_text())["cases"]


def _answer(catalog: str, question: str) -> dict:
    req = urllib.request.Request(
        f"{API}/answer",
        data=json.dumps({"catalog": catalog, "question": question}).encode(),
        headers={"X-Beta-Code": BETA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def _api_up() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/healthz", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.integration


@pytest.mark.skipif(not BETA, reason="set CHATIFU_BETA_CODE to run golden queries")
@pytest.mark.parametrize("case", GOLDEN, ids=[c["catalog"] for c in GOLDEN])
def test_golden_query(case):
    if not _api_up():
        pytest.skip("vault API not reachable on 127.0.0.1:8123")
    data = _answer(case["catalog"], case["question"])
    assert not data.get("error"), f"{case['catalog']}: {data.get('error')}"
    hits = data.get("hits") or []
    assert len(hits) >= case.get("min_hits", 1), f"{case['catalog']}: no hits"
    pages = {h.get("page") for h in hits}
    assert case["expect_page"] in pages, (
        f"{case['catalog']}: expected page {case['expect_page']}, got {sorted(p for p in pages if p)}"
    )
    if "expect_section" in case:
        secs = " | ".join((h.get("section") or "") for h in hits).lower()
        assert case["expect_section"].lower() in secs, (
            f"{case['catalog']}: section '{case['expect_section']}' not in [{secs}]"
        )
