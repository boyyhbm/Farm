"""Explicit Linux browser-test host. NEVER bundled; no real Steam requests or secrets."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'apps/api'),str(ROOT/'apps/windows-agent'),str(ROOT/'packages/gameplay-test-engine')]
from desktop_entry import configure_environment
root = Path(sys.argv[1]).resolve()
if not str(root).startswith('/tmp/farm-vault-browser-'):
    raise SystemExit('Only an isolated temporary E2E directory is allowed')
configure_environment(root, 'http://localhost:3000')
from app.services import vault
from app.services.vault_crypto import VaultStore
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
class ExplicitBrowserTestKey:
    key=b'\x36'*32
    def wrap(self, value):
        nonce=os.urandom(12);return nonce+AESGCM(self.key).encrypt(nonce,value,b'e2e-only')
    def unwrap(self, value):return AESGCM(self.key).decrypt(value[:12],value[12:],b'e2e-only')
vault._store=VaultStore(root,protector=ExplicitBrowserTestKey())
vault.get_store=lambda:vault._store
# Keep synthetic SteamID records from sending requests to actual Steam services.
import httpx
original_client=httpx.Client
def client(*args,**kwargs):
    kwargs['transport']=httpx.MockTransport(lambda request:httpx.Response(503,json={'error':'Explicit browser test adapter unavailable'}))
    return original_client(*args,**kwargs)
httpx.Client=client
import uvicorn
uvicorn.run('app.main:app',host='127.0.0.1',port=8000,log_level='warning',access_log=False)
