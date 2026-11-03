'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { Supervisor, assertPortFree, resolvePaths } = require('../lib/supervisor.cjs');

test('packaged paths use bundled interpreters and runtime, no PATH dependencies', () => {
  const value = resolvePaths({ packaged: true, resourcesPath: 'C:/program/resources', repositoryRoot: '/ignored', dataDir: '/data' });
  assert.ok(value.runtime.endsWith('runtime/farm-runtime/farm-runtime.exe'));
  assert.ok(value.node.endsWith('node/node.exe'));
  assert.deepEqual(value.runtimePrefix, []);
  assert.ok(value.agentInstaller.endsWith('downloads/CS2-Farm-Agent-Setup.exe'));
});
test('occupied ports are rejected rather than attaching to unrelated local process', async () => {
  const server = net.createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await assert.rejects(assertPortFree(server.address().port), /занят/); }
  finally { await new Promise(resolve => server.close(resolve)); }
});
test('supervisor really configures child stdin, starts children, verifies ownership and stops/restarts', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'farm-supervisor-'));
  const fixture = path.join(__dirname, 'runtime-fixture.cjs');
  const supervisor = new Supervisor({ dataDir: directory, runtime: process.execPath, runtimePrefix: [fixture], node: process.execPath, dashboard: fixture, cwd: __dirname });
  try {
    await supervisor.configure({ mode: 'server', username: 'test-admin', password: 'never-written-password-1234' });
    assert.ok(!fs.readFileSync(path.join(directory, 'configured-fixture.json'), 'utf8').includes('never-written-password'));
    await supervisor.start('server');
    assert.equal(supervisor.snapshot().services.backend.status, 'up');
    assert.equal(supervisor.snapshot().services.dashboard.status, 'up');
    const before = supervisor.children.get('backend').pid;
    await supervisor.restart();
    assert.notEqual(supervisor.children.get('backend').pid, before);
    supervisor.instanceId = 'wrong-instance'; await supervisor.probe();
    assert.equal(supervisor.snapshot().services.backend.status, 'degraded');
    assert.equal(supervisor.snapshot().services.queue.status, 'unknown');
    assert.equal(supervisor.restartTimer, null);
    await supervisor.probe(); await supervisor.probe();
    assert.ok(supervisor.restartTimer, 'three failed health probes schedule bounded recovery');
    clearTimeout(supervisor.restartTimer); supervisor.restartTimer = null;
    supervisor.restartBudget = 3; supervisor.scheduleRecovery('backend');
    assert.equal(supervisor.snapshot().services.backend.status, 'failed');
    assert.equal(supervisor.running, false);
    await supervisor.stop(); assert.equal(supervisor.children.size, 0);
    assert.equal(supervisor.snapshot().services.database.status, 'stopped');
  } finally { await supervisor.stop(); fs.rmSync(directory, { recursive: true, force: true }); }
});

test('portable embedded Windows Python is resolved without system Python',()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'farm-embedded-'));
  try{
    const runtime=path.join(directory,'runtime','farm-runtime');fs.mkdirSync(runtime,{recursive:true});fs.writeFileSync(path.join(runtime,'python.exe'),'fixture');
    const value=resolvePaths({packaged:true,resourcesPath:directory,repositoryRoot:'/ignored',dataDir:'/data'});
    assert.equal(value.runtime,path.join(runtime,'python.exe'));
    assert.deepEqual(value.runtimePrefix,[path.join(runtime,'desktop_entry.py')]);
  }finally{fs.rmSync(directory,{recursive:true,force:true});}
});
