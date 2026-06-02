from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from medical_device_vocab import ParsedMedicalDeviceQuery


OPENFDA_UDI_URL = "https://api.fda.gov/device/udi.json"
ACCESSGUDID_LOOKUP_V3 = "https://accessgudid.nlm.nih.gov/api/v3/devices/lookup.json"
ACCESSGUDID_LOOKUP_V2 = "https://accessgudid.nlm.nih.gov/api/v2/devices/lookup.json"
DEFAULT_GUDID_CACHE_PATH = Path("/home/biscuited/.biscuited/hermes/DGX/cache/chatifu_gudid_cache.sqlite3")


@dataclass
class GUDIDDevice:
    source: str
    record_key: str | None
    device_identifier: str | None
    company_name: str | None
    brand_name: str | None
    catalog_number: str | None
    version_or_model_number: str | None
    device_description: str | None
    gmdn_terms: list[str]
    product_codes: list[str]
    premarket_submissions: list[str]
    raw: dict[str, Any]

    def as_candidate(self) -> dict[str, Any]:
        return {
            "company_name": self.company_name,
            "brand_name": self.brand_name,
            "catalog_number": self.catalog_number or self.device_identifier or self.record_key,
            "model_number": self.version_or_model_number,
            "device_name": self.device_description,
            "gmdn_terms": self.gmdn_terms,
            "product_codes": self.product_codes,
            "source": self.source,
            "source_text": self.device_description,
            "is_gudid_identity_only": True,
        }


