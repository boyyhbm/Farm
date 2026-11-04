'use strict';
const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, dialog, shell, Notification } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const { Supervisor, resolvePaths, readJson, ORIGIN } = require('./lib/supervisor.cjs');
const { readConfig, saveConfig, validateSetup, parseEnrollment, httpsUrl, sanitize } = require('./lib/config.cjs');
const { serviceStatus, serviceAdmin } = require('./lib/windows-service.cjs');
const { diagnose, explain } = require('./lib/diagnostics.cjs');
const { checkUpdates, downloadUpdate } = require('./lib/updates.cjs');

const productFile = path.join(process.resourcesPath || __dirname, 'product.json');
const product = fs.existsSync(productFile) ? JSON.parse(fs.readFileSync(productFile, 'utf8')) : { productName: 'CS2 Farm Manager', defaultMode: 'server' };
app.setName(product.productName || 'CS2 Farm Manager');
app.setAppUserModelId(product.defaultMode === 'agent' ? 'com.cs2farm.agent' : 'com.cs2farm.manager');
if (process.platform === 'win32') app.setPath('userData', path.join(app.getPath('appData'), app.getName()));
const smokeMode = process.argv.includes('--smoke-test');
const smokeIndex = process.argv.indexOf('--result');
const smokeResult = smokeIndex >= 0 ? process.argv[smokeIndex + 1] : null;
let dataDir = process.env.FARM_DESKTOP_DATA_DIR || path.join(app.getPath('userData'), 'data');
if (smokeMode) { const index = process.argv.indexOf('--smoke-data-dir'); dataDir = index >= 0 ? path.resolve(process.argv[index + 1]) : fs.mkdtempSync(path.join(os.tmpdir(), 'farm-desktop-smoke-')); }
let management; let dashboard; let tray; let supervisor; let config; let quitting = false; let operation = false; let updateResult;
let pendingEnrollment; let lastDiagnostics;
const rendererPath = path.join(__dirname, 'renderer/index.html');
const rendererUrl = pathToFileURL(rendererPath).toString();
const localWebPreferences = { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true };
const requestLock = app.requestSingleInstanceLock();
if (!requestLock) app.quit();
if (requestLock && process.argv.includes('--shutdown')) app.exit(0);

