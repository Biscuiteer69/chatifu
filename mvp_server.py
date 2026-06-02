from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from ifu_answer import IFUAnswerer, AnswerResult, _is_direct_pdf_url
from ifu_cache import IFUDocumentCache
from gudid_client import GUDIDClient
from ifu_resolvers.registry import IFUResolverRegistry
from medical_device_vocab import (
    ParsedMedicalDeviceQuery,
    concept_display_name,
    parse_medical_device_query,
    problem_categories_for_terms,
    score_device_candidate,
    score_device_candidate_details,
)
from mvp_lookup import (
    SQLITE_PATH,
    fetch_ifu_rows,
    get_best_ifu_url,
    get_device,
    ensure_ifu_for_catalog,
    lookup_catalog,
    search_devices,
)


SAFETY_NOTE = (
    "ChatIFU searches manufacturer IFU sources and answers using cited IFU passages. "
    "Verify all information in the full IFU before clinical use."
)
OPTION_B_NOTE = (
    "IFU answers are fetched in real time and discarded immediately. "
    "No PDF text is stored."
)

_CSS = """
    :root {
      color-scheme: light;
      --ink: #1d252f;
      --muted: #5d6978;
      --line: #d9dee7;
      --panel: #ffffff;
      --bg: #f4f6f8;
      --found: #0f766e;
      --candidate: #9a5b00;
      --error: #b42318;
      --focus: #285ea8;
      --answer: #1e3a5f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 16px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    main {
      width: min(1280px, calc(100% - 32px));
      margin: 48px auto 32px;
    }
    header { margin-bottom: 22px; }
    h1 { margin: 0; font-size: 2.35rem; }
    h2 { margin: 12px 0 10px; font-size: 1.35rem; }
    h3 { margin: 0 0 8px; font-size: 1.1rem; }
    .subtitle { margin: 6px 0 0; color: var(--muted); font-size: 1.05rem; }
    form.search { display: flex; gap: 10px; margin: 24px 0; }
    input[type="search"], input[type="text"] {
      flex: 1; min-width: 0; height: 48px;
      border: 1px solid var(--line); border-radius: 8px;
      padding: 0 14px; font: inherit; background: #fff;
    }
    input[type="search"]:focus, input[type="text"]:focus {
      outline: 3px solid color-mix(in srgb, var(--focus), transparent 75%);
      border-color: var(--focus);
    }
    button, .button {
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 44px; border: 0; border-radius: 8px; padding: 0 16px;
      color: #fff; background: var(--focus);
      font: 700 0.96rem system-ui, sans-serif;
      text-decoration: none; cursor: pointer; white-space: nowrap;
    }
    .button-outline {
      background: transparent; color: var(--focus);
      border: 1.5px solid var(--focus);
    }
    .card {
      border: 1px solid var(--line); border-radius: 8px;
      background: var(--panel); padding: 20px;
      box-shadow: 0 1px 2px rgba(12,20,30,.05);
      margin-bottom: 14px;
    }
    .card-row {
      display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 14px; margin: 16px 0;
    }
    .device-card {
      border: 1px solid var(--line); border-radius: 8px;
      background: var(--panel); padding: 16px;
      box-shadow: 0 1px 2px rgba(12,20,30,.05);
    }
    .device-card .brand { font-weight: 700; font-size: 1.05rem; }
    .device-card .meta { color: var(--muted); font-size: 0.88rem; margin-top: 4px; }
    .device-card .actions { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
    .badge {
      display: inline-flex; align-items: center; min-height: 26px;
      border-radius: 999px; padding: 0 10px;
      color: #fff; font-size: 0.82rem; font-weight: 800;
      text-transform: uppercase; letter-spacing: .02em;
    }
    .badge-found { background: var(--found); }
    .badge-candidate { background: var(--candidate); }
    .badge-error { background: var(--error); }
    .badge-answer { background: var(--answer); }
    dl {
      display: grid; grid-template-columns: 150px 1fr;
      gap: 8px 14px; margin: 16px 0;
    }
    dt { color: var(--muted); font-weight: 700; }
    dd { margin: 0; overflow-wrap: anywhere; }
    .warning {
      margin: 14px 0; border-left: 4px solid var(--candidate);
      background: #fff7ed; padding: 12px 14px; border-radius: 6px;
      color: #613b00;
    }
    .error-card { border-color: #f0b4ae; background: #fff8f7; }
    .answer-card {
      border: 2px solid color-mix(in srgb, var(--answer), transparent 70%);
      border-radius: 8px; background: #f0f5ff; padding: 18px;
      margin-top: 16px;
    }
    .hit {
      background: #fff; border: 1px solid var(--line);
      border-radius: 6px; padding: 14px; margin-top: 10px;
    }
    .hit-page { font-size: 0.8rem; font-weight: 700; color: var(--muted);
                text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
    .hit-section { font-size: 0.92rem; font-weight: 700; margin-bottom: 8px; }
    .hit-snippet { line-height: 1.55; white-space: pre-wrap; }
    .hit-snippet mark { background: #fff2a8; padding: 0 .15em; border-radius: 3px; }
    .page-jump { margin-top: 10px; min-height: 36px; padding: 0 12px; font-size: .88rem; }
    .answer-split {
      display: grid; grid-template-columns: 3fr 2fr; gap: 16px;
      height: min(80vh, 900px); margin-top: 16px;
    }
    .ifu-pane {
      border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
      background: #fff;
    }
    .answer-pane {
      overflow-y: auto; padding: 16px; border: 1px solid var(--line);
      border-radius: 8px; background: #fff;
    }
    .loader { color: var(--muted); font-style: italic; padding: 16px 0; }
    .timing { font-size: 0.78rem; color: var(--muted); margin-top: 8px; }
    .candidates { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--line); }
    .candidate-item { padding: 10px 0; border-bottom: 1px solid var(--line); }
    footer { margin-top: 28px; color: var(--muted); font-size: 0.95rem; }
    a { color: var(--focus); }
    @media (max-width: 620px) {
      main { width: min(100% - 24px, 960px); margin-top: 24px; }
      form.search { flex-direction: column; }
      button, .button, input { width: 100%; }
      dl { grid-template-columns: 1fr; gap: 3px; }
      h1 { font-size: 2rem; }
      .card-row { grid-template-columns: 1fr; }
      .answer-split { grid-template-columns: 1fr; height: auto; }
      .ifu-pane { min-height: 58vh; }
    }
"""

_PAGE_SHELL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{css}</style>
</head>
<body>
  <main>
    <header>
      <h1><a href="/" style="text-decoration:none;color:inherit">ChatIFU</a></h1>
      <p class="subtitle">{subtitle}</p>
    </header>
    {body}
    <footer>{footer}</footer>
  </main>
