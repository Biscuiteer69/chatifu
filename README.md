# ChatIFU DGX Local Vault

This package moves ChatIFU away from paid Supabase vector storage and into a local DGX vault:

- Qdrant local file-backed vector collection at `/home/biscuited/projects/chatifu_vault/qdrant`
- SQLite device metadata at `/home/biscuited/projects/chatifu_vault/chatifu.sqlite3`
- Ollama `nomic-embed-text` embeddings with 768 dimensions
- Nightly scraper runner for midnight-6am America/Denver

## One-Time Setup

```bash
cd /home/biscuited/projects/chatifu_vault
/home/biscuited/projects/chatifu_production/venv/bin/python vault_status.py
```

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
