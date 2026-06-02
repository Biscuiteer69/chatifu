from __future__ import annotations

import argparse
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import fitz
import requests
from playwright.sync_api import sync_playwright

from company_targets import TOP_DEVICE_TARGETS, implemented_targets, target_by_key
from vault import DocumentChunk, device_sku, mark_sku, pending_devices, pending_devices_any, upsert_chunks


OLLAMA_URL = os.environ.get("CHATIFU_OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embeddings")
EMBED_MODEL = os.environ.get("CHATIFU_EMBED_MODEL", "nomic-embed-text")
DOC_MODEL = os.environ.get("CHATIFU_DOC_MODEL", "qwen3:14b")
DOC_GENERATE_URL = os.environ.get("CHATIFU_DOC_GENERATE_URL", "http://127.0.0.1:11434/api/generate")


def chunks(text: str, chunk_size: int = 1024, overlap: int = 128) -> list[str]:
    output: list[str] = []
    start = 0
    while start < len(text):
        output.append(text[start : start + chunk_size])
        start += chunk_size - overlap
    return [part for part in output if part.strip()]


def embed(text: str) -> list[float]:
    res = requests.post(OLLAMA_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=60)
    res.raise_for_status()
    vector = res.json().get("embedding")
    if not isinstance(vector, list):
        raise RuntimeError("Ollama embedding response did not include an embedding list.")
    return [float(x) for x in vector]


