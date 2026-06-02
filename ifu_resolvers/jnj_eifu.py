from __future__ import annotations

from pathlib import Path
from typing import Any

from medical_device_vocab import ParsedMedicalDeviceQuery
from mvp_lookup import SQLITE_PATH, get_best_ifu_url
from resolvers.eifu_resolver import EifuResolver

from .base import ResolvedIFU


class JnjEifuResolver:
    """Adapter around the existing e-ifu.com resolver."""

    def __init__(self, db_path: str | Path = SQLITE_PATH) -> None:
        self.db_path = Path(db_path)

    def can_handle(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> bool:
        text = " ".join(
            str(candidate.get(key) or "").lower()
            for key in ("company_name", "brand_name", "source_url", "document_url")
        )
        return any(
            term in text
            for term in (
                "ethicon",
                "johnson",
                "j&j",
                "jnj",
                "depuy",
                "biosense webster",
                "e-ifu.com",
            )
        )

    def resolve(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> ResolvedIFU | None:
        catalog = str(candidate.get("catalog_number") or candidate.get("catalog") or "").strip()
        if not catalog:
            return None

        document_url = get_best_ifu_url(catalog, db_path=self.db_path)
        if not document_url:
            EifuResolver(db_path=self.db_path).resolve(catalog)
            document_url = get_best_ifu_url(catalog, db_path=self.db_path)
        if not document_url:
            return None

        return ResolvedIFU(
            manufacturer=candidate.get("company_name"),
            catalog=catalog,
            device_name=candidate.get("brand_name") or candidate.get("device_name"),
            document_url=document_url,
            pdf_url=None,
            iframe_url=None,
            open_full_ifu_url=document_url,
            source_url=document_url,
            source_type="jnj_eifu",
            confidence=0.9,
        )
