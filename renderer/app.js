// Bridge to the local engine (same origin as this window).
(() => {
  const call = async (m, r, b) => {
    const res = await fetch(r, { method: m, headers: { 'Content-Type': 'application/json' }, body: b ? JSON.stringify(b) : undefined });
    const j = await res.json();
    if (!res.ok || (j && !Array.isArray(j) && j.error && m !== 'GET')) throw new Error((j && j.error) || res.status);
    return j;
  };
  const subs = {};
  const emit = (ch, d) => (subs[ch] || []).forEach((cb) => cb(d));
  window.agw = {
    api: call,
    appInfo: () => call('GET', '/app/info'),
    checkUpdates: async () => { const i = await call('GET', '/app/check-updates'); if (!i.error) emit('update-info', i); return i; },
    openExternal: (url) => call('POST', '/app/open', { url }),
    on: (ch, cb) => { (subs[ch] = subs[ch] || []).push(cb); },
    installUpdate: async () => {
      const t = setInterval(async () => { try { emit('update-progress', await call('GET', '/app/update-progress')); } catch {} }, 700);
      try { return await call('POST', '/app/install-update'); }
      catch (e) { clearInterval(t); emit('update-progress', { error: e.message }); return { error: e.message }; }
    },
  };
  setTimeout(() => window.agw.checkUpdates().catch(() => {}), 3000);
  setInterval(() => window.agw.checkUpdates().catch(() => {}), 6 * 3600 * 1000);
})();
const $ = (id) => document.getElementById(id);
const api = (m, r, b) => window.agw.api(m, r, b);
let groups = [], current = null;

function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }
function show(view) {
  document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  ['groups', 'queue', 'settings'].forEach((v) => $('view-' + v).classList.toggle('hidden', v !== view));
  if (view === 'queue') loadQueue();
  if (view === 'settings') loadSettings();
}
document.querySelectorAll('nav button').forEach((b) => b.onclick = () => show(b.dataset.view));

async function refreshStatus() {
  try {
    const s = await api('GET', '/status');
    const q = s.queue || {};
    $('status').textContent = !s.configured ? 'Set up Telegram in Settings'
      : !s.authorized ? 'Not signed in to Telegram'
      : s.suspended ? 'Paused (Mac asleep)'
      : `Connected · ${q.pending || 0} queued · ${q.failed || 0} failed`;
    const pb = $('pause-banner');
    if (s.paused_reason) {
      pb.innerHTML = `${esc(s.paused_reason)} <button id="resume-ai">Resume</button>`;
      pb.classList.remove('hidden');
      $('resume-ai').onclick = async () => { await api('POST', '/queue/resume'); refreshStatus(); };
    } else pb.classList.add('hidden');
  } catch { $('status').textContent = 'Background service starting…'; }
}

async function loadGroups() {
  try { groups = await api('GET', '/groups'); } catch { return; }
  $('group-list').innerHTML = groups.map((g) =>
    `<div class="gitem ${current === g.chat_id ? 'sel' : ''}" data-id="${g.chat_id}"><span>${esc(g.title)}</span>${g.watched ? '<span class="dot"></span>' : ''}</div>`).join('');
  document.querySelectorAll('.gitem').forEach((el) => el.onclick = () => openGroup(Number(el.dataset.id)));
}

async function openGroup(id) {
  current = id; show('groups');
  const g = groups.find((x) => x.chat_id === id);
  $('group-empty').classList.add('hidden'); $('group-detail').classList.remove('hidden');
  $('g-title').textContent = g.title; $('g-watch').checked = !!g.watched; $('g-auto').checked = !!g.auto_reply;
  $('g-persona').value = g.persona || '';
  loadGroups(); loadFeed();
}

async function loadFeed() {
  if (!current) return;
  const f = await api('GET', `/groups/${current}/feed`);
  $('g-summaries').innerHTML = f.summaries.map((s) => `<div class="card">${esc(s.body)}<div class="hint">${new Date(s.created * 1000).toLocaleString()}</div></div>`).join('') || '<div class="hint">No notes yet.</div>';
  $('g-messages').innerHTML = f.messages.slice(-60).map((m) => `<div class="msg"><b>${esc(m.sender)}</b> ${esc(m.text)}</div>`).join('') || '<div class="hint">No messages collected yet.</div>';
}

