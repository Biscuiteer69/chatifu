"""ChatIFU scraper fleet — a 24/7 supervisor that runs one worker per
manufacturer target, each looping its resolver over the pending-device backlog.

Design goals:
  * Politeness first. Each manufacturer's resolver already rate-limits itself
    (e.g. Medtronic sleeps 1.5s/request). The fleet adds an inter-batch gap and
    exponential backoff on any WAF/error signal, so we never hammer a site.
  * Per-site isolation. One worker per target, each hitting a DIFFERENT host, so
    they run concurrently without affecting each other's rate limits. One
    target crashing or getting blocked never sinks the others.
  * Don't crash the box. A load-average ceiling pauses new batches if the DGX is
    busy; batches run one-at-a-time per target (never a fan-out storm). SQLite is
    WAL + busy-timeout, so concurrent writers wait rather than fail.
  * Add a manufacturer by adding one TARGETS entry (its batch command). No new
    plumbing.

Run:  .venv/bin/python scraper_fleet.py         (or via the systemd service)
Stop: SIGTERM/SIGINT -> drains cleanly.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VAULT = Path("/home/biscuited/projects/chatifu_vault")
PY = str(VAULT / ".venv" / "bin" / "python")
LOG_PATH = VAULT / "logs" / "scraper_fleet.log"
TZ = ZoneInfo("America/Denver")

# ---------------------------------------------------------------------------
# Per-manufacturer config. `cmd` runs exactly ONE batch and exits. The worker
# loops it. `batch_re` parses "how many devices this batch attempted" so we can
# tell when the backlog is dry (0) and idle instead of busy-looping.
# ---------------------------------------------------------------------------
TARGETS: dict[str, dict] = {
    "medtronic": {
        "enabled": True, "rank": 1,
        "cmd": [PY, "-m", "resolvers.medtronic_resolver", "--batch", "300"],
        "batch_re": re.compile(r"Resolving (\d+) Medtronic devices"),  # header count
        "sleep_between": 10,       # gap between batches (on top of the resolver's own per-request delay)
        "idle_sleep": 6 * 3600,    # backlog dry -> re-check every 6h (new GUDID devices, retries)
        "batch_timeout": 3600,     # kill a wedged batch after 1h
    },
    "abbott": {
        "enabled": True, "rank": 5,
        "cmd": [PY, "-m", "resolvers.abbott_resolver", "--batch", "40"],
        "batch_re": re.compile(r"Resolving (\d+) Abbott devices"),  # header count
        "sleep_between": 15, "idle_sleep": 12 * 3600, "batch_timeout": 2400,
    },
    "edwards": {
        "enabled": True, "rank": 13,
        "cmd": [PY, "-m", "resolvers.edwards_resolver", "--limit", "150"],
        "count_re": re.compile(r":\s*\d+\s*document"),  # one match per device attempted; 0 -> dry
        "sleep_between": 12,
        "idle_sleep": 12 * 3600,   # small maker (~1.7k devices); re-check twice a day
        "batch_timeout": 1800,
    },
    # --- Stryker & Zimmer: same Qarad/CloudFront backend. The WAF blocked on
    #     request FINGERPRINT (bot UA + missing sec-ch-ua/sec-fetch-*), not rate;
    #     a real Chrome header set (stryker_resolver.HEADERS) returns 200. Each
    #     device is ~6-11s (2-3 API calls at 2s+jitter), so keep batches modest. ---
    # MODERATE (2026-07-16, caching ON): PDF caching is enabled (CHATIFU_CACHE_PDFS=1),
    # so serving reads cached bytes and no longer depends on serve-time Qarad re-mint —
    # scraping and serving are decoupled. Still self-limiting on the Qarad WAF: a burst
    # then a gap, with a 4h max_backoff so a rate-ban self-heals (goes quiet -> lifts ->
    # resumes) instead of hammering. As the scraper resolves docs it caches their bytes,
    # so coverage becomes servable as it grows.
    #
    # OLD FAST CONFIG (banned the IP): batch 60, sleep_between 30, no max_backoff.
    # HARD-THROTTLE CONFIG (ban-lift, caching off): batch 8, sleep_between 3600.
    "stryker": {
        "enabled": True, "rank": 3,
        "cmd": [PY, "-m", "resolvers.stryker_resolver", "--batch", "25"],
        "batch_re": re.compile(r"Resolving (\d+) Stryker devices"),
        "sleep_between": 300, "idle_sleep": 12 * 3600, "batch_timeout": 2400,
        "max_backoff": 4 * 3600,   # a WAF hit backs off up to 4h so the ban self-heals
    },
    "zimmer_biomet": {
        "enabled": True, "rank": 15,
        "cmd": [PY, "-m", "resolvers.zimmer_resolver", "--batch", "25"],
        "batch_re": re.compile(r"Resolving (\d+) Zimmer Biomet devices"),
        "sleep_between": 300, "idle_sleep": 12 * 3600, "batch_timeout": 2400,
        "max_backoff": 4 * 3600,
    },
    # abbott_resolver has no --batch mode (single --catalog only); needs a batch wrapper first.
}

# WAF / rate-limit fingerprints in a batch's output -> back off hard.
# Must be UNAMBIGUOUS phrases: bare "403"/"429"/"blocked" false-match device
# model numbers and product names. The resolvers print "WAF blocked the client
# (HTTP 403)" on a real block, so match that shape instead.
WAF_SIGNS = ("waf blocked", "http 403", "http 429", "too many requests",
             "rate limit exceeded", "captcha required", "access denied")

STOP = threading.Event()
_LOG_LOCK = threading.Lock()
LOAD_CEILING = (os.cpu_count() or 8) * 0.85   # pause new batches above this 1-min load

# Auto-advance pipeline: run at most this many scrapers at once, highest revenue
# rank first. When a scraper's backlog goes dry it exits and frees a slot, and
# the next-highest-rank incomplete target is promoted — so we always keep the
# top companies working. A completed target is re-checked after DONE_RECHECK (new
# GUDID devices / roster refreshes).
MAX_ACTIVE = int(os.environ.get("CHATIFU_FLEET_MAX_ACTIVE", "6"))
DONE_RECHECK = 24 * 3600


def log(msg: str) -> None:
    line = f"[{datetime.now(TZ).isoformat(timespec='seconds')}] {msg}"
    with _LOG_LOCK:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line, flush=True)


def system_busy() -> bool:
    try:
        return os.getloadavg()[0] > LOAD_CEILING
    except OSError:
        return False


def run_target(key: str, cfg: dict) -> None:
    backoff = 30
    log(f"[{key}] worker started")
    while not STOP.is_set():
        if system_busy():
            log(f"[{key}] system load high, pausing 60s")
            STOP.wait(60)
            continue
        try:
            proc = subprocess.run(
                cfg["cmd"], cwd=str(VAULT),
                capture_output=True, text=True, timeout=cfg["batch_timeout"],
            )
        except subprocess.TimeoutExpired:
            log(f"[{key}] batch timed out (>{cfg['batch_timeout']}s); backoff {backoff}s")
            STOP.wait(backoff)
            backoff = min(backoff * 2, cfg.get("max_backoff", 1800))
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"[{key}] batch error: {exc}; backoff {backoff}s")
            STOP.wait(backoff)
            backoff = min(backoff * 2, cfg.get("max_backoff", 1800))
            continue

        # A shutdown SIGTERM kills the in-flight batch (returncode -15); that's
        # not a failure, just the fleet stopping. Don't flag/backoff on it.
        if STOP.is_set():
            return

        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        low = out.lower()
        waf = any(sign in low for sign in WAF_SIGNS)

        if proc.returncode != 0 or waf:
            reason = "WAF/rate-limit" if waf else f"exit={proc.returncode}"
            log(f"[{key}] batch flagged ({reason}); backoff {backoff}s. tail: {out.strip()[-200:]}")
            STOP.wait(backoff)
            backoff = min(backoff * 2, cfg.get("max_backoff", 1800))
            continue

        backoff = 30  # healthy batch -> reset backoff
        # Attempted-count: header style ("Resolving N devices") or per-line count.
        if cfg.get("batch_re"):
            m = cfg["batch_re"].search(out)
            attempted = int(m.group(1)) if m else None
        elif cfg.get("count_re"):
            attempted = len(cfg["count_re"].findall(out))
        else:
            attempted = None
        last = out.strip().splitlines()[-1] if out.strip() else "(no output)"

        if attempted == 0:
            log(f"[{key}] backlog dry — target complete; freeing pipeline slot")
            return  # supervisor promotes the next company; re-checks this one later

        log(f"[{key}] batch ok ({last})")
        STOP.wait(cfg["sleep_between"])

    log(f"[{key}] worker stopped")


def _handle_stop(*_a) -> None:
    log("stop signal received; draining workers")
    STOP.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Priority order = revenue rank (lower rank number first).
    enabled = sorted(
        (k for k, c in TARGETS.items() if c.get("enabled")),
        key=lambda k: TARGETS[k].get("rank", 999),
    )
    log(f"pipeline starting; MAX_ACTIVE={MAX_ACTIVE}, load ceiling {LOAD_CEILING:.1f}; "
        f"queue by rank: {enabled}")

    active: dict[str, threading.Thread] = {}
    done_at: dict[str, float] = {}   # target -> monotonic time it last went dry

    def promote() -> None:
        for key in enabled:                       # already rank-sorted
            if len(active) >= MAX_ACTIVE:
                break
            if key in active:
                continue
            if key in done_at and (time.monotonic() - done_at[key]) < DONE_RECHECK:
                continue                           # completed recently; re-check later
            t = threading.Thread(target=run_target, args=(key, TARGETS[key]), name=key, daemon=True)
            active[key] = t
            t.start()
            log(f"[{key}] promoted to active ({len(active)}/{MAX_ACTIVE} slots)")
            time.sleep(3)                          # stagger starts

    promote()
    while not STOP.is_set():
        for key, t in list(active.items()):
            if not t.is_alive():
                done_at[key] = time.monotonic()    # dry or crashed -> free the slot
                del active[key]
        if not STOP.is_set():
            promote()
        STOP.wait(15)

    STOP.set()
    for t in list(active.values()):
        t.join(timeout=10)
    log("fleet stopped")


if __name__ == "__main__":
    main()
