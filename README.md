# ChatIFU DGX Local Vault

This package moves ChatIFU away from paid Supabase vector storage and into a local DGX vault:

- Qdrant local file-backed vector collection at `/home/biscuited/projects/chatifu_vault/qdrant`
- SQLite device metadata at `/home/biscuited/projects/chatifu_vault/chatifu.sqlite3`
- Ollama `nomic-embed-text` embeddings with 768 dimensions
- Nightly scraper runner for midnight-6am America/Denver

## One-Time Setup

```bash
cd /home/biscuited/projects/chatifu_vault
/home/biscuited/projects/chatifu_production/venv/bin/python -m pip install -r requirements.txt
/home/biscuited/projects/chatifu_production/venv/bin/python vault_status.py
```

## Serve The Vault API

The live website should call a narrow API instead of reading SQLite/Qdrant directly.

Minimum DGX environment:

```bash
export CHATIFU_API_TOKEN="replace-with-a-long-random-token"
export CHATIFU_ALLOWED_ORIGINS="https://chatifu.com,https://www.chatifu.com"
export CHATIFU_VAULT_DIR="/home/biscuited/projects/chatifu_vault"
export CHATIFU_QDRANT_PATH="/home/biscuited/projects/chatifu_vault/qdrant"
export CHATIFU_SQLITE_PATH="/home/biscuited/projects/chatifu_vault/chatifu.sqlite3"
export CHATIFU_OLLAMA_EMBED_URL="http://127.0.0.1:11434/api/embeddings"
export CHATIFU_DOC_GENERATE_URL="http://127.0.0.1:11434/api/generate"
```

Start locally on the DGX:

```bash
cd /home/biscuited/projects/chatifu_vault
/home/biscuited/projects/chatifu_production/venv/bin/uvicorn api:app --host 127.0.0.1 --port 8123
```

Production user service on the DGX:

```bash
systemctl --user status chatifu-vault-api.service --no-pager
systemctl --user restart chatifu-vault-api.service
journalctl --user -u chatifu-vault-api.service -n 100 --no-pager
```

Useful checks:

```bash
curl http://127.0.0.1:8123/healthz
curl -H "Authorization: Bearer $CHATIFU_API_TOKEN" http://127.0.0.1:8123/readyz
curl -H "Authorization: Bearer $CHATIFU_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"Stryker 17-0186 instructions for use","limit":3}' \
  http://127.0.0.1:8123/query
```

For `chatifu.com`, keep the API behind HTTPS and pass only authenticated requests through to the DGX service. A simple production shape is:

- `https://chatifu.com` serves the beta UI.
- `https://chatifu.com/api/*` proxies to `127.0.0.1:8123` on the DGX or to a private tunnel that reaches it. For example, map `/api/healthz` to the backend's local `/healthz`.
- The frontend stores no service-role or database credentials.
- The backend requires `Authorization: Bearer ...` or `x-api-key`.
- Backend `/healthz` can be public for uptime monitoring through `/api/healthz`; `/api/readyz`, `/api/stats`, `/api/query`, and `/api/ask` stay protected.

## Seed Targets From FDA AccessGUDID

The production queue can be rebuilt without Supabase from the FDA AccessGUDID full release:

```bash
cd /home/biscuited/projects/chatifu_vault
/home/biscuited/projects/chatifu_production/venv/bin/python import_accessgudid.py \
  --source /path/to/AccessGUDID_Delimited_Full_Release_20260302.zip \
  --target jnj \
  --target stryker
```

The active DGX queue was seeded from the local FDA backup with J&J-family and Stryker targets.

## Export From Supabase

Fast path with the existing service key in `/home/biscuited/projects/chatifu_production/.env`:

```bash
cd /home/biscuited/projects/chatifu_vault
/home/biscuited/projects/chatifu_production/venv/bin/python export_supabase.py --tables devices documents --page-size 1000
/home/biscuited/projects/chatifu_production/venv/bin/python ingest_export.py
/home/biscuited/projects/chatifu_production/venv/bin/python vault_status.py
```

Most complete path from the Supabase dashboard:

1. Open Supabase project settings.
2. Go to `Database` then `Connection string`.
3. Copy the pooled or direct URI and replace the password placeholder with the database password.
4. From a machine with `pg_dump`, run:

```bash
pg_dump "$SUPABASE_DB_URL" --data-only --table public.devices --table public.documents --file chatifu_supabase_data.sql
```

That SQL dump is the safest full-fidelity archive. The JSONL exporter above is easier today because we already have the service key available on the DGX.

If Supabase is restored but unhealthy due to database-size quota, keep ChatIFU running from the local FDA queue. Upgrade only if we need the exact old Supabase `documents` rows/vectors instead of rebuilding them locally.

## Nightly Run

```bash
cd /home/biscuited/projects/chatifu_vault
/home/biscuited/projects/chatifu_production/venv/bin/python nightly_chatifu.py --target jnj --loop
```

The production schedule starts this command at midnight. It keeps running J&J batches until the 6am Mountain time cutoff and writes logs to `/home/biscuited/projects/chatifu_vault/logs`.

For a one-off daytime smoke test, add `--force` and leave off `--loop`:

```bash
/home/biscuited/projects/chatifu_production/venv/bin/python nightly_chatifu.py --force --target jnj --limit 5
```

J&J is the active target. Stryker stays available because its API path already worked:

```bash
/home/biscuited/projects/chatifu_production/venv/bin/python scrape_local.py --list-targets
/home/biscuited/projects/chatifu_production/venv/bin/python nightly_chatifu.py --target stryker --loop
```

## Query The Local Vault

```bash
cd /home/biscuited/projects/chatifu_vault
/home/biscuited/projects/chatifu_production/venv/bin/python query_vault.py "Stryker 17-0186 instructions for use" --limit 3
```

## Beta Readiness Checklist

- DGX SSH or tunnel reachability is stable and documented.
- `vault_status.py` reports nonzero devices and vector chunks.
- `/readyz` returns `ready` with healthy Ollama embeddings.
- `https://chatifu.com` shows branded ChatIFU beta UI, not the default Streamlit shell.
- The live frontend points to the API URL through HTTPS, not direct database credentials.
- Supabase export/import counts are recorded before switching traffic.
- Nightly scraper logs are monitored and alert on nonzero errors.
- Beta copy clearly tells users to verify any answer against the official IFU/source document.
