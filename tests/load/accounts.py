#!/usr/bin/env python3
"""Authenticated real account registry load probe. Explicitly creates test records.

Run only on an isolated test deployment. Existing records are never modified.
No claimed throughput is baked into the report; every duration is observed.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import io
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--origin', help='PUBLIC_ORIGIN if different from request URL')
    parser.add_argument('--count', type=int, choices=(10, 100, 1000, 5000), default=1000)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--reads', type=int, default=200)
    parser.add_argument('--report', default='artifacts/load-report.json')
    parser.add_argument('--confirm-isolated-test', action='store_true', required=True)
    args = parser.parse_args()
    username = os.environ.get('LOAD_USERNAME', 'admin')
    password = os.environ.get('LOAD_PASSWORD')
    if not password:
        parser.error('set LOAD_PASSWORD in the process environment')
    if args.concurrency < 1 or args.concurrency > 100 or args.reads < 1:
        parser.error('concurrency must be 1..100 and reads positive')
    with httpx.Client(base_url=args.url.rstrip('/'), timeout=60, headers={'Origin': (args.origin or args.url).rstrip('/')}) as client:
        response = client.post('/api/v1/auth/login', json={'username': username, 'password': password})
        response.raise_for_status()
        auth = client.get('/api/v1/auth/me')
        auth.raise_for_status()
        csrf = auth.json()['csrf_token']
        client.headers['X-CSRF-Token'] = csrf
        client.headers['Origin'] = (args.origin or args.url).rstrip('/')
        # Separate synthetic records are never displayed as operational farm metrics.
        # SteamIDs are syntax-valid test values; no external Steam requests occur.
        nonce = time.time_ns() % 1_000_000_000
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(['steam_id', 'notes', 'tags'])
        writer.writerows((str(76561190000000000 + nonce + i), 'ISOLATED LOAD TEST ONLY', 'load-test')
                         for i in range(args.count))
        started = time.perf_counter()
        imported = client.post('/api/v1/accounts/import', content=stream.getvalue(), headers={'Content-Type': 'text/csv'})
        import_seconds = time.perf_counter() - started
        imported.raise_for_status()

        def read_page(index: int) -> dict:
            start = time.perf_counter()
            try:
                result = client.get('/api/v1/accounts', params={'page': index % max(1, args.count // 50) + 1, 'page_size': 50})
                result.raise_for_status()
                result.json()
                return {'seconds': time.perf_counter() - start, 'status': result.status_code}
            except httpx.HTTPError as exc:
                return {'seconds': time.perf_counter() - start, 'error': str(exc)}

        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(read_page, range(args.reads)))
        wall_seconds = time.perf_counter() - started
        successful = [item['seconds'] for item in results if 'error' not in item]
        sorted_latencies = sorted(successful)
        p95 = sorted_latencies[min(len(sorted_latencies) - 1, int(len(sorted_latencies) * .95))] if successful else None
        report = {
            'observed_at': datetime.now(timezone.utc).isoformat(), 'target': args.url,
            'accounts_requested': args.count,
            'import_response': {k: v for k, v in imported.json().items() if k != 'ids'},
            'imported_id_count': len(imported.json().get('ids', [])),
            'import_seconds': import_seconds, 'reads_requested': args.reads,
            'concurrency': args.concurrency, 'successful_reads': len(successful),
            'failed_reads': [item for item in results if 'error' in item],
            'wall_seconds': wall_seconds, 'reads_per_second': len(successful) / wall_seconds,
            'latency_mean_seconds': statistics.mean(successful) if successful else None,
            'latency_p95_seconds': p95,
            'scope': 'Registry import and paginated reads only; excludes CS2 capacity, agent fanout and external Steam throughput.',
        }
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report['failed_reads']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
