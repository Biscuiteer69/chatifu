"""Stryker's "Placeholder Document" stub (lorem ipsum) must never become a served IFU."""
from __future__ import annotations

from resolvers.stryker_resolver import StrykerResolver, is_placeholder_document


def test_placeholder_names_are_recognised():
    assert is_placeholder_document("Placeholder Document")
    assert is_placeholder_document("IFU placeholder")
    assert not is_placeholder_document("Triathlon Total Knee System IFU")
    assert not is_placeholder_document(None)


def _file(name: str) -> dict:
    return {"name": name, "documentUrl": f"https://s3/stryker/{name}.pdf?X-Amz-Expires=21600",
            "languages": [{"isoCode": "en"}], "latestVersion": True, "version": "2"}


def test_placeholder_document_is_dropped_and_real_ifu_kept(tmp_path):
    resolver = StrykerResolver(db_path=tmp_path / "t.sqlite3")
    resolver.business_units = lambda: {"joint": 1}  # type: ignore[method-assign]
    resolver.product_types = lambda bu: {"knee": 2}  # type: ignore[method-assign]
    resolver._request = lambda url: {  # type: ignore[method-assign]
        "name": "Triathlon",
        "documentTypes": [{"name": "Instructions for Use", "documents": [
            {"name": "Placeholder Document", "files": [_file("IFU 1 B V2")]},
            {"name": "Triathlon IFU", "files": [_file("90-01951_AB_IFU_EN")]},
        ]}],
    }
    docs = resolver.ifu_documents({"id": 7, "businessUnit": "joint", "productType": "knee"})
    assert [d["document_title"] for d in docs] == ["Triathlon IFU"]


def test_only_a_placeholder_means_not_found(tmp_path):
    resolver = StrykerResolver(db_path=tmp_path / "t.sqlite3")
    resolver.business_units = lambda: {"joint": 1}  # type: ignore[method-assign]
    resolver.product_types = lambda bu: {"knee": 2}  # type: ignore[method-assign]
    resolver._request = lambda url: {  # type: ignore[method-assign]
        "name": "Stub",
        "documentTypes": [{"name": "Instructions for Use", "documents": [
            {"name": "Placeholder Document", "files": [_file("IFU 1 B V2")]}]}],
    }
    assert resolver.ifu_documents({"id": 7, "businessUnit": "joint", "productType": "knee"}) == []
