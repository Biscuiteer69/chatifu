from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanyResolverConfig:
    canonical_name: str
    domains: tuple[str, ...]
    search_paths: tuple[str, ...]
    pdf_link_keywords: tuple[str, ...]
    ifu_link_keywords: tuple[str, ...]
    deny_keywords: tuple[str, ...]
    prefer_pdf: bool = True


COMMON_PDF_KEYWORDS = ("pdf", ".pdf", "fetchpdf")
COMMON_IFU_KEYWORDS = (
    "ifu", "eifu", "e-ifu", "instructions for use", "directions for use",
    "instructions", "manual", "user manual", "operator manual", "package insert",
    "labeling",
)
COMMON_DENY_KEYWORDS = (
    "marketing", "careers", "news", "privacy", "terms", "cookie", "investor",
    "login", "sign in", "press release", "brochure",
)


COMPANY_RESOLVER_CONFIGS: tuple[CompanyResolverConfig, ...] = (
    CompanyResolverConfig("Johnson & Johnson MedTech", ("e-ifu.com", "jnjmedtech.com", "ethicon.com", "depuysynthes.com"), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Medtronic", ("medtronic.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Stryker", ("stryker.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Abbott", ("cardiovascular.abbott", "abbott.com", "manuals.eifu.abbott"), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Boston Scientific", ("bostonscientific.com", "bsci.com", "ifu.bostonscientific.com"), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("BD", ("bd.com", "bardaccess.com"), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Olympus", ("olympusamerica.com", "olympus-europa.com", "olympusmedical.com"), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Cook Medical", ("cookmedical.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Teleflex", ("teleflex.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Intuitive Surgical", ("intuitive.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Smith+Nephew", ("smith-nephew.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Zimmer Biomet", ("zimmerbiomet.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("B. Braun", ("bbraun.com", "aesculapusa.com"), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Baxter", ("baxter.com", "hillrom.com", "welchallyn.com"), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
    CompanyResolverConfig("Steris", ("steris.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS),
)


def config_for_company_name(company_name: str | None) -> CompanyResolverConfig | None:
    low = (company_name or "").lower()
    for config in COMPANY_RESOLVER_CONFIGS:
        if config.canonical_name.lower() in low or any(domain.split(".")[0] in low for domain in config.domains):
            return config
    return None
