'use strict';
// Browser test of local HTML wizard with an explicitly mocked IPC adapter only.
// Does not claim Electron, native dialogs, real setup or Windows installation coverage.
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const { chromium } = require('../../dashboard/node_modules/@playwright/test');
(async () => {
  const root = path.resolve(__dirname, '../renderer');
  const server = http.createServer((req, res) => { const name = req.url === '/' ? 'index.html' : req.url.slice(1); if (!['index.html', 'renderer.js', 'style.css', 'tray.png'].includes(name)) { res.statusCode = 404; res.end(); return; } res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.png') ? 'image/png' : 'text/html'); res.end(fs.readFileSync(path.join(root, name))); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
  const errors = []; page.on('pageerror', value => errors.push(value.message));
  await page.addInitScript(() => {
    window.__calls = [];
    const status = { configured: false, config: null, services: {}, logs: [], product: { name: 'CS2 Farm Manager', version: '0.2.0', defaultMode: 'server' }, system: { ok: true, message: 'Explicit test adapter: components available', platform: 'linux', release: 'test', arch: 'x64', memoryBytes: 8 * 1024 ** 3, diskFreeBytes: 5 * 1024 ** 3 } };
    window.farmSetup = { onStatus: () => () => {}, onEnrollment: () => () => {}, invoke: async (action, payload) => {
      window.__calls.push({ action, ...(action === 'configure' ? { mode: payload.mode, username: payload.username, passwordWasProvided: payload.password.length >= 14 } : {}) });
      if (action === 'configure') return { ...status, configured: true, config: { mode: 'server', autoStart: true, closeToTray: true }, services: { backend: { status: 'up' }, database: { status: 'up' }, queue: { status: 'up' }, dashboard: { status: 'up' } } };
      return status;
    } };
  });
  try {
    await page.goto('http://127.0.0.1:' + server.address().port + '/');
    await page.getByText('Explicit test adapter: components available', { exact: false }).waitFor();
    await page.locator('#next').click(); await page.locator('#next').click();
    await page.locator('#username').fill('test-admin'); await page.locator('#password').fill('wizard-test-password-1234'); await page.locator('#password-repeat').fill('wizard-test-password-1234');
    await page.locator('#next').click(); await page.locator('[data-step="5"]').waitFor({ state: 'visible' });
    if (await page.locator('#password').inputValue()) throw new Error('Password retained after successful IPC setup');
    await page.locator('#next').click(); await page.locator('[data-step="6"]').waitFor({ state: 'visible' }); await page.locator('#next').click();
    await page.locator('#management').waitFor({ state: 'visible' });
    const calls = await page.evaluate(() => window.__calls);
    if (!calls.some(value => value.action === 'configure' && value.passwordWasProvided) || !calls.some(value => value.action === 'open-dashboard')) throw new Error('Wizard did not invoke setup/open-dashboard');
    if (errors.length) throw new Error(errors.join('; '));
    const directory = path.join(__dirname, 'results'); fs.mkdirSync(directory, { recursive: true });
    await page.screenshot({ path: path.join(directory, 'system-management.png'), fullPage: true });
    fs.writeFileSync(path.join(directory, 'wizard-browser.json'), JSON.stringify({ checkedAt: new Date().toISOString(), scope: 'Chromium local HTML with explicitly mocked IPC; not Electron or Windows installation', six_step_flow: true, password_cleared_after_setup: true, pageErrors: errors, passed: true }, null, 2));
    console.log('PASS six-step wizard HTML, IPC invocation, password cleanup, System Management rendering (mock IPC only)');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
