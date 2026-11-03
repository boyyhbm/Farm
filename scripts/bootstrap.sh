#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v docker >/dev/null; then
  echo 'Docker Engine with Compose v2 is required.' >&2
  exit 1
fi
docker compose version >/dev/null
if [[ ! -f .env ]]; then
  umask 077
  python3 - <<'PY'
from pathlib import Path
import secrets
password = secrets.token_urlsafe(24)
values = {
    'POSTGRES_PASSWORD': secrets.token_hex(32), 'ADMIN_USERNAME': 'admin',
    'ADMIN_PASSWORD': password,
    'PUBLIC_ORIGIN': 'https://localhost', 'SITE_ADDRESS': 'localhost',
    'TLS_EMAIL': 'admin@localhost', 'SESSION_SECURE': 'true', 'ENVIRONMENT': 'production',
    'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_CHAT_ID': '', 'TELEGRAM_MIN_SEVERITY': 'warning',
    'RETENTION_DAYS': '30',
    'SESSION_HOURS': '12', 'NODE_OFFLINE_SECONDS': '45',
}
Path('.env').write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
Path('.env').chmod(0o600)
print('Created protected .env. Initial administrator: admin; password is stored in .env.')
PY
fi
docker compose config --quiet
docker compose up --build --detach --wait --wait-timeout 240
printf '\nDashboard: https://localhost (or configured SITE_ADDRESS).\nFor localhost, trust the local Caddy CA as described in docs/deployment.md.\n'
