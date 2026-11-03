"""Development helper: use .env secrets without exposing them in command output."""
import os, sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'apps/api'))
for line in (root/'.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        key,value=line.split('=',1)
        os.environ.setdefault(key,value)
os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://farm:'+os.environ['POSTGRES_PASSWORD']+'@127.0.0.1:5432/farm')
os.environ.setdefault('REDIS_URL','redis://127.0.0.1:6379/0')
os.environ['DATABASE_URL']=os.environ['DATABASE_URL'].replace('@postgres:', '@127.0.0.1:').replace('@db:', '@127.0.0.1:')
os.environ['REDIS_URL']=os.environ['REDIS_URL'].replace('redis:6379','127.0.0.1:6379')
os.environ['SESSION_SECURE']='false'
os.environ['PUBLIC_ORIGIN']='http://localhost:3000'
if sys.argv[1:] and sys.argv[1]=='migrate':
    from alembic.config import Config
    from alembic import command
    os.chdir(root/'apps/api'); command.upgrade(Config('alembic.ini'),'head')
else:
    import uvicorn
    uvicorn.run('app.main:app',host='0.0.0.0',port=8000)
