// Electron runner: launches the Python daemon, relays sleep/wake, checks GitHub for updates.
const { app, BrowserWindow, ipcMain, powerMonitor, shell, Notification } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');

const pkg = require('../package.json');
const GITHUB_REPO = (process.env.AGW_GITHUB_REPO || pkg.repository.replace('github:', ''));
const DATA_DIR = path.join(app.getPath('userData'), 'data');
fs.mkdirSync(DATA_DIR, { recursive: true });

let win = null;
let daemon = null;
let daemonPort = 0;
let restarts = 0;
let quitting = false;

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

// Python env lives in the user's data folder so it survives app updates.
const VENV = app.isPackaged ? path.join(app.getPath('userData'), 'pyenv') : path.join(__dirname, '..', 'daemon', '.venv');
function pythonPath() {
  const venv = path.join(VENV, 'bin', 'python3');
  return fs.existsSync(venv) ? venv : 'python3';
}
function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: 'ignore', ...opts });
    p.on('error', reject);
    p.on('exit', (c) => (c === 0 ? resolve() : reject(new Error(`${path.basename(cmd)} exited ${c}`))));
  });
}
async function ensurePython() {
  const reqFile = path.join(path.dirname(daemonScript()), 'requirements.txt');
  const stamp = path.join(VENV, '.req');
  const req = fs.readFileSync(reqFile, 'utf8');
  if (fs.existsSync(stamp) && fs.readFileSync(stamp, 'utf8') === req) return;
  if (!fs.existsSync(path.join(VENV, 'bin', 'python3'))) await run('/usr/bin/env', ['python3', '-m', 'venv', VENV]);
  await run(path.join(VENV, 'bin', 'python3'), ['-m', 'pip', 'install', '-q', '-r', reqFile]);
  fs.writeFileSync(stamp, req);
}

function daemonScript() {
  const root = app.isPackaged ? path.join(process.resourcesPath, 'app') : path.join(__dirname, '..');
  return path.join(root, 'daemon', 'whisperd.py');
}

async function startDaemon() {
  try { await ensurePython(); } catch (e) { console.error('Python setup failed:', e.message); }
  daemonPort = await freePort();
  daemon = spawn(pythonPath(), [daemonScript(), '--port', String(daemonPort), '--data', DATA_DIR], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const log = fs.createWriteStream(path.join(DATA_DIR, 'daemon.log'), { flags: 'a' });
  daemon.stdout.pipe(log); daemon.stderr.pipe(log);
  daemon.on('exit', (code) => {
    daemon = null;
    if (quitting) return;
    if (restarts < 5) { restarts++; setTimeout(startDaemon, 2000 * restarts); }
    send('daemon-status', { running: false, code });
  });
  setTimeout(() => { restarts = 0; }, 60000);
}

async function api(method, route, body) {
  const res = await fetch(`http://127.0.0.1:${daemonPort}${route}`, {
    method, headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Daemon error ${res.status}`);
  return data;
}

function send(ch, payload) { if (win && !win.isDestroyed()) win.webContents.send(ch, payload); }

// ---- Sleep / wake queue protection ----
function wirePower() {
  const pause = (reason) => api('POST', '/power', { state: 'suspend', reason }).catch(() => {});
  const resume = (reason) => api('POST', '/power', { state: 'resume', reason }).catch(() => {});
  powerMonitor.on('suspend', () => pause('suspend'));
  powerMonitor.on('lock-screen', () => {});
  powerMonitor.on('resume', () => setTimeout(() => resume('resume'), 3000)); // let network come back
  powerMonitor.on('shutdown', () => pause('shutdown'));
}

// ---- GitHub update checker + in-app installer ----
let latestAsset = null;
function cmpVer(a, b) {
  const pa = a.replace(/^v/, '').split('.').map(Number), pb = b.replace(/^v/, '').split('.').map(Number);
  for (let i = 0; i < 3; i++) { if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0); }
  return 0;
}
async function checkForUpdates(manual = false) {
  try {
    const r = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/releases/latest`, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'ai-group-whisper' },
    });
    if (!r.ok) throw new Error(`GitHub responded ${r.status}`);
    const rel = await r.json();
    const latest = rel.tag_name || '0.0.0';
    const asset = (rel.assets || []).find((a) => a.name.endsWith(`darwin-${process.arch}.zip`));
    latestAsset = asset || null;
    const info = { current: app.getVersion(), latest, url: rel.html_url, notes: rel.body || '',
      available: cmpVer(latest, app.getVersion()) > 0,
      canInstall: !app.isPackaged || !!asset };
    if (info.available && !manual && Notification.isSupported()) {
      new Notification({ title: 'AI Group Whisper update', body: `Version ${latest} is available.` }).show();
    }
    send('update-info', info);
    return info;
  } catch (e) {
    const info = { current: app.getVersion(), error: e.message };
    send('update-info', info);
    return info;
  }
}

