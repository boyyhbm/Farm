#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 2 || "$2" != '--replace-database' || ! -f "$1" ]]; then
  echo 'Usage: ./scripts/restore.sh backups/farm-TIMESTAMP.dump --replace-database' >&2
  echo 'Stops application services and replaces stored data; take a backup first.' >&2
  exit 2
fi
backup_file="$1"
docker compose exec -T postgres pg_restore --list < "$backup_file" >/dev/null
docker compose stop caddy dashboard scheduler worker inventory api
docker compose exec -T postgres pg_restore -U farm -d farm --clean --if-exists --no-owner --single-transaction < "$backup_file"
docker compose up -d --wait