</body>
</html>"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _page(title: str, subtitle: str, body: str, footer: str = SAFETY_NOTE) -> str:
    return _PAGE_SHELL.format(
        title=esc(title), css=_CSS, subtitle=esc(subtitle), body=body, footer=esc(footer)
    )


def _clean_user_query(q: str) -> str:
    value = html.unescape(q or "").translate(str.maketrans({
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "`": "'",
    }))
    value = re.sub(r"\s+", " ", value).strip()
    for _ in range(8):
        m = re.match(r"(?is)^results\s+for\s+(.+)$", value)
        if not m:
            break
        candidate = m.group(1).strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
            candidate = candidate[1:-1].strip()
        candidate = re.sub(r"\s+", " ", candidate)
        if candidate == value:
            break
        value = candidate
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_ifu_question(q: str) -> bool:
    value = _clean_user_query(q).lower()
    if "?" in value:
        return True
    phrases = (
        "what should i do", "what do i do", "wont", "won't", "will not",
        "cannot", "can't", "stuck", "won't open", "wont open",
        "won't fire", "wont fire", "error", "warning", "warnings",
        "contraindication", "contraindications", "how many times",
        "what size", "trocar", "reload",
    )
    if any(phrase in value for phrase in phrases):
        return True
    return bool(re.search(r"\bfire(?:d|s|ing)?\b", value))


def _extract_device_terms(q: str) -> str:
    value = _clean_user_query(q).lower()
    value = value.replace("won't", "wont").replace("can't", "cannot")
    tokens = re.findall(r"[a-z0-9][a-z0-9-]*", value)
    stop = {
        "results", "for", "wont", "won", "will", "not", "fire", "fires",
        "fired", "firing", "tissue", "open", "stuck", "what", "should",
        "do", "next", "cannot", "cant", "can", "and", "its", "it", "is",
        "on", "the", "a", "an", "with", "problem", "issue", "i", "me",
        "my", "this", "that", "to", "of", "in", "are", "be", "needed",
        "need", "needs", "error", "warning", "warnings", "contraindication",
        "contraindications", "how", "many", "times",
    }
    kept: list[str] = []
    for token in tokens:
        if token in stop:
            continue
        if token not in kept:
            kept.append(token)

    expanded = list(kept)
    if "echelon" in kept:
        for token in ("ech", "echelon"):
            if token not in expanded:
                expanded.append(token)
    if "stapler" in kept:
        for token in ("cutter", "reload"):
            if token not in expanded:
                expanded.append(token)
    return " ".join(expanded)


def _device_identity_searches(parsed: ParsedMedicalDeviceQuery) -> list[str]:
    searches: list[str] = []
    manufacturers = parsed.manufacturer_terms[:2]
    sizes = [s for s in parsed.size_terms if " " not in s][:2]

    concept_identity: list[str] = []
    if "LAPAROSCOPIC_TROCAR_ACCESS" in parsed.detected_concepts:
        concept_identity.extend(["trocar", "access", "cannula", "sleeve", "endopath", "xcel"])
    if "SURGICAL_STAPLER" in parsed.detected_concepts:
        concept_identity.extend(["stapler", "cutter", "reload", "cartridge", "echelon", "endopath"])
    if "DRAIN_TROCAR_OR_SPIKE" in parsed.detected_concepts:
        concept_identity.extend(["blake", "drain", "trocar", "spike", "suction"])

    def add(parts: list[str]) -> None:
        cleaned = " ".join(_unique_terms(parts))
        if cleaned and cleaned not in searches:
            searches.append(cleaned)

    if manufacturers:
        add(manufacturers + sizes + concept_identity[:4])
        for term in concept_identity[:6]:
            add(manufacturers + [term])
        for term in parsed.device_terms[:5]:
            add(manufacturers + [term])

    if sizes and concept_identity:
        add(sizes + concept_identity[:4])
    if concept_identity:
        add(concept_identity[:6])
    if parsed.search_terms:
        add(parsed.search_terms[:8])

    legacy_terms = _extract_device_terms(parsed.original_question)
    if legacy_terms:
        searches.append(legacy_terms)
    return searches


def _unique_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value).strip().lower())
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _candidate_key(dev: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(dev.get("catalog_number") or "").upper(),
        str(dev.get("brand_name") or "").upper(),
        str(dev.get("company_name") or "").upper(),
    )


