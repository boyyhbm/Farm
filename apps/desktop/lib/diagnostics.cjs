'use strict';
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const { readJson, BACKEND_PORT, ORIGIN } = require('./supervisor.cjs');
const { sanitize, httpsUrl } = require('./config.cjs');
function explain(error) {
  const message = sanitize(error?.message || error);
  const rules = [
    [/EADDRINUSE|порт .*занят/i, 'PORT_BUSY', 'Локальный порт занят', 'Закройте другое приложение, использующее порт, затем нажмите «Перезапустить компоненты».'],
    [/CERT_|certificate|сертификат|self.signed|UNABLE_TO_VERIFY/i, 'TLS_CERTIFICATE', 'Сертификат сервера не подтверждён', 'Сверьте адрес и часы Windows. Импортируйте публичный CA из System Management сервера; не отключайте проверку TLS.'],
    [/ENOTFOUND|getaddrinfo/i, 'DNS', 'Не найден адрес сервера', 'Проверьте адрес, подключение к сети и VPN.'],
    [/ECONNREFUSED|actively refused/i, 'REFUSED', 'Сервер не принимает соединение', 'На главном ПК проверьте Backend и настройки HTTPS. Разрешайте доступ в Windows Firewall только для доверенной сети.'],
    [/timed? ?out|ETIMEDOUT|время/i, 'TIMEOUT', 'Компонент не ответил вовремя', 'Проверьте сеть и журнал ошибок. Для локальных компонентов доступен перезапуск.'],
    [/EACCES|EPERM|Access.*denied|Permission/i, 'PERMISSION', 'Недостаточно прав', 'Проверьте доступ к каталогу данных и уведомления защитного ПО. Для службы подтвердите запрос администратора.'],
    [/locked|SQLITE_BUSY/i, 'DATABASE_BUSY', 'База данных занята', 'Закройте второй экземпляр приложения и повторите проверку. Не удаляйте файлы базы данных.'],
    [/malformed|corrupt|integrity/i, 'DATABASE_INTEGRITY', 'Обнаружена проблема целостности базы', 'Сохраните копию каталога данных и восстановите проверенную резервную копию через System Management.'],
    [/ENOSPC|disk.*full|space/i, 'DISK_FULL', 'Недостаточно места на диске', 'Освободите место на диске с каталогом данных, затем повторите проверку.'],
    [/ENOENT|missing|Отсутствуют/i, 'MISSING_COMPONENT', 'Не найден компонент установки', 'Повторно установите приложение из официального установщика. Существующие данные сохраняются.'],
    [/identity|идентификатор|посторонний/i, 'IDENTITY', 'Ответил посторонний процесс', 'Закройте конфликтующее приложение и перезапустите компоненты.'],
  ];
  const rule = rules.find(([pattern]) => pattern.test(message));
  return { code: rule?.[1] || 'COMPONENT_ERROR', title: rule?.[2] || 'Ошибка компонента', detail: message, remedy: rule?.[3] || 'Сохраните диагностический отчёт. Посмотрите журнал ошибок и попробуйте перезапустить компоненты.' };
}
function tlsHealth(url, ca) {
  return new Promise((resolve, reject) => {
    const target = new URL(httpsUrl(url) + '/health/ready');
    const req = https.get(target, { ...(ca ? { ca } : {}), rejectUnauthorized: true, timeout: 7000, agent: false }, response => {
      let raw = '';
      response.on('data', chunk => { raw += chunk; if (raw.length > 262144) req.destroy(new Error('Health response too large')); });
      response.on('end', () => {
        try { const value = JSON.parse(raw); if (response.statusCode !== 200 || value.status !== 'ready') throw new Error(`HTTP ${response.statusCode}: сервер не готов`); resolve(value); } catch (error) { reject(error); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('Connection timed out'))); req.on('error', reject);
  });
}
async function diagnose(supervisor, config, version) {
  const checks = [];
  const record = async (id, title, check) => {
    try { const result = await check(); checks.push({ id, title, status: 'passed', detail: result || 'Проверено' }); }
    catch (error) { checks.push({ id, title, status: 'failed', ...explain(error), title }); }
  };
  const mode = config?.mode || supervisor.mode || 'server';
  await record('files', 'Файлы установки', () => { const result = supervisor.check(mode); if (!result.ok) throw new Error('Отсутствуют файлы: ' + result.missing.join(', ')); return 'Все необходимые локальные компоненты найдены'; });
  await record('storage', 'Каталог данных и свободное место', () => {
    const file = path.join(supervisor.paths.dataDir, '.diagnostic-' + require('node:crypto').randomUUID());
    try { fs.writeFileSync(file, 'ok', { flag: 'wx', mode: 0o600 }); } finally { if (fs.existsSync(file)) fs.unlinkSync(file); }
    const free = supervisor.check(mode).diskFreeBytes;
    if (free === null) throw new Error('Свободное место определить не удалось');
    if (free < 256 * 1024 ** 2) throw new Error('Disk space below 256 MB');
    return `Доступ для записи подтверждён; свободно ${(free / 1024 ** 3).toFixed(1)} GB`;
  });
  if (mode === 'server') {
    for (const [id, title, url] of [['backend', 'Backend, база и очередь', `http://127.0.0.1:${BACKEND_PORT}/health/ready`], ['dashboard', 'Dashboard → API', `${ORIGIN}/health/ready`]]) await record(id, title, async () => {
      const health = await readJson(url);
      if (health.desktop_instance_id !== supervisor.instanceId) throw new Error('Backend identity mismatch');
      if (health.local_worker_running !== true) throw new Error('Фоновая очередь не работает');
      return 'Готовность и идентификатор процесса подтверждены';
    });
    await record('database', 'Целостность локальных баз данных', async () => {
      const result = JSON.parse(await supervisor.runCommand(['data-check', '--data-dir', supervisor.paths.dataDir], null, 30000));
      if (!result.ok) throw new Error('Database integrity check failed');
      return Object.entries(result.databases).map(([name, value]) => `${name}: ${value.status}`).join('; ');
    });
  } else {
    await record('tls', 'Защищённое соединение с сервером', async () => {
      let ca;
      const file = path.join(supervisor.paths.dataDir, 'trusted-server-ca.pem');
      if (mode === 'agent' && fs.existsSync(file)) ca = fs.readFileSync(file, 'utf8');
      await tlsHealth(config?.serverUrl, ca); return 'HTTPS: сертификат, имя сервера и готовность API подтверждены';
    });
    if (mode === 'agent' && config?.serviceMode) await record('windows-service', 'Фоновая служба Windows', async () => {
      const service = JSON.parse(await supervisor.runCommand(['service-status']));
      if (!service.installed || service.status !== 'running') throw new Error(`Windows Service: ${service.status}`);
      return `SCM подтвердил RUNNING; автозапуск ${service.automatic_start ? 'включён' : 'выключен'}`;
    });
    if (mode === 'agent') checks.push({ id: 'heartbeat', title: 'Heartbeat агента', status: 'manual', detail: 'Подтверждается на сервере в Nodes по времени последнего heartbeat. Работа процесса сама по себе не означает online.' });
  }
  return { checkedAt: new Date().toISOString(), version, platform: process.platform, mode, checks, passed: checks.every(check => check.status !== 'failed') };
}
module.exports = { explain, diagnose, tlsHealth };
