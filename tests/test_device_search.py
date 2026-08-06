"""Device search must return the product the clinician named.

Written after a beta tester searched "medtronic gia stapler isnt working can you help me
toruble shoot it" and was shown the TSRH Spinal System. Four separate defects combined to
produce that, and each one is pinned here so it cannot come back quietly:

  1. no relevance ranking  -- results were ORDER BY brand_name, so every Medtronic device
                              tied and " TSRH® Spinal System" won on a leading space
  2. unindexed description -- "stapler" lives in GUDID's deviceDescription, never indexed
  3. no parent company     -- the GIA stapler's company is "Covidien LP", and nothing said
                              Medtronic owns it
  4. no stop words         -- OR semantics matched Curbell products on the token "it"

Run:  .venv/bin/python -m pytest tests/test_device_search.py -v
Needs the real chatifu.sqlite3; these are integration tests over live GUDID data.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mvp_lookup import SQLITE_PATH, search_devices, search_terms  # noqa: E402

pytestmark = pytest.mark.skipif(
    not Path(SQLITE_PATH).exists(), reason="needs the populated device database"
)

THE_TESTERS_QUERY = "medtronic gia stapler isnt working can you help me toruble shoot it"

# query -> substrings, any of which identifies an acceptable maker for the top hit.
# Subsidiaries are listed because GUDID files them under their own name: the GIA stapler is
# "Covidien LP" and Stryker's hips are "Howmedica Osteonics".
CROSS_MAKER_CASES = [
    (THE_TESTERS_QUERY, ("covidien", "medtronic")),
    ("medtronic gia stapler", ("covidien", "medtronic")),
    ("stryker hip stem", ("stryker", "howmedica", "osteonics")),
    ("stryker knee", ("stryker", "howmedica", "osteonics")),
    ("zimmer knee", ("zimmer",)),
    ("abbott pacemaker", ("abbott", "st. jude")),
    ("boston scientific stent", ("boston",)),
    ("arthrex anchor", ("arthrex",)),
    ("alphatec invictus screw", ("alphatec",)),
    ("globus creo", ("globus",)),
    ("nuvasive reline", ("nuvasive",)),
    ("aesculap forceps", ("aesculap", "b braun", "b. braun")),
    ("edwards heart valve", ("edwards",)),
    ("smith nephew shoulder", ("smith",)),
    ("covidien trocar", ("covidien",)),
]


@pytest.mark.parametrize("query,acceptable", CROSS_MAKER_CASES)
def test_top_hit_is_from_the_named_maker(query: str, acceptable: tuple[str, ...]) -> None:
    devices = search_devices(query, limit=5)
    assert devices, f"no results for {query!r}"
    company = devices[0]["company_name"].lower()
    assert any(a in company for a in acceptable), (
        f"{query!r} -> {devices[0]['brand_name']!r} by {devices[0]['company_name']!r}"
    )


def test_the_regression_that_started_this() -> None:
    """The tester's exact query must find a stapler, not a spinal system."""
    devices = search_devices(THE_TESTERS_QUERY, limit=5)
    assert devices
    top = devices[0]
    assert "gia" in top["brand_name"].lower(), f"expected a GIA stapler, got {top!r}"
    assert "spinal" not in top["brand_name"].lower()
    assert "tsrh" not in top["brand_name"].lower()


def test_conversational_words_are_not_searched() -> None:
    """"it" once matched Curbell model numbers containing ",IT,"."""
    terms = search_terms(THE_TESTERS_QUERY)
    for noise in ("it", "me", "can", "you", "help", "isnt", "working", "shoot"):
        assert noise not in terms, f"{noise!r} should not be a search term"
    for signal in ("medtronic", "gia", "stapler"):
        assert signal in terms, f"{signal!r} should be a search term"


def test_a_query_of_only_stop_words_still_searches() -> None:
    """Stripping every term must not silently search for nothing.

    An odd device name made of common words is likelier than a query with no content, so the
    raw tokens are kept rather than returning an empty term list (which would match all rows).
    """
    assert search_terms("it") == ["it"]


def test_identifier_search_still_works() -> None:
    """The hyphen and digit-heavy identifier path must survive the tokenizer change."""
    devices = search_devices("GIA8048S", limit=5)
    assert devices
    assert any("gia" in d["brand_name"].lower() for d in devices)


def test_answerable_devices_are_preferred() -> None:
    """Among equally good matches, a device we hold an IFU for should come first.

    Not a filter -- an uncovered device is still a valid answer to "do you have this" -- so
    this only asserts the top hit is answerable, not that uncovered ones are absent.
    """
    from mvp_lookup import get_servable_ifu_documents

    devices = search_devices("medtronic gia stapler", limit=5)
    assert devices
    identifier = devices[0]["catalog_number"] or devices[0]["model_number"]
    assert get_servable_ifu_documents(identifier), (
        f"top hit {identifier!r} has no servable IFU"
    )