async function installUpdate() {
  const os = require('os');
  const progress = (stage, pct) => send('update-progress', { stage, pct });
  try {
    if (!app.isPackaged) {
      // Running from source: pull latest code from GitHub and restart.
      const root = path.join(__dirname, '..');
      progress('Downloading update…', null);
      await run('git', ['-C', root, 'pull', '--ff-only']);
      progress('Restarting…', 100);
      quitting = true; if (daemon) daemon.kill();
      app.relaunch(); app.exit(0);
      return { ok: true };
    }
    if (!latestAsset) throw new Error('This release has no Mac build attached yet. Try again in a few minutes.');
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'agw-update-'));
    const zip = path.join(tmp, 'update.zip');
    const r = await fetch(latestAsset.browser_download_url, { headers: { 'User-Agent': 'ai-group-whisper' } });
    if (!r.ok) throw new Error(`Download failed (${r.status})`);
    const total = Number(r.headers.get('content-length')) || latestAsset.size || 0;
    const out = fs.createWriteStream(zip);
    let got = 0;
    for await (const chunk of r.body) {
      got += chunk.length; out.write(chunk);
      if (total) progress('Downloading update…', Math.round((got / total) * 100));
    }
    await new Promise((res) => out.end(res));
    progress('Installing…', 100);
    const ex = path.join(tmp, 'x');
    await run('/usr/bin/ditto', ['-x', '-k', zip, ex]);
    const newApp = fs.readdirSync(ex).find((f) => f.endsWith('.app'));
    if (!newApp) throw new Error('Downloaded update is not a Mac app.');
    const curApp = path.resolve(process.execPath, '..', '..', '..'); // .../X.app
    const script = path.join(tmp, 'swap.sh');
    fs.writeFileSync(script, `#!/bin/bash
while kill -0 ${process.pid} 2>/dev/null; do sleep 0.5; done
rm -rf "${curApp}.old"
mv "${curApp}" "${curApp}.old" && mv "${path.join(ex, newApp)}" "${curApp}" || mv "${curApp}.old" "${curApp}"
xattr -dr com.apple.quarantine "${curApp}" 2>/dev/null
rm -rf "${curApp}.old"
open "${curApp}"
`, { mode: 0o755 });
    spawn('/bin/bash', [script], { detached: true, stdio: 'ignore' }).unref();
    progress('Restarting…', 100);
    setTimeout(() => app.quit(), 500);
    return { ok: true };
  } catch (e) {
    send('update-progress', { error: e.message });
    return { error: e.message };
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1200, height: 800, titleBarStyle: 'hiddenInset', backgroundColor: '#0f1115',
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false },
  });
  win.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
}

ipcMain.handle('api', (_e, method, route, body) => api(method, route, body));
ipcMain.handle('check-updates', () => checkForUpdates(true));
ipcMain.handle('install-update', () => installUpdate());
ipcMain.handle('open-external', (_e, url) => { if (/^https:\/\//.test(url)) shell.openExternal(url); });
ipcMain.handle('app-info', () => ({ version: app.getVersion(), dataDir: DATA_DIR, repo: GITHUB_REPO }));

app.whenReady().then(async () => {
  await startDaemon();
  wirePower();
  createWindow();
  setTimeout(() => checkForUpdates(false), 10000);
  setInterval(() => checkForUpdates(false), 6 * 60 * 60 * 1000);
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('before-quit', async () => {
  quitting = true;
  if (daemon) { try { await api('POST', '/power', { state: 'suspend', reason: 'quit' }); } catch {} daemon.kill('SIGTERM'); }
});
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
