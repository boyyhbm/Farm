'use strict';
const $ = id => document.getElementById(id);
const bridge = window.farmSetup;
let state = {}; let step = 1; let setupActive = true; let busy = false; let fingerprint = null;
const statusText = { up: 'Работает', starting: 'Запуск', stopped: 'Остановлен', failed: 'Ошибка', degraded: 'Проблема', unknown: 'Не подтверждено' };
function error(message) { const target = setupActive ? $('wizard-error') : $('management-error'); target.textContent = message; target.hidden = !message; }
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function mode() { return document.querySelector('input[name=mode]:checked').value; }
function disable(value) { busy = value; document.querySelectorAll('button').forEach(button => { button.disabled = value; }); }
async function action(name, payload) {
  error(''); disable(true);
  try { return await bridge.invoke(name, payload); }
  catch (err) { error(err.message.replace(/^Error invoking remote method '[^']+': Error: /, '')); throw err; }
  finally { disable(false); }
}
function applyStep() {
  document.querySelectorAll('[data-step]').forEach(element => { element.hidden = Number(element.dataset.step) !== step; });
  document.querySelectorAll('#steps li').forEach((element, index) => { element.className = index + 1 === step ? 'active' : index + 1 < step ? 'done' : ''; });
  $('back').hidden = step === 1 || step >= 4;
  $('next').hidden = step === 4;
  $('next').textContent = step === 6 ? (mode() === 'agent' ? 'Открыть управление агентом' : 'Открыть Dashboard') : step === 3 ? 'Настроить автоматически →' : 'Продолжить →';
  $('server-fields').hidden = mode() !== 'server';
  $('remote-fields').hidden = mode() === 'server';
  $('agent-fields').hidden = mode() !== 'agent';
  $('credentials-title').textContent = mode() === 'server' ? 'Создайте администратора' : mode() === 'agent' ? 'Подключите компьютер' : 'Адрес существующего сервера';
}
function renderStatus(value) {
  state = { ...state, ...value };
  if (value.error) error(value.error);
  $('logs').textContent = (state.logs || []).map(entry => `${entry.time} [${entry.service}] ${entry.message}`).join('\n') || 'Ошибки и события пока не зарегистрированы.';
  $('logs').scrollTop = $('logs').scrollHeight;
  const progress = Object.entries(state.services || {}).map(([name, service]) => `${name}: ${statusText[service.status] || service.status}${service.detail ? ' — ' + service.detail : ''}`).join('\n');
  $('setup-progress').textContent = progress || 'Подготовка конфигурации…';
  if (setupActive) return;
  $('service-cards').replaceChildren();
  const services = state.mode === 'agent' ? ['agent'] : ['backend', 'database', 'queue', 'dashboard'];
  const labels = { backend: 'Backend API', database: 'База данных', queue: 'Очередь и scheduler', dashboard: 'Dashboard', agent: 'Farm Agent' };
  for (const name of services) {
    const service = state.services?.[name] || { status: state.config?.mode === 'remote' ? 'unknown' : 'stopped', detail: state.config?.mode === 'remote' ? 'Удалённый сервер: проверяйте состояние в панели.' : '' };
    const card = document.createElement('div'); card.className = 'service';
    const title = document.createElement('h3'); title.textContent = labels[name];
    const status = document.createElement('strong'); status.textContent = statusText[service.status] || service.status; status.className = 'status-' + service.status;
    const description = document.createElement('small'); description.textContent = service.detail || (name === 'database' ? state.database : name === 'queue' ? state.queue : service.pid ? `PID ${service.pid}` : '');
    card.append(title, status, description); $('service-cards').append(card);
  }
  $('data-directory').textContent = 'Данные: ' + (state.dataDirectory || 'неизвестно');
  $('network-panel').hidden = state.config?.mode !== 'server';
  $('notifications-panel').hidden = state.config?.mode !== 'server';
  $('agent-applications-panel').hidden = state.config?.mode !== 'agent' || !!state.config?.serviceMode;
  $('windows-service-panel').hidden = !state.config?.serviceMode;
  if (value.windowsService) $('windows-service-state').textContent = `Состояние: ${value.windowsService.status}. Автозапуск: ${value.windowsService.automatic_start ? 'да' : 'не подтверждён'}.`;
  $('backup').disabled = busy || state.config?.mode !== 'server';
  $('restore').disabled = busy || state.config?.mode !== 'server';
  $('restart').disabled = busy || state.config?.mode === 'remote';
  $('open-dashboard').hidden = state.config?.mode === 'agent';
}
function showManagement() {
  setupActive = false; $('wizard').hidden = true; $('management').hidden = false;
  $('autostart').checked = !!state.config?.autoStart;
  $('close-tray').checked = state.config?.closeToTray !== false;
  renderStatus(state);
}
async function setup() {
  const input = { mode: mode(), autoStart: $('setup-autostart').checked, closeToTray: $('setup-tray').checked };
  if (input.mode === 'server') {
    if ($('password').value !== $('password-repeat').value) { error('Пароли не совпадают.'); return; }
    input.username = $('username').value.trim(); input.password = $('password').value;
  } else {
    input.serverUrl = $('server-url').value.trim();
    if (input.mode === 'agent') { input.serviceMode = $('setup-service').checked; input.token = $('enrollment-token').value.trim(); input.fingerprint = fingerprint; input.ca_certificate = $('ca-certificate').value.trim() || null; }
  }
  step = 4; applyStep();
  try {
    const result = await action('configure', input);
    $('password').value = ''; $('password-repeat').value = ''; $('enrollment-token').value = ''; $('enrollment-link').value = '';
    input.password = ''; input.token = '';
    state = { ...state, ...result }; step = 5;
    $('connection-result').textContent = input.mode === 'server' ? 'Backend, база, очередь и Dashboard ответили на проверку готовности.' : input.mode === 'agent' ? 'Агент зарегистрирован; фоновый процесс запущен.' : 'Защищённый HTTPS-сервер подтвердил готовность API. Вход выполняется в Dashboard.';
    $('connection-note').textContent = input.mode === 'agent' ? 'Проверьте реальный статус online и телеметрию этого узла в Node Manager главного компьютера. Факт запуска процесса не подменяет heartbeat.' : input.mode === 'server' ? 'Для дополнительных ПК включите защищённый сетевой доступ в System Management и создайте одноразовый код в Nodes.' : 'Никакие локальные серверные службы в этом режиме не запускаются.';
    $('ready-message').textContent = input.mode === 'agent' ? 'Этот компьютер будет передавать телеметрию и принимать разрешённые задания. Авторизация Steam выполняется вручную в официальном клиенте.' : 'Добавьте Steam-аккаунты и подключите компьютеры в панели. Авторизация Steam выполняется вручную на каждом ПК.';
    applyStep();
  } catch { if (state.configured) showManagement(); else { step = 3; applyStep(); } }
}
$('next').addEventListener('click', async () => {
  if (busy) return;
  if (step === 3) { await setup(); return; }
  if (step === 6) { showManagement(); if (state.config?.mode !== 'agent') await action('open-dashboard').catch(() => {}); return; }
  error(''); step++; applyStep();
});
$('back').addEventListener('click', () => { if (step > 1) { step--; error(''); applyStep(); } });
$('setup-form').addEventListener('submit', event => { event.preventDefault(); });
document.querySelectorAll('input[name=mode]').forEach(input => input.addEventListener('change', applyStep));
function applyEnrollment(value) {
  if (state.configured) { error('Приложение уже настроено. Для нового подключения используйте отдельную установку агента или восстановление конфигурации.'); return; }
  document.querySelector('input[name=mode][value=agent]').checked = true;
  $('server-url').value = value.serverUrl || ''; $('enrollment-token').value = value.token || ''; fingerprint = value.fingerprint;
  step = 3; applyStep();
}
$('parse-link').addEventListener('click', () => {
  try {
    const url = new URL($('enrollment-link').value.trim());
    if (url.protocol !== 'cs2farm:' || url.hostname !== 'enroll') throw new Error('Используйте ссылку cs2farm://enroll из панели.');
    applyEnrollment({ serverUrl: url.searchParams.get('server'), token: url.searchParams.get('token'), fingerprint: url.searchParams.get('fingerprint') });
  } catch (err) { error(err.message); }
});
$('open-dashboard').addEventListener('click', () => action('open-dashboard').catch(() => {}));
$('refresh-status').addEventListener('click', () => action('status').then(renderStatus).catch(() => {}));
$('diagnose').addEventListener('click', () => action('diagnose').then(value => { renderStatus(value); $('diagnostic-results').replaceChildren();
  for (const check of value.diagnostics.checks) {
    const card = document.createElement('div'); card.className = check.status === 'failed' ? 'error' : 'card';
    const title = document.createElement('strong'); title.textContent = `${check.status === 'passed' ? '✓' : check.status === 'manual' ? '○' : '✗'} ${check.title}${check.code ? ' [' + check.code + ']' : ''}`;
    const detail = document.createElement('p'); detail.textContent = check.detail;
    card.append(title, detail);
    if (check.remedy) { const remedy = document.createElement('p'); remedy.textContent = 'Что делать: ' + check.remedy; card.append(remedy); }
    $('diagnostic-results').append(card);
  }
  notice(value.diagnostics.passed ? 'Автоматические проверки пройдены. Ручные проверки отмечены отдельно.' : 'Обнаружены проблемы. Ниже указаны причины и действия.'); }).catch(() => {}));
$('restart').addEventListener('click', () => { if (confirm('Перезапустить локальные компоненты? Открытые соединения временно прервутся.')) action('restart').then(renderStatus).catch(() => {}); });
$('backup').addEventListener('click', () => action('backup').then(result => { if (!result.cancelled) notice('Резервная копия сохранена: ' + result.saved); }).catch(() => {}));
$('restore').addEventListener('click', () => action('restore').then(result => { if (!result.cancelled) notice('Данные восстановлены. Компоненты перезапущены.'); }).catch(() => {}));
$('save-preferences').addEventListener('click', () => action('preferences', { autoStart: $('autostart').checked, closeToTray: $('close-tray').checked }).then(value => { renderStatus(value); notice('Настройки сохранены.'); }).catch(() => {}));
$('check-updates').addEventListener('click', () => action('check-updates').then(result => { $('update-status').textContent = result.message; $('install-update').hidden = !result.available; }).catch(() => {}));
$('install-update').addEventListener('click', () => action('install-update').catch(() => {}));
function renderNetwork(result) {
  $('network-enabled').checked = !!result.enabled;
  $('bind-host').value = result.bind_host || '';
  $('advertised-host').value = result.advertised_host || '';
  $('network-port').value = result.port || 17832;
  $('network-output').hidden = !result.enabled;
  $('network-url').value = result.url || '';
  $('network-ca').value = result.ca_certificate || '';
}
$('network-refresh').addEventListener('click', () => action('network-info').then(renderNetwork).catch(() => {}));
$('network-save').addEventListener('click', () => action('network-configure', { enabled: $('network-enabled').checked, bind_host: $('bind-host').value.trim(), advertised_host: $('advertised-host').value.trim(), port: Number($('network-port').value), confirmed: $('network-confirm').checked }).then(value => { renderNetwork(value); notice('Настройки HTTPS применены. Правила Windows Firewall не изменялись.'); }).catch(() => {}));
$('quit').addEventListener('click', () => action('quit').catch(() => {}));
bridge.onStatus(renderStatus); bridge.onEnrollment(applyEnrollment);
bridge.invoke('status').then(value => {
  state = value;
  $('setup-service').checked = value.system.platform === 'win32';
  $('setup-service').disabled = value.system.platform !== 'win32';
  $('version').textContent = `${value.product.name} · ${value.product.version}`;
  $('system-check').textContent = `${value.system.message}\n${value.system.platform} ${value.system.release} · ${value.system.arch}\nRAM: ${(value.system.memoryBytes / 1024 ** 3).toFixed(1)} GB${value.system.diskFreeBytes === null ? '' : '\nСвободно на диске: ' + (value.system.diskFreeBytes / 1024 ** 3).toFixed(1) + ' GB'}${value.system.platform === 'win32' ? '' : '\nСреда разработки: установка Windows здесь не проверяется.'}`;
  for (const iface of value.system.interfaces || []) { const option = document.createElement('option'); option.value = iface.address; option.label = iface.name; $('local-interfaces').append(option); }
  const preferredInterface = value.system.interfaces?.[0]?.address;
  if (preferredInterface) { $('bind-host').value = preferredInterface; $('advertised-host').value = preferredInterface; }
  const selected = document.querySelector(`input[name=mode][value=${value.product.defaultMode || 'server'}]`);
  if (selected) selected.checked = true;
  if (value.product.agentOnly) document.querySelectorAll('input[name=mode]').forEach(input => { input.disabled = input.value !== 'agent'; });
  if (value.configured) showManagement(); else applyStep();
  renderStatus(value);
}).catch(err => error(err.message));

function renderTelegram(value) { $('telegram-state').textContent = value.configured ? 'Уведомления настроены.' : 'Telegram не настроен.'; $('telegram-severity').value = value.min_severity || 'warning'; }
$('telegram-refresh').addEventListener('click', () => action('notifications-info').then(renderTelegram).catch(() => {}));
$('telegram-save').addEventListener('click', () => {
  if (!confirm('Сохранить настройки Telegram и перезапустить Backend? Пустой токен отключает уведомления.')) return;
  action('notifications-configure', { token: $('telegram-token').value.trim(), chat_id: $('telegram-chat').value.trim(), min_severity: $('telegram-severity').value }).then(value => { $('telegram-token').value = ''; $('telegram-chat').value = ''; renderTelegram(value); notice('Настройки уведомлений сохранены.'); }).catch(() => {});
});

function renderAgentApplications(value) {
  $('steam-path').value = value.steam_path || ''; $('cs2-path').value = value.cs2_path || '';
  $('agent-applications-state').textContent = `Steam: ${value.steam_available ? 'найден' : 'не настроен'}; CS2: ${value.cs2_available ? 'найден' : 'не настроен'}.`;
}
$('agent-applications-refresh').addEventListener('click', () => action('agent-applications-info').then(renderAgentApplications).catch(() => {}));
for (const kind of ['steam', 'cs2']) $('choose-' + kind).addEventListener('click', () => action('choose-agent-application', { kind }).then(value => { if (!value.cancelled) $(kind === 'steam' ? 'steam-path' : 'cs2-path').value = value.path; }).catch(() => {}));
$('agent-applications-save').addEventListener('click', () => { if (confirm('Сохранить разрешённые приложения и перезапустить агент?')) action('agent-applications-configure', { steam_path: $('steam-path').value, cs2_path: $('cs2-path').value }).then(value => { renderAgentApplications(value); notice('Разрешённые приложения сохранены.'); }).catch(() => {}); });

$('bind-host').addEventListener('change', () => { if (!$('advertised-host').value) $('advertised-host').value = $('bind-host').value; });

$('export-diagnostics').addEventListener('click', () => action('export-diagnostics').then(result => { if (!result.cancelled) notice('Отчёт сохранён: ' + result.saved); }).catch(() => {}));

$('service-refresh').addEventListener('click', () => action('service-status').then(renderStatus).catch(() => {}));
for (const operation of ['start', 'stop', 'remove']) $('service-' + operation).addEventListener('click', () => action('service-control', { action: operation }).then(value => { if (!value.cancelled) renderStatus(value); }).catch(() => {}));
