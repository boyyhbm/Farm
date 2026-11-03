# API schema

`openapi.json` is generated from the real FastAPI routes and Pydantic schemas.
Regenerate from repository root with:

```sh
PYTHONPATH=apps/api .venv/bin/python -c 'import json; from pathlib import Path; from app.main import app; Path("packages/shared-schemas/openapi.json").write_text(json.dumps(app.openapi(),ensure_ascii=False,indent=2))'
```

The live `/openapi.json` is authoritative for the deployed revision. Dashboard domain types live in `apps/dashboard/types`; the agent WebSocket protocol is documented in `docs/API-CONTRACT.md`. Credentials are not part of exported configuration.
