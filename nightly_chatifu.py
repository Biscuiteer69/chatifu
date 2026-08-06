from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time as sleep_time
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


VAULT_DIR = Path("/home/biscuited/projects/chatifu_vault")
LOG_DIR = VAULT_DIR / "logs"
TZ = ZoneInfo("America/Denver")


def in_window(now: datetime) -> bool:
    current = now.time()
    return time(0, 0) <= current < time(6, 0)


def run_step(name: str, cmd: list[str], log_path: Path, timeout: int) -> dict[str, object]:
    started = datetime.now(TZ).isoformat()
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{started}] START {name}: {' '.join(cmd)}\n")
        proc = subprocess.run(
            cmd,
            cwd=str(VAULT_DIR),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        finished = datetime.now(TZ).isoformat()
        log.write(f"[{finished}] END {name}: exit={proc.returncode}\n")
    return {"name": name, "exit_code": proc.returncode, "started": started, "finished": finished}


def scrape_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(VAULT_DIR / "scrape_local.py"),
        "--scraper",
        args.scraper,
        "--stryker-limit",
        str(args.stryker_limit),
        "--jnj-limit",
        str(args.jnj_limit),
        "--delay",
        str(args.delay),
    ]
    if args.target:
        cmd.extend(["--target", args.target, "--limit", str(args.limit)])
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(description="Nightly ChatIFU local-vault scraper runner.")
    parser.add_argument("--force", action="store_true", help="Run even outside midnight-6am.")
    parser.add_argument("--scraper", choices=["stryker", "jnj", "all"], default="jnj")
    parser.add_argument("--target", choices=["jnj", "stryker"], help="Named company target. Defaults to --scraper behavior.")
    parser.add_argument("--limit", type=int, default=50, help="Per-iteration batch size when using --target.")
    parser.add_argument("--stryker-limit", type=int, default=100)
    parser.add_argument("--jnj-limit", type=int, default=50)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--loop", action="store_true", help="Keep running batches until the 6am cutoff.")
    parser.add_argument("--loop-sleep", type=int, default=120, help="Seconds between loop batches.")
    parser.add_argument("--timeout", type=int, default=60 * 45)
    args = parser.parse_args()

    now = datetime.now(TZ)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"nightly_{now:%Y%m%d_%H%M%S}.log"
    if not args.force and not in_window(now):
        message = {
            "status": "skipped_outside_window",
            "now": now.isoformat(),
            "window": "00:00-06:00 America/Denver",
        }
        print(json.dumps(message, indent=2))
        return

    steps = []
    iteration = 0
    while True:
        iteration += 1
        steps.append(run_step(f"scrape_local_{iteration}", scrape_command(args), log_path, args.timeout))
        steps.append(run_step(f"vault_status_{iteration}", [sys.executable, str(VAULT_DIR / "vault_status.py")], log_path, 300))

        now = datetime.now(TZ)
        should_stop = any(step["exit_code"] != 0 for step in steps[-2:])
        if should_stop or not args.loop or (not args.force and not in_window(now)):
            break
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[{now.isoformat()}] sleeping {args.loop_sleep}s before next batch\n")
        sleep_time.sleep(args.loop_sleep)

    # Refresh the search index's derived columns after the night's scraping. has_ifu decides
    # whether a device is ranked as answerable, and it is computed from ifu_links, so without
    # this every device the fleet resolved overnight keeps being demoted in search as though
    # we still had nothing for it.
    steps.append(run_step(
        "search_index_refresh",
        [sys.executable, str(VAULT_DIR / "migrate_search_index.py")],
        log_path,
        5400,
    ))

    status = {
        "status": "ok" if all(step["exit_code"] == 0 for step in steps) else "error",
        "log": str(log_path),
        "loop": args.loop,
        "steps": steps,
    }
    (VAULT_DIR / "latest_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    raise SystemExit(0 if status["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
