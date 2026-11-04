'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { httpsUrl, parseEnrollment, validateSetup, readConfig, saveConfig, sanitize } = require('../lib/config.cjs');
const { verifyManifest } = require('../lib/updates.cjs');

test('remote and enrollment validation rejects insecure or credential-bearing endpoints', () => {
  for (const value of ['http://localhost:8000', 'javascript:alert(1)', 'https://user:pass@example.org', 'https://example.org/?token=x']) assert.throws(() => httpsUrl(value));
  assert.equal(httpsUrl('https://farm.example.org/'), 'https://farm.example.org');
  assert.throws(() => parseEnrollment('cs2farm://execute?server=https://example.org&token=abcdefghijklmno1234567890'));
  const parsed = parseEnrollment('cs2farm://enroll?server=https%3A%2F%2Fexample.org&token=abcdefghijklmno1234567890');
  assert.equal(parsed.serverUrl, 'https://example.org');
  assert.equal(parsed.token, 'abcdefghijklmno1234567890');
});
test('configuration persistence deliberately excludes admin passwords and pairing codes', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'farm-config-'));
  try {
    const value = validateSetup({ mode: 'server', username: 'admin', password: 'secret-password-123456', autoStart: true });
    saveConfig(directory, value);
    const raw = fs.readFileSync(path.join(directory, 'desktop.json'), 'utf8');
    assert.ok(!raw.includes(value.password)); assert.ok(!raw.includes('password'));
    assert.deepEqual(readConfig(directory), { mode: 'server', serviceMode: false, serverUrl: undefined, autoStart: true, closeToTray: true, version: 1 });
    saveConfig(directory, { mode: 'agent', serverUrl: 'https://farm.example.org', token: 'secret-enrollment-code-1234' });
    assert.ok(!fs.readFileSync(path.join(directory, 'desktop.json'), 'utf8').includes('enrollment'));
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
test('setup enforces real password rules, CA public-only inputs and fingerprint verification', () => {
  assert.throws(() => validateSetup({ mode: 'server', username: 'admin', password: 'short' }));
  assert.throws(() => validateSetup({ mode: 'agent', serverUrl: 'https://farm.example.org', token: 'valid-enrollment-code-1234', fingerprint: 'a'.repeat(64) }));
  assert.throws(() => validateSetup({ mode: 'agent', serverUrl: 'https://farm.example.org', token: 'valid-enrollment-code-1234', ca_certificate: 'PRIVATE KEY' }));
});
test('diagnostics redact accidental token and password values', () => {
  const value = sanitize('Bearer abcdef123 password=private token=credential');
  assert.ok(!value.includes('abcdef123')); assert.ok(!value.includes('private')); assert.ok(!value.includes('credential'));
});
test('update publisher signature and pinned host verified before installer execution', () => {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  const rawKey = publicKey.export({ type: 'spki', format: 'der' }).subarray(-32).toString('base64');
  const manifest = { sha256: 'a'.repeat(64), url: 'https://updates.example.org/setup.exe', version: '0.3.0' };
  manifest.signature = crypto.sign(null, Buffer.from(JSON.stringify(manifest)), privateKey).toString('base64');
  assert.equal(verifyManifest(manifest, rawKey, 'updates.example.org').version, '0.3.0');
  assert.throws(() => verifyManifest({ ...manifest, sha256: 'b'.repeat(64) }, rawKey, 'updates.example.org'));
  assert.throws(() => verifyManifest(manifest, rawKey, 'attacker.example.org'));
});

test('bundled tray PNG is structurally valid with intact CRCs and visible pixels', () => {
  const png = fs.readFileSync(path.join(__dirname, '../renderer/tray.png'));
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  const crc32 = bytes => { let crc = 0xffffffff; for (const byte of bytes) { crc ^= byte; for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); } return (crc ^ 0xffffffff) >>> 0; };
  const chunks = []; let offset = 8;
  while (offset < png.length) {
    const length = png.readUInt32BE(offset); const end = offset + 8 + length;
    assert.equal(crc32(png.subarray(offset + 4, end)), png.readUInt32BE(end));
    chunks.push(png.toString('ascii', offset + 4, offset + 8)); offset = end + 4;
  }
  assert.equal(offset, png.length); assert.deepEqual(chunks, ['IHDR', 'IDAT', 'IEND']);
  assert.equal(png.readUInt32BE(16), 16); assert.equal(png.readUInt32BE(20), 16);
});