function notify(title, body) {
  if (Notification.isSupported()) new Notification({ title, body: sanitize(body) }).show();
}
function managementSender(event) {
  return management && event.sender === management.webContents && event.senderFrame?.url === rendererUrl;
}
function showManagement() {
  if (!management || management.isDestroyed()) {
    management = new BrowserWindow({ width: 1080, height: 790, minWidth: 760, minHeight: 600, backgroundColor: '#101613', title: 'CS2 Farm Manager — System Management', webPreferences: localWebPreferences, show: false });
    management.setMenuBarVisibility(false);
    management.loadFile(rendererPath);
    management.once('ready-to-show', () => management.show());
    management.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    management.webContents.on('will-navigate', event => event.preventDefault());
    management.webContents.on('did-finish-load', () => {
      if (pendingEnrollment) management.webContents.send('farm:enrollment', pendingEnrollment);
    });
    management.on('close', event => {
      if (!quitting && config?.closeToTray !== false && tray) { event.preventDefault(); management.hide(); }
      else if (!quitting && config?.closeToTray === false) app.quit();
    });
    management.on('closed', () => { management = null; });
  } else { management.show(); management.focus(); }
}
function installTray() {
  // Native PNG generated in memory; no image-generation or external asset dependency.
  const icon = nativeImage.createFromPath(path.join(__dirname, 'renderer/tray.png'));
  if (icon.isEmpty()) throw new Error('Bundled tray icon is invalid; reinstall the application.');
  tray = new Tray(icon);
  tray.setToolTip(app.getName());
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Открыть Dashboard', click: () => openDashboard().catch(showError) },
    { label: 'Управление системой', click: showManagement },
    { label: 'Перезапустить компоненты', click: () => (config?.serviceMode ? serviceAdmin(supervisor, 'stop').then(() => serviceAdmin(supervisor, 'start')).then(refreshService) : guarded(() => supervisor.restart())).catch(showError) },
    { type: 'separator' }, { label: 'Выход', click: () => app.quit() },
  ]));
  tray.on('double-click', () => config?.mode === 'agent' ? showManagement() : openDashboard().catch(showError));
}
function showError(error) {
  supervisor?.log('desktop', error.message || error);
  showManagement();
  if (management && !management.isDestroyed()) management.webContents.send('farm:status', { ...supervisor.snapshot(), error: sanitize(error.message || error) });
}
async function guarded(callback) {
  if (operation) throw new Error('Другая операция ещё выполняется.');
  operation = true;
  try { return await callback(); } finally { operation = false; }
}
function setAutoStart(enabled) {
  if (process.platform === 'win32' || process.platform === 'darwin') app.setLoginItemSettings({ openAtLogin: enabled, path: app.getPath('exe') });
}
async function openDashboard() {
  if (!config) { showManagement(); return; }
  if (config.mode === 'agent') { showManagement(); return; }
  if (config.mode === 'server' && !supervisor.running) await guarded(() => supervisor.start('server'));
  const url = config.mode === 'remote' ? httpsUrl(config.serverUrl) : ORIGIN;
  if (dashboard && !dashboard.isDestroyed()) { dashboard.show(); dashboard.focus(); return; }
  const local = config.mode === 'server';
  dashboard = new BrowserWindow({ width: 1440, height: 950, minWidth: 900, minHeight: 650, backgroundColor: '#101613', title: app.getName(), show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true,
      ...(local ? { preload: path.join(__dirname, 'dashboard-preload.cjs') } : {}) } });
  dashboard.setMenu(Menu.buildFromTemplate([
    { label: 'Приложение', submenu: [ { label: 'Управление системой', click: showManagement }, { label: 'Перезагрузить окно', role: 'reload' }, { type: 'separator' }, { label: 'Выход', role: 'quit' } ] },
    { label: 'Окно', submenu: [{ role: 'minimize' }, { role: 'togglefullscreen' }] },
  ]));
  dashboard.webContents.setWindowOpenHandler(({ url: target }) => {
    try { const parsed = new URL(target); if (parsed.protocol === 'https:') shell.openExternal(parsed.toString()); } catch { /* Reject malformed external link. */ }
    return { action: 'deny' };
  });
  dashboard.webContents.on('will-navigate', (event, target) => { if (new URL(target).origin !== new URL(url).origin) event.preventDefault(); });
  let reloadTimer;
  dashboard.webContents.on('did-fail-load', (_event, code, message, _url, mainFrame) => {
    if (code === -3 || mainFrame === false) return;
    supervisor.log('dashboard', `Не удалось открыть панель: ${message}`); showManagement();
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(() => { if (dashboard && !dashboard.isDestroyed() && !quitting) dashboard.loadURL(url).catch(showError); }, 5000);
  });
  dashboard.once('ready-to-show', () => dashboard?.show());
  dashboard.on('close', event => { if (!quitting && config?.closeToTray !== false && tray) { event.preventDefault(); dashboard.hide(); } else if (!quitting && config?.closeToTray === false) app.quit(); });
  dashboard.on('closed', () => { clearTimeout(reloadTimer); dashboard = null; });
  await dashboard.loadURL(url);
  if (management && !management.isDestroyed()) management.hide();
}
function enrollmentFromArguments(argv) {
  const link = argv.find(value => typeof value === 'string' && value.startsWith('cs2farm://'));
  if (!link) return;
  try { pendingEnrollment = parseEnrollment(link); showManagement(); management?.webContents.send('farm:enrollment', pendingEnrollment); }
  catch (error) { showError(error); }
}
function state() {
  return { ...supervisor.snapshot(), configured: !!config, config, product: { name: app.getName(), version: app.getVersion(), defaultMode: product.defaultMode, agentOnly: !!product.agentOnly }, system: supervisor.check(config?.mode || product.defaultMode || 'server'), update: updateResult };
}
function createIpc() {
  ipcMain.handle('farm:open-system', event => {
    if (!dashboard || event.sender !== dashboard.webContents || new URL(event.senderFrame?.url || 'about:blank').origin !== ORIGIN || config?.mode !== 'server') throw new Error('Untrusted dashboard IPC sender');
    showManagement();
  });
  ipcMain.handle('farm:management', async (event, action, payload) => {
    if (!managementSender(event)) throw new Error('Untrusted management IPC sender');
    const allowed = ['status', 'configure', 'open-dashboard', 'restart', 'backup', 'restore', 'preferences', 'check-updates', 'install-update', 'network-info', 'network-configure', 'notifications-info', 'notifications-configure', 'agent-applications-info', 'choose-agent-application', 'agent-applications-configure', 'diagnose', 'export-diagnostics', 'service-status', 'service-control', 'quit'];
    if (!allowed.includes(action)) throw new Error('Unknown desktop action');
    try {
      switch (action) {
        case 'status': return state();
        case 'configure': return await guarded(async () => {
          if (config) throw new Error('Первичная настройка уже выполнена.');
          const input = validateSetup(payload);
          if (product.agentOnly && input.mode !== 'agent') throw new Error('Этот установщик содержит только агент.');
          if (input.mode === 'agent' && input.serviceMode) {
            await serviceAdmin(supervisor, 'install', { server_url: input.serverUrl, token: input.token, ...(input.ca_certificate ? { ca_certificate: input.ca_certificate } : {}) });
            if (input.ca_certificate) fs.writeFileSync(path.join(dataDir, 'trusted-server-ca.pem'), input.ca_certificate, { mode: 0o600 });
          } else if (input.mode !== 'remote') await supervisor.configure(input);
          else {
            const response = await fetch(httpsUrl(input.serverUrl) + '/health/ready', { redirect: 'error', signal: AbortSignal.timeout(15000) });
            if (!response.ok) throw new Error(`Сервер не подтвердил готовность: HTTP ${response.status}`);
            const health = await response.json();
            if (health.status !== 'ready') throw new Error('Сервер не подтвердил готовность API.');
          }
          config = saveConfig(dataDir, input); setAutoStart(config.autoStart);
          if (config.mode !== 'remote' && !config.serviceMode) await supervisor.start(config.mode);
          if (config.serviceMode) await refreshService();
          return state();
        });
        case 'open-dashboard': await openDashboard(); return state();
        case 'restart': return await guarded(async () => {
          if (!config || config.mode === 'remote') throw new Error('Удалённый сервер перезапускается его администратором.');
          if (config.serviceMode) { await serviceAdmin(supervisor, 'stop'); await serviceAdmin(supervisor, 'start'); await refreshService(); }
          else await supervisor.restart(); notify(app.getName(), 'Локальные компоненты перезапущены.'); return state();
        });
        case 'backup': return await guarded(async () => {
          if (config?.mode !== 'server') throw new Error('Резервные копии доступны на главном компьютере.');
          const result = await dialog.showSaveDialog(management, { title: 'Сохранить резервную копию', defaultPath: `CS2-Farm-${new Date().toISOString().slice(0, 10)}.farmbackup`, filters: [{ name: 'Farm backup', extensions: ['farmbackup'] }] });
          if (result.canceled) return { cancelled: true };
          await supervisor.runCommand(['backup', '--data-dir', dataDir, '--output', result.filePath], null, 120000);
          return { saved: result.filePath };
        });
        case 'restore': return await guarded(async () => {
          if (config?.mode !== 'server') throw new Error('Восстановление доступно на главном компьютере.');
          const pick = await dialog.showOpenDialog(management, { title: 'Выберите резервную копию', properties: ['openFile'], filters: [{ name: 'Farm backup', extensions: ['farmbackup'] }] });
          if (pick.canceled) return { cancelled: true };
          const confirm = await dialog.showMessageBox(management, { type: 'warning', buttons: ['Отмена', 'Восстановить'], defaultId: 0, cancelId: 0, message: 'Заменить локальную базу резервной копией?', detail: 'Компоненты будут остановлены. Восстановление заменит данные текущей установки.' });
          if (confirm.response !== 1) return { cancelled: true };
          await supervisor.stop();
          try { await supervisor.runCommand(['restore', '--data-dir', dataDir, '--input', pick.filePaths[0]], null, 120000); }
          finally { await supervisor.start('server'); }
          dashboard?.reload(); return { restored: true };
        });
        case 'preferences': {
          if (!config) throw new Error('Сначала завершите настройку.');
          config = saveConfig(dataDir, { ...config, autoStart: !!payload?.autoStart, closeToTray: payload?.closeToTray !== false });
          setAutoStart(config.autoStart); return state();
        }
        case 'network-info': {
          if (config?.mode !== 'server') throw new Error('Сетевой доступ настраивается на главном компьютере.');
          const output = await supervisor.runCommand(['network-info', '--data-dir', dataDir]); return JSON.parse(output);
        }
        case 'network-configure': return await guarded(async () => {
          if (config?.mode !== 'server') throw new Error('Сетевой доступ настраивается на главном компьютере.');
          if (payload?.enabled && payload.confirmed !== true) throw new Error('Подтвердите включение защищённого сетевого доступа.');
          const input = { enabled: !!payload.enabled, bind_host: String(payload.bind_host || ''), advertised_host: String(payload.advertised_host || ''), port: Number(payload.port || 17832) };
          const output = await supervisor.runCommand(['network-configure', '--data-dir', dataDir], input);
          await supervisor.restart(); return JSON.parse(output);
        });
        case 'notifications-info': {
          if (config?.mode !== 'server') throw new Error('Уведомления настраиваются на главном компьютере.');
          return JSON.parse(await supervisor.runCommand(['notifications-info', '--data-dir', dataDir]));
        }
        case 'notifications-configure': return await guarded(async () => {
          if (config?.mode !== 'server') throw new Error('Уведомления настраиваются на главном компьютере.');
          const input = { token: String(payload?.token || ''), chat_id: String(payload?.chat_id || ''), min_severity: String(payload?.min_severity || 'warning') };
          await supervisor.runCommand(['notifications-configure', '--data-dir', dataDir], input);
          await supervisor.restart(); return JSON.parse(await supervisor.runCommand(['notifications-info', '--data-dir', dataDir]));
        });
        case 'agent-applications-info': {
          if (config?.mode !== 'agent') throw new Error('Приложения настраиваются на компьютере агента.');
          if (config.serviceMode) return await guarded(() => serviceAdmin(supervisor, 'applications-info'));
          return JSON.parse(await supervisor.runCommand(['agent-applications-info', '--data-dir', dataDir]));
        }
        case 'choose-agent-application': {
          if (config?.mode !== 'agent' || !['steam', 'cs2'].includes(payload?.kind)) throw new Error('Выберите Steam или CS2 на компьютере агента.');
          const expected = payload.kind + '.exe';
          const result = await dialog.showOpenDialog(management, { title: `Выберите ${expected}`, properties: ['openFile'], filters: [{ name: expected, extensions: ['exe'] }] });
          if (result.canceled) return { cancelled: true };
          const filename = result.filePaths[0];
          if (path.basename(filename).toLowerCase() !== expected) throw new Error(`Разрешён только ${expected}.`);
          return { path: filename };
        }
        case 'agent-applications-configure': return await guarded(async () => {
          if (config?.mode !== 'agent') throw new Error('Приложения настраиваются на компьютере агента.');
          const input = { steam_path: String(payload?.steam_path || ''), cs2_path: String(payload?.cs2_path || '') };
          if (config.serviceMode) return await serviceAdmin(supervisor, 'applications', input);
          await supervisor.stop();
          try { return JSON.parse(await supervisor.runCommand(['agent-applications-configure', '--data-dir', dataDir], input)); }
          finally { await supervisor.start('agent'); }
        });
        case 'service-status': return await refreshService();
        case 'service-control': return await guarded(async () => {
          if (config?.mode !== 'agent' || !config.serviceMode || !['start', 'stop', 'remove', 'refresh', 'reset'].includes(payload?.action)) throw new Error('Недопустимое действие службы.');
          const answer = await dialog.showMessageBox(management, { type: 'warning', message: payload.action === 'reset' ? 'Отвязать агент и удалить его защищённые данные?' : payload.action === 'refresh' ? 'Обновить файлы службы из установленного приложения?' : payload.action === 'remove' ? 'Остановить и удалить службу Windows?' : 'Изменить состояние службы Windows?', detail: payload.action === 'reset' ? 'Управляемые процессы будут остановлены, служба и локальные учётные данные удалены. Отзовите прежний Node в главной панели. Затем понадобится новый одноразовый код.' : 'Windows запросит права администратора. Защищённые данные подключения сохраняются. Остановка службы завершает запущенные ею процессы. Обновляйте службу после завершения активных заданий.', buttons: ['Отмена', 'Продолжить'], defaultId: 0, cancelId: 0 });
          if (answer.response !== 1) return { cancelled: true };
          await serviceAdmin(supervisor, payload.action);
          if (payload.action === 'reset') {
            fs.rmSync(path.join(dataDir, 'desktop.json'), { force: true }); config = null;
            setAutoStart(false); management.reload(); return { reset: true };
          }
          return await refreshService();
        });
        case 'diagnose': return await guarded(async () => {
          lastDiagnostics = await diagnose(supervisor, config, app.getVersion());
          return { ...state(), diagnostics: lastDiagnostics };
        });
        case 'export-diagnostics': return await guarded(async () => {
          if (!lastDiagnostics) lastDiagnostics = await diagnose(supervisor, config, app.getVersion());
          const result = await dialog.showSaveDialog(management, { title: 'Сохранить диагностику', defaultPath: 'CS2-Farm-Diagnostics.json', filters: [{ name: 'Диагностика', extensions: ['json'] }] });
          if (result.canceled) return { cancelled: true };
          // Deliberately exclude config, credentials, request payloads and database values.
          fs.writeFileSync(result.filePath, JSON.stringify(lastDiagnostics, null, 2), { mode: 0o600 });
          return { saved: result.filePath };
        });
        case 'check-updates': updateResult = await checkUpdates(product, app.getVersion()); return updateResult;
        case 'install-update': return await guarded(async () => {
          if (!updateResult?.available) throw new Error('Нет проверенного доступного обновления.');
          const response = await dialog.showMessageBox(management, { type: 'question', message: `Скачать и запустить установщик ${updateResult.version}?`, detail: 'Подпись издателя и хеш загрузки будут проверены. Windows может запросить согласие администратора.', buttons: ['Отмена', 'Установить'], cancelId: 0, defaultId: 0 });
          if (response.response !== 1) return { cancelled: true };
          const filename = await downloadUpdate(updateResult.manifest, product, dataDir);
          await supervisor.stop(); const error = await shell.openPath(filename);
          if (error) throw new Error(error);
          app.quit(); return { started: true };
        });
        case 'quit': app.quit(); return { quitting: true };
      }
    } catch (error) { supervisor.log('operation', error.message); const issue = explain(error); throw new Error(`${issue.title} [${issue.code}]\n${issue.detail}\nЧто делать: ${issue.remedy}`); }
  });
}
async function refreshService() {
  const value = await serviceStatus(supervisor);
  supervisor.mode = 'agent';
  supervisor.state('agent', value.status === 'running' ? 'up' : value.status === 'stopped' ? 'stopped' : value.installed ? 'starting' : 'failed', `Windows Service: ${value.status}; автозапуск ${value.automatic_start ? 'включён' : 'не подтверждён'}. Подключение проверяйте в Nodes.`);
  return { ...state(), windowsService: value };
}
async function smokeTest() {
  const report = { platform: process.platform, packaged: app.isPackaged, checkedAt: new Date().toISOString(), checks: [], passed: false };
  const check = name => report.checks.push({ name, passed: true });
  try {
    if (!smokeResult) throw new Error('--result absolute path is required');
    if (process.argv.includes('--smoke-resume')) {
      const before = JSON.parse(await supervisor.runCommand(['data-check', '--data-dir', dataDir]));
      if (!before.ok || before.databases['farm.sqlite3'].tables.steam_accounts.rows < 1) throw new Error('No persisted account database to resume');
      await supervisor.start('server'); check('existing_database_backend_queue_dashboard_start');
      const win = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } });
      await win.loadURL(ORIGIN); check('existing_database_dashboard_browser_loaded'); win.destroy();
      report.passed = true; return;
    }
    const password = 'Smoke-' + crypto.randomBytes(20).toString('hex');
    await supervisor.configure({ mode: 'server', username: 'desktop-smoke', password }); check('packaged_runtime_configures_admin_and_database');
    await supervisor.start('server'); check('supervisor_backend_and_dashboard_healthy');
    const win = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } });
    await win.loadURL(ORIGIN); check('dashboard_browser_loaded');
    const authResponse = await fetch(`${ORIGIN}/api/v1/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ username: 'desktop-smoke', password }) });
    if (!authResponse.ok) throw new Error(`Login failed ${authResponse.status}`);
    const auth = await authResponse.json(); const cookie = authResponse.headers.get('set-cookie')?.split(';')[0];
    const headers = { 'Content-Type': 'application/json', Origin: ORIGIN, 'X-CSRF-Token': auth.csrf_token, Cookie: cookie };
    const created = await fetch(`${ORIGIN}/api/v1/accounts`, { method: 'POST', headers, body: JSON.stringify({ steam_id: '76561199999888882', notes: 'Explicit isolated installer smoke test' }) });
    if (!created.ok) throw new Error(`Account create HTTP ${created.status}`);
    const account = await created.json(); check('dashboard_api_database_account_write');
    await supervisor.restart(); check('supervisor_clean_restart');
    const stored = await fetch(`${ORIGIN}/api/v1/accounts/${account.id}`, { headers: { Cookie: cookie } });
    if (!stored.ok || (await stored.json()).id !== account.id) throw new Error('Persisted account not found after restart');
    check('database_persists_after_restart');
    const protection = await fetch(`${ORIGIN}/api/v1/protection/overview`, { headers: { Cookie: cookie } });
    if (!protection.ok) throw new Error(`Protection HTTP ${protection.status}`);
    const security = await protection.json();
    if (security.prediction_available !== false || security.accounts < 1 || !security.risk_levels) throw new Error('Protection module did not return sourced risk status');
    const protectionWindow = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } });
    await protectionWindow.loadURL(ORIGIN + '/protection'); protectionWindow.destroy();
    check('packaged_protection_api_and_page');
    const engineResponse = await fetch(`${ORIGIN}/api/v1/protection/safety/status`, { headers: { Cookie: cookie } });
    if (!engineResponse.ok) throw new Error(`Safety engine HTTP ${engineResponse.status}`);
    const engine = await engineResponse.json();
    if (engine.version !== '2.0' || !engine.manager_integrity || engine.manager_integrity.status === 'FAILED' || engine.manager_integrity.report.files_checked < 1) throw new Error('Packaged safety engine or actual Manager component checks failed');
    const health = await fetch(`${ORIGIN}/api/v1/protection/health/accounts`, { headers: { Cookie: cookie } });
    if (!health.ok || !(await health.json()).items.some(value => value.id === account.id && value.health?.source === 'protection_engine_v2_derived')) throw new Error('Account Health API missing');
    check('packaged_safety_engine_v2_health_and_component_checks');
    const diagnostic = await diagnose(supervisor, { mode: 'server' }, app.getVersion());
    if (!diagnostic.passed) throw new Error('Packaged GUI diagnosis failed: ' + JSON.stringify(diagnostic.checks));
    check('graphical_self_diagnostics_all_automatic_checks');
    win.destroy(); report.passed = true;
  } catch (error) { report.error = sanitize(error.message); }
  finally {
    await supervisor.stop();
    report.consoleWindows = 'Requires clean Windows visual verification; child processes use windowsHide';
    report.dataDirectory = dataDir;
    if (smokeResult) { fs.mkdirSync(path.dirname(smokeResult), { recursive: true }); fs.writeFileSync(smokeResult, JSON.stringify(report, null, 2)); }
    app.exit(report.passed ? 0 : 1);
  }
}
app.on('second-instance', (_event, argv) => { if(argv.includes('--shutdown')) { app.quit(); return; } enrollmentFromArguments(argv); showManagement(); });
app.on('open-url', (event, url) => { event.preventDefault(); try { pendingEnrollment = parseEnrollment(url); if (app.isReady()) showManagement(); } catch (error) { if (app.isReady()) showError(error); } });
app.on('activate', () => showManagement());
app.on('window-all-closed', () => { if (!tray && !quitting) app.quit(); });
app.on('before-quit', event => {
  if (quitting) return;
  event.preventDefault(); quitting = true;
  Promise.resolve(supervisor?.stop()).finally(() => { tray?.destroy(); app.quit(); });
});
if (requestLock) app.whenReady().then(async () => {
  app.setAsDefaultProtocolClient('cs2farm');
  const paths = resolvePaths({ packaged: app.isPackaged, resourcesPath: process.resourcesPath, repositoryRoot: path.resolve(__dirname, '../..'), dataDir });
  supervisor = new Supervisor(paths);
  supervisor.on('status', value => { if (management && !management.isDestroyed()) management.webContents.send('farm:status', { ...value, configured: !!config, config }); });
  supervisor.on('failure', message => { notify('CS2 Farm Manager — ошибка', message); showManagement(); });
  if (process.argv.includes('--remove-service')) {
    try { const installedConfig = readConfig(dataDir); if (installedConfig?.serviceMode && (await serviceStatus(supervisor)).installed) await serviceAdmin(supervisor, 'remove'); app.exit(0); }
    catch (error) { dialog.showErrorBox('Не удалось удалить службу', sanitize(error.message)); app.exit(1); }
    return;
  }
  if (smokeMode) { await smokeTest(); return; }
  installTray(); createIpc(); showManagement();
  try {
    config = readConfig(dataDir); setAutoStart(!!config?.autoStart);
    enrollmentFromArguments(process.argv);
    if (config && config.mode !== 'remote' && !config.serviceMode) await supervisor.start(config.mode);
    if (config?.serviceMode) {
      await refreshService();
      const timer = setInterval(() => refreshService().catch(showError), 10000); timer.unref();
    }
    if (config && config.mode !== 'agent') await openDashboard();
  } catch (error) { showError(error); }
}).catch(error => { if (smokeResult) fs.writeFileSync(smokeResult, JSON.stringify({ passed: false, error: sanitize(error.message) })); else dialog.showErrorBox('CS2 Farm Manager — ошибка запуска', sanitize(error.message)); app.exit(1); });
