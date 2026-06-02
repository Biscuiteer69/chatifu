from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from medical_device_vocab import ParsedMedicalDeviceQuery


@dataclass
class ResolvedIFU:
    manufacturer: str | None
    catalog: str | None
    device_name: str | None
    document_url: str | None
    pdf_url: str | None
    iframe_url: str | None
    open_full_ifu_url: str | None
    source_url: str | None
    source_type: str
    confidence: float


class IFUResolver(Protocol):
    def can_handle(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> bool:
        ...

    def resolve(
        self,
        candidate: dict[str, Any],
        parsed_query: ParsedMedicalDeviceQuery | None = None,
    ) -> ResolvedIFU | None:
        ...
