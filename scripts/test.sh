#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python - <<'PY'
import os, subprocess
from pathlib import Path
import psycopg
values=dict(line.split('=',1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
password=values['POSTGRES_PASSWORD']
with psycopg.connect(host='127.0.0.1',user='farm',password=password,dbname='farm',autocommit=True) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname='farm_test'").fetchone(): conn.execute('CREATE DATABASE farm_test')
env={**os.environ,'PYTHONPATH':'apps/api:packages/gameplay-test-engine:apps/windows-agent','DATABASE_URL':f'postgresql+psycopg://farm:{password}@127.0.0.1:5432/farm_test','REDIS_URL':'redis://127.0.0.1:6379/14','PUBLIC_ORIGIN':'http://testserver','SESSION_SECURE':'false','ADMIN_USERNAME':'testadmin','ADMIN_PASSWORD':'test-password-123456'}
# Clear only isolated test Redis DB14 so repeated suites do not inherit rate buckets.
from redis import Redis
Redis.from_url(env['REDIS_URL']).flushdb()
raise SystemExit(subprocess.call(['.venv/bin/python','-m','pytest','tests/unit','tests/integration','-q'],env=env))
PY
