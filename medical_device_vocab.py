from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from company_registry import COMPANY_REGISTRY, CompanyEntry, iter_company_terms


TYPO_NORMALIZATIONS = {
    "asnd": "and",
    "adn": "and",
    "teh": "the",
    "touble": "trouble",
    "troublshoot": "troubleshoot",
    "wont": "won't",
    "dont": "don't",
    "cant": "can't",
}

GENERIC_STOP_WORDS = {
    "i", "have", "has", "a", "an", "the", "and", "its", "it", "on", "in",
    "with", "what", "can", "should", "do", "to", "for", "trouble", "shoot",
    "troubleshoot", "problem", "issue", "is", "my", "this", "that", "of",
    "as", "at", "by", "from", "next", "needed", "need", "needs",
}

PROBLEM_CATEGORIES: dict[str, tuple[str, ...]] = {
    "LEAK_OR_SEAL_PROBLEM": (
        "leaking air", "air leak", "gas leak", "gas leakage", "seal leak",
        "insufflation leak", "loss of pneumoperitoneum", "losing pneumo",
        "won't hold pneumo", "won't hold pneumoperitoneum", "valve leak",
        "stopcock leak", "cap leak", "port leak", "trocar leak",
    ),
    "MECHANICAL_STUCK_OR_LOCKED": (
        "stuck", "locked", "jammed", "won't open", "wont open",
        "will not open", "cannot open", "can't open", "won't close",
        "won't release", "release problem", "manual release",
    ),
    "FIRING_OR_RELOAD_PROBLEM": (
        "won't fire", "wont fire", "will not fire", "cannot fire",
        "misfire", "partial fire", "incomplete fire", "firing problem",
        "reload problem", "staple malformed", "staple line problem",
    ),
    "ENERGY_DEVICE_PROBLEM": (
        "not sealing", "won't seal", "no energy", "generator alarm",
        "error code", "not cutting", "not coagulating", "weak seal",
        "thermal spread", "smoke", "footswitch not working",
    ),
    "ENDOSCOPY_PROBLEM": (
        "blurry", "fogging", "leak test failed", "no image", "poor image",
        "suction not working", "air water not working", "biopsy channel blocked",
        "scope leak",
    ),
    "CATHETER_PROBLEM": (
        "occluded", "blocked", "won't flush", "cannot flush", "leaking hub",
        "balloon won't inflate", "balloon won't deflate", "kinked",
        "resistance", "unable to advance",
    ),
    "GENERAL_SAFETY_OR_LABELING": (
        "error", "warning", "warnings", "caution", "contraindication",
        "contraindications",
    ),
}

PROBLEM_TERMS = tuple(term for terms in PROBLEM_CATEGORIES.values() for term in terms)

PROBLEM_SINGLE_WORDS = {
    "leaking", "leak", "air", "gas", "troubleshoot", "trouble", "shoot",
    "stuck", "error", "warning", "caution", "open", "fire", "fired",
    "firing", "pneumoperitoneum", "pneumo", "locked", "jammed", "release",
    "misfire", "seal", "sealing", "smoke", "occluded", "blocked", "kinked",
    "resistance", "blurry", "fogging",
}

