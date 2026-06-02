from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanyEntry:
    canonical_name: str
    aliases: tuple[str, ...]
    subsidiaries_or_brands: tuple[str, ...] = ()
    ifu_domains: tuple[str, ...] = ()
    resolver_hint: str = "generic_pdf"


COMPANY_REGISTRY: tuple[CompanyEntry, ...] = (
    CompanyEntry(
        canonical_name="Johnson & Johnson MedTech",
        aliases=("johnson & johnson", "johnson and johnson", "j&j", "jnj", "jjmd", "j j", "j&j medtech"),
        subsidiaries_or_brands=("ethicon", "depuy synthes", "depuy syntheses", "biosense webster", "abiomed", "mentor"),
        ifu_domains=("e-ifu.com", "jnjmedtech.com", "ethicon.com", "depuysynthes.com"),
        resolver_hint="jnj_eifu",
    ),
    CompanyEntry("Medtronic", ("medtronic", "covidien", "valleylab", "superdimension", "minimed"), ifu_domains=("medtronic.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Stryker", ("stryker", "stryker corporation"), ifu_domains=("stryker.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Abbott", ("abbott", "abbott medical", "abbott vascular", "st jude medical", "st. jude medical"), ifu_domains=("cardiovascular.abbott", "abbott.com", "manuals.eifu.abbott"), resolver_hint="generic_company_pdf"),
    CompanyEntry("Boston Scientific", ("boston scientific", "bsc"), ifu_domains=("bostonscientific.com", "bsci.com", "ifu.bostonscientific.com"), resolver_hint="generic_company_pdf"),
    CompanyEntry("BD", ("bd", "becton dickinson", "becton dickinson and company", "bard", "cr bard", "c. r. bard"), ifu_domains=("bd.com", "bardaccess.com"), resolver_hint="generic_company_pdf"),
    CompanyEntry("GE HealthCare", ("ge healthcare", "ge medical systems"), ifu_domains=("gehealthcare.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Siemens Healthineers", ("siemens healthineers", "siemens medical"), ifu_domains=("siemens-healthineers.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Philips", ("philips", "philips healthcare", "philips medical systems"), ifu_domains=("philips.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Cardinal Health", ("cardinal health", "cordis"), ifu_domains=("cardinalhealth.com", "cordis.com"), resolver_hint="generic_company_pdf"),
    CompanyEntry("B. Braun", ("b braun", "b. braun", "bbraun", "aesculap"), ifu_domains=("bbraun.com", "aesculapusa.com"), resolver_hint="generic_company_pdf"),
    CompanyEntry("Baxter", ("baxter", "hillrom", "hill-rom", "welch allyn"), ifu_domains=("baxter.com", "hillrom.com", "welchallyn.com"), resolver_hint="generic_company_pdf"),
    CompanyEntry("Alcon", ("alcon",), ifu_domains=("alcon.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Intuitive Surgical", ("intuitive", "intuitive surgical", "da vinci"), ifu_domains=("intuitive.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Zimmer Biomet", ("zimmer biomet", "zimmer", "biomet"), ifu_domains=("zimmerbiomet.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Olympus", ("olympus", "olympus medical"), ifu_domains=("olympusamerica.com", "olympus-europa.com", "olympusmedical.com"), resolver_hint="generic_company_pdf"),
    CompanyEntry("Terumo", ("terumo", "terumo medical"), ifu_domains=("terumomedical.com", "terumo.com"), resolver_hint="generic_company_pdf"),
    CompanyEntry("Smith+Nephew", ("smith nephew", "smith & nephew", "smith+nephew", "smith and nephew"), ifu_domains=("smith-nephew.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Edwards Lifesciences", ("edwards", "edwards lifesciences"), ifu_domains=("edwards.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("ResMed", ("resmed",), ifu_domains=("resmed.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Fresenius Medical Care", ("fresenius", "fresenius medical care"), ifu_domains=("freseniusmedicalcare.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Cook Medical", ("cook", "cook medical"), ifu_domains=("cookmedical.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Teleflex", ("teleflex", "arrow", "lma", "weck"), ifu_domains=("teleflex.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Steris", ("steris", "steris endoscopy"), ifu_domains=("steris.com",), resolver_hint="generic_company_pdf"),
    CompanyEntry("Fujifilm", ("fujifilm", "fujifilm healthcare", "fujinon"), ifu_domains=("fujifilm.com",), resolver_hint="generic_company_pdf"),
)


def iter_company_terms(entry: CompanyEntry) -> tuple[str, ...]:
    return (entry.canonical_name.lower(), *entry.aliases, *entry.subsidiaries_or_brands)


def match_company_terms(text: str) -> list[CompanyEntry]:
    low = f" {text.lower()} "
    matches: list[CompanyEntry] = []
    for entry in COMPANY_REGISTRY:
        if any(f" {term.lower()} " in low for term in iter_company_terms(entry)):
            matches.append(entry)
    return matches
