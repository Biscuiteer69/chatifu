# Security Rules & Protocols

## Core Medical Liability Mitigation
1. **NO MEDICAL ADVICE**: The MVP must NOT provide medical advice, clinical interpretation, or treatment recommendations.
2. **NO PARAPHRASING**: The LLM must not paraphrase safety-critical language. Provide verbatim excerpts only.
3. **NO UNSOURCED CLAIMS**: Never produce an answer without a strict citation (Document, Page, Section).
4. **SAFETY FALLBACK**: If confidence is low or no relevant section is found, return: *"I was unable to confidently identify a relevant section in the indexed IFUs. Please refine your question."*

## Infrastructure Security
- **Credentials**: Never expose or store sensitive credentials in code. Use `.env` files locally and secure environment variables in production.
- **Secrets in Logs**: Never print secrets or API keys to any log file or console.
- **Nightly Audit**: A cron job runs at 2:00 AM MT to scan for dependency vulnerabilities, credential exposure, and suspicious access logs. Results are recorded in `security_log.md`.
- **Self-Protection**: Any corrupted files, exposed credentials, or suspicious behavior will be logged and the operator (Sean) will be immediately alerted.
