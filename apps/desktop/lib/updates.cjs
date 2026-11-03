'use strict';
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { httpsUrl } = require('./config.cjs');
function verifyManifest(manifest, publicKeyBase64, allowedHost) {
  if (!manifest || Object.keys(manifest).sort().join(',') !== 'sha256,signature,url,version') throw new Error('Invalid update manifest');
  if (!/^\d+\.\d+\.\d+$/.test(manifest.version) || !/^[a-f0-9]{64}$/.test(manifest.sha256)) throw new Error('Invalid update version or hash');
  const url = new URL(httpsUrl(manifest.url));
  if (url.hostname !== allowedHost) throw new Error('Update host differs from pinned publisher');
  const raw = Buffer.from(publicKeyBase64, 'base64');
  if (raw.length !== 32) throw new Error('Invalid publisher public key');
  const key = crypto.createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), raw]), type: 'spki', format: 'der' });
  const canonical = JSON.stringify({ sha256: manifest.sha256, url: manifest.url, version: manifest.version });
  if (!crypto.verify(null, Buffer.from(canonical), key, Buffer.from(manifest.signature, 'base64'))) throw new Error('Publisher signature verification failed');
  return manifest;
}
async function readLimited(response, limit) {
  if (!response.ok) throw new Error(`Update service HTTP ${response.status}`);
  let size = 0; const chunks = [];
  for await (const chunk of response.body) { size += chunk.length; if (size > limit) throw new Error('Update response exceeds limit'); chunks.push(Buffer.from(chunk)); }
  return Buffer.concat(chunks);
}
async function checkUpdates(product, currentVersion) {
  if (!product.updateManifestUrl || !product.updatePublicKey) return { configured: false, available: false, message: 'Издатель не настроил канал подписанных обновлений. Проверка не выполнялась.' };
  const feed = new URL(httpsUrl(product.updateManifestUrl));
  const response = await fetch(feed, { redirect: 'error', signal: AbortSignal.timeout(15000) });
  const manifest = verifyManifest(JSON.parse((await readLimited(response, 16384)).toString('utf8')), product.updatePublicKey, feed.hostname);
  const newer = (left, right) => {
    const a = left.split('.').map(Number), b = right.split('.').map(Number);
    for (let i = 0; i < 3; i++) { if (a[i] !== b[i]) return a[i] > b[i]; }
    return false;
  };
  const available = newer(manifest.version, currentVersion);
  return { configured: true, available, version: manifest.version, manifest, message: available ? `Доступна версия ${manifest.version}.` : 'Используется актуальная версия из настроенного канала.' };
}
async function downloadUpdate(manifest, product, dataDir) {
  const host = new URL(httpsUrl(product.updateManifestUrl)).hostname;
  verifyManifest(manifest, product.updatePublicKey, host);
  const directory = path.join(dataDir, 'verified-updates');
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const filename = path.join(directory, `CS2-Farm-${manifest.version}-Setup.exe`);
  const temporary = filename + '.download';
  const hash = crypto.createHash('sha256');
  let bytes = 0;
  try {
    const response = await fetch(manifest.url, { redirect: 'error', signal: AbortSignal.timeout(300000) });
    if (!response.ok) throw new Error(`Download HTTP ${response.status}`);
    const file = fs.openSync(temporary, 'w', 0o600);
    try {
      for await (const chunk of response.body) {
        bytes += chunk.length;
        if (bytes > 1024 * 1024 * 1024) throw new Error('Installer exceeds 1GB limit');
        hash.update(chunk); fs.writeSync(file, Buffer.from(chunk));
      }
    } finally { fs.closeSync(file); }
    if (hash.digest('hex') !== manifest.sha256) throw new Error('Installer SHA-256 mismatch');
    fs.renameSync(temporary, filename);
    return filename;
  } catch (error) { fs.rmSync(temporary, { force: true }); throw error; }
}
module.exports = { verifyManifest, checkUpdates, downloadUpdate };
