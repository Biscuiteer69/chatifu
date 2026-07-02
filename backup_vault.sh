#!/usr/bin/env bash
# ChatIFU vault backup: SQLite (online .backup), Qdrant dir, IFU PDF cache.
# Runs daily at 06:30 (after the 00:00-06:00 nightly scrape window) with
# 7-day rotation. Intentionally does NOT source .env (stray lines there can
# abort set -e scripts); paths are read individually with safe defaults.
set -euo pipefail

VAULT_DIR="${CHATIFU_VAULT_DIR:-/home/biscuited/projects/chatifu_vault}"
BACKUP_ROOT="${CHATIFU_BACKUP_ROOT:-/home/biscuited/backups/chatifu}"
KEEP_DAYS=7

env_get() { # env_get VAR_NAME -> value from .env, or empty
    grep -m1 "^${1}=" "${VAULT_DIR}/.env" 2>/dev/null | cut -d= -f2- || true
}

SQLITE_PATH="$(env_get CHATIFU_SQLITE_PATH)"
SQLITE_PATH="${SQLITE_PATH:-${VAULT_DIR}/chatifu.sqlite3}"
QDRANT_PATH="$(env_get CHATIFU_QDRANT_PATH)"
QDRANT_PATH="${QDRANT_PATH:-${VAULT_DIR}/qdrant}"
IFU_CACHE_DIR="$(env_get CHATIFU_IFU_CACHE_DIR)"
IFU_CACHE_DIR="${IFU_CACHE_DIR:-/home/biscuited/.biscuited/hermes/DGX/cache/ifu_docs}"

STAMP="$(date +%F)"
DEST="${BACKUP_ROOT}/${STAMP}"
mkdir -p "${DEST}"

echo "[backup] $(date -Is) start -> ${DEST}"

# 1. SQLite online backup (safe against concurrent readers/writers)
sqlite3 "${SQLITE_PATH}" ".backup '${DEST}/chatifu.sqlite3'"
echo "[backup] sqlite done ($(du -h "${DEST}/chatifu.sqlite3" | cut -f1))"

# 2. Qdrant storage (file-based store; only safe while ingestion is idle,
#    which the 06:30 slot guarantees). --delete keeps the copy exact.
if [ -d "${QDRANT_PATH}" ]; then
    rsync -a --delete "${QDRANT_PATH}/" "${DEST}/qdrant/"
    echo "[backup] qdrant done ($(du -sh "${DEST}/qdrant" | cut -f1))"
fi

# 3. IFU PDF cache (source documents served to the frontend)
if [ -d "${IFU_CACHE_DIR}" ]; then
    rsync -a --delete "${IFU_CACHE_DIR}/" "${DEST}/ifu_docs/"
    echo "[backup] ifu cache done ($(du -sh "${DEST}/ifu_docs" | cut -f1))"
fi

# 4. Integrity check: the backup must open and count devices
COUNT="$(sqlite3 "file:${DEST}/chatifu.sqlite3?mode=ro" "select count(*) from devices;")"
if [ "${COUNT}" -lt 1 ]; then
    echo "[backup] ERROR: backup sqlite has ${COUNT} devices" >&2
    exit 1
fi
echo "[backup] verify ok: ${COUNT} devices"

# 5. Rotate: drop date-stamped dirs older than KEEP_DAYS
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime +"${KEEP_DAYS}" -exec rm -rf {} +
echo "[backup] $(date -Is) done"
