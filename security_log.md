# Nightly Security Sweep - Sunday, March 15th, 2026

## 1. Dependency Vulnerabilities
- `frontend`: `npm audit` found **0 vulnerabilities**.
- `backend`: No locked dependencies flag known vulnerabilities. 

## 2. Dependency Updates
**Frontend (NPM):**
- `@types/node` (20.19.37 -> 25.5.0)
- `eslint` (9.39.4 -> 10.0.3)
- `react` (19.2.3 -> 19.2.4)
- `react-dom` (19.2.3 -> 19.2.4)

**Frontend (Python):**
- `pandas` (2.3.3 -> 3.0.1)
- `protobuf` (6.33.5 -> 7.34.0)

**Backend (Python):**
- `cachetools` (6.2.6 -> 7.0.5)
- `google-ai-generativelanguage` (0.6.15 -> 0.10.0)
- `google-api-core` (2.25.2 -> 2.30.0)
- `grpcio-status` (1.71.2 -> 1.78.0)
- `protobuf` (5.29.6 -> 7.34.0)
- `pydantic_core` (2.41.5 -> 2.42.0)
- `websockets` (15.0.1 -> 16.0)

## 3. Credential Exposure
- Scanned workspace and `.git` history for leaked secrets.
- `backend/.env` exists but is correctly untracked (`.gitignore` functioning as expected). 
- No exposed credentials found in tracked files.

## 4. Server Access Logs
- Checked recent logins via `last`. Only expected user (`abefroman`) and system reboots/shutdowns.
- No `~/.ssh/authorized_keys` file found.

**Conclusion:** Sweep clean. No critical vulnerabilities or exposures detected.
