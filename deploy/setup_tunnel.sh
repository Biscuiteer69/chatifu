#!/usr/bin/env bash
# ChatIFU beta deploy — run AFTER `cloudflared tunnel login` has succeeded
# (that writes ~/.cloudflared/cert.pem for the account that owns chatifu.com).
#
# Exposes, via one Cloudflare Tunnel from the DGX:
#   api.chatifu.com  -> http://127.0.0.1:8123  (vault API)
#   chatifu.com/www  -> http://127.0.0.1:8080  (static frontend)
set -euo pipefail

CF="$HOME/.local/bin/cloudflared"
TUNNEL="chatifu-dgx"
CFG_DIR="$HOME/.cloudflared"
ENV_FILE="/home/biscuited/projects/chatifu_vault/.env"
UNIT_DIR="$HOME/.config/systemd/user"

[ -f "$CFG_DIR/cert.pem" ] || { echo "ERROR: run '$CF tunnel login' first (no cert.pem)"; exit 1; }

# 1. Create the tunnel (skip if it already exists) and capture its UUID.
if ! "$CF" tunnel list 2>/dev/null | grep -q " $TUNNEL "; then
  "$CF" tunnel create "$TUNNEL"
fi
UUID="$("$CF" tunnel list | awk -v n="$TUNNEL" '$2==n{print $1}')"
echo "tunnel UUID: $UUID"

# 2. Ingress config.
cat > "$CFG_DIR/config.yml" <<YML
tunnel: $UUID
credentials-file: $CFG_DIR/$UUID.json
ingress:
  - hostname: api.chatifu.com
    service: http://127.0.0.1:8123
  - hostname: chatifu.com
    service: http://127.0.0.1:8080
  - hostname: www.chatifu.com
    service: http://127.0.0.1:8080
  - service: http_status:404
YML
echo "wrote $CFG_DIR/config.yml"

# 3. DNS. api.* is new (safe). The apex + www are the CUTOVER from Render —
#    pass --overwrite explicitly to replace the existing records.
OVERWRITE="${OVERWRITE:-0}"
route() { "$CF" tunnel route dns ${2:+--overwrite} "$TUNNEL" "$1" || true; }
"$CF" tunnel route dns "$TUNNEL" api.chatifu.com || true
if [ "$OVERWRITE" = "1" ]; then
  echo ">> CUTOVER: repointing chatifu.com + www to the tunnel (retires Render)"
  "$CF" tunnel route dns --overwrite "$TUNNEL" chatifu.com || true
  "$CF" tunnel route dns --overwrite "$TUNNEL" www.chatifu.com || true
else
  echo ">> Skipping apex/www cutover (re-run with OVERWRITE=1 to repoint chatifu.com)."
fi

# 4. CORS: frontend origin must be allowed by the API.
if grep -q '^CHATIFU_ALLOWED_ORIGINS=' "$ENV_FILE"; then
  sed -i 's#^CHATIFU_ALLOWED_ORIGINS=.*#CHATIFU_ALLOWED_ORIGINS=https://chatifu.com,https://www.chatifu.com#' "$ENV_FILE"
else
  echo 'CHATIFU_ALLOWED_ORIGINS=https://chatifu.com,https://www.chatifu.com' >> "$ENV_FILE"
fi

# 5. Services: frontend static server + cloudflared, as user units.
cp /home/biscuited/projects/chatifu_vault/deploy/chatifu-frontend.service "$UNIT_DIR/"
"$CF" service install 2>/dev/null || true   # may no-op for user-mode; fallback below
cat > "$UNIT_DIR/cloudflared-chatifu.service" <<UNIT
[Unit]
Description=cloudflared tunnel (ChatIFU)
After=network.target
[Service]
ExecStart=$CF --no-autoupdate --config $CFG_DIR/config.yml tunnel run $TUNNEL
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now chatifu-frontend.service
systemctl --user enable --now cloudflared-chatifu.service
systemctl --user restart chatifu-vault-api.service

echo "---"
echo "done. verify:  curl -s https://api.chatifu.com/healthz"
echo "frontend:      https://chatifu.com  (after DNS propagates)"
