#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
backup_file="backups/farm-$(date -u +%Y%m%dT%H%M%SZ).dump"
temporary_file="${backup_file}.partial"
trap 'rm -f "$temporary_file"' EXIT
docker compose exec -T postgres pg_dump -U farm -d farm --format=custom > "$temporary_file"
test -s "$temporary_file"
mv "$temporary_file" "$backup_file"
printf '%s\n' "$backup_file"
