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
    if (agent) return;
    const fs = require('node:fs');
    for (const file of ['node_modules/next/package.json', 'node_modules/react/package.json', '.next/BUILD_ID', 'server.js']) {
      const target = path.join(context.appOutDir, 'resources/dashboard', file);
      if (!fs.existsSync(target)) throw new Error(`Incomplete standalone Dashboard payload: ${file}`);
    }
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