const upd = (b) => api('POST', `/groups/${current}`, b).then(loadGroups);
$('g-watch').onchange = (e) => upd({ watched: e.target.checked ? 1 : 0 });
$('g-auto').onchange = (e) => upd({ auto_reply: e.target.checked ? 1 : 0 });
$('g-persona').onchange = (e) => upd({ persona: e.target.value });
$('g-summarize').onclick = () => api('POST', `/groups/${current}/summarize`).then(() => setTimeout(loadFeed, 4000));
$('g-draft').onclick = () => api('POST', `/groups/${current}/draft_reply`).then(() => setTimeout(loadFeed, 4000));

async function loadQueue() {
  const rows = await api('GET', '/queue');
  $('queue-table').innerHTML = '<tr><th>#</th><th>Type</th><th>Status</th><th>Tries</th><th>Last error</th></tr>' +
    rows.map((r) => `<tr><td>${r.id}</td><td>${esc(r.kind)}</td><td>${esc(r.status)}</td><td>${r.attempts}</td><td>${esc(r.last_error || '')}</td></tr>`).join('');
}

async function loadSettings() {
  const s = await api('GET', '/settings');
  $('s-api-id').value = s.tg_api_id || ''; $('s-api-hash').value = s.tg_api_hash || '';
  $('s-ai-model').value = s.ai_model || '';
  $('s-ai-key').placeholder = s.fal_key_set ? 'fal.ai key saved (leave blank to keep)' : 'fal.ai key';
}
const msg = (t) => { $('s-msg').textContent = t; };
$('s-save').onclick = async () => {
  await api('POST', '/settings', { tg_api_id: $('s-api-id').value, tg_api_hash: $('s-api-hash').value,
    ai_model: $('s-ai-model').value, fal_key: $('s-ai-key').value });
  $('s-ai-key').value = ''; msg('Saved.'); loadSettings();
};
$('l-send').onclick = () => api('POST', '/login/code', { phone: $('l-phone').value }).then(() => msg('Code sent — check Telegram.')).catch((e) => msg(e.message));
$('l-verify').onclick = async () => {
  try {
    const r = await api('POST', '/login/verify', { code: $('l-code').value, password: $('l-pass').value });
    msg(r.need_password ? 'Enter your 2FA password and press Sign in again.' : 'Signed in!');
    loadGroups();
  } catch (e) { msg(e.message); }
};

function renderUpdate(info) {
  const b = $('update-banner');
  if (info.available) {
    b.innerHTML = `Version ${esc(info.latest)} is available (you have ${esc(info.current)}). ` +
      (info.canInstall ? `<button id="upd-install">Update now</button>` : `<span>Mac build is still being prepared — check again shortly.</span>`) +
      ` <a href="#" id="upd-notes">What's new</a> <span id="upd-status"></span>`;
    b.classList.remove('hidden');
    $('upd-notes').onclick = (e) => { e.preventDefault(); window.agw.openExternal(info.url); };
    if ($('upd-install')) $('upd-install').onclick = async () => {
      $('upd-install').disabled = true;
      const r = await window.agw.installUpdate();
      if (r && r.error) { $('upd-install').disabled = false; }
    };
  } else b.classList.add('hidden');
}
window.agw.on('update-info', renderUpdate);
window.agw.on('update-progress', (p) => {
  const st = $('upd-status'); if (!st) return;
  st.textContent = p.error ? 'Update failed: ' + p.error : p.stage + (p.pct != null ? ` ${p.pct}%` : '');
});
$('check-updates').onclick = async (e) => {
  e.preventDefault();
  const info = await window.agw.checkUpdates();
  if (info.error) alert('Could not check for updates: ' + info.error);
  else if (!info.available) alert(`You're up to date (${info.current}).`);
  else if (info.canInstall && confirm(`Version ${info.latest} is available. Update now? The app will restart.`)) $('upd-install')?.click();
};

window.agw.appInfo().then((i) => { $('version').textContent = 'v' + i.version; });
refreshStatus(); loadGroups();
setInterval(refreshStatus, 4000);
setInterval(() => { loadFeed(); if (!$('view-queue').classList.contains('hidden')) loadQueue(); }, 8000);
