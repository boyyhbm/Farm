const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
for (const [source, target] of [['.next/static', '.next/standalone/.next/static'], ['public', '.next/standalone/public']]) {
  fs.cpSync(path.join(root, source), path.join(root, target), {recursive:true});
}
