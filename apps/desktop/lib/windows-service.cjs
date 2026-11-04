'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { sanitize } = require('./config.cjs');
const quote = value => "'" + String(value).replaceAll("'", "''") + "'";
async function serviceStatus(supervisor) {
  if (process.platform !== 'win32') return { installed: false, status: 'unsupported' };
  return JSON.parse(await supervisor.runCommand(['service-status']));
}
async function serviceAdmin(supervisor, action, payload = {}) {
  if (process.platform !== 'win32') throw new Error('Фоновая служба доступна только на Windows.');
  if (!['install', 'start', 'stop', 'remove', 'applications', 'applications-info', 'refresh', 'reset'].includes(action)) throw new Error('Unknown service action');
  const request = path.join(supervisor.paths.dataDir, '.service-' + crypto.randomUUID() + '.json');
  const result = request + '.result';
  fs.writeFileSync(request, JSON.stringify({ action, payload }), { flag: 'wx', mode: 0o600 });
  try {
    // Protect the one-time enrollment code before the UAC helper reads it.
    await supervisor.runCommand(['protect-file', '--input', request]);
    const argv = [...supervisor.paths.runtimePrefix, 'service-admin', '--input', request, '--output', result];
    // Every path is a literal, never interpolated as PowerShell code or sent remotely.
    // Quote individual Windows argv entries, including paths with spaces.
    const argumentsString = argv.map(value => '"' + String(value).replaceAll('"', '\\"') + '"').join(' ');
    const script = `$ErrorActionPreference='Stop'; try { $p=Start-Process -FilePath ${quote(supervisor.paths.runtime)} -ArgumentList ${quote(argumentsString)} -Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode } catch { exit 1223 }`;
    const code = await new Promise((resolve, reject) => {
      const child = spawn(path.join(process.env.SystemRoot || 'C:/Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'), ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')], { windowsHide: true, stdio: 'ignore' });
      child.once('error', reject); child.once('exit', resolve);
    });
    const response = fs.existsSync(result) ? JSON.parse(fs.readFileSync(result, 'utf8')) : null;
    if (code !== 0 || !response?.ok) throw new Error(sanitize(response?.error || (code === 1223 ? 'Запрос администратора отменён. Служба не изменена.' : `Служба Windows: код ${code}. Откройте диагностику.`)));
    return response.result;
  } finally { fs.rmSync(request, { force: true }); fs.rmSync(result, { force: true }); }
}
module.exports = { serviceStatus, serviceAdmin, quote };
