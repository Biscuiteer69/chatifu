from __future__ import annotations

from pathlib import Path
from typing import Any

from medical_device_vocab import ParsedMedicalDeviceQuery
from mvp_lookup import SQLITE_PATH

from .base import ResolvedIFU
from .company_configs import config_for_company_name
from .edwards_eifu import EdwardsEifuResolver
from .generic_company_pdf import GenericCompanyPdfResolver
from .generic_pdf import GenericPdfResolver
from .jnj_eifu import JnjEifuResolver


class IFUResolverRegistry:
    def __init__(self, db_path: str | Path = SQLITE_PATH) -> None:
        self.db_path = Path(db_path)

    def resolver_attempts(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
        resolve: bool = False,
    ) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for resolver in self._resolvers(candidate):
            name = resolver.__class__.__name__
            can_handle = False
            result: ResolvedIFU | None = None
            failure = None
            try:
                can_handle = resolver.can_handle(candidate, parsed_query)
                if can_handle and resolve:
                    result = resolver.resolve(candidate, parsed_query)
                    if result is None:
                        failure = getattr(resolver, "last_failure", "not resolved")
                elif can_handle:
                    failure = "not resolved in debug mode"
            except Exception as exc:
                failure = str(exc)
            attempts.append({
                "resolver": name,
                "can_handle": can_handle,
                "document_url": result.document_url if result else None,
                "pdf_url": result.pdf_url if result else None,
                "source_type": result.source_type if result else None,
                "confidence": result.confidence if result else None,
                "failure": failure,
            })
        return attempts

    def resolve(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> ResolvedIFU | None:
        for resolver in self._resolvers(candidate):
            try:
                if resolver.can_handle(candidate, parsed_query):
                    result = resolver.resolve(candidate, parsed_query)
                    if result:
                        return result
            except Exception:
                continue
        return None

    def _resolvers(self, candidate: dict[str, Any]) -> list[Any]:
        company = f"{candidate.get('company_name') or ''} {candidate.get('brand_name') or ''}".lower()
        resolvers: list[Any] = []
        if any(term in company for term in ("johnson", "ethicon", "depuy", "biosense", "abiomed")):
            resolvers.append(JnjEifuResolver(db_path=self.db_path))
        if "edwards lifesciences" in company:
            resolvers.append(EdwardsEifuResolver(db_path=self.db_path))
        if config_for_company_name(candidate.get("company_name")) is not None:
            resolvers.append(GenericCompanyPdfResolver())
        resolvers.append(GenericPdfResolver())
        return resolvers