class GUDIDCache:
    def __init__(self, path: str | Path | None = None, ttl_days: int | None = None) -> None:
        env_path = os.environ.get("CHATIFU_GUDID_CACHE_PATH")
        env_ttl = os.environ.get("CHATIFU_GUDID_CACHE_TTL_DAYS")
        self.path = Path(path or env_path or DEFAULT_GUDID_CACHE_PATH)
        self.ttl_seconds = int(ttl_days if ttl_days is not None else (env_ttl or 30)) * 86400
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def get(self, key: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute("select payload, fetched_at from gudid_cache where cache_key=?", (key,)).fetchone()
            if not row:
                return None
            if self.ttl_seconds > 0 and time.time() - float(row[1]) > self.ttl_seconds:
                return None
            return json.loads(row[0])
        finally:
            conn.close()

    def put(self, key: str, payload: dict[str, Any]) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """
                insert into gudid_cache(cache_key, payload, fetched_at)
                values (?, ?, ?)
                on conflict(cache_key) do update set
                  payload=excluded.payload,
                  fetched_at=excluded.fetched_at
                """,
                (key, json.dumps(payload), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def stats(self) -> dict[str, Any]:
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute("select count(*), min(fetched_at), max(fetched_at) from gudid_cache").fetchone()
            return {
                "path": str(self.path),
                "entries": int(row[0] or 0),
                "oldest": row[1],
                "newest": row[2],
            }
        finally:
            conn.close()

    def _init(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """
                create table if not exists gudid_cache (
                  cache_key text primary key,
                  payload text not null,
                  fetched_at real not null
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


class GUDIDClient:
    def __init__(
        self,
        opener: Any | None = None,
        cache: GUDIDCache | None = None,
        timeout: int = 10,
    ) -> None:
        self.opener = opener or urllib.request.build_opener()
        self.cache = cache if cache is not None else GUDIDCache()
        self.timeout = timeout

    def search_openfda_udi(self, parsed_query: ParsedMedicalDeviceQuery, limit: int = 25) -> list[GUDIDDevice]:
        query = self._openfda_query(parsed_query)
        if not query:
            return []
        params = {"search": query, "limit": str(limit)}
        try:
            payload = self._get_json(OPENFDA_UDI_URL, params)
        except Exception:
            return []
        return [_normalize_openfda_device(item) for item in payload.get("results", [])]

    def lookup_accessgudid(
        self,
        *,
        di: str | None = None,
        udi: str | None = None,
        record_key: str | None = None,
    ) -> GUDIDDevice | None:
        params = {k: v for k, v in {"di": di, "udi": udi, "record_key": record_key}.items() if v}
        if not params:
            return None
        for base_url in (ACCESSGUDID_LOOKUP_V3, ACCESSGUDID_LOOKUP_V2):
            try:
                payload = self._get_json(base_url, params)
                return _normalize_accessgudid_device(payload.get("gudid", {}).get("device") or payload)
            except Exception:
                continue
        return None

    def _openfda_query(self, parsed_query: ParsedMedicalDeviceQuery) -> str:
        parts: list[str] = []
        for term in parsed_query.manufacturer_terms[:2]:
            parts.append(f'company_name:"{_escape_query(term)}"')
        identity_terms = [
            *parsed_query.device_terms[:4],
            *[term for term in parsed_query.search_terms if term not in parsed_query.problem_terms][:8],
        ]
        for term in identity_terms:
            if len(term) >= 2:
                escaped = _escape_query(term)
                parts.append(
                    f'(brand_name:"{escaped}"+catalog_number:"{escaped}"+'
                    f'version_or_model_number:"{escaped}"+device_description:"{escaped}"+gmdn_terms.name:"{escaped}")'
                )
        return "+AND+".join(parts[:10])

    def _get_json(self, base_url: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        req = urllib.request.Request(url, headers={"User-Agent": "ChatIFU/1.0", "Accept": "application/json"})
        with self.opener.open(req, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.cache.put(key, payload)
        return payload


def _normalize_openfda_device(raw: dict[str, Any]) -> GUDIDDevice:
    return GUDIDDevice(
        source="openfda_udi",
        record_key=_first(raw.get("record_key")),
        device_identifier=_first(raw.get("identifiers", [{}])[0].get("id") if raw.get("identifiers") else raw.get("device_identifier")),
        company_name=_first(raw.get("company_name")),
        brand_name=_first(raw.get("brand_name")),
        catalog_number=_first(raw.get("catalog_number")),
        version_or_model_number=_first(raw.get("version_or_model_number")),
        device_description=_first(raw.get("device_description")),
        gmdn_terms=_gmdn_terms(raw),
        product_codes=_list_values(raw.get("product_codes") or raw.get("product_code")),
        premarket_submissions=_list_values(raw.get("premarket_submissions") or raw.get("premarket_submission")),
        raw=raw,
    )


def _normalize_accessgudid_device(raw: dict[str, Any]) -> GUDIDDevice:
    gmdn = raw.get("gmdnTerms") or raw.get("gmdn_terms") or []
    return GUDIDDevice(
        source="accessgudid",
        record_key=_first(raw.get("publicDeviceRecordKey") or raw.get("record_key")),
        device_identifier=_first(raw.get("deviceIdentifier") or raw.get("di")),
        company_name=_first(raw.get("companyName") or raw.get("company_name")),
        brand_name=_first(raw.get("brandName") or raw.get("brand_name")),
        catalog_number=_first(raw.get("catalogNumber") or raw.get("catalog_number")),
        version_or_model_number=_first(raw.get("versionModelNumber") or raw.get("version_or_model_number")),
        device_description=_first(raw.get("deviceDescription") or raw.get("device_description")),
        gmdn_terms=[str(item.get("gmdnPTName") or item.get("name") or item) for item in gmdn],
        product_codes=_list_values(raw.get("productCodes") or raw.get("product_codes")),
        premarket_submissions=_list_values(raw.get("premarketSubmissions") or raw.get("premarket_submissions")),
        raw=raw,
    )


def _gmdn_terms(raw: dict[str, Any]) -> list[str]:
    terms = raw.get("gmdn_terms") or []
    result: list[str] = []
    for item in terms:
        if isinstance(item, dict):
            value = item.get("name") or item.get("gmdnPTName")
        else:
            value = item
        if value:
            result.append(str(value))
    return result


def _list_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                result.extend(str(v) for v in item.values() if v)
            elif item:
                result.append(str(item))
        return result
    return [str(value)]


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value not in (None, "") else None


def _escape_query(value: str) -> str:
    return value.replace('"', '\\"')
