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

function pythonPath() {
  const root = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
  const venv = path.join(root, 'daemon', '.venv', 'bin', 'python3');
  return fs.existsSync(venv) ? venv : 'python3';
}

function daemonScript() {
  const root = app.isPackaged ? path.join(process.resourcesPath, 'app') : path.join(__dirname, '..');
  return path.join(root, 'daemon', 'whisperd.py');
}

async function startDaemon() {
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

// ---- GitHub update checker ----
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
    const info = { current: app.getVersion(), latest, url: rel.html_url, notes: rel.body || '',
      available: cmpVer(latest, app.getVersion()) > 0 };
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

function createWindow() {
  win = new BrowserWindow({
    width: 1200, height: 800, titleBarStyle: 'hiddenInset', backgroundColor: '#0f1115',
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false },
  });
  win.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
}

ipcMain.handle('api', (_e, method, route, body) => api(method, route, body));
ipcMain.handle('check-updates', () => checkForUpdates(true));
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
