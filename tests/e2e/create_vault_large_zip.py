"""Generate the explicit synthetic fixture consumed by vault-large-zip-smoke.cjs."""
import base64
import json
from pathlib import Path
import secrets
import zipfile

path = Path('/tmp/vault-browser-large.zip')
with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for index in range(70):
        value = {'account_name': f'vault_large_browser_{index}',
                 'shared_secret': base64.b64encode(secrets.token_bytes(20)).decode(),
                 'identity_secret': base64.b64encode(secrets.token_bytes(20)).decode(),
                 'Session': {'test_padding': secrets.token_hex(160000)}}
        archive.writestr(f'{index}.maFile', json.dumps(value))
print(json.dumps({'path': str(path), 'bytes': path.stat().st_size, 'records': 70, 'synthetic_only': True}))