def pdf_text(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def ingest_pdf(sku: str, pdf_bytes: bytes, source: str) -> int:
    text = pdf_text(pdf_bytes)
    doc_chunks: list[DocumentChunk] = []
    for index, chunk in enumerate(chunks(text)):
        doc_chunks.append(
            DocumentChunk(
                content=chunk,
                embedding=embed(chunk),
                metadata={"source": source, "chunk_index": index, "sku": sku},
            )
        )
    inserted = upsert_chunks(doc_chunks)
    mark_sku(sku, "ingested", source)
    return inserted


def target_devices(target_key: str, limit: int) -> list[dict[str, Any]]:
    target = target_by_key(target_key)
    patterns = [str(pattern) for pattern in target.get("company_patterns", [])]
    return pending_devices_any(patterns, limit)


def list_targets() -> None:
    for target in TOP_DEVICE_TARGETS:
        marker = "ready" if target["adapter"] != "planned" else "planned"
        print(f"{target['rank']:>2}. {target['key']:<18} {marker:<7} {target['name']}")


def ask_doc_for_selectors(page_name: str, html: str) -> dict[str, str]:
    prompt = f"""
You are Doc, the ChatIFU scraper strategist. A medical-device IFU portal changed its markup.
Return strict JSON only with CSS selectors that may help automate this page.
Required keys: hcp_selector, continue_selector, acknowledge_selector, pdf_selector.
If a selector is unknown, use an empty string.

Page: {page_name}
HTML snippet:
{html[:9000]}
"""
    try:
        res = requests.post(
            DOC_GENERATE_URL,
            json={"model": DOC_MODEL, "prompt": prompt, "stream": False},
            timeout=90,
        )
        res.raise_for_status()
        response = str(res.json().get("response", ""))
        match = re.search(r"\{.*\}", response, flags=re.DOTALL)
        if not match:
            return {}
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            return {}
        return {str(key): str(value) for key, value in parsed.items()}
    except Exception as exc:
        print(f"[doc] selector advice unavailable for {page_name}: {exc}")
        return {}


def click_first(page: Any, selectors: list[str], timeout: int = 5000) -> bool:
    for selector in selectors:
        if not selector:
            continue
        try:
            page.locator(selector).first.click(force=True, timeout=timeout)
            return True
        except Exception:
            continue
    return False


def jnj_fetch_pdf_bytes(page: Any, href: str) -> tuple[bytes | None, str | None]:
    url = urljoin("https://www.e-ifu.com", href)
    response = page.context.request.get(url, timeout=60000)
    body = response.body()
    content_type = response.headers.get("content-type", "")
    if body.startswith(b"%PDF") or "application/pdf" in content_type.lower():
        return body, url

    text = body.decode("utf-8", errors="replace")
    textarea = re.search(r"<textarea[^>]*>(.*?)</textarea>", text, flags=re.DOTALL | re.IGNORECASE)
    modal_json = textarea.group(1) if textarea else text
    try:
        commands = json.loads(modal_json)
    except json.JSONDecodeError:
        commands = []

    for command in commands if isinstance(commands, list) else []:
        data = command.get("data") if isinstance(command, dict) else None
        if not isinstance(data, str):
            continue
        iframe_match = re.search(r'src="([^"]*fetchPdf[^"]*)"', data)
        if not iframe_match:
            continue
        iframe_src = iframe_match.group(1)
        parsed = urlparse(urljoin("https://www.e-ifu.com", iframe_src))
        file_values = parse_qs(parsed.query).get("file")
        fetch_path = unquote(file_values[0]) if file_values else iframe_src
        fetch_url = urljoin("https://www.e-ifu.com", fetch_path)
        pdf_response = page.context.request.get(fetch_url, timeout=60000)
        pdf_body = pdf_response.body()
        if pdf_body.startswith(b"%PDF") or "application/pdf" in pdf_response.headers.get("content-type", "").lower():
            return pdf_body, fetch_url
    return None, None


STRYKER_HEADERS = {
    "accept": "*/*",
    "accept-language": "en",
    "origin": "https://labeling.stryker.com",
    "referer": "https://labeling.stryker.com/",
    "user-agent": "Mozilla/5.0",
}


def scrape_stryker(limit: int, delay: float) -> dict[str, int]:
    devices = target_devices("stryker", limit)
    print(f"[stryker] pending devices: {len(devices)}")
    stats = {"checked": 0, "ingested": 0, "missed": 0, "errors": 0}
    if not devices:
        return stats

    bu_res = requests.get("https://api-public.qarad.eifu.online/api/v1/business-units", headers=STRYKER_HEADERS, timeout=30)
    bu_res.raise_for_status()
    bu_map = {item["slug"]: item["id"] for item in bu_res.json().get("items", [])}

    for device in devices:
        sku = device_sku(device)
        if not sku:
            continue
        stats["checked"] += 1
        print(f"[stryker] {stats['checked']}/{len(devices)} {sku}")
        try:
            payload = {"attributes": [{"name": "cross-field-search", "value": sku}], "country": "US"}
            search_res = requests.post(
                "https://api-public.qarad.eifu.online/api/v1/business-units/0/product-types/1/products",
                params={"audience": "HCP", "page": 0, "size": 5},
                headers=STRYKER_HEADERS,
                json=payload,
                timeout=30,
            )
            search_res.raise_for_status()
            items = search_res.json().get("items", [])
            if not items:
                print(f"[stryker] miss {sku}")
                mark_sku(sku, "no_eifu_found", "stryker_api")
                stats["missed"] += 1
                continue

            item = items[0]
            bu_id = bu_map.get(item.get("businessUnit"))
            if not bu_id:
                mark_sku(sku, "no_business_unit", "stryker_api")
                stats["missed"] += 1
                continue

            pt_res = requests.get(
                f"https://api-public.qarad.eifu.online/api/v1/business-units/{bu_id}/product-types",
                headers=STRYKER_HEADERS,
                timeout=30,
            )
            pt_res.raise_for_status()
            pt_map = {pt["slug"]: pt["id"] for pt in pt_res.json().get("items", [])}
            pt_id = pt_map.get(item.get("productType"))
            if not pt_id:
                mark_sku(sku, "no_product_type", "stryker_api")
                stats["missed"] += 1
                continue

            doc_res = requests.get(
                f"https://api-public.qarad.eifu.online/api/v1/business-units/{bu_id}/product-types/{pt_id}/products/{item.get('id')}?audience=HCP",
                headers=STRYKER_HEADERS,
                timeout=30,
            )
            doc_res.raise_for_status()
            product_data = doc_res.json()
            pdf_url = None
            for group in product_data.get("documentTypes", []):
                if "instructions for use" not in str(group.get("name", "")).lower() and "ifu" not in str(group.get("name", "")).lower():
                    continue
                for doc in group.get("documents", []):
                    for file_info in doc.get("files", []):
                        if file_info.get("documentUrl"):
                            pdf_url = file_info["documentUrl"]
                            break
                    if pdf_url:
                        break
                if pdf_url:
                    break
            if not pdf_url:
                mark_sku(sku, "no_pdf", "stryker_api")
                stats["missed"] += 1
                continue

            pdf_res = requests.get(pdf_url, headers=STRYKER_HEADERS, timeout=60)
            pdf_res.raise_for_status()
            inserted = ingest_pdf(sku, pdf_res.content, f"stryker:{sku}.pdf")
            print(f"[stryker] ingested {sku}: {inserted} chunks")
            stats["ingested"] += 1
        except Exception as exc:
            print(f"[stryker] error {sku}: {exc}")
            stats["errors"] += 1
        time.sleep(delay)
    return stats


def scrape_jnj(limit: int, delay: float, headless: bool) -> dict[str, int]:
    devices = target_devices("jnj", limit)
    print(f"[jnj] pending devices: {len(devices)}")
    stats = {"checked": 0, "found": 0, "missed": 0, "errors": 0}
    if not devices:
        return stats

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            page.goto("https://www.e-ifu.com/welcome")
            if not click_first(page, ["label[for='edit-site-user-hcp']", "input[value='hcp']"], timeout=8000):
                selectors = ask_doc_for_selectors("J&J welcome", page.content())
                click_first(page, [selectors.get("hcp_selector", "")], timeout=8000)
            if not click_first(page, ["#edit-submit", "input[value='Continue']"], timeout=8000):
                selectors = ask_doc_for_selectors("J&J continue", page.content())
                click_first(page, [selectors.get("continue_selector", "")], timeout=8000)
            page.wait_for_load_state("networkidle", timeout=15000)
            try:
                if not click_first(page, ["label[for='edit-acknowledge']", "input[name='acknowledge']"], timeout=5000):
                    selectors = ask_doc_for_selectors("J&J acknowledgement", page.content())
                    click_first(page, [selectors.get("acknowledge_selector", "")], timeout=5000)
                click_first(page, ["#edit-submit", "input[value='Continue']"], timeout=5000)
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            for device in devices:
                sku = device_sku(device)
                if not sku:
                    continue
                stats["checked"] += 1
                print(f"[jnj] {stats['checked']}/{len(devices)} {sku}")
                try:
                    page.goto(f"https://www.e-ifu.com/search-document-metadata/{sku}")
                    page.wait_for_load_state("networkidle", timeout=15000)
                    if "0 documents" in page.title() or "Sorry, no result found" in page.content():
                        mark_sku(sku, "no_eifu_found", "jnj_eifu")
                        stats["missed"] += 1
                        continue
                    links = page.query_selector_all("a[href*='.pdf'], a[href*='/viewpdf-iframe/'], a.save[href*='/user-confirm/']")
                    if not links:
                        selectors = ask_doc_for_selectors("J&J search results", page.content())
                        pdf_selector = selectors.get("pdf_selector")
                        if pdf_selector:
                            links = page.query_selector_all(pdf_selector)
                    if not links:
                        mark_sku(sku, "result_no_pdf_link", "jnj_eifu")
                        stats["missed"] += 1
                        continue
                    pdf_url = links[0].get_attribute("href")
                    if not pdf_url:
                        mark_sku(sku, "result_no_pdf_url", "jnj_eifu")
                        stats["missed"] += 1
                        continue
                    pdf_bytes, resolved_url = jnj_fetch_pdf_bytes(page, pdf_url)
                    if not pdf_bytes:
                        mark_sku(sku, "pdf_fetch_failed", "jnj_eifu")
                        stats["missed"] += 1
                        continue
                    inserted = ingest_pdf(sku, pdf_bytes, f"jnj:{sku}.pdf")
                    print(f"[jnj] ingested {sku}: {inserted} chunks")
                    stats["found"] += 1
                except Exception as exc:
                    print(f"[jnj] error {sku}: {exc}")
                    stats["errors"] += 1
                time.sleep(delay)
        finally:
            browser.close()
    return stats


def scrape_target(target: str, limit: int, delay: float, headless: bool) -> dict[str, int]:
    adapter = str(target_by_key(target).get("adapter"))
    if adapter == "jnj":
        return scrape_jnj(limit, delay, headless=headless)
    if adapter == "stryker":
        return scrape_stryker(limit, delay)
    print(f"[{target}] no adapter implemented yet")
    return {"checked": 0, "ingested": 0, "missed": 0, "errors": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape IFUs into the local ChatIFU DGX vault.")
    parser.add_argument("--scraper", choices=["stryker", "jnj", "all"], default="jnj")
    parser.add_argument("--target", choices=implemented_targets(), help="Run a named company target.")
    parser.add_argument("--limit", type=int, default=50, help="Per-target batch size when using --target.")
    parser.add_argument("--stryker-limit", type=int, default=100)
    parser.add_argument("--jnj-limit", type=int, default=50)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--list-targets", action="store_true")
    args = parser.parse_args()

    if args.list_targets:
        list_targets()
        return

    summary: dict[str, Any] = {}
    if args.target:
        summary[args.target] = scrape_target(args.target, args.limit, args.delay, headless=not args.headed)
    elif args.scraper in {"stryker", "all"}:
        summary["stryker"] = scrape_stryker(args.stryker_limit, args.delay)
    if not args.target and args.scraper in {"jnj", "all"}:
        summary["jnj"] = scrape_jnj(args.jnj_limit, args.delay, headless=not args.headed)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
