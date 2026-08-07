"""A device must reach its own IFU — and never another manufacturer's.

GUDID and the manufacturer portals punctuate the same part number differently, and the
spine makers key their documents to the MODEL number rather than the catalog. A lookup
that only matched the catalog string exactly left 169,615 devices — most of Nuvasive,
Alphatec, Globus and Medtronic Sofamor Danek — dark despite their IFU already being on
disk, and reported them as "covered" in coverage counts while returning nothing to a user.

Widening the match is only safe with a manufacturer check, and this is the case that
proves it: GUDID device `62-00620` is a Stryker Leibinger part, while `62006-20` in our
own table is Alphatec's Zodiac spine implant. Same digits, different maker. Serving one
for the other would put the wrong surgical instructions in front of a clinician, so the
cross-maker refusals below matter more than the unlocks.

Run:  .venv/bin/python -m pytest tests/test_catalog_matching.py -v
Needs the real chatifu.sqlite3; these are integration tests over live data.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mvp_lookup import (  # noqa: E402
    MIN_CATALOG_KEY_LEN,
    catalog_key,
    family_for_company,
    get_servable_ifu_documents,
)

pytestmark = pytest.mark.skipif(
    not Path("chatifu.sqlite3").exists(), reason="needs the populated device database"
)


def test_catalog_key_ignores_only_punctuation():
    assert catalog_key("45-20004") == "4520004"
    assert catalog_key("02.007.026") == "02007026"
    assert catalog_key("00-8777-040-04") == "00877704004"
    # different part numbers must not collapse onto each other
    assert catalog_key("4520004") != catalog_key("4520005")


def test_family_for_company_follows_subsidiaries():
    # the acquisitions are where a naive company==family check breaks
    assert family_for_company("Synthes GmbH") == "johnson_and_johnson"
    assert family_for_company("WRIGHT MEDICAL TECHNOLOGY, INC.") == "stryker"
    assert family_for_company("ST. JUDE MEDICAL, INC.") == "abbott"
    assert family_for_company("Aesculap AG") == "b_braun"
    assert family_for_company("Covidien LP") == "medtronic"
    assert family_for_company("Some Unknown Backstreet Devices Ltd") is None
    assert family_for_company(None) is None


def test_punctuation_variant_resolves_for_the_right_maker():
    """`45-20004` as GUDID stores it; `4520004` as Stryker's portal stored it."""
    docs = get_servable_ifu_documents("45-20004", company_name="Stryker GmbH")
    assert docs, "Stryker device should reach its own IFU despite the hyphen"
    assert docs[0]["manufacturer_family"] == "stryker"


def test_exact_match_is_unaffected():
    docs = get_servable_ifu_documents("4520004", company_name="Stryker GmbH")
    assert docs and docs[0]["manufacturer_family"] == "stryker"


def test_model_number_reaches_spine_documents():
    """Alphatec files under the model number; the catalog is empty for these."""
    docs = get_servable_ifu_documents(
        "", company_name="ALPHATEC SPINE, INC.", model_number="87531-65"
    )
    assert docs, "Alphatec device should reach its IFU via the model number"
    assert docs[0]["manufacturer_family"] == "alphatec_spine"


# --- the refusals: each of these serving a document would be a safety failure ---

def test_refuses_when_the_maker_is_unknown():
    """No company means nothing vouches for the match, so only an exact hit counts."""
    assert get_servable_ifu_documents("45-20004") == []


def test_refuses_a_document_belonging_to_another_maker():
    """`62006-20` is Alphatec's Zodiac. A Stryker Leibinger device must not get it."""
    docs = get_servable_ifu_documents(
        "62-00620", company_name="Stryker Leibinger GmbH & Co. KG"
    )
    assert docs == [], "must never serve another manufacturer's IFU"


def test_the_same_number_still_resolves_for_its_real_owner():
    docs = get_servable_ifu_documents("62-00620", company_name="ALPHATEC SPINE, INC.")
    assert docs and docs[0]["manufacturer_family"] == "alphatec_spine"


def test_refuses_short_identifiers_that_collide_by_chance():
    short = "1" * (MIN_CATALOG_KEY_LEN - 1)
    assert get_servable_ifu_documents(short, company_name="Stryker GmbH") == []


def test_unknown_catalog_returns_nothing():
    assert get_servable_ifu_documents("99-99999", company_name="Stryker GmbH") == []