DEVICE_CONCEPTS: dict[str, tuple[str, ...]] = {
    "LAPAROSCOPIC_TROCAR_ACCESS": (
        "trocar", "trocars", "access port", "port", "cannula", "sleeve",
        "sheath", "obturator", "laparoscopic access", "endoscopic access",
        "optical trocar", "bladeless trocar", "seal", "reducer cap",
        "stopcock", "insufflation", "desufflation", "pneumoperitoneum",
        "pneumo", "xcel",
    ),
    "SURGICAL_STAPLER": (
        "stapler", "cutter", "linear cutter", "endocutter", "reload",
        "cartridge", "jaw", "anvil", "firing", "fire", "tissue compression",
        "echelon", "endopath",
    ),
    "DRAIN_TROCAR_OR_SPIKE": (
        "blake", "drain", "channel drain", "jp drain", "jackson pratt",
        "bulb", "suction", "trocar tip", "drain trocar", "spike",
    ),
    "LAPAROSCOPIC_INSTRUMENTS": (
        "laparoscopic instrument", "grasper", "dissector", "scissors",
        "laparoscopic scissors", "hook", "spatula", "clip applier",
        "clip applier cartridge", "specimen bag", "retrieval bag",
        "endoscopic instrument", "laparoscopic hand instrument", "roticulator",
        "articulating instrument",
    ),
    "STAPLERS_RELOADS": (
        "stapler", "linear stapler", "linear cutter", "endocutter",
        "endoscopic stapler", "circular stapler", "reload", "cartridge",
        "staple load", "buttress", "reinforcement", "anvil", "jaw", "clamp",
        "firing", "fire", "fired", "firing knob", "articulation",
        "tissue compression", "staple line", "echelon",
    ),
    "ENERGY_DEVICES": (
        "energy device", "electrosurgical", "electrosurgery", "monopolar",
        "bipolar", "advanced bipolar", "ultrasonic", "harmonic",
        "vessel sealer", "vessel sealing", "generator", "handpiece",
        "footswitch", "active electrode", "return electrode", "grounding pad",
        "smoke evacuation", "coagulation", "cut mode", "coag mode",
    ),
    "CATHETERS": (
        "catheter", "guidewire", "guide wire", "sheath", "introducer",
        "dilator", "balloon catheter", "angioplasty balloon", "stent delivery",
        "lumen", "hub", "luer", "flush", "flushing", "infusion", "aspiration",
        "drainage catheter", "central venous catheter", "urinary catheter",
        "foley",
    ),
    "IMPLANTS": (
        "implant", "prosthesis", "graft", "mesh", "stent", "screw", "plate",
        "nail", "rod", "cage", "spacer", "anchor", "suture anchor",
        "arthroplasty", "hip implant", "knee implant", "shoulder implant",
        "fixation", "bone screw", "pedicle screw",
    ),
    "DRAINS": (
        "drain", "drainage", "channel drain", "blake", "jp", "jackson pratt",
        "bulb", "reservoir", "suction", "trocar tip", "drain trocar",
        "drain spike", "silicone drain",
    ),
    "ORTHOPEDIC_SYSTEMS": (
        "orthopedic", "orthopaedic", "screw", "plate", "nail", "rod",
        "reamer", "drill", "guide", "cutting block", "saw blade", "tibial",
        "femoral", "acetabular", "humeral", "hip", "knee", "shoulder",
        "trauma", "spine", "pedicle", "fixation system",
    ),
    "ENDOSCOPY_DEVICES": (
        "endoscope", "colonoscope", "gastroscope", "bronchoscope",
        "duodenoscope", "cystoscope", "ureteroscope", "arthroscope", "scope",
        "biopsy channel", "working channel", "valve", "insufflation valve",
        "suction valve", "air water valve", "leak test", "light guide",
        "camera head", "processor", "endoscopy tower",
    ),
}

CONCEPT_DISPLAY = {
    "LAPAROSCOPIC_TROCAR_ACCESS": "laparoscopic trocar / access port",
    "SURGICAL_STAPLER": "surgical stapler",
    "DRAIN_TROCAR_OR_SPIKE": "drain trocar / spike",
    "LAPAROSCOPIC_INSTRUMENTS": "laparoscopic instruments",
    "STAPLERS_RELOADS": "staplers / reloads",
    "ENERGY_DEVICES": "energy devices",
    "CATHETERS": "catheters",
    "IMPLANTS": "implants",
    "DRAINS": "drains",
    "ORTHOPEDIC_SYSTEMS": "orthopedic systems",
    "ENDOSCOPY_DEVICES": "endoscopy devices",
}


