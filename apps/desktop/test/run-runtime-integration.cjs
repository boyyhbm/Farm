'use strict';
// Actual existing API/SQLite + production Next standalone, not the synthetic unit fixture.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { Supervisor, resolvePaths, ORIGIN } = require('../lib/supervisor.cjs');
(async () => {
  const root = path.resolve(__dirname, '../../..');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'farm-real-supervisor-'));
  const backupDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'farm-real-backup-'));
  const supervisor = new Supervisor(resolvePaths({ packaged: false, repositoryRoot: root, dataDir: directory }));
  const report = { checkedAt: new Date().toISOString(), environment: `Real Node supervisor, Python API, SQLite durable queue and production Next standalone on ${process.platform}; not an Electron or Windows installer test`, checks: [], passed: false };
  const check = name => { report.checks.push({ name, passed: true }); process.stdout.write(`PASS ${name}\n`); };
  const requireOk = async response => { if (!response.ok) throw new Error(`HTTP ${response.status}: ${(await response.text()).slice(0, 300)}`); return response.json(); };
  try {
    const password = 'temporary-' + crypto.randomBytes(20).toString('hex');
    await supervisor.configure({ mode: 'server', username: 'desktop-integration', password }); check('runtime_creates_database_and_admin_without_redis');
    await supervisor.start('server'); check('actual_backend_worker_next_start_health_identity');
    const loginResponse = await fetch(`${ORIGIN}/api/v1/auth/login`, { method: 'POST', headers: { Origin: ORIGIN, 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'desktop-integration', password }) });
    const cookie = loginResponse.headers.get('set-cookie')?.split(';')[0];
    const login = await requireOk(loginResponse); const headers = { Origin: ORIGIN, 'X-CSRF-Token': login.csrf_token, Cookie: cookie, 'Content-Type': 'application/json' };
    const first = await requireOk(await fetch(`${ORIGIN}/api/v1/accounts`, { method: 'POST', headers, body: JSON.stringify({ steam_id: '76561199999888882', notes: 'Explicit isolated supervisor test record' }) }));
    check('actual_dashboard_proxy_auth_and_database_write');
    const backup = path.join(backupDirectory, 'integration.farmbackup');
    await supervisor.runCommand(['backup', '--data-dir', directory, '--output', backup]); check('gui_runtime_backup_command_creates_snapshot');
    const second = await requireOk(await fetch(`${ORIGIN}/api/v1/accounts`, { method: 'POST', headers, body: JSON.stringify({ steam_id: '76561199999888883', notes: 'Record after backup' }) }));
    await supervisor.restart();
    await requireOk(await fetch(`${ORIGIN}/api/v1/accounts/${first.id}`, { headers })); check('account_and_auth_session_persist_after_full_restart');
    await supervisor.stop(); await supervisor.runCommand(['restore', '--data-dir', directory, '--input', backup]); await supervisor.start('server');
    const oldSession = await fetch(`${ORIGIN}/api/v1/accounts/${first.id}`, { headers });
    if (oldSession.status !== 401) throw new Error('Restore did not revoke old authentication session');
    check('restore_invalidates_old_sessions');
    const restoredLoginResponse = await fetch(`${ORIGIN}/api/v1/auth/login`, { method: 'POST', headers: { Origin: ORIGIN, 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'desktop-integration', password }) });
    const restoredCookie = restoredLoginResponse.headers.get('set-cookie')?.split(';')[0];
    const restoredLogin = await requireOk(restoredLoginResponse); headers.Cookie = restoredCookie; headers['X-CSRF-Token'] = restoredLogin.csrf_token;
    await requireOk(await fetch(`${ORIGIN}/api/v1/accounts/${first.id}`, { headers }));
    const absent = await fetch(`${ORIGIN}/api/v1/accounts/${second.id}`, { headers }); if (absent.status !== 404) throw new Error('Post-backup account unexpectedly survives restore');
    check('restore_replaces_database_with_verified_snapshot');
    report.passed = true;
  } catch (error) { report.error = error.message; report.logs = supervisor.logs; console.error(error.message); process.exitCode = 1; }
  finally {
    await supervisor.stop(); check('all_owned_server_components_stop');
    fs.mkdirSync(path.join(__dirname, 'results'), { recursive: true });
    fs.writeFileSync(path.join(__dirname, 'results/supervisor-integration.json'), JSON.stringify(report, null, 2));
    fs.rmSync(directory, { recursive: true, force: true });
    fs.rmSync(backupDirectory, { recursive: true, force: true });
  }
})();