def _ranked_search_devices(
    parsed: ParsedMedicalDeviceQuery,
    db_path: Path,
    *,
    limit: int = 40,
    gudid_client: GUDIDClient | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    searches = _device_identity_searches(parsed)
    seen: set[tuple[str, str, str]] = set()
    candidates: list[dict[str, Any]] = []
    for search in searches:
        for dev in search_devices(search, db_path=db_path, limit=limit):
            key = _candidate_key(dev)
            if key in seen:
                continue
            seen.add(key)
            scored = dict(dev)
            details = score_device_candidate_details(parsed, scored)
            scored["_score"] = details.score
            scored["_confidence"] = details.confidence
            scored["_score_reasons"] = [
                {
                    "label": reason.label,
                    "delta": reason.delta,
                    "field": reason.field,
                    "evidence": reason.evidence,
                }
                for reason in details.reasons
            ]
            candidates.append(scored)
    if gudid_client is not None:
        try:
            for gudid_dev in gudid_client.search_openfda_udi(parsed, limit=25):
                scored = gudid_dev.as_candidate()
                key = _candidate_key(scored)
                if key in seen:
                    continue
                seen.add(key)
                details = score_device_candidate_details(parsed, scored)
                scored["_score"] = details.score
                scored["_confidence"] = details.confidence
                scored["_score_reasons"] = [
                    {
                        "label": reason.label,
                        "delta": reason.delta,
                        "field": reason.field,
                        "evidence": reason.evidence,
                    }
                    for reason in details.reasons
                ]
                candidates.append(scored)
        except Exception:
            pass
    candidates.sort(key=lambda dev: (-int(dev.get("_score") or 0), str(dev.get("brand_name") or "")))
    return candidates, searches


def _render_detected_summary(parsed: ParsedMedicalDeviceQuery) -> str:
    rows: list[tuple[str, str]] = []
    if parsed.manufacturer_terms:
        rows.append(("Manufacturer", ", ".join(term.title() for term in parsed.manufacturer_terms)))
    if parsed.detected_concepts:
        rows.append(("Device concept", ", ".join(concept_display_name(c) for c in parsed.detected_concepts)))
    if parsed.size_terms:
        compact_sizes = [s for s in parsed.size_terms if " " not in s]
        rows.append(("Size", ", ".join(compact_sizes or parsed.size_terms)))
    if parsed.problem_terms:
        rows.append(("Problem", ", ".join(parsed.problem_terms)))
    if not rows:
        return ""
    items = "".join(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in rows)
    return f"""
    <section class="card">
      <h2>Detected</h2>
      <dl>{items}</dl>
    </section>
    """


def _group_ranked_devices(
    parsed: ParsedMedicalDeviceQuery,
    devices: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    if not parsed.manufacturer_terms:
        return [("Best matches", devices)] if devices else []

    manufacturer_terms = [term.lower() for term in parsed.manufacturer_terms]
    best: list[dict[str, Any]] = []
    same: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for dev in devices:
        company_brand = f"{dev.get('company_name') or ''} {dev.get('brand_name') or ''}".lower()
        same_mfr = any(term in company_brand for term in manufacturer_terms)
        score = int(dev.get("_score") or 0)
        if same_mfr and score >= 900:
            best.append(dev)
        elif same_mfr:
            same.append(dev)
        else:
            other.append(dev)
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    if best:
        groups.append(("Best matches", best))
    if same:
        groups.append(("Same manufacturer matches", same))
    if other:
        groups.append(("Other possible matches", other))
    return groups


def _device_link(catalog: str, question: str | None = None) -> str:
    href = f"/device?catalog={quote(catalog)}"
    if question:
        href += f"&q={quote(question)}"
    return href


def _pdf_proxy_url(catalog: str) -> str:
    return f"/ifu/pdf?catalog={quote(catalog)}"


def _feedback_path() -> Path:
    return Path(os.environ.get(
        "CHATIFU_FEEDBACK_PATH",
        "/home/biscuited/.biscuited/hermes/DGX/feedback/chatifu_feedback.jsonl",
    ))


def _hash_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.75:
        return "High confidence"
    if confidence >= 0.4:
        return "Medium confidence"
    return "Low confidence"


def _safe_score_snapshot(dev: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "label": reason.get("label"),
            "delta": reason.get("delta"),
            "field": reason.get("field"),
            "evidence": reason.get("evidence"),
        }
        for reason in dev.get("_score_reasons", [])[:12]
        if isinstance(reason, dict)
    ]


def _best_auto_device(devices: list[dict[str, Any]], db_path: Path) -> dict[str, Any] | None:
    if len(devices) == 1:
        return devices[0]
    if not devices:
        return None
    cached_ifu_matches = [
        dev for dev in devices
        if dev.get("catalog_number") and get_best_ifu_url(str(dev["catalog_number"]), db_path=db_path)
    ]
    if len(cached_ifu_matches) == 1:
        top_score = max((int(dev.get("_score") or 0) for dev in devices), default=0)
        cached_score = int(cached_ifu_matches[0].get("_score") or 0)
        if top_score == 0 or (cached_score >= 900 and cached_score >= top_score - 100):
            return cached_ifu_matches[0]
    return None


# ------------------------------------------------------------------
# Search home page
# ------------------------------------------------------------------

def render_search_home(query: str = "", error: str | None = None) -> str:
    err_html = ""
    if error:
        err_html = f'<p class="warning">{esc(error)}</p>'
    body = f"""
    <form class="search" method="get" action="/search">
      <input type="search" name="q" value="{esc(query)}"
             placeholder="echelon stapler, TECNIS, GIB00U0340&hellip;"
             aria-label="Search devices" autofocus>
      <button type="submit">Search</button>
    </form>
    {err_html}
    <p style="color:var(--muted);font-size:.95rem">
      Search by device name, brand, company, or catalog number.
      Click a device to ask a question from its IFU.
    </p>
    <p style="color:var(--muted);font-size:.9rem">
      Or look up by exact catalog number:
      <a href="/lookup">catalog lookup &rarr;</a>
    </p>
    """
    return _page("ChatIFU", "Ask questions from medical device IFUs", body)


# ------------------------------------------------------------------
# Search results page
# ------------------------------------------------------------------

def render_search_results(
    query: str,
    devices: list[dict[str, Any]],
    *,
    initial_question: str | None = None,
    device_terms: str | None = None,
    parsed_query: ParsedMedicalDeviceQuery | None = None,
    searched_terms: list[str] | None = None,
) -> str:
    detected_html = _render_detected_summary(parsed_query) if parsed_query else ""
    if not devices:
        if initial_question:
            if (
                parsed_query
                and parsed_query.manufacturer_terms
                and "LAPAROSCOPIC_TROCAR_ACCESS" in parsed_query.detected_concepts
            ):
                manufacturer = parsed_query.manufacturer_terms[0].title()
                size = parsed_query.size_terms[0].replace(" ", "") if parsed_query.size_terms else ""
                size_text = f" + {size}" if size else ""
                message = (
                    f"I detected {esc(manufacturer)}{esc(size_text)} + trocar/access port, "
                    "but could not confidently find a matching IFU. Try adding the catalog "
                    "number or product family such as ENDOPATH/XCEL."
                )
            else:
                message = (
                    "I could not confidently identify the device/IFU from this question. "
                    "Try adding a catalog number, manufacturer, or exact device name."
                )
        else:
            message = f"No devices found for <strong>{esc(query)}</strong>."
        body = f"""
        <form class="search" method="get" action="/search">
          <input type="search" name="q" value="{esc(query)}"
                 aria-label="Search devices">
          <button type="submit">Search</button>
        </form>
        {detected_html}
        <p class="warning">{message}</p>
        """
        return _page("Search — ChatIFU", f"Results for “{query}”", body)

    def render_cards(group_devices: list[dict[str, Any]]) -> str:
        cards = []
        for dev in group_devices:
            brand = esc(dev.get("brand_name") or "")
            company = esc(dev.get("company_name") or "")
            catalog = esc(dev.get("catalog_number") or "")
            model = esc(dev.get("model_number") or "")
            score = dev.get("_score")
            confidence = float(dev.get("_confidence") or 0.0)
            reasons = dev.get("_score_reasons") or []
            score_text = f"Match score: {esc(score)}" if score is not None else ""
            confidence_text = _confidence_label(confidence) if confidence else ""
            source_note = "FDA device identity match — IFU not resolved yet" if dev.get("is_gudid_identity_only") else ""
            meta_parts = [p for p in [company, f"Catalog: {catalog}", f"Model: {model}", score_text, confidence_text, source_note] if p]
            href = esc(_device_link(catalog, initial_question))
            action_label = "Use this IFU" if initial_question else "Ask IFU question"
            why = ""
            if reasons:
                items = "".join(
                    f"<li>{esc(r.get('delta'))}: {esc(r.get('label'))}</li>"
                    for r in reasons[:6]
                    if isinstance(r, dict)
                )
                why = f'<details style="margin-top:10px"><summary>Why this match?</summary><ul>{items}</ul></details>'
            cards.append(f"""
            <div class="device-card">
              <div class="brand">{brand or catalog}</div>
              <div class="meta">{" &bull; ".join(meta_parts)}</div>
              {why}
              <div class="actions">
                <a class="button" href="{href}">{action_label}</a>
                <button type="button" class="button button-outline"
                  onclick="sendFeedback('wrong_ifu','{catalog}','{esc(_hash_text(initial_question or query))}')">Wrong IFU?</button>
                <button type="button" class="button button-outline"
                  onclick="sendFeedback('right_ifu','{catalog}','{esc(_hash_text(initial_question or query))}')">This is the right IFU</button>
              </div>
            </div>""")
        return f'<div class="card-row">{"".join(cards)}</div>'

    grouped = _group_ranked_devices(parsed_query, devices) if parsed_query else [("", devices)]
    group_html = []
    for title, group_devices in grouped:
        heading = f"<h2>{esc(title)}</h2>" if title else ""
        group_html.append(heading + render_cards(group_devices))

    lead = (
        '<p style="color:var(--muted)">I found possible IFUs for your question. '
        'Choose the matching device to run the question against its IFU.</p>'
        if initial_question else ""
    )
    displayed_terms = device_terms or ", ".join(searched_terms or [])
    terms_line = (
        f'<p style="color:var(--muted);font-size:.9rem">Device terms searched: '
        f'<strong>{esc(displayed_terms)}</strong></p>'
        if initial_question and displayed_terms else ""
    )
    body = f"""
    <form class="search" method="get" action="/search">
      <input type="search" name="q" value="{esc(query)}"
             aria-label="Search devices">
      <button type="submit">Search</button>
    </form>
    {detected_html}
    {lead}
    {terms_line}
    <p style="color:var(--muted)"><strong>{len(devices)}</strong> device(s) found for
       &ldquo;{esc(query)}&rdquo;</p>
    {"".join(group_html)}
    {_feedback_script()}
    """
    return _page("Search — ChatIFU", f"Results for “{query}”", body)


def _feedback_script() -> str:
    return """
    <script>
    async function sendFeedback(type, catalog, queryHash) {
      const comment = type === 'wrong_ifu'
        ? prompt('What should it have been? Manufacturer/catalog/device name. Do not include patient-identifying information.') || ''
        : '';
      try {
        await fetch('/api/feedback', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            feedback_type: type,
            catalog: catalog,
            query_hash: queryHash,
            question_hash: queryHash,
            comment: comment
          })
        });
      } catch (err) {}
    }
    </script>"""


def _store_feedback(payload: dict[str, Any]) -> bool:
    allowed = {"wrong_ifu", "right_ifu", "helpful_answer", "not_helpful_answer"}
    feedback_type = str(payload.get("feedback_type") or "")
    if feedback_type not in allowed:
        raise ValueError("invalid feedback_type")
    catalog = str(payload.get("catalog") or "")[:120]
    comment = str(payload.get("comment") or "")[:1000]
    question = str(payload.get("question") or "")
    parsed_fields = payload.get("parsed_fields") if isinstance(payload.get("parsed_fields"), dict) else {}
    record = {
        "feedback_type": feedback_type,
        "catalog": catalog,
        "selected_device": str(payload.get("selected_device") or "")[:200],
        "query_hash": str(payload.get("query_hash") or _hash_text(question)) if (payload.get("query_hash") or question) else "",
        "question_hash": str(payload.get("question_hash") or _hash_text(question)) if (payload.get("question_hash") or question) else "",
        "correct_device_hint": str(payload.get("correct_device_hint") or "")[:300],
        "comment": comment,
        "score_snapshot": payload.get("score_snapshot") if isinstance(payload.get("score_snapshot"), list) else [],
        "parsed_fields": {
            "manufacturer_terms": parsed_fields.get("manufacturer_terms") or [],
            "detected_concepts": parsed_fields.get("detected_concepts") or [],
            "size_terms": parsed_fields.get("size_terms") or [],
            "problem_categories": parsed_fields.get("problem_categories") or [],
        },
    }
    if not os.environ.get("CHATIFU_ALLOW_RAW_FEEDBACK_QUESTION"):
        record.pop("question", None)
    else:
        record["question"] = question[:1000]
    path = _feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def _search_debug_payload(
    q: str,
    db_path: Path,
    gudid_client: GUDIDClient | None = None,
) -> dict[str, Any]:
    parsed = parse_medical_device_query(q)
    ranked, searches = _ranked_search_devices(parsed, db_path, gudid_client=gudid_client)
    registry = IFUResolverRegistry(db_path=db_path)
    candidates: list[dict[str, Any]] = []
    for rank, dev in enumerate(ranked[:50], start=1):
        attempts = registry.resolver_attempts(dev, parsed)
        candidates.append({
            "rank": rank,
            "score": dev.get("_score"),
            "confidence": dev.get("_confidence"),
            "manufacturer": dev.get("company_name"),
            "brand_name": dev.get("brand_name"),
            "catalog_number": dev.get("catalog_number"),
            "model_number": dev.get("model_number"),
            "source": dev.get("source") or "local",
            "resolver_hint": "gudid_identity_only" if dev.get("is_gudid_identity_only") else None,
            "score_reasons": dev.get("_score_reasons") or [],
            "resolver_attempts": attempts,
        })
    return {
        "cleaned_query": q,
        "parsed_query": {
            "cleaned_query": parsed.cleaned_query,
            "manufacturer_terms": parsed.manufacturer_terms,
            "manufacturer_aliases": parsed.manufacturer_aliases,
            "device_terms": parsed.device_terms,
            "concept_terms": parsed.concept_terms,
            "size_terms": parsed.size_terms,
            "problem_terms": parsed.problem_terms,
            "problem_categories": problem_categories_for_terms(parsed.problem_terms),
            "search_terms": parsed.search_terms,
            "detected_concepts": parsed.detected_concepts,
            "original_question_hash": _hash_text(parsed.original_question),
        },
        "generated_search_strings": searches,
        "candidates": candidates,
        "cache": {
            "gudid": gudid_client.cache.stats() if gudid_client else {"enabled": False},
        },
        "feedback_summary": _feedback_summary_for_hash(_hash_text(q)),
    }


def _feedback_summary_for_hash(query_hash: str) -> dict[str, int]:
    counts = {"wrong_ifu": 0, "right_ifu": 0, "helpful_answer": 0, "not_helpful_answer": 0}
    path = _feedback_path()
    if not path.exists():
        return counts
    try:
        for line in path.read_text("utf-8").splitlines():
            item = json.loads(line)
            if item.get("query_hash") == query_hash or item.get("question_hash") == query_hash:
                feedback_type = item.get("feedback_type")
                if feedback_type in counts:
                    counts[feedback_type] += 1
    except Exception:
        return counts
    return counts


def _render_search_debug(debug: dict[str, Any]) -> str:
    parsed = debug.get("parsed_query") or {}
    rows = []
    for cand in debug.get("candidates") or []:
        reasons = "".join(
            f"<li>{esc(reason.get('delta'))}: {esc(reason.get('label'))}</li>"
            for reason in cand.get("score_reasons", [])[:8]
            if isinstance(reason, dict)
        )
        attempts = "".join(
            f"<li>{esc(a.get('resolver'))}: can_handle={esc(a.get('can_handle'))} "
            f"document={esc(a.get('document_url'))} failure={esc(a.get('failure'))}</li>"
            for a in cand.get("resolver_attempts", [])
            if isinstance(a, dict)
        )
        rows.append(f"""
        <tr>
          <td>{esc(cand.get('rank'))}</td>
          <td>{esc(cand.get('score'))}</td>
          <td>{esc(cand.get('confidence'))}</td>
          <td>{esc(cand.get('manufacturer'))}</td>
          <td>{esc(cand.get('brand_name'))}</td>
          <td>{esc(cand.get('catalog_number'))}</td>
          <td><details><summary>Reasons</summary><ul>{reasons}</ul></details></td>
          <td><details><summary>Resolvers</summary><ul>{attempts}</ul></details></td>
        </tr>
        """)
    body = f"""
    <section class="card">
      <h2>Parsed query</h2>
      <pre>{esc(json.dumps(parsed, indent=2))}</pre>
      <h2>Generated search strings</h2>
      <pre>{esc(json.dumps(debug.get('generated_search_strings'), indent=2))}</pre>
    </section>
    <section class="card">
      <h2>Ranked candidates</h2>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr><th>Rank</th><th>Score</th><th>Confidence</th><th>Manufacturer</th><th>Brand</th><th>Catalog</th><th>Why</th><th>Resolvers</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </section>
    """
    return _page("Admin Search Debug — ChatIFU", f"Debug for “{debug.get('cleaned_query')}”", body)


def _render_cache_admin(stats: dict[str, Any]) -> str:
    body = f"""
    <section class="card">
      <h2>IFU cache</h2>
      <pre>{esc(json.dumps(stats.get('ifu_cache'), indent=2))}</pre>
      <h2>GUDID cache</h2>
      <pre>{esc(json.dumps(stats.get('gudid_cache'), indent=2))}</pre>
      <h2>Feedback path</h2>
      <p>{esc(stats.get('feedback_path'))}</p>
    </section>
    """
    return _page("Admin Cache — ChatIFU", "Cache and feedback storage", body)


# ------------------------------------------------------------------
# Device page
# ------------------------------------------------------------------

def render_device_page(
    catalog: str,
    device: dict[str, Any] | None,
    ifu_title: str | None,
    ifu_url: str | None,
    initial_question: str | None = None,
    error: str | None = None,
) -> str:
    if device is None:
        body = f'<div class="card error-card"><p>Device <strong>{esc(catalog)}</strong> not found.</p></div>'
        return _page(f"{esc(catalog)} — ChatIFU", "Device not found", body)

    brand = esc(device.get("brand_name") or "")
    company = esc(device.get("company_name") or "")
    catalog_e = esc(device.get("catalog_number") or catalog)
    model = esc(device.get("model_number") or "")

    ifu_section = _render_ask_section(catalog_e, ifu_title, ifu_url, error, initial_question)

    body = f"""
    <div class="card">
      <h2>{brand or catalog_e}</h2>
      <dl>
        <dt>Catalog</dt><dd>{catalog_e}</dd>
        <dt>Model</dt><dd>{model or "&mdash;"}</dd>
        <dt>Manufacturer</dt><dd>{company or "&mdash;"}</dd>
      </dl>
    </div>
    {ifu_section}
    """
    return _page(
        f"{brand or catalog_e} — ChatIFU",
        "Ask a question from this device's IFU",
        body,
        footer=OPTION_B_NOTE,
    )


def _render_ask_section(
    catalog: str,
    ifu_title: str | None,
    ifu_url: str | None,
    error: str | None,
    initial_question: str | None = None,
) -> str:
    if error:
        return f'<div class="card error-card"><div class="badge badge-error">error</div><p>{esc(error)}</p></div>'

    if not ifu_url:
        return f"""
        <div class="card">
          <div class="badge badge-candidate">no ifu</div>
          <h2>IFU not indexed for this device</h2>
          <p>No manufacturer IFU link has been cached for catalog <strong>{catalog}</strong>.
             Try asking a question anyway — the system will attempt to locate the IFU
             when you submit.</p>
          {_ask_form(catalog, initial_question)}
        </div>
        <div id="answer-area"></div>
        {_ask_script()}"""

    title_display = esc(ifu_title or ifu_url)
    return f"""
    <div class="card">
      <div class="badge badge-found">ifu ready</div>
      <h2>Ask a question from this IFU</h2>
      <p style="color:var(--muted);font-size:.92rem">Source: {title_display}</p>
      {_ask_form(catalog, initial_question)}
    </div>
    <div id="answer-area"></div>
    {_ask_script()}"""


def _ask_form(catalog: str, initial_question: str | None = None) -> str:
    return f"""
    <form class="search" id="ask-form" style="margin-top:14px">
      <input type="hidden" name="catalog" value="{catalog}">
      <input type="text" name="q" placeholder="e.g. what are the warnings for reuse"
             value="{esc(initial_question or '')}" aria-label="Your question" required>
      <button type="submit">Ask</button>
    </form>"""


def _ask_script() -> str:
    return """
    <script>
    const askForm = document.getElementById('ask-form');
    askForm.addEventListener('submit', async function(e) {
      e.preventDefault();
      const catalog = this.querySelector('[name=catalog]').value;
      const q = this.querySelector('[name=q]').value.trim();
      if (!q) return;
      const area = document.getElementById('answer-area');
      area.innerHTML = '<p class="loader">Fetching IFU&#8230; this takes 1&#8211;3 seconds</p>';
      try {
        const resp = await fetch('/api/ask?catalog=' + encodeURIComponent(catalog) +
                                 '&q=' + encodeURIComponent(q));
        const data = await resp.json();
        area.innerHTML = renderAnswer(data);
      } catch(err) {
        area.innerHTML = '<p class="warning">Request failed: ' + err.message + '</p>';
      }
    });

    const initialQuestion = askForm.querySelector('[name=q]').value.trim();
    if (initialQuestion) {
      setTimeout(function() {
        if (askForm.requestSubmit) {
          askForm.requestSubmit();
        } else {
          askForm.dispatchEvent(new Event('submit', { cancelable: true }));
        }
      }, 0);
    }

    function renderAnswer(data) {
      if (data.error) {
        return '<div class="card error-card"><div class="badge badge-error">error</div>' +
               '<p>' + escHtml(data.error) + '</p></div>';
      }

      const hits = data.hits || [];
      const timing = data.timing_ms || {};
      const totalMs = timing.total_ms ? Math.round(timing.total_ms) : '?';
      const pageCount = data.page_count ? ' / ' + data.page_count + ' pages searched' : '';
      const iframeUrl = chooseIframeUrl(data);
      const openFullIfuUrl = data.open_full_ifu_url || data.document_url || data.pdf_url ||
        (isGenericLandingUrl(data.source_url) ? '' : data.source_url);

      // FIX 3: Prominent "Open Full IFU" button always at top
      const ifuBtn = openFullIfuUrl
        ? '<a href="' + escHtml(openFullIfuUrl) + '" target="_blank" rel="noopener" ' +
          'style="display:inline-block;padding:10px 20px;background:#0066cc;color:white;' +
          'border-radius:4px;text-decoration:none;font-weight:bold;margin-bottom:16px;">' +
          'Open Full IFU</a>'
        : '';
      const timingLine = '<p class="timing">Fetched in ' + totalMs + 'ms' + pageCount + '</p>';
      const disclaimer = '<p style="font-size:0.8rem;color:var(--muted);margin-top:8px">' +
        'AI-assisted. Verify all information in the full IFU before clinical use.</p>';

      if (!hits.length) {
        const emptyPanel = ifuBtn +
          '<div class="badge badge-answer">answer</div>' +
          '<p style="margin-top:10px">No matching passages found for this question in the IFU.</p>' +
          timingLine + disclaimer;
        return renderAnswerLayout(iframeUrl, emptyPanel, openFullIfuUrl);
      }

      const title = data.document_title
        ? '<h3 style="margin:10px 0 6px">' + escHtml(data.document_title) + '</h3>'
        : '';

      const hitsHtml = hits.map(function(h) {
        const page = Number(h.page) || 0;
        const section = h.section ? escHtml(h.section) : 'Relevant IFU passage';
        return '<div class="hit">' +
               '<div class="hit-page">Page ' + escHtml(h.page) + '</div>' +
               '<div class="hit-section">Section: ' + section + '</div>' +
               '<div style="font-size:0.86rem;color:var(--muted);margin-bottom:6px">' +
               'The IFU passage says:</div>' +
               '<div class="hit-snippet"><mark>' + escHtml(h.snippet) + '</mark></div>' +
               '<button type="button" class="page-jump" onclick="goToPage(' + page + ')">' +
               'Go to page ' + escHtml(h.page) + '</button>' +
               '<button type="button" class="page-jump" onclick="postAnswerFeedback(\\'helpful_answer\\')">' +
               'Helpful</button>' +
               '<button type="button" class="page-jump" onclick="postAnswerFeedback(\\'not_helpful_answer\\')">' +
               'Not helpful</button>' +
               '<button type="button" class="page-jump" onclick="postAnswerFeedback(\\'wrong_ifu\\')">' +
               'Wrong IFU?</button>' +
               '</div>';
      }).join('');

      const rightPanel = ifuBtn + title + hitsHtml + timingLine + disclaimer;
      return renderAnswerLayout(iframeUrl, rightPanel, openFullIfuUrl);
    }

    function chooseIframeUrl(data) {
      const candidates = [data.iframe_url, data.document_url, data.pdf_url];
      for (const candidate of candidates) {
        if (candidate && !isGenericLandingUrl(candidate)) return candidate;
      }
      if (data.source_url && !isGenericLandingUrl(data.source_url)) return data.source_url;
      return '';
    }

    function isGenericLandingUrl(url) {
      if (!url) return false;
      const low = String(url).toLowerCase();
      const looksLikeDocument = low.includes('/ifu/pdf') || low.includes('/fetchpdf/') ||
        low.includes('/viewpdf') || low.includes('.pdf') || low.includes('e-ifu.com');
      const looksGeneric = low.includes('jnjmedtech.com') ||
        low.includes('johnson') || low.includes('product-page');
      return looksGeneric && !looksLikeDocument;
    }

    function renderAnswerLayout(iframeUrl, rightPanel, openFullIfuUrl) {
      // The e-IFU viewer is cross-origin, so we do not attempt iframe DOM
      // highlighting. The right-panel excerpt is highlighted instead.
      if (iframeUrl) {
        return '<div class="answer-split">' +
               '<div class="ifu-pane">' +
               '<iframe id="ifu-frame" src="' + escHtml(iframeUrl) + '" data-base-src="' +
               escHtml(iframeUrl) + '" width="100%" height="100%" ' +
               'style="border:none;" title="IFU Document Viewer"></iframe>' +
               '</div>' +
               '<div class="answer-pane">' + rightPanel + '</div>' +
               '</div>';
      }
      if (openFullIfuUrl) {
        return '<div class="answer-split">' +
               '<div class="ifu-pane" style="padding:18px;color:var(--muted)">' +
               'The IFU answer was generated from the document, but the document viewer could not be embedded. ' +
               'Use Open Full IFU.</div>' +
               '<div class="answer-pane">' + rightPanel + '</div>' +
               '</div>';
      }
      return '<div class="answer-card">' +
             '<div class="badge badge-answer">answer</div>' +
             rightPanel + '</div>';
    }

    function goToPage(page) {
      const iframe = document.getElementById('ifu-frame');
      if (!iframe || !page) return;

      try {
        iframe.contentWindow.postMessage({ type: 'goToPage', page: page }, '*');
      } catch (err) {
        // Cross-origin viewers may ignore this.
      }

      try {
        const base = iframe.dataset.baseSrc || iframe.src.split('#')[0];
        iframe.src = base + '#page=' + encodeURIComponent(page);
      } catch (err) {
        // Ignore navigation failures.
      }
    }

    function escHtml(s) {
      return String(s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    }

    async function postAnswerFeedback(type) {
      const catalog = askForm.querySelector('[name=catalog]').value;
      const comment = type === 'wrong_ifu'
        ? prompt('What should it have been? Manufacturer/catalog/device name. Do not include patient-identifying information.') || ''
        : '';
      try {
        await fetch('/api/feedback', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({feedback_type: type, catalog: catalog, comment: comment})
        });
      } catch (err) {}
    }
    </script>"""


# ------------------------------------------------------------------
# Original catalog lookup page (kept intact for backward compat)
# ------------------------------------------------------------------

def render_page(catalog: str = "", result: dict[str, Any] | None = None, error: str | None = None) -> str:
    title = "ChatIFU — Catalog Lookup"
    result_html = ""
    if error:
        result_html = f"""
        <section class="card error-card">
          <div class="badge badge-error">error</div>
          <h2>Lookup error</h2>
          <p>{esc(error)}</p>
        </section>
        """
    elif result:
        result_html = _render_lookup_result(result)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{_CSS}</style>
</head>
<body>
  <main>
    <header>
      <h1><a href="/" style="text-decoration:none;color:inherit">ChatIFU</a></h1>
      <p class="subtitle">Find manufacturer e-IFU metadata links by catalog number</p>
    </header>
    <form class="search" method="get" action="/lookup">
      <input type="search" name="catalog" value="{esc(catalog)}" placeholder="GIB00U0340"
             aria-label="Catalog number" required>
      <button type="submit">Search</button>
    </form>
    {result_html}
    <footer>{SAFETY_NOTE}</footer>
  </main>
</body>
</html>
"""


def _render_lookup_result(result: dict[str, Any]) -> str:
    status = result.get("status") or "unknown"
    badge_class = (
        "badge-found" if status == "found"
        else "badge-candidate" if status == "candidate_broad"
        else "badge-error"
    )
    button = ""
    if result.get("document_url"):
        button = (
            f'<a class="button" href="{esc(result["document_url"])}"'
            f' target="_blank" rel="noopener noreferrer">Open manufacturer e-IFU</a>'
        )
    warning = ""
    if result.get("warning") or status == "candidate_broad":
        warning = f'<div class="warning">{esc(result.get("warning") or "Broad candidate result. Verify before use.")}</div>'
    candidates = _render_candidates(result.get("candidates") or [])
    source_file = ""
    if result.get("source_file_name"):
        source_file = f"<dt>Source file</dt><dd>{esc(result['source_file_name'])}</dd>"
    return f"""
    <section class="card">
      <div class="badge {badge_class}">{esc(status)}</div>
      <h2>{esc(result.get('document_title') or 'No manufacturer document link found')}</h2>
      {warning}
      <dl>
        <dt>Catalog</dt><dd>{esc(result.get('catalog_number'))}</dd>
        <dt>Confidence</dt><dd>{esc(result.get('match_confidence'))}</dd>
        <dt>Language</dt><dd>{esc(result.get('language'))}</dd>
        <dt>Revision</dt><dd>{esc(result.get('revision'))}</dd>
        <dt>Source</dt><dd>e-IFU metadata</dd>
        {source_file}
        <dt>Retrieved</dt><dd>{esc(result.get('retrieved_at'))}</dd>
        <dt>Last checked</dt><dd>{esc(result.get('last_checked_at'))}</dd>
      </dl>
      {button}
      {candidates}
    </section>
    """


def _render_candidates(candidates: list[dict[str, Any]]) -> str:
    if len(candidates) <= 1:
        return ""
    items = []
    for candidate in candidates:
        link = ""
        if candidate.get("document_url"):
            link = (
                f' <a href="{esc(candidate["document_url"])}"'
                f' target="_blank" rel="noopener noreferrer">Open</a>'
            )
        items.append(
            f"""<div class="candidate-item">
              <strong>{esc(candidate.get('document_title') or 'Untitled candidate')}</strong>{link}
              <div>Confidence: {esc(candidate.get('match_confidence'))}</div>
            </div>"""
        )
    return f'<div class="candidates"><h3>Broad candidates</h3>{"".join(items)}</div>'


# ------------------------------------------------------------------
# Request handler
# ------------------------------------------------------------------

class MvpHandler(BaseHTTPRequestHandler):
    db_path: Path = SQLITE_PATH
    answerer: IFUAnswerer = IFUAnswerer()
    ifu_cache: IFUDocumentCache | None = None
    gudid_client: GUDIDClient | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        def param(name: str, default: str = "") -> str:
            return (params.get(name) or [default])[0].strip()

        if path == "/":
            self.send_html(render_search_home())
            return

        if path == "/search":
            q = _clean_user_query(param("q"))
            if not q:
                self.send_html(render_search_home(error="Please enter a search term."))
                return
            devices = search_devices(q, db_path=self.db_path)
            if _looks_like_ifu_question(q):
                parsed_query = parse_medical_device_query(q)
                ranked_devices, searched_terms = _ranked_search_devices(
                    parsed_query,
                    self.db_path,
                    gudid_client=self.gudid_client,
                )
                if ranked_devices:
                    devices = ranked_devices
                else:
                    searched_terms = [_extract_device_terms(q)]
                best = _best_auto_device(devices, self.db_path)
                if best and best.get("catalog_number"):
                    catalog_best = str(best["catalog_number"])
                    device = get_device(catalog_best, db_path=self.db_path)
                    ifu_url = get_best_ifu_url(catalog_best, db_path=self.db_path)
                    ifu_title: str | None = None
                    if ifu_url:
                        for row in fetch_ifu_rows(catalog_best, db_path=self.db_path):
                            if row.get("document_url") == ifu_url:
                                ifu_title = row.get("document_title")
                                break
                    self.send_html(render_device_page(
                        catalog_best, device, ifu_title, ifu_url, initial_question=q
                    ))
                    return
                self.send_html(render_search_results(
                    q,
                    devices,
                    initial_question=q,
                    device_terms=" ".join(parsed_query.search_terms),
                    parsed_query=parsed_query,
                    searched_terms=searched_terms,
                ))
                return
            self.send_html(render_search_results(q, devices))
            return

        if path == "/device" or path.startswith("/device/"):
            path_catalog = path.removeprefix("/device/") if path.startswith("/device/") else ""
            catalog = param("catalog") or path_catalog
            if not catalog:
                self.send_html(render_search_home(error="catalog parameter is required."), status=400)
                return
            catalog = _clean_user_query(catalog)
            initial_question = _clean_user_query(param("q"))
            device = get_device(catalog, db_path=self.db_path)
            ifu_url = get_best_ifu_url(catalog, db_path=self.db_path)
            ifu_title: str | None = None
            if ifu_url:
                rows = fetch_ifu_rows(catalog, db_path=self.db_path)
                for row in rows:
                    if row.get("document_url") == ifu_url:
                        ifu_title = row.get("document_title")
                        break
            self.send_html(render_device_page(
                catalog, device, ifu_title, ifu_url, initial_question=initial_question
            ))
            return

        if path == "/ifu/pdf":
            catalog = _clean_user_query(param("catalog"))
            if not catalog:
                self.send_json({"error": "catalog is required"}, status=400)
                return
            doc_url = get_best_ifu_url(catalog, db_path=self.db_path)
            if not doc_url:
                try:
                    ensure_ifu_for_catalog(catalog, db_path=self.db_path)
                    doc_url = get_best_ifu_url(catalog, db_path=self.db_path)
                except Exception as exc:
                    self.send_json({"error": f"IFU lookup failed: {exc}"}, status=500)
                    return
            if not doc_url:
                self.send_json({"error": "No IFU found for this device on e-ifu.com"}, status=404)
                return
            try:
                if self.ifu_cache is not None and not _is_direct_pdf_url(doc_url):
                    pdf_bytes, _cached_doc, cache_hit = self.ifu_cache.get_or_fetch(
                        doc_url,
                        lambda: self.answerer.fetch_pdf_bytes(doc_url),
                    )
                else:
                    pdf_bytes, _pdf_url, _title = self.answerer.fetch_pdf_bytes(doc_url)
                    cache_hit = False
            except Exception as exc:
                self.send_json({"error": f"PDF fetch failed: {exc}"}, status=502)
                return
            self.send_pdf(pdf_bytes, filename=f"{catalog}-ifu.pdf", extra_headers={"X-ChatIFU-Cache": "hit" if cache_hit else "miss"})
            return

        if path == "/api/ask":
            catalog = param("catalog")
            question = param("q")
            if not catalog or not question:
                self.send_json({"error": "catalog and q are required"}, status=400)
                return

            # Ensure we have an IFU link (run resolver if needed)
            doc_url = get_best_ifu_url(catalog, db_path=self.db_path)
            if not doc_url:
                try:
                    ensure_ifu_for_catalog(catalog, db_path=self.db_path)
                    doc_url = get_best_ifu_url(catalog, db_path=self.db_path)
                except Exception as exc:
                    self.send_json({"error": f"IFU lookup failed: {exc}", "hits": []})
                    return

            if not doc_url:
                self.send_json({
                    "error": "No IFU found for this device on e-ifu.com",
                    "hits": [],
                    "catalog_number": catalog,
                })
                return

            result: AnswerResult = self.answerer.answer(doc_url, question)
            actual_document_url = result.document_url or result.pdf_url
            open_full_ifu_url = result.open_full_ifu_url or actual_document_url or doc_url
            iframe_url = _pdf_proxy_url(catalog) if actual_document_url and not result.error else None
            self.send_json({
                "catalog_number": catalog,
                "document_title": result.document_title,
                "source_url": result.source_url,
                "manufacturer_url": result.manufacturer_url or doc_url,
                "metadata_url": doc_url,
                "document_url": actual_document_url,
                "iframe_url": iframe_url,
                "open_full_ifu_url": open_full_ifu_url,
                "pdf_url": result.pdf_url,
                "page_count": result.page_count,
                "hits": [
                    {"page": h.page, "section": h.section, "snippet": h.snippet}
                    for h in result.hits
                ],
                "timing_ms": result.timing_ms,
                "error": result.error,
            })
            return

        if path in {"/admin/search_debug", "/api/admin/search_debug"}:
            if not self._admin_allowed(params):
                self.send_json({"error": "admin access denied"}, status=403)
                return
            q = _clean_user_query(param("q"))
            debug = _search_debug_payload(q, self.db_path, self.gudid_client)
            if path.startswith("/api/"):
                self.send_json(debug)
            else:
                self.send_html(_render_search_debug(debug))
            return

        if path == "/admin/cache":
            if not self._admin_allowed(params):
                self.send_json({"error": "admin access denied"}, status=403)
                return
            stats = {
                "ifu_cache": self.ifu_cache.stats() if self.ifu_cache else {"enabled": False},
                "gudid_cache": self.gudid_client.cache.stats() if self.gudid_client else None,
                "feedback_path": str(_feedback_path()),
            }
            self.send_html(_render_cache_admin(stats))
            return

        if path in {"/lookup", "/api/lookup"}:
            catalog = param("catalog")
            refresh = param("refresh") in {"1", "true", "yes"}
            if not catalog:
                if path == "/api/lookup":
                    self.send_json({"error": "catalog is required"}, status=400)
                else:
                    self.send_html(render_page(error="Catalog number is required."), status=400)
                return
            try:
                result_lu = lookup_catalog(catalog, db_path=self.db_path, refresh=refresh)
            except Exception as exc:
                if path == "/api/lookup":
                    self.send_json({"catalog_number": catalog, "error": str(exc)}, status=500)
                else:
                    self.send_html(render_page(catalog=catalog, error=str(exc)), status=500)
                return
            if path == "/api/lookup":
                self.send_json(result_lu)
            else:
                self.send_html(render_page(catalog=catalog, result=result_lu))
            return

        self.send_html(render_page(error="Page not found."), status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/api/feedback":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                stored = _store_feedback(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except Exception as exc:
                self.send_json({"error": f"feedback failed: {exc}"}, status=500)
                return
            self.send_json({"ok": True, "stored": stored})
            return

        if parsed.path == "/admin/cache/purge":
            if not self._admin_allowed(params):
                self.send_json({"error": "admin access denied"}, status=403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            cache_key = str(payload.get("cache_key") or "")
            document_url = str(payload.get("document_url") or "")
            if document_url and not cache_key:
                if self.ifu_cache is None:
                    self.send_json({"error": "IFU cache is not enabled"}, status=400)
                    return
                cache_key = self.ifu_cache.key_for_url(document_url)
            if not cache_key:
                self.send_json({"error": "cache_key or document_url is required"}, status=400)
                return
            self.send_json({"ok": self.ifu_cache.purge(cache_key) if self.ifu_cache else False, "cache_key": cache_key})
            return

        self.send_json({"error": "Page not found."}, status=404)

    def _admin_allowed(self, params: dict[str, list[str]]) -> bool:
        token = os.environ.get("CHATIFU_ADMIN_TOKEN")
        if token:
            supplied = self.headers.get("X-Admin-Token") or (params.get("token") or [""])[0]
            return supplied == token
        host = self.client_address[0] if self.client_address else ""
        return host in {"127.0.0.1", "::1", "localhost"}

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_html(self, content: str, status: int = 200) -> None:
        payload = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_pdf(
        self,
        payload: bytes,
        filename: str,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local ChatIFU MVP server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default=str(SQLITE_PATH))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    MvpHandler.db_path = Path(args.db)
    MvpHandler.ifu_cache = IFUDocumentCache()
    MvpHandler.answerer = IFUAnswerer(document_cache=MvpHandler.ifu_cache)
    MvpHandler.gudid_client = GUDIDClient(timeout=4) if os.environ.get("CHATIFU_ENABLE_GUDID") else None
    server = ThreadingHTTPServer((args.host, args.port), MvpHandler)
    print(f"ChatIFU MVP running at http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ChatIFU MVP.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
