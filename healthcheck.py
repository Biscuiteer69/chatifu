"""ChatIFU health-check — pings every moving part and fires a Telegram alarm on
any failure, so a silent outage during beta doesn't go unnoticed.

Checks: local API / frontend / qdrant, the public path through the Cloudflare
tunnel (api.chatifu.com + chatifu.com), and that all five systemd user services
are active. Run from cron every ~30 min. Exits non-zero if anything is down.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib import error, parse

CF_EDGE = "104.21.4.12"  # any Cloudflare anycast IP for the zone
FUMBL_ENV = Path("/home/biscuited/projects/fumbl_dgx_scratch/.env")
SERVICES = (
    "chatifu-vault-api", "chatifu-frontend", "chatifu-qdrant",
    "cloudflared-chatifu", "chatifu-scraper-fleet",
)


def _env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val:
        return val
    try:
        for line in FUMBL_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return default


def telegram_alert(text: str) -> None:
    token = _env("TELEGRAM_TOKEN")
    chat = _env("TELEGRAM_CHAT_ID", "8572138697")
    if not token:
        return
    try:
        data = parse.urlencode({"chat_id": chat, "text": text,
                                "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=20,
        )
    except Exception:
        pass


def check_http(name: str, url: str, expect_ok_json: bool = False) -> tuple[str, bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            code = resp.status
            body = resp.read(500).decode("utf-8", "replace")
        if code != 200:
            return name, False, f"HTTP {code}"
        if expect_ok_json and '"ok"' not in body and '"status": "ok"' not in body and '"status":"ok"' not in body:
            return name, False, f"unexpected body: {body[:80]}"
        return name, True, "200"
    except error.HTTPError as exc:
        return name, False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return name, False, str(exc)[:80]


def check_public(name: str, host: str, path: str) -> tuple[str, bool, str]:
    """Public path through Cloudflare, resolving the host to the edge IP (this
    box's own resolver can't see chatifu.com, but the tunnel path is what users
    hit)."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "20",
             "--resolve", f"{host}:443:{CF_EDGE}", f"https://{host}{path}"],
            capture_output=True, text=True, timeout=30,
        )
        code = out.stdout.strip()
        return name, code == "200", f"HTTP {code or '000'}"
    except Exception as exc:  # noqa: BLE001
        return name, False, str(exc)[:80]


def check_service(svc: str) -> tuple[str, bool, str]:
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", f"{svc}.service"],
                             capture_output=True, text=True, timeout=10)
        state = out.stdout.strip()
        return f"svc:{svc}", state == "active", state or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"svc:{svc}", False, str(exc)[:80]


def main() -> int:
    results: list[tuple[str, bool, str]] = [
        check_http("api_local", "http://127.0.0.1:8123/healthz", expect_ok_json=True),
        check_http("frontend_local", "http://127.0.0.1:8080/"),
        check_http("qdrant_local", "http://127.0.0.1:6333/readyz"),
        check_public("api_public", "api.chatifu.com", "/healthz"),
        check_public("site_public", "chatifu.com", "/"),
        *[check_service(s) for s in SERVICES],
    ]
    failures = [(n, d) for n, ok, d in results if not ok]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    for name, ok, detail in results:
        print(f"{'OK ' if ok else 'FAIL'} {name}: {detail}")

    if failures:
        lines = "\n".join(f"• {n}: {d}" for n, d in failures)
        telegram_alert(f"🚨 ChatIFU health-check FAILED ({stamp})\n{lines}")
        return 1
    print(f"all healthy @ {stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
