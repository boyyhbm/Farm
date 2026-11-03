# Real registry load probe

Use an isolated PostgreSQL/Redis deployment with a test administrator. The probe inserts syntax-valid **synthetic test account records** and never synchronizes them with Steam. It does not touch real registered accounts and does not represent real players, XP, drops, or process capacity. Remove the isolated database after testing.

```sh
# API Python environment already includes httpx
read -rs LOAD_PASSWORD
export LOAD_PASSWORD
python tests/load/accounts.py --url http://127.0.0.1:8000 --count 1000 \
  --concurrency 8 --reads 200 --confirm-isolated-test
```

Set `LOAD_USERNAME` for a non-default user. Use `--origin http://localhost:3000` if requesting a development API directly on port 8000 while its configured `PUBLIC_ORIGIN` is port 3000. Same-site deployments may require the URL matching `PUBLIC_ORIGIN` to pass strict origin checks; for localhost development set `PUBLIC_ORIGIN=http://127.0.0.1:8000` and `SESSION_SECURE=false`. Do not disable TLS certificate validation: trust the Caddy root CA or use a valid domain certificate.

Output includes observed import time, successful/failed requests, wall time, actual throughput, mean and p95 latency. It writes `artifacts/load-report.json`, which is excluded from source control. `--count 5000` measures registry behavior only. It makes no statement about simultaneously running 5,000 CS2 clients. Rate-limit errors are retained as failures, not discarded.
