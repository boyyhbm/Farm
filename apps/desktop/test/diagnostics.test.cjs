'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { diagnose, explain, tlsHealth } = require('../lib/diagnostics.cjs');
const { Supervisor } = require('../lib/supervisor.cjs');
test('diagnostic errors give distinct causes, actionable remedies and redact secrets', () => {
  for (const [message, code] of [['EADDRINUSE', 'PORT_BUSY'], ['CERT_HAS_EXPIRED', 'TLS_CERTIFICATE'], ['ENOTFOUND', 'DNS'], ['ECONNREFUSED', 'REFUSED'], ['SQLITE_BUSY', 'DATABASE_BUSY'], ['database malformed', 'DATABASE_INTEGRITY'], ['ENOSPC', 'DISK_FULL'], ['EACCES', 'PERMISSION']]) {
    const result = explain(new Error(message + ' token=secret-value'));
    assert.equal(result.code, code); assert.ok(result.remedy.length > 20); assert.ok(!result.detail.includes('secret-value'));
  }
});
test('stopped real services produce failed diagnostics, never unconditional success', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'farm-diagnose-'));
  const supervisor = new Supervisor({ dataDir: dir, runtime: '/missing/runtime.exe', runtimePrefix: [], node: '/missing/node.exe', dashboard: '/missing/server.js', cwd: dir });
  try {
    const report = await diagnose(supervisor, { mode: 'server' }, 'test');
    assert.equal(report.passed, false);
    assert.equal(report.checks.find(check => check.id === 'files').code, 'MISSING_COMPONENT');
    assert.equal(report.checks.find(check => check.id === 'backend').status, 'failed');
    assert.equal(report.checks.find(check => check.id === 'storage').status, 'passed');
    assert.equal(fs.readdirSync(dir).filter(name => name.startsWith('.diagnostic')).length, 0);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
test('TLS diagnosis rejects insecure URLs before opening a socket', async () => {
  await assert.rejects(tlsHealth('http://example.invalid'), /HTTPS/);
});
