#!/bin/sh
set -eu
: "${GATEWAY_API_KEY:?Définir la clé personnelle gw_...}"
: "${GATEWAY_MODEL:?Choisir un modèle retourné par /v1/models}"
base_url=${GATEWAY_BASE_URL:-http://127.0.0.1:8787/v1}
curl --fail-with-body --silent --show-error "$base_url/models" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
curl --fail-with-body --no-buffer "$base_url/chat/completions" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$GATEWAY_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Bonjour !\"}],\"stream\":true}"
