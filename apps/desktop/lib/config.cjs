'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function httpsUrl(raw) {
  if (typeof raw !== 'string' || raw.length > 2048) throw new Error('Введите HTTPS-адрес сервера.');
  const parsed = new URL(raw.trim());
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash)
    throw new Error('Разрешён только HTTPS-адрес без паролей и параметров.');
  return parsed.toString().replace(/\/$/, '');
}
function parseEnrollment(raw) {
  if (typeof raw !== 'string' || raw.length > 4096) throw new Error('Недопустимая ссылка подключения.');
  const parsed = new URL(raw);
  if (parsed.protocol !== 'cs2farm:' || parsed.hostname !== 'enroll') throw new Error('Неверная ссылка подключения.');
  const serverUrl = httpsUrl(parsed.searchParams.get('server'));
  const token = parsed.searchParams.get('token');
  if (!token || !/^[A-Za-z0-9_-]{20,200}$/.test(token)) throw new Error('Недопустимый одноразовый код.');
  const fingerprint = parsed.searchParams.get('fingerprint');
  if (fingerprint && !/^[a-fA-F0-9]{64}$/.test(fingerprint)) throw new Error('Недопустимый отпечаток сертификата.');
  return { serverUrl, token, fingerprint: fingerprint || null };
}
function validateSetup(input) {
  if (!input || typeof input !== 'object' || !['server', 'agent', 'remote'].includes(input.mode)) throw new Error('Выберите режим приложения.');
  const output = { mode: input.mode, autoStart: !!input.autoStart, closeToTray: input.closeToTray !== false, version: 1 };
  if (input.mode === 'server') {
    if (typeof input.username !== 'string' || !/^[\w.@-]{1,100}$/.test(input.username)) throw new Error('Имя администратора: 1–100 латинских букв, цифр или . @ _ -');
    if (typeof input.password !== 'string' || input.password.length < 14 || input.password.length > 1024) throw new Error('Пароль должен содержать не менее 14 символов.');
    output.username = input.username;
    output.password = input.password;
  } else {
    output.serverUrl = httpsUrl(input.serverUrl);
    if (input.mode === 'agent') {
      if (typeof input.token !== 'string' || !/^[A-Za-z0-9_-]{20,200}$/.test(input.token)) throw new Error('Введите одноразовый код из панели.');
      output.token = input.token;
      output.serviceMode = input.serviceMode === true;
      if (input.fingerprint && !/^[a-fA-F0-9]{64}$/.test(input.fingerprint)) throw new Error('Отпечаток сертификата должен содержать 64 hex символа.');
      if (input.fingerprint) output.fingerprint = input.fingerprint.toLowerCase();
      if (input.ca_certificate) {
        if (typeof input.ca_certificate !== 'string' || input.ca_certificate.length > 65536 || !input.ca_certificate.includes('-----BEGIN CERTIFICATE-----') || !input.ca_certificate.includes('-----END CERTIFICATE-----') || input.ca_certificate.includes('PRIVATE KEY')) throw new Error('Введите публичный сертификат CA в PEM формате без приватного ключа.');
        output.ca_certificate = input.ca_certificate.trim();
      }
      if (input.fingerprint) {
        if (!output.ca_certificate) throw new Error('Для проверки отпечатка из ссылки нужен сертификат CA в PEM формате.');
        const actual = new crypto.X509Certificate(output.ca_certificate).fingerprint256.replaceAll(':', '').toLowerCase();
        if (actual !== input.fingerprint.toLowerCase()) throw new Error('Отпечаток CA не совпадает со ссылкой подключения.');
      }
    }
  }
  return output;
}
function readConfig(dataDir) {
  const filename = path.join(dataDir, 'desktop.json');
  if (!fs.existsSync(filename)) return null;
  const input = JSON.parse(fs.readFileSync(filename, 'utf8'));
  if (!['server', 'agent', 'remote'].includes(input.mode)) throw new Error('Повреждена конфигурация desktop.json. Восстановите резервную копию через управление системой.');
  if (input.serverUrl) httpsUrl(input.serverUrl);
  return { mode: input.mode, serviceMode: input.serviceMode === true, serverUrl: input.serverUrl, autoStart: !!input.autoStart, closeToTray: input.closeToTray !== false, version: 1 };
}
function saveConfig(dataDir, input) {
  // Deliberately construct persisted fields. Passwords and enrollment codes never enter this file.
  const output = { version: 1, mode: input.mode, serviceMode: input.mode === 'agent' && input.serviceMode === true, autoStart: !!input.autoStart, closeToTray: input.closeToTray !== false };
  if (input.serverUrl) output.serverUrl = httpsUrl(input.serverUrl);
  fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  const filename = path.join(dataDir, 'desktop.json');
  const temp = filename + '.' + crypto.randomUUID() + '.tmp';
  fs.writeFileSync(temp, JSON.stringify(output, null, 2), { mode: 0o600 });
  fs.renameSync(temp, filename);
  return output;
}
function sanitize(text) {
  return String(text).replace(/(bearer\s+)[\w.~-]+/gi, '$1[redacted]')
    .replace(/((?:password|token|credential|secret)\s*[=:]\s*)[^\s,;]+/gi, '$1[redacted]')
    .slice(0, 3000);
}
module.exports = { httpsUrl, parseEnrollment, validateSetup, readConfig, saveConfig, sanitize };
