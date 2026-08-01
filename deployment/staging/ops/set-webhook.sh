#!/usr/bin/env bash
set -euo pipefail
env_file="${1:-/opt/creator-assistant-staging/shared/.env.staging}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Never delete a working webhook.  setWebhook is idempotent and preserves the
# queued updates because webhook_readiness.py sets drop_pending_updates=false.
exec "${PYTHON_BIN:-python3}" "$script_dir/webhook_readiness.py" \
  --env-file "$env_file" --phase post --repair --force-set --clear-stale-error
