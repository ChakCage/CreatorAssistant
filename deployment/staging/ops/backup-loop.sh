#!/bin/sh
set -eu
while true; do
  /ops/backup-now.sh
  sleep 86400
done
