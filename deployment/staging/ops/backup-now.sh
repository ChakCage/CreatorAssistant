#!/bin/sh
set -eu
: "${BACKUP_ENCRYPTION_KEY:?BACKUP_ENCRYPTION_KEY is required}"
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
plain="/backups/creator-assistant-${stamp}.dump"
encrypted="${plain}.aes256"
trap 'rm -f "$plain"' EXIT
pg_dump --format=custom --no-owner --no-acl --file="$plain"
pg_restore --list "$plain" >/dev/null
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$plain" -out="$encrypted" -pass env:BACKUP_ENCRYPTION_KEY
sha256sum "$encrypted" >"${encrypted}.sha256"
touch /backups/.last-success
find /backups -type f -name '*.aes256' -mtime "+${BACKUP_RETENTION_DAYS:-7}" -delete
find /backups -type f -name '*.aes256.sha256' -mtime "+${BACKUP_RETENTION_DAYS:-7}" -delete
printf '%s\n' "$encrypted"
