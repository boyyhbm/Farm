'use strict';
// Explicit synthetic child fixture for supervisor lifecycle tests, not a production runtime.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const command = process.argv[2];
if (command === 'configure') {
  let raw = ''; process.stdin.on('data', data => { raw += data; });
  process.stdin.on('end', () => {
    const value = JSON.parse(raw);
    const directory = process.argv[process.argv.indexOf('--data-dir') + 1];
    fs.writeFileSync(path.join(directory, 'configured-fixture.json'), JSON.stringify({ username: value.username, hash: crypto.createHash('sha256').update(value.password).digest('hex') }));
    process.stdout.write('{"configured":true}\n');
  });
} else if (command === 'serve') {
  const server = http.createServer((_req, res) => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ desktop_instance_id: process.env.DESKTOP_INSTANCE_ID, local_worker_running: true })); });
  server.listen(17831, '127.0.0.1');
  process.stdin.on('data', data => { if (data.toString() === 'shutdown\n') server.close(() => process.exit(0)); });
} else {
  const server = http.createServer((_req, res) => { const request = http.get('http://127.0.0.1:17831/health/ready', response => { response.pipe(res); }); request.on('error', () => { res.statusCode = 503; res.end('{}'); }); });
  server.listen(17830, '127.0.0.1');
  process.on('SIGTERM', () => server.close(() => process.exit(0)));
}
