'use strict';
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const http = require('node:http');
const os = require('node:os');
const { sanitize } = require('./config.cjs');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const BACKEND_PORT = 17831;
const DASHBOARD_PORT = 17830;
const ORIGIN = `http://127.0.0.1:${DASHBOARD_PORT}`;

function resolvePaths({ packaged, resourcesPath, repositoryRoot, dataDir }) {
  const windows = process.platform === 'win32';
  const runtimeRoot=path.join(resourcesPath||'', 'runtime','farm-runtime');
  const embedded=packaged && !fs.existsSync(path.join(runtimeRoot,'farm-runtime.exe')) && fs.existsSync(path.join(runtimeRoot,'python.exe'));
  return {
    dataDir,
    runtime: process.env.FARM_RUNTIME_EXECUTABLE || (packaged
      ? path.join(runtimeRoot, embedded ? 'python.exe' : 'farm-runtime.exe')
      : path.join(repositoryRoot, '.venv', windows ? 'Scripts/python.exe' : 'bin/python')),
    runtimePrefix: packaged ? (embedded ? [path.join(runtimeRoot,'desktop_entry.py')] : []) : [path.join(repositoryRoot, 'apps/api/desktop_entry.py')],
    node: process.env.FARM_NODE_EXECUTABLE || (packaged ? path.join(resourcesPath, 'node/node.exe') : 'node'),
    dashboard: process.env.FARM_DASHBOARD_SERVER || (packaged ? path.join(resourcesPath, 'dashboard/server.js')
      : path.join(repositoryRoot, 'apps/dashboard/.next/standalone/server.js')),
    cwd: packaged ? resourcesPath : repositoryRoot,
    agentInstaller: packaged ? path.join(resourcesPath, 'downloads/CS2-Farm-Agent-Setup.exe') : null,
  };
}
function readJson(url, timeout = 2000) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout, agent: false }, res => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', chunk => { raw += chunk; if (raw.length > 262144) req.destroy(new Error('Health response too large')); });
      res.on('end', () => {
        if (res.statusCode !== 200) return reject(new Error(`Health HTTP ${res.statusCode}`));
        try { resolve(JSON.parse(raw)); } catch { reject(new Error('Invalid health response')); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('Health check timed out')));
    req.on('error', reject);
  });
}
function assertPortFree(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', () => reject(new Error(`Локальный порт ${port} занят другим приложением. Закройте его или перезапустите компьютер. Автоматическое подключение к чужому процессу запрещено.`)));
    server.listen({ host: '127.0.0.1', port, exclusive: true }, () => server.close(resolve));
  });
}
class Supervisor extends EventEmitter {
  constructor(paths, options = {}) {
    super();
    this.paths = paths;
    this.children = new Map();
    this.services = {};
    this.logs = [];
    this.running = false;
    this.stopping = false;
    this.restartBudget = 0;
    this.restartTimer = null;
    this.mode = null;
    this.monitor = null;
    this.options = options;
    this.instanceId = crypto.randomUUID();
    this.healthFailures = 0;
    fs.mkdirSync(paths.dataDir, { recursive: true, mode: 0o700 });
  }
  log(service, message) {
    const entry = { time: new Date().toISOString(), service, message: sanitize(message) };
    this.logs.push(entry);
    if (this.logs.length > 200) this.logs.shift();
    try {
      const file = path.join(this.paths.dataDir, 'supervisor.log');
      if (fs.existsSync(file) && fs.statSync(file).size > 2 * 1024 * 1024) fs.renameSync(file, file + '.1');
      fs.appendFileSync(file, `${entry.time} ${service} ${entry.message}\n`, { mode: 0o600 });
    } catch { /* Visible in-memory diagnostics remain available if the disk is full. */ }
    this.emit('status', this.snapshot());
  }
  state(name, status, detail = '') {
    this.services[name] = { status, detail: sanitize(detail), pid: this.children.get(name)?.pid || null };
    this.emit('status', this.snapshot());
  }
  snapshot() {
    return { mode: this.mode, running: this.running, services: structuredClone(this.services),
      logs: [...this.logs], dashboardUrl: this.mode === 'server' ? ORIGIN : null,
      dataDirectory: this.paths.dataDir, database: this.mode === 'server' ? 'SQLite (WAL)' : 'remote',
      queue: this.mode === 'server' ? 'Persistent local queue (SQLite)' : 'remote', restartAttempts: this.restartBudget };
  }
  check(mode = 'server') {
    const required = [this.paths.runtime, ...(mode === 'server' ? [this.paths.dashboard] : [])];
    if (this.paths.node !== 'node' && mode === 'server') required.push(this.paths.node);
    const missing = required.filter(filename => !fs.existsSync(filename));
    let diskFreeBytes = null;
    try { const disk = fs.statfsSync(this.paths.dataDir); diskFreeBytes = disk.bavail * disk.bsize; } catch { /* Report unavailable. */ }
    return { ok: missing.length === 0, missing, platform: process.platform, release: os.release(), arch: process.arch,
      memoryBytes: os.totalmem(), diskFreeBytes, noFirewallChanges: true,
      interfaces: Object.entries(os.networkInterfaces()).flatMap(([name, addresses]) => (addresses || []).filter(value => value.family === 'IPv4' && !value.internal).map(value => ({ name, address: value.address }))),
      message: missing.length ? 'Отсутствуют файлы поставки. Переустановите приложение; ручная установка Python или Node.js не требуется.' : 'Компоненты поставки найдены.' };
  }
  runCommand(args, input, timeout = 60000) {
    return new Promise((resolve, reject) => {
      const child = spawn(this.paths.runtime, [...this.paths.runtimePrefix, ...args], {
        cwd: this.paths.cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONUNBUFFERED: '1' },
      });
      let output = ''; let diagnostic = ''; let settled = false;
      const finish = (error, value) => { if (settled) return; settled = true; clearTimeout(timer); error ? reject(error) : resolve(value); };
      const timer = setTimeout(() => { child.kill(); finish(new Error('Операция превысила допустимое время. См. диагностику.')); }, timeout);
      child.stdout.on('data', bytes => { output = (output + bytes.toString()).slice(-32768); });
      child.stderr.on('data', bytes => { diagnostic = (diagnostic + bytes.toString()).slice(-32768); });
      child.once('error', error => finish(new Error(`Не удалось запустить локальный компонент: ${sanitize(error.message)}`)));
      child.once('exit', code => {
        if (code !== 0) { this.log('runtime', diagnostic || output || `Exit code ${code}`); return finish(new Error(sanitize(diagnostic || output || `Runtime exited ${code}`))); }
        finish(null, output.trim());
      });
      child.stdin.on('error', () => {});
      child.stdin.end(input ? JSON.stringify(input) : undefined);
    });
  }
  async configure(input) {
    if (this.running) throw new Error('Приложение уже настроено и работает.');
    this.state('configuration', 'starting', 'Создание базы и администратора');
    if (!this.check(input.mode === 'server' ? 'server' : 'agent').ok) throw new Error(this.check(input.mode).message);
    if (input.mode === 'server') {
      await this.runCommand(['configure', '--data-dir', this.paths.dataDir, '--origin', ORIGIN], { username: input.username, password: input.password });
    } else if (input.mode === 'agent') {
      await this.runCommand(['agent-enroll', '--data-dir', this.paths.dataDir],
        { server_url: input.serverUrl, token: input.token, ...(input.ca_certificate ? { ca_certificate: input.ca_certificate } : {}) });
    }
    this.state('configuration', 'up', 'Настройка сохранена');
  }
  launch(name, executable, args, extraEnv = {}) {
    const child = spawn(executable, args, { cwd: this.paths.cwd, windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, ...extraEnv } });
    this.children.set(name, child);
    this.state(name, 'starting');
    child.stdin.on('error', () => {});
    child.stdout.on('data', bytes => this.log(name, bytes.toString().trim()));
    child.stderr.on('data', bytes => this.log(name, bytes.toString().trim()));
    child.on('error', error => { this.log(name, error.message); this.state(name, 'failed', error.message); });
    child.on('exit', (code, signal) => {
      this.children.delete(name);
      this.state(name, this.stopping ? 'stopped' : 'failed', `Exit ${code ?? signal}`);
      if (!this.stopping && this.running) this.scheduleRecovery(name);
    });
    return child;
  }
  async waitReady(name, url, predicate = () => true, timeout = 45000) {
    const deadline = Date.now() + timeout;
    let latest = 'Компонент запускается';
    while (Date.now() < deadline) {
      const child = this.children.get(name);
      if (!child || child.exitCode !== null) throw new Error(`${name}: процесс остановился до готовности. См. журнал.`);
      try {
        const value = await readJson(url);
        if (predicate(value)) { this.state(name, 'up'); return value; }
        latest = 'Неверный идентификатор компонента; найден посторонний процесс';
      } catch (error) { latest = error.message; }
      await wait(250);
    }
    throw new Error(`${name}: ${latest}`);
  }
  async start(mode) {
    if (this.running) return this.snapshot();
    if (this.stopping) throw new Error('Компоненты ещё завершают работу.');
    this.mode = mode;
    this.instanceId = crypto.randomUUID();
    const check = this.check(mode);
    if (!check.ok) throw new Error(`${check.message}\n${check.missing.join('\n')}`);
    this.stopping = false;
    try {
      if (mode === 'server') {
        await assertPortFree(BACKEND_PORT); await assertPortFree(DASHBOARD_PORT);
        this.launch('backend', this.paths.runtime, [...this.paths.runtimePrefix, 'serve', '--data-dir', this.paths.dataDir, '--origin', ORIGIN, '--port', String(BACKEND_PORT)],
          { DESKTOP_INSTANCE_ID: this.instanceId, PYTHONUNBUFFERED: '1', ...(this.paths.agentInstaller ? { AGENT_INSTALLER_PATH: this.paths.agentInstaller } : {}) });
        await this.waitReady('backend', `http://127.0.0.1:${BACKEND_PORT}/health/ready`, data => data.desktop_instance_id === this.instanceId && data.local_worker_running === true);
        this.state('database', 'up', 'SQLite WAL; данные сохраняются на диске');
        this.state('queue', 'up', 'Локальный worker с устойчивой очередью SQLite');
        this.launch('dashboard', this.paths.node, [this.paths.dashboard], { PORT: String(DASHBOARD_PORT), HOSTNAME: '127.0.0.1', NODE_ENV: 'production', API_URL: `http://127.0.0.1:${BACKEND_PORT}` });
        // Next serves an HTML page; its build-side readiness endpoint is the API rewrite.
        await this.waitReady('dashboard', `${ORIGIN}/health/ready`, data => data.desktop_instance_id === this.instanceId && data.local_worker_running === true);
      } else if (mode === 'agent') {
        this.launch('agent', this.paths.runtime, [...this.paths.runtimePrefix, 'agent', '--data-dir', this.paths.dataDir], { PYTHONUNBUFFERED: '1' });
        await wait(1000);
        if (!this.children.has('agent')) throw new Error('Агент завершился при запуске. Проверьте соединение и код подключения.');
        this.state('agent', 'up', 'Процесс запущен; подключение и telemetry подтверждаются в Node Manager');
      } else throw new Error('Неизвестный локальный режим.');
      this.running = true;
      this.monitor = setInterval(() => this.probe().catch(error => this.log('health', error.message)), 10000);
      this.monitor.unref();
      this.emit('status', this.snapshot());
      return this.snapshot();
    } catch (error) {
      this.log('startup', error.message);
      await this.stop();
      throw error;
    }
  }
  async probe() {
    if (!this.running || this.stopping || this.mode !== 'server') return this.snapshot();
    try {
      const health = await readJson(`http://127.0.0.1:${BACKEND_PORT}/health/ready`);
      if (health.desktop_instance_id !== this.instanceId || health.local_worker_running !== true) throw new Error('Backend identity or local worker health mismatch');
      this.healthFailures = 0;
      this.state('backend', 'up'); this.state('database', 'up'); this.state('queue', 'up');
    } catch (error) {
      this.state('backend', 'degraded', error.message);
      this.state('database', 'unknown', 'Health check не подтвердил состояние');
      this.state('queue', 'unknown', 'Health check не подтвердил состояние');
      this.healthFailures += 1;
      if (this.healthFailures >= 3) this.scheduleRecovery('backend');
    }
    return this.snapshot();
  }
  scheduleRecovery(service) {
    if (this.restartTimer || this.stopping) return;
    if (this.restartBudget >= 3) {
      this.running = false;
      this.state(service, 'failed', 'Лимит 3 автоматических перезапусков исчерпан. Откройте диагностику.');
      this.emit('failure', `${service}: автоматическое восстановление остановлено`);
      return;
    }
    const delay = 1000 * 2 ** this.restartBudget++;
    this.log('supervisor', `Восстановление ${service} через ${delay / 1000} секунд`);
    this.restartTimer = setTimeout(async () => {
      this.restartTimer = null;
      const mode = this.mode;
      try { await this.stop(); await this.start(mode); }
      catch (error) { this.log('recovery', error.message); this.emit('failure', error.message); }
    }, delay);
  }
  async terminate(child) {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = new Promise(resolve => child.once('exit', resolve));
    // Runtime reads shutdown from the inherited private stdin pipe on Windows too.
    if (child.stdin && !child.stdin.destroyed && (this.children.get('backend') === child || this.children.get('agent') === child)) child.stdin.end('shutdown\n');
    else child.kill('SIGTERM');
    let deadline;
    const stopped = await Promise.race([exited.then(() => true), new Promise(resolve => { deadline = setTimeout(() => resolve(false), 15000); })]);
    clearTimeout(deadline);
    if (!stopped) {
      if (process.platform === 'win32') await new Promise(resolve => {
        const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
        killer.once('exit', resolve); killer.once('error', resolve);
      });
      else child.kill('SIGKILL');
      await Promise.race([exited, wait(2000)]);
    }
  }
  async stop() {
    this.stopping = true; this.running = false;
    clearTimeout(this.restartTimer); this.restartTimer = null;
    clearInterval(this.monitor); this.monitor = null;
    for (const name of ['dashboard', 'agent', 'backend']) {
      const child = this.children.get(name);
      if (child) await this.terminate(child);
      this.children.delete(name);
      if (this.services[name]) this.state(name, 'stopped');
    }
    for (const name of ['queue', 'database']) if (this.services[name]) this.state(name, 'stopped');
    this.stopping = false;
    this.emit('status', this.snapshot());
  }
  async restart() { const mode = this.mode; this.restartBudget = 0; await this.stop(); return this.start(mode); }
}
module.exports = { Supervisor, resolvePaths, readJson, assertPortFree, BACKEND_PORT, DASHBOARD_PORT, ORIGIN };
