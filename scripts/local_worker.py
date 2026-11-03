"""Run Celery worker or beat against development loopback PostgreSQL/Redis."""
import os,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'apps/api'))
for line in (root/'.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        key,value=line.split('=',1); os.environ.setdefault(key,value)
os.environ.setdefault('DATABASE_URL','postgresql+psycopg://farm:'+os.environ['POSTGRES_PASSWORD']+'@127.0.0.1:5432/farm')
os.environ.setdefault('REDIS_URL','redis://127.0.0.1:6379/0')
from app.workers.tasks import celery_app
celery_app.start(['beat','--loglevel=INFO','--schedule=/tmp/farm-local-beat'] if sys.argv[1:] and sys.argv[1]=='beat' else ['worker','--loglevel=INFO','--concurrency=2','--queues=celery,inventory'])