@dataclass
class ParsedMedicalDeviceQuery:
    cleaned_query: str
    manufacturer_terms: list[str]
    manufacturer_aliases: list[str]
    device_terms: list[str]
    concept_terms: list[str]
    size_terms: list[str]
    problem_terms: list[str]
    search_terms: list[str]
    detected_concepts: list[str]
    original_question: str


@dataclass
class ScoreReason:
    label: str
    delta: int
    field: str | None = None
    evidence: str | None = None


@dataclass
class ScoreResult:
    score: int
    confidence: float
    reasons: list[ScoreReason]


def normalize_query_text(q: str) -> str:
    value = re.sub(r"\s+", " ", (q or "").strip().lower())
    for typo, replacement in TYPO_NORMALIZATIONS.items():
        value = re.sub(rf"\b{re.escape(typo)}\b", replacement, value)
    value = re.sub(r"\bpneumo\b", "pneumoperitoneum pneumo", value)
    value = re.sub(r"\btrouble\s+shoot(?:ing)?\b", "troubleshoot", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_medical_device_query(q: str) -> ParsedMedicalDeviceQuery:
    original = q or ""
    cleaned = normalize_query_text(original)
    token_text = cleaned.replace("'", "")
    tokens = re.findall(r"[a-z0-9][a-z0-9+&.-]*", token_text)

    company_matches = _detect_companies(cleaned)
    manufacturer_terms = _unique([
        term
        for entry, term in company_matches
    ])
    manufacturer_aliases = _unique([
        alias
        for entry, _term in company_matches
        for alias in iter_company_terms(entry)
    ])

    detected_concepts, concept_terms = _detect_concepts(cleaned)
    size_terms = _extract_sizes(cleaned)
    problem_terms = _detect_problem_terms(cleaned)

    problem_words = set(PROBLEM_SINGLE_WORDS)
    for phrase in problem_terms:
        problem_words.update(re.findall(r"[a-z0-9]+", phrase.replace("'", "")))

    device_terms: list[str] = []
    for token in tokens:
        if token in GENERIC_STOP_WORDS or token in problem_words:
            continue
        if any(token in term.split() for term in manufacturer_terms):
            continue
        if token not in device_terms:
            device_terms.append(token)

    search_terms = _build_search_terms(
        manufacturer_terms=manufacturer_terms,
        device_terms=device_terms,
        concept_terms=concept_terms,
        size_terms=size_terms,
        detected_concepts=detected_concepts,
    )

    return ParsedMedicalDeviceQuery(
        cleaned_query=cleaned,
        manufacturer_terms=manufacturer_terms,
        manufacturer_aliases=manufacturer_aliases,
        device_terms=device_terms,
        concept_terms=concept_terms,
        size_terms=size_terms,
        problem_terms=problem_terms,
        search_terms=search_terms,
        detected_concepts=detected_concepts,
        original_question=original,
    )


def _detect_companies(cleaned: str) -> list[tuple[CompanyEntry, str]]:
    padded = f" {cleaned} "
    matches: list[tuple[CompanyEntry, str]] = []
    for entry in COMPANY_REGISTRY:
        for term in iter_company_terms(entry):
            low = term.lower()
            if f" {low} " in padded and (entry, low) not in matches:
                matches.append((entry, low))
    return matches


def _detect_concepts(cleaned: str) -> tuple[list[str], list[str]]:
    detected: list[str] = []
    terms: list[str] = []
    for concept, concept_terms in DEVICE_CONCEPTS.items():
        for term in concept_terms:
            if _contains_term(cleaned, term):
                if concept not in detected:
                    detected.append(concept)
                if term not in terms and term not in PROBLEM_SINGLE_WORDS:
                    terms.append(term)
    if "DRAIN_TROCAR_OR_SPIKE" in detected and "DRAINS" not in detected:
        detected.append("DRAINS")
    if "SURGICAL_STAPLER" in detected and "STAPLERS_RELOADS" not in detected:
        detected.append("STAPLERS_RELOADS")
    if "LAPAROSCOPIC_TROCAR_ACCESS" in detected and "DRAIN_TROCAR_OR_SPIKE" in detected:
        if any(_contains_term(cleaned, term) for term in ("blake", "drain", "jp drain", "jackson pratt", "suction", "spike")):
            detected = ["DRAIN_TROCAR_OR_SPIKE"] + [c for c in detected if c != "DRAIN_TROCAR_OR_SPIKE"]
        else:
            detected = ["LAPAROSCOPIC_TROCAR_ACCESS"] + [c for c in detected if c != "LAPAROSCOPIC_TROCAR_ACCESS"]
    return detected, terms


def _extract_sizes(cleaned: str) -> list[str]:
    sizes: list[str] = []
    for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s*(mm|fr|f|cm|g|ga)\b", cleaned):
        compact = f"{m.group(1)}{m.group(2)}"
        spaced = f"{m.group(1)} {m.group(2)}"
        for value in (compact, spaced):
            if value not in sizes:
                sizes.append(value)
    return sizes


def _detect_problem_terms(cleaned: str) -> list[str]:
    terms: list[str] = []
    for term in PROBLEM_TERMS:
        normalized = normalize_query_text(term)
        if _contains_term(cleaned, normalized):
            terms.append("air leak" if normalized == "leaking air" else normalized)
    if "leaking" in cleaned and "air" in cleaned and "air leak" not in terms:
        terms.append("air leak")
    if "troubleshoot" in cleaned and "troubleshooting" not in terms:
        terms.append("troubleshooting")
    return _unique(terms)


def _build_search_terms(
    manufacturer_terms: list[str],
    device_terms: list[str],
    concept_terms: list[str],
    size_terms: list[str],
    detected_concepts: list[str],
) -> list[str]:
    terms = []
    terms.extend(manufacturer_terms)
    terms.extend(size_terms)
    terms.extend(device_terms)
    preferred = _preferred_concept_terms(detected_concepts, concept_terms)
    terms.extend(preferred)
    return _unique([term for term in terms if term and term not in GENERIC_STOP_WORDS])


def _preferred_concept_terms(detected_concepts: list[str], concept_terms: list[str]) -> list[str]:
    preferred: list[str] = []
    if "LAPAROSCOPIC_TROCAR_ACCESS" in detected_concepts:
        preferred.extend(["trocar", "access", "cannula", "sleeve", "endopath", "xcel"])
    if "SURGICAL_STAPLER" in detected_concepts:
        preferred.extend(["stapler", "cutter", "reload", "cartridge", "echelon", "endopath"])
    if "DRAIN_TROCAR_OR_SPIKE" in detected_concepts:
        preferred.extend(["blake", "drain", "suction", "trocar", "spike"])
    if "LAPAROSCOPIC_INSTRUMENTS" in detected_concepts:
        preferred.extend(["laparoscopic", "grasper", "dissector", "scissors", "clip applier", "retrieval bag"])
    if "STAPLERS_RELOADS" in detected_concepts:
        preferred.extend(["stapler", "reload", "cartridge", "linear cutter", "endocutter"])
    if "ENERGY_DEVICES" in detected_concepts:
        preferred.extend(["energy", "electrosurgical", "harmonic", "vessel sealer", "generator", "handpiece"])
    if "CATHETERS" in detected_concepts:
        preferred.extend(["catheter", "guidewire", "sheath", "balloon", "lumen", "hub"])
    if "IMPLANTS" in detected_concepts:
        preferred.extend(["implant", "screw", "plate", "stent", "anchor", "fixation"])
    if "DRAINS" in detected_concepts:
        preferred.extend(["drain", "blake", "jp", "reservoir", "suction"])
    if "ORTHOPEDIC_SYSTEMS" in detected_concepts:
        preferred.extend(["orthopedic", "screw", "plate", "nail", "tibial", "fixation"])
    if "ENDOSCOPY_DEVICES" in detected_concepts:
        preferred.extend(["endoscope", "colonoscope", "scope", "leak test", "working channel"])
    preferred.extend(concept_terms)
    return preferred


def score_device_candidate(parsed_query: ParsedMedicalDeviceQuery, candidate: dict[str, Any]) -> int:
    return score_device_candidate_details(parsed_query, candidate).score


def score_device_candidate_details(
    parsed_query: ParsedMedicalDeviceQuery,
    candidate: dict[str, Any],
) -> ScoreResult:
    text = _candidate_text(candidate)
    score = 0
    reasons: list[ScoreReason] = []

    def add(delta: int, label: str, field: str | None = None, evidence: str | None = None) -> None:
        nonlocal score
        score += delta
        reasons.append(ScoreReason(label=label, delta=delta, field=field, evidence=evidence))

    explicit_manufacturer = bool(parsed_query.manufacturer_terms)
    manufacturer_matched = False
    if explicit_manufacturer:
        for term in parsed_query.manufacturer_terms:
            if _contains_term(text, term):
                add(1000, f"manufacturer match: {term}", "manufacturer", term)
                manufacturer_matched = True
                break
        if not manufacturer_matched and any(_contains_term(text, alias) for alias in parsed_query.manufacturer_aliases):
            alias = next(alias for alias in parsed_query.manufacturer_aliases if _contains_term(text, alias))
            add(700, f"parent/sub-brand match: {alias}", "manufacturer", alias)
            manufacturer_matched = True
        if not manufacturer_matched:
            add(-600, "manufacturer mismatch", "manufacturer", candidate.get("company_name"))

    concept_matches = _candidate_concept_matches(text)
    for concept in parsed_query.detected_concepts:
        if concept in concept_matches:
            add(350, f"device concept match: {concept_display_name(concept)}", "device_concept", concept)
        elif concept in ("LAPAROSCOPIC_TROCAR_ACCESS", "DRAIN_TROCAR_OR_SPIKE"):
            add(-250, f"unrelated concept: missing {concept_display_name(concept)}", "device_concept", concept)

    if "LAPAROSCOPIC_TROCAR_ACCESS" in parsed_query.detected_concepts:
        access_markers = ("xcel", "versaport", "trocar", "cannula", "sleeve", "access", "port", "obturator")
        if any(_contains_term(text, fam) for fam in access_markers):
            marker = next(fam for fam in access_markers if _contains_term(text, fam))
            add(250, f"product family/access match: {marker}", "product_family", marker)
        if _contains_term(text, "endopath") and any(_contains_term(text, fam) for fam in access_markers):
            add(100, "ENDOPATH access-family boost", "product_family", "endopath")
        stapler_markers = ("echelon", "stapler", "cutter", "reload", "cartridge")
        if any(_contains_term(text, term) for term in stapler_markers) and not any(
            _contains_term(text, term) for term in access_markers
        ):
            add(-400, "stapler/reload result not preferred for trocar/access query", "device_concept", "stapler")
        if _contains_term(text, "blake") and not any(term in parsed_query.device_terms for term in ("blake", "drain")):
            add(-200, "BLAKE/drain result not preferred for laparoscopic trocar query", "device_concept", "blake")

    if "DRAIN_TROCAR_OR_SPIKE" in parsed_query.detected_concepts:
        if any(_contains_term(text, fam) for fam in ("blake", "drain", "suction", "spike")):
            marker = next(fam for fam in ("blake", "drain", "suction", "spike") if _contains_term(text, fam))
            add(250, f"drain/spike family match: {marker}", "product_family", marker)
        if any(_contains_term(text, term) for term in ("laparoscopic", "access", "cannula", "sleeve")):
            add(-200, "laparoscopic access result not preferred for drain/spike query", "device_concept", "access")

    for size in parsed_query.size_terms:
        compact = size.replace(" ", "")
        if compact in text.replace(" ", ""):
            add(150, f"size match: {compact}", "size", compact)
            break

    for term in parsed_query.device_terms:
        if len(term) >= 2 and _contains_term(text, term):
            add(100 if _looks_catalogish(term) else 50, f"device/catalog term match: {term}", "device_term", term)

    problem_score = 0
    for term in parsed_query.problem_terms:
        if _contains_term(text, term):
            problem_score += 10
    if problem_score:
        add(min(problem_score, 10), "limited problem-term match", "problem", ", ".join(parsed_query.problem_terms))

    identity_terms = parsed_query.manufacturer_terms + parsed_query.device_terms + parsed_query.concept_terms + parsed_query.size_terms
    identity_hit = any(_contains_term(text, term) for term in identity_terms if term not in PROBLEM_SINGLE_WORDS)
    problem_hit = any(_contains_term(text, term) for term in parsed_query.problem_terms)
    if problem_hit and not identity_hit:
        add(-300, "problem-only match penalized", "problem", ", ".join(parsed_query.problem_terms))

    if _contains_term(text, "air") and "air leak" in parsed_query.problem_terms and not identity_hit:
        add(-300, "AIR matched leak problem, not device identity", "problem", "air leak")

    confidence = _score_confidence(score, parsed_query, concept_matches, manufacturer_matched, identity_hit)
    return ScoreResult(score=score, confidence=confidence, reasons=reasons)


def candidate_sort_key(parsed_query: ParsedMedicalDeviceQuery, candidate: dict[str, Any]) -> tuple[int, str]:
    return (-score_device_candidate(parsed_query, candidate), _candidate_text(candidate))


def concept_display_name(concept: str) -> str:
    return CONCEPT_DISPLAY.get(concept, concept.replace("_", " ").lower())


def _candidate_text(candidate: dict[str, Any]) -> str:
    values = [
        candidate.get("company_name"),
        candidate.get("brand_name"),
        candidate.get("catalog_number"),
        candidate.get("model_number"),
        candidate.get("device_name"),
        candidate.get("document_title"),
        candidate.get("metadata"),
        candidate.get("source_text"),
        " ".join(candidate.get("gmdn_terms") or []) if isinstance(candidate.get("gmdn_terms"), list) else None,
        " ".join(candidate.get("product_codes") or []) if isinstance(candidate.get("product_codes"), list) else None,
    ]
    return " ".join(str(v).lower() for v in values if v)


def problem_categories_for_terms(problem_terms: list[str]) -> list[str]:
    categories: list[str] = []
    for category, terms in PROBLEM_CATEGORIES.items():
        if any(term in problem_terms for term in terms):
            categories.append(category)
    if "air leak" in problem_terms and "LEAK_OR_SEAL_PROBLEM" not in categories:
        categories.append("LEAK_OR_SEAL_PROBLEM")
    if "troubleshooting" in problem_terms and "GENERAL_SAFETY_OR_LABELING" not in categories:
        categories.append("GENERAL_SAFETY_OR_LABELING")
    return categories


def _score_confidence(
    score: int,
    parsed_query: ParsedMedicalDeviceQuery,
    concept_matches: set[str],
    manufacturer_matched: bool,
    identity_hit: bool,
) -> float:
    confidence = min(0.99, max(0.05, score / 1800))
    if parsed_query.manufacturer_terms and not manufacturer_matched:
        confidence *= 0.55
    if parsed_query.detected_concepts and not any(c in concept_matches for c in parsed_query.detected_concepts):
        confidence *= 0.7
    if not identity_hit:
        confidence *= 0.65
    return round(max(0.05, min(0.99, confidence)), 2)


def _candidate_concept_matches(text: str) -> set[str]:
    matches = set()
    for concept, terms in DEVICE_CONCEPTS.items():
        if any(_contains_term(text, term) for term in terms):
            matches.add(concept)
    return matches


def _contains_term(text: str, term: str) -> bool:
    term = term.lower().strip()
    if not term:
        return False
    if " " in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def _looks_catalogish(term: str) -> bool:
    return bool(re.search(r"\d", term) and re.search(r"[a-z]", term, re.IGNORECASE))


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for value in values:
        clean = value.strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result
