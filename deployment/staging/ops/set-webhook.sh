#!/usr/bin/env bash
set -euo pipefail
env_file="${1:-/opt/creator-assistant-staging/shared/.env.staging}"
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
: "${CREATOR_BOT_TOKEN:?CREATOR_BOT_TOKEN is required}"
: "${CREATOR_BOT_WEBHOOK_SECRET:?CREATOR_BOT_WEBHOOK_SECRET is required}"
: "${STAGING_BOT_DOMAIN:?STAGING_BOT_DOMAIN is required}"
api="https://api.telegram.org/bot${CREATOR_BOT_TOKEN}"
if [[ "${DELETE_OLD_WEBHOOK:-false}" == "true" ]]; then
  curl --fail --silent --show-error --request POST "${api}/deleteWebhook" \
    --data-urlencode "drop_pending_updates=false" >/dev/null
fi
curl --fail --silent --show-error --request POST "${api}/setWebhook" \
  --data-urlencode "url=https://${STAGING_BOT_DOMAIN}/telegram/webhook" \
  --data-urlencode "secret_token=${CREATOR_BOT_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]' >/dev/null
curl --fail --silent --show-error "${api}/getWebhookInfo" |
  sed -E 's#("url":[[:space:]]*"https://)[^/]+#\1<configured-host>#'
