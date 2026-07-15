"""ChatIFU accuracy spot-check.

Samples covered devices across manufacturers, asks universal IFU questions
(contraindications / warnings / indications — passages virtually every IFU
carries), calls the live /answer, and uses the local LLM (qwen3) to judge
whether the highlighted passage actually addresses the question.

Reports three things that matter for a medical beta:
  * hit-rate      — did we surface a passage at all?
  * relevance     — of the passages we surfaced, how many were on-topic?
  * FLAGGED       — surfaced a passage that is NOT on-topic (a confident-wrong
                    highlight — the single most dangerous failure to ship).

Usage: .venv/bin/python accuracy_spotcheck.py [--per-family 4]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

from ollama_client import generate

VAULT = Path(__file__).resolve().parent
DB = str(VAULT / "chatifu.sqlite3")
API = "http://127.0.0.1:8123"
LOG_DIR = VAULT / "logs"

FAMILIES = ["johnson_and_johnson", "medtronic", "stryker", "zimmer_biomet", "edwards", "abbott"]
QUESTIONS = [
    "What are the contraindications for this device?",
    "What are the warnings and precautions?",
    "What is this device indicated for?",
]


def beta_code() -> str:
    for line in (VAULT / ".env").read_text().splitlines():
        if line.startswith("CHATIFU_BETA_CODES="):
            return line.split("=", 1)[1].strip().strip('"').split(",")[0]
    return "beta-PslkzuMAr5YC"


def sample_devices(per_family: int) -> list[tuple[str, str]]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    out: list[tuple[str, str]] = []
    try:
        for fam in FAMILIES:
            rows = conn.execute(
                "SELECT DISTINCT catalog_number FROM ifu_links "
                "WHERE status='found' AND manufacturer_family=? AND catalog_number IS NOT NULL "
                "LIMIT ?",
                (fam, per_family),
            ).fetchall()
            for r in rows:
                out.append((fam, str(r["catalog_number"])))
    finally:
        conn.close()
    return out


def ask(catalog: str, question: str, beta: str) -> dict:
    data = json.dumps({"catalog": catalog, "question": question}).encode()
    req = urllib.request.Request(
        f"{API}/answer", data=data,
        headers={"content-type": "application/json", "x-beta-code": beta},
    )
    with urllib.request.urlopen(req, timeout=150) as resp:
        return json.loads(resp.read())


def judge(question: str, section: str, snippet: str) -> dict | None:
    prompt = (
        "You are auditing a medical-device IFU search. /no_think\n"
        f"Question: {question}\n"
        f'Returned passage (labelled section "{section}"):\n"""{snippet[:1200]}"""\n'
        "Does this passage directly address the question? "
        'Reply ONLY compact JSON: {"relevant": true or false, "reason": "<=12 words"}'
    )
    try:
        out = generate(prompt, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return {"relevant": None, "reason": f"judge error: {exc}"[:60]}
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return {"relevant": None, "reason": "judge returned no JSON"}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"relevant": None, "reason": "judge JSON parse fail"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-family", type=int, default=4)
    args = ap.parse_args()
    beta = beta_code()

    devices = sample_devices(args.per_family)
    probes = [(fam, cat, QUESTIONS[i % len(QUESTIONS)]) for i, (fam, cat) in enumerate(devices)]
    print(f"Spot-check: {len(probes)} probes across {len(set(f for f,_ in devices))} families\n")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = LOG_DIR / f"accuracy_spotcheck_{stamp}.jsonl"

    n = len(probes)
    hits = relevant = flagged = no_hit = errors = 0
    flags: list[dict] = []
    with out_path.open("w") as fh:
        for idx, (fam, cat, q) in enumerate(probes, 1):
            rec: dict = {"family": fam, "catalog": cat, "question": q}
            try:
                data = ask(cat, q, beta)
            except Exception as exc:  # noqa: BLE001
                rec["result"] = "error"; rec["detail"] = str(exc)[:80]; errors += 1
                fh.write(json.dumps(rec) + "\n")
                print(f"[{idx}/{n}] {fam:20} {cat:16} ERROR {rec['detail']}")
                continue
            hitlist = data.get("hits") or []
            if not hitlist:
                rec["result"] = "no_hit"; no_hit += 1
                fh.write(json.dumps(rec) + "\n")
                print(f"[{idx}/{n}] {fam:20} {cat:16} NO-HIT ({q[:30]}…)")
                continue
            hits += 1
            top = hitlist[0]
            verdict = judge(q, top.get("section", ""), top.get("snippet", ""))
            rel = verdict.get("relevant") if verdict else None
            rec.update({"result": "hit", "page": top.get("page"), "section": top.get("section"),
                        "snippet": (top.get("snippet") or "")[:200],
                        "document_title": data.get("document_title"),
                        "relevant": rel, "reason": (verdict or {}).get("reason")})
            fh.write(json.dumps(rec) + "\n")
            if rel is True:
                relevant += 1
                mark = "OK  "
            elif rel is False:
                flagged += 1; flags.append(rec)
                mark = "FLAG"
            else:
                mark = "?   "
            print(f"[{idx}/{n}] {fam:20} {cat:16} {mark} p{top.get('page')} "
                  f"[{(top.get('section') or '')[:22]}] — {(verdict or {}).get('reason','')[:40]}")

    print("\n================ SUMMARY ================")
    print(f"probes:        {n}")
    print(f"returned hit:  {hits}  ({hits*100//max(n,1)}%)")
    print(f"no hit:        {no_hit}")
    print(f"errors:        {errors}")
    if hits:
        print(f"relevant:      {relevant}/{hits}  ({relevant*100//hits}% of hits)")
        print(f"FLAGGED wrong: {flagged}  (confident but off-topic)")
    if flags:
        print("\n--- FLAGGED (confident-wrong highlights to review) ---")
        for f in flags:
            print(f"  {f['family']}/{f['catalog']} — Q: {f['question']}")
            print(f"    section [{f.get('section')}] p{f.get('page')}: {f.get('reason')}")
    print(f"\nfull results: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
