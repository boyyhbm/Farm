/* Portable packaging contract checks; they do not claim to execute a Windows binary. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const modulePath = path.resolve(__dirname, 'electron-builder.cjs');
for (const product of ['manager', 'agent']) {
  process.env.FARM_PRODUCT = product;
  delete require.cache[modulePath];
  const config = require(modulePath);
  assert.equal(config.nsis.oneClick, false);
  assert.equal(config.nsis.createDesktopShortcut, true);
  assert.equal(config.nsis.createStartMenuShortcut, true);
  assert.equal(config.nsis.deleteAppDataOnUninstall, false);
  assert.equal(config.win.requestedExecutionLevel, 'asInvoker');
  assert.equal(config.publish, null);
  assert.equal(config.artifactName, product === 'manager' ? 'CS2-Farm-Manager-Setup.exe' : 'CS2-Farm-Agent-Setup.exe');
  assert(config.extraResources.some(resource => resource.to === 'runtime/farm-runtime'));
  assert.equal(config.extraResources.some(resource => resource.to === 'node'), product === 'manager');
  assert.equal(config.extraResources.some(resource => resource.to === 'downloads'), product === 'manager');
}
const node = JSON.parse(fs.readFileSync(path.join(__dirname, 'node-runtime.json'), 'utf8'));
assert.equal(new URL(node.url).protocol, 'https:');
assert.equal(new URL(node.url).hostname, 'nodejs.org');
assert.match(node.sha256, /^[0-9a-f]{64}$/);
console.log('Windows packaging contracts passed (config only; no Windows runtime execution).');
