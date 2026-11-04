"""Build-time immutable file hashes. A local hash manifest is not a publisher signature."""
import hashlib,json,sys
from pathlib import Path

def generate(root):
    root=Path(root)
    files={}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts or path.suffix in ('.pyc','.log','.sqlite3') or path.name=='component-integrity.json':continue
        with path.open('rb') as stream:files[path.relative_to(root).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()
    if not 1<=len(files)<=20000:raise ValueError('Unexpected component count')
    target=root/'component-integrity.json'
    target.write_text(json.dumps({'schema':1,'baseline_trust':'unsigned_local_manifest','files':files},sort_keys=True))
    return len(files)
if __name__=='__main__':print(f'Component baseline: {generate(sys.argv[1])} files')
