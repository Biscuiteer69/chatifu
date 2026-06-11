"""
Live diagnostic: section-aware extraction on the 3 test devices × 4 questions.
Run once; does not store PDFs (fetch→parse→discard).

Usage (from chatifu_vault/):
    PYTHONPATH=. python3 tests/diagnose_section_extraction.py
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ifu_answer import IFUAnswerer

DEVICES = [
    ("J&J",     "GIB00U0340", "https://www.e-ifu.com/viewpdf-iframe/47270/1/0/V0G000000000701"),
    ("Abbott",  "SGC0101",    "https://manuals.eifu.abbott/content/dam/av/manuals-eifu/vascular/EL2106481%20Rev.%20B.pdf"),
    ("Edwards", "D98100",     "https://eifu.edwards.com/eifu/5970f1b346e0fb00015e5f4d/10005783002.pdf"),
]

QUESTIONS = [
    ("contraindications",  "what are the contraindications?"),
    ("warnings",           "what are the warnings?"),
    ("storage/shelf life", "what are the storage conditions and shelf life?"),
    ("MRI safety",         "is this device MRI safe?"),
]

# Keywords that would indicate the OLD (wrong) keyword-page result.
# If a snippet starts with one of these, it's likely a false hit.
_WRONG_INDICATORS = [
    "is supplied",
    "sgc0101 is",
    "d98100",
    "gib00",
    "catalog number",
    "the device is packaged",
    "the device is supplied",
    "package is supplied",
]


def _looks_correct(q_label: str, snippet: str, section: str | None) -> str:
    low = snippet.lower()
    # If snippet starts with a product supply / catalog phrase → wrong
    for bad in _WRONG_INDICATORS:
        if bad in low[:80]:
            return "WRONG (catalog/supply boilerplate)"
    # Section label sanity check
    if q_label == "contraindications" and section and "CONTRAINDICATION" in section.upper():
        return "CORRECT (section targeted)"
    if q_label == "warnings" and section and any(w in section.upper() for w in ("WARNING", "CAUTION", "PRECAUTION")):
        return "CORRECT (section targeted)"
    if q_label == "storage/shelf life" and section and any(w in section.upper() for w in ("STORAGE", "SHELF")):
        return "CORRECT (section targeted)"
    if q_label == "MRI safety" and section and any(w in section.upper() for w in ("MRI", "MAGNETIC")):
        return "CORRECT (section targeted)"
    return "INDETERMINATE (check snippet)"


def run() -> None:
    answerer = IFUAnswerer()
    correct = 0
    wrong = 0
    total = 0

    for mfr, catalog, url in DEVICES:
        print(f"\n{'='*60}")
        print(f"  {mfr}  {catalog}")
        print(f"{'='*60}")

        for q_label, question in QUESTIONS:
            total += 1
            print(f"\n  Q: {q_label}")
            print(f"     [{question}]")
            result = answerer.answer(url, question, max_hits=1)

            if result.error:
                print(f"  ERROR: {result.error}")
                wrong += 1
                continue

            if not result.hits:
                print("  NO HIT RETURNED")
                wrong += 1
                continue

            hit = result.hits[0]
            verdict = _looks_correct(q_label, hit.snippet, hit.section)
            if "CORRECT" in verdict:
                correct += 1
            elif "WRONG" in verdict:
                wrong += 1

            print(f"  Page: {hit.page}   Section: {hit.section!r}")
            wrapped = textwrap.fill(hit.snippet, width=72, initial_indent="  Snippet: ",
                                    subsequent_indent="           ")
            print(wrapped)
            print(f"  Verdict: {verdict}")
            timing = result.timing_ms
            print(f"  Timing: total={timing.get('total_ms'):.0f}ms  "
                  f"fetch={timing.get('fetch_ms', 0):.0f}ms  "
                  f"parse={timing.get('parse_ms', 0):.0f}ms  "
                  f"search={timing.get('search_ms', 0):.0f}ms  "
                  f"cache_hit={int(timing.get('cache_hit', 0))}")

    print(f"\n{'='*60}")
    print(f"RESULTS: {correct}/{total} correct  ({wrong} wrong/indeterminate)")
    print("No PDFs stored (fetch→parse→discard confirmed by IFUAnswerer design).")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
