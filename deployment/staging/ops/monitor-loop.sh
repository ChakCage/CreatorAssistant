#!/usr/bin/env bash
set -uo pipefail
notify() {
  printf '{"time":"%s","level":"error","check":"%s"}\n' "$(date -u +%FT%TZ)" "$1"
  if [[ -n "${ALERT_WEBHOOK_URL:-}" ]]; then
    curl --fail --silent --show-error --max-time 5 -H 'Content-Type: application/json' \
      --data "{\"text\":\"Creator Assistant staging check failed: $1\"}" "$ALERT_WEBHOOK_URL" >/dev/null || true
  fi
}
while true; do
  curl -fsS --max-time 5 "${API_URL}/ready" >/dev/null || notify api
  curl -fsS --max-time 5 "${BOT_URL}/ready" >/dev/null || notify bot
  pg_isready -q || notify postgres
  redis-cli -h redis -a "$REDIS_PASSWORD" --no-auth-warning ping 2>/dev/null | grep -q PONG || notify redis
  webhook="$(curl -fsS --max-time 8 "https://api.telegram.org/bot${CREATOR_BOT_TOKEN}/getWebhookInfo" 2>/dev/null)" || {
    notify telegram-webhook-api
    webhook=""
  }
  printf '%s' "$webhook" | grep -Fq "${PUBLIC_BOT_URL}/telegram/webhook" || notify telegram-webhook-url
  pending="$(psql -Atqc "select count(*) from bot_notifications where status in ('PENDING','PROCESSING')" 2>/dev/null || echo -1)"
  failed_notifications="$(psql -Atqc "select count(*) from bot_notifications where status='FAILED'" 2>/dev/null || echo -1)"
  failed_activations="$(psql -Atqc "select count(*) from license_events where event_type='ACTIVATE' and result<>'SUCCESS' and created_at > now() - interval '1 hour'" 2>/dev/null || echo -1)"
  refresh_errors="$(psql -Atqc "select count(*) from license_events where event_type='REFRESH' and result<>'SUCCESS' and created_at > now() - interval '1 hour'" 2>/dev/null || echo -1)"
  printf '{"time":"%s","level":"info","queue_depth":%s,"failed_notifications":%s,"failed_activations_1h":%s,"refresh_errors_1h":%s}\n' \
    "$(date -u +%FT%TZ)" "$pending" "$failed_notifications" "$failed_activations" "$refresh_errors"
  test -f /backups/.last-success || notify backup-never-completed
  if test -f /backups/.last-success && find /backups/.last-success -mmin +1500 -print -quit | grep -q .; then notify backup-stale; fi
  expiration="$(echo | openssl s_client -servername "$STAGING_API_DOMAIN" -connect "$STAGING_API_DOMAIN:443" 2>/dev/null |
    openssl x509 -noout -checkend 604800 2>/dev/null)" || notify certificate-expiring
  df -P / | awk 'NR==2 {gsub("%","",$5); if ($5 >= 85) exit 1}' || notify disk-usage
  sleep 60
done
