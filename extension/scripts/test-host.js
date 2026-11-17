/* Runs out/test/index.js inside a VS Code extension host, using the VS Code
   already installed on this machine with a throw-away profile, so the user's
   own settings and extensions are not touched.

     node scripts/test-host.js

   Set VSCODE_EXE to the VS Code executable if it is not found automatically. */
const { spawnSync, execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

function findCode() {
  if (process.env.VSCODE_EXE) return process.env.VSCODE_EXE;
  if (process.platform === 'win32') {
    const shim = execSync('where code', { encoding: 'utf8' }).split(/\r?\n/)[0].trim();
    return path.join(path.dirname(path.dirname(shim)), 'Code.exe');
  }
  if (process.platform === 'darwin') return '/Applications/Visual Studio Code.app/Contents/MacOS/Electron';
  return execSync('which code', { encoding: 'utf8' }).trim();
}

const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'bugtracker-vscode-'));
const result = spawnSync(findCode(), [
  `--extensionDevelopmentPath=${root}`,
  `--extensionTestsPath=${path.join(root, 'out', 'test', 'index.js')}`,
  `--user-data-dir=${path.join(profile, 'user')}`,
  `--extensions-dir=${path.join(profile, 'extensions')}`,
  '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes', '--disable-updates', '--new-window',
], { stdio: 'inherit', env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined } });
fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5 });
console.log(result.status === 0 ? 'Extension host test passed.' : `Extension host test FAILED (exit ${result.status}).`);
process.exit(result.status ?? 1);
