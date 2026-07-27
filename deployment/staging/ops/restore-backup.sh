#!/bin/sh
set -eu
: "${BACKUP_FILE:?BACKUP_FILE is required}"
: "${BACKUP_ENCRYPTION_KEY:?BACKUP_ENCRYPTION_KEY is required}"
if [ "${CONFIRM_RESTORE:-}" != "RESTORE-${PGDATABASE}" ]; then
  echo "Refusing restore. Set CONFIRM_RESTORE=RESTORE-${PGDATABASE}" >&2
  exit 64
fi
test -f "$BACKUP_FILE"
sha256sum -c "${BACKUP_FILE}.sha256"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
openssl enc -d -aes-256-cbc -pbkdf2 -in "$BACKUP_FILE" -out "$tmp" -pass env:BACKUP_ENCRYPTION_KEY
pg_restore --list "$tmp" >/dev/null
pg_restore --clean --if-exists --no-owner --no-acl --dbname="$PGDATABASE" "$tmp"
