from __future__ import annotations

import unittest

from ifu_resolvers.company_configs import CompanyResolverConfig, COMMON_DENY_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_PDF_KEYWORDS
from ifu_resolvers.generic_company_pdf import GenericCompanyPdfResolver
from ifu_resolvers.generic_pdf import GenericPdfResolver
from ifu_resolvers.registry import IFUResolverRegistry


class FakeHeaders(dict):
    def get(self, key: str, default: str = "") -> str:
        return super().get(key, default)


class FakeResponse:
    def __init__(self, body: bytes, url: str = "https://example.com/page", content_type: str = "text/html") -> None:
        self.body = body
        self.url = url
        self.headers = FakeHeaders({"Content-Type": content_type})

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self, *_args: object) -> bytes:
        return self.body

    def geturl(self) -> str:
        return self.url


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def open(self, req: object, timeout: int) -> FakeResponse:
        return self.response


class IFUResolverTests(unittest.TestCase):
    def test_generic_company_resolver_picks_ifu_pdf(self) -> None:
        html = b'''
        <a href="/privacy">Privacy</a>
        <a href="/docs/product-ifu.pdf">Instructions for Use PDF</a>
        '''
        config = CompanyResolverConfig(
            "ExampleCo", ("example.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS
        )
        resolver = GenericCompanyPdfResolver(config=config, opener=FakeOpener(FakeResponse(html, "https://example.com/products/device")))
        result = resolver.resolve({
            "company_name": "ExampleCo",
            "brand_name": "Trocar",
            "source_url": "https://example.com/products/device",
        })
        self.assertIsNotNone(result)
        self.assertEqual(result.document_url, "https://example.com/docs/product-ifu.pdf")

    def test_generic_company_resolver_rejects_marketing_only_page(self) -> None:
        html = b'<a href="/news/product">Marketing news</a>'
        config = CompanyResolverConfig(
            "ExampleCo", ("example.com",), (), COMMON_PDF_KEYWORDS, COMMON_IFU_KEYWORDS, COMMON_DENY_KEYWORDS
        )
        resolver = GenericCompanyPdfResolver(config=config, opener=FakeOpener(FakeResponse(html, "https://example.com/products/device")))
        self.assertIsNone(resolver.resolve({
            "company_name": "ExampleCo",
            "brand_name": "Trocar",
            "source_url": "https://example.com/products/device",
        }))

    def test_unknown_company_falls_back_to_generic_pdf(self) -> None:
        registry = IFUResolverRegistry()
        attempts = registry.resolver_attempts({
            "company_name": "UnknownCo",
            "brand_name": "Device",
            "document_url": "https://unknown.example/ifu.pdf",
        })
        self.assertEqual(attempts[-1]["resolver"], "GenericPdfResolver")

    def test_edwards_company_uses_edwards_resolver_before_generic(self) -> None:
        registry = IFUResolverRegistry()
        attempts = registry.resolver_attempts({
            "company_name": "Edwards Lifesciences LLC",
            "brand_name": "SAPIEN 3",
            "catalog_number": "9600TFX20",
        })
        self.assertEqual(attempts[0]["resolver"], "EdwardsEifuResolver")

    def test_generic_pdf_direct_pdf_resolves(self) -> None:
        resolver = GenericPdfResolver()
        result = resolver.resolve({"document_url": "https://example.com/ifu.pdf"})
        self.assertIsNotNone(result)
        self.assertEqual(result.pdf_url, "https://example.com/ifu.pdf")


if __name__ == "__main__":
    unittest.main()
