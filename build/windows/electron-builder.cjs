/* The application is built twice from identical sources, with explicit product identity. */
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const agent = process.env.FARM_PRODUCT === 'agent';
const productName = agent ? 'CS2 Farm Agent' : 'CS2 Farm Manager';
module.exports = {
  electronVersion: require(path.join(root,"apps/desktop/package.json")).devDependencies.electron,
  appId: agent ? 'com.cs2farm.agent' : 'com.cs2farm.manager',
  productName,
  executableName: agent ? 'CS2-Farm-Agent' : 'CS2-Farm-Manager',
  artifactName: agent ? 'CS2-Farm-Agent-Setup.exe' : 'CS2-Farm-Manager-Setup.exe',
  directories: { app: path.join(root, 'apps/desktop'), output: path.join(root, 'artifacts/windows', agent ? 'agent' : 'manager') },
  extraMetadata: { name: agent ? 'cs2-farm-agent-desktop' : 'cs2-farm-manager-desktop', productName },
  files: ['main.cjs', 'preload.cjs', 'dashboard-preload.cjs', 'lib/**', 'renderer/**', 'package.json', '!**/*.test.cjs'],
  asar: true,
  compression: 'maximum',
  extraResources: [
    { from: path.join(root, 'build/windows/stage/runtime/farm-runtime'), to: 'runtime/farm-runtime', filter: ['**/*'] },
    { from: path.join(root, 'build/windows/stage/product.json'), to: 'product.json' },
    ...(!agent ? [
      { from: path.join(root, 'build/windows/stage/node'), to: 'node', filter: ['node.exe', 'LICENSE'] },
      { from: path.join(root, 'build/windows/stage/dashboard'), to: 'dashboard', filter: ['**/*'] },
      // electron-builder's FileMatcher always excludes root node_modules.
      // A separate source root preserves Next's actual traced dependencies.
      { from: path.join(root, 'build/windows/stage/dashboard/node_modules'), to: 'dashboard/node_modules', filter: ['**/*'] },
      { from: path.join(root, 'build/windows/stage/downloads'), to: 'downloads', filter: ['*.exe', '*.json'] }
    ] : [])
  ],
  afterPack: async context => {
    const fs = require('node:fs');
    const crypto = require('node:crypto');
    const resources = path.join(context.appOutDir, 'resources');
    const runtime = path.join(resources, 'runtime/farm-runtime');
    const manifestFile = path.join(runtime, 'component-integrity.json');
    function filesUnder(root) {
      const output = [];
      for (const item of fs.readdirSync(root, { withFileTypes: true })) {
        if (item.name === '__pycache__' || item.name === 'component-integrity.json') continue;
        const file = path.join(root, item.name);
        if (item.isDirectory()) output.push(...filesUnder(file));
        else if (item.isFile() && !file.endsWith('.pyc')) output.push(file);
      }
      return output;
    }
    const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    const manifest = fs.existsSync(manifestFile) ? JSON.parse(fs.readFileSync(manifestFile, 'utf8')) : {
      schema: 1, baseline_trust: 'unsigned_local_manifest',
      files: Object.fromEntries(filesUnder(runtime).map(file => [path.relative(runtime, file).split(path.sep).join('/'), hash(file)]))
    };
    if (!agent) {
      const desktopFiles = [path.join(resources, 'app.asar'), path.join(resources, 'product.json'), path.join(resources, 'node/node.exe'), path.join(resources, 'dashboard/server.js'), ...filesUnder(path.join(resources, 'dashboard/.next/static'))];
      manifest.desktop_files = Object.fromEntries(desktopFiles.map(file => [path.relative(resources, file).split(path.sep).join('/'), hash(file)]));
    }
    fs.writeFileSync(manifestFile, JSON.stringify(manifest));
    if (agent) return;
    for (const file of ['node_modules/next/package.json', 'node_modules/react/package.json', '.next/BUILD_ID', 'server.js']) {
      const target = path.join(context.appOutDir, 'resources/dashboard', file);
      if (!fs.existsSync(target)) throw new Error(`Incomplete standalone Dashboard payload: ${file}`);
    }
  },
  afterSign: async context => {
    // Refresh hashes after Authenticode changes executable bytes in a signed build.
    const fs = require('node:fs'); const crypto = require('node:crypto');
    const resources = path.join(context.appOutDir, 'resources');
    const runtime = path.join(resources, 'runtime/farm-runtime');
    const file = path.join(runtime, 'component-integrity.json');
    const manifest = JSON.parse(fs.readFileSync(file, 'utf8'));
    const hash = target => crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex');
    for (const name of Object.keys(manifest.files)) manifest.files[name] = hash(path.join(runtime, name));
    for (const name of Object.keys(manifest.desktop_files || {})) manifest.desktop_files[name] = hash(path.join(resources, name));
    fs.writeFileSync(file, JSON.stringify(manifest));
  },
  win: {
    target: [{ target: 'nsis', arch: ['x64'] }],
    requestedExecutionLevel: 'asInvoker',
    // A signing certificate is optional; unsigned builds remain explicitly unsigned.
    signAndEditExecutable: true
  },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowElevation: true,
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: productName,
    uninstallDisplayName: productName,
    runAfterFinish: true,
    deleteAppDataOnUninstall: false,
    include: path.join(root, 'build/windows/installer.nsh'),
    installerLanguages: ['ru_RU', 'en_US'],
    language: '1049',
    displayLanguageSelector: true,
    differentialPackage: true
  },
  publish: null
};
