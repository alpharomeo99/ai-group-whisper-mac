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
  ['overview', 'network', 'personas', 'messages', 'groups', 'accounts', 'automation', 'queue', 'settings'].forEach((v) => {
    const el = $('view-' + v);
    if (el) el.classList.toggle('hidden', v !== view);
  });
  document.querySelector('main').classList.toggle('flush', view === 'network');
  if (view === 'network') window.loadNetwork();
  if (view === 'overview') loadOverview();
  if (view === 'queue') loadQueue();
  if (view === 'settings') loadSettings();
  if (view === 'accounts') loadAccounts();
  if (view === 'automation') loadAutomation();
  if (view === 'personas') loadPersonas();
  if (view === 'messages') loadDirectChats();
}
document.querySelectorAll('nav button').forEach((b) => b.onclick = () => show(b.dataset.view));
document.querySelectorAll('[data-go]').forEach((b) => b.onclick = () => show(b.dataset.go));

function loadAutomation() {
  loadProxies();
  loadCfx();
  pollProv();
}

async function refreshStatus() {
  try {
    const s = await api('GET', '/status');
    const q = s.queue || {};
    $('status').textContent = !s.accounts ? 'No accounts yet — open Accounts'
      : !s.authorized ? 'Accounts not connected'
      : s.suspended ? 'Paused (Mac asleep)'
      : `${s.connected} account${s.connected === 1 ? '' : 's'} · ${q.pending || 0} queued · ${q.failed || 0} failed`;
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
  const multiAcc = new Set(groups.map((g) => g.account_id).filter(Boolean)).size > 1;
  $('group-list').innerHTML = groups.map((g) =>
    `<div class="gitem ${current === g.chat_id ? 'sel' : ''}" data-id="${g.chat_id}"><span>${esc(g.title)}${g.account_name && multiAcc ? ` <small class="hint">· ${esc(g.account_name)}</small>` : ''}</span>${g.watched ? '<span class="dot"></span>' : ''}</div>`).join('');
  document.querySelectorAll('#group-list .gitem').forEach((el) => el.onclick = () => openGroup(Number(el.dataset.id)));
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
  $('s-ai-model').value = s.ai_model || '';
  $('s-ai-key').placeholder = s.fal_key_set ? 'fal.ai key saved (leave blank to keep)' : 'fal.ai key';
  $('s-ai-key-badge').classList.toggle('hidden', !s.fal_key_set);
  $('s-vmos-ak').value = s.vmos_ak || ''; $('s-vmos-pad').value = s.vmos_pad || '';
  $('s-vmos-sk').placeholder = s.vmos_sk_set ? 'Secret key saved (leave blank to keep)' : 'Secret Access Key';
  $('s-vmos-sk-badge').classList.toggle('hidden', !s.vmos_sk_set);
  $('s-tv-user').value = s.tv_user || ''; $('s-tv-max').value = s.tv_max_price || '';
  $('s-tv-key').placeholder = s.tv_key_set ? 'API key saved (leave blank to keep)' : 'TextVerified API key';
  $('s-tv-key-badge').classList.toggle('hidden', !s.tv_key_set);
}
const msg = (t) => { $('s-msg').textContent = t; };
async function saveSettings() {
  await api('POST', '/settings', { ai_model: $('s-ai-model').value, fal_key: $('s-ai-key').value,
    vmos_ak: $('s-vmos-ak').value.trim(), vmos_sk: $('s-vmos-sk').value.trim(), vmos_pad: $('s-vmos-pad').value.trim(),
    tv_user: $('s-tv-user').value.trim(), tv_key: $('s-tv-key').value.trim(), tv_max_price: $('s-tv-max').value.trim() });
  ['s-ai-key', 's-vmos-sk', 's-tv-key'].forEach((id) => { $(id).value = ''; $(id).placeholder = 'Saved ✓ (leave blank to keep)'; });
}
$('s-save').onclick = async () => { await saveSettings(); msg('Saved.'); loadSettings(); };
$('s-vmos-test').onclick = async () => {
  $('s-vmos-msg').textContent = 'Testing…';
  try { await saveSettings(); const r = await api('POST', '/integrations/vmos/test');
    $('s-vmos-msg').textContent = `Connected. ${r.pads.length} cloud phone(s)` + (r.pad ? `, using ${r.pad}` : ''); loadSettings();
  } catch (e) { $('s-vmos-msg').textContent = e.message; }
};
$('s-tv-test').onclick = async () => {
  $('s-tv-msg').textContent = 'Checking…';
  try { await saveSettings(); const r = await api('POST', '/integrations/textverified/test');
    $('s-tv-msg').textContent = `Connected. Balance: $${r.balance ?? '?'}`; loadSettings();
  } catch (e) { $('s-tv-msg').textContent = e.message; }
};

// ----- Proxies -----
let proxyList = [];
function proxyOptions(sel, current) {
  return `<option value="">No proxy</option>` + proxyList.map((p) =>
    `<option value="${p.id}" ${String(current) === String(p.id) ? 'selected' : ''}>${esc(p.label)}${p.ok ? ' ✓' : ''}</option>`).join('');
}
async function fetchProxies() { proxyList = await api('GET', '/proxies'); return proxyList; }
async function loadProxies() {
  await fetchProxies();
  $('px-table').innerHTML = proxyList.length ? '<tr><th>Proxy</th><th>Status</th><th>Exit IP</th><th>Used by</th><th></th></tr>' +
    proxyList.map((p) => `<tr><td>${esc(p.label)}</td><td><span class="badge ${p.ok ? 'ok' : p.last_check ? 'bad' : 'off'}">${p.ok ? 'Working' : p.last_check ? 'Failed' : 'Not tested'}</span></td>
      <td>${esc(p.last_ip || '')}</td><td>${esc(p.accounts.join(', '))}</td>
      <td><button data-pt="${p.id}">Test</button> <button data-pd="${p.id}">Delete</button></td></tr>`).join('')
    : '<tr><td class="empty">No proxies yet.</td></tr>';
  document.querySelectorAll('[data-pt]').forEach((b) => b.onclick = async () => { b.disabled = true; b.textContent = '…';
    const r = await api('POST', `/proxies/${b.dataset.pt}/test`); if (!r.ok) $('px-msg').textContent = r.error; loadProxies(); });
  document.querySelectorAll('[data-pd]').forEach((b) => b.onclick = async () => { await api('DELETE', '/proxies/' + b.dataset.pd); loadProxies(); });
}
$('px-add').onclick = async () => {
  try { const r = await api('POST', '/proxies', { text: $('px-text').value });
    $('px-msg').textContent = `Added ${r.added}.` + (r.bad.length ? ` Could not read: ${r.bad.join(', ')}` : '');
    $('px-text').value = ''; loadProxies();
  } catch (e) { $('px-msg').textContent = e.message; }
};
$('px-testall').onclick = async () => {
  $('px-msg').textContent = 'Testing…';
  await Promise.all(proxyList.map((p) => api('POST', `/proxies/${p.id}/test`).catch(() => {})));
  $('px-msg').textContent = 'Done.'; loadProxies();
};

// ----- Camoufox -----
let cfxTimer = null;
function cfxText(st) {
  return st.running ? 'Setting up Camoufox... (first time downloads about 300 MB)' : st.ready ? 'Camoufox is installed and ready' : 'Camoufox is being prepared';
}
async function cfxNav() {
  try { const st = await api('GET', '/camoufox/status');
    $('nav-cfx').className = 'dot-s ' + (st.ready ? 'ok' : st.running ? 'busy' : 'bad'); return st; } catch { return {}; }
}
async function loadCfx() {
  const [st, s] = await Promise.all([cfxNav(), api('GET', '/settings'), fetchProxies()]);
  $('cfx-state').textContent = cfxText(st);
  $('cfx-dot').className = 'dot-s ' + (st.ready ? 'ok' : st.running ? 'busy' : 'bad');
  $('cfx-install').disabled = !!st.running;
  $('cfx-log').classList.toggle('hidden', !(st.running || (!st.ready && st.log))); $('cfx-log').textContent = (st.log || '').split('\n').map((l) => l.split('\r').pop()).join('\n');
  $('cfx-log').scrollTop = 1e9;
  $('cfx-proxy').innerHTML = proxyOptions(null, s.cfx_proxy_id);
  $('cfx-headless').checked = s.cfx_headless !== false; $('cfx-title').value = s.cfx_app_title || '';
  clearTimeout(cfxTimer);
  if (st.running || !st.ready) cfxTimer = setTimeout(loadCfx, 3000);
}
$('cfx-install').onclick = async () => { await api('POST', '/camoufox/install'); setTimeout(loadCfx, 500); };
$('cfx-proxy').onchange = async () => { await api('POST', '/settings', { cfx_proxy_id: $('cfx-proxy').value ? Number($('cfx-proxy').value) : null }); $('cfx-ip').textContent = 'Saved.'; };
$('cfx-test').onclick = async () => {
  $('cfx-test').disabled = true; $('cfx-ip').textContent = 'Opening Camoufox...';
  try { const r = await api('POST', '/camoufox/test', { proxy_id: $('cfx-proxy').value ? Number($('cfx-proxy').value) : null });
    $('cfx-ip').textContent = r.ok ? `Works. Websites see IP ${r.ip}${r.proxied ? ' (through proxy)' : ' (no proxy)'}` : r.error;
  } catch (e) { $('cfx-ip').textContent = e.message; }
  $('cfx-test').disabled = false;
};
$('cfx-save').onclick = async () => {
  await api('POST', '/settings', { cfx_headless: $('cfx-headless').checked, cfx_app_title: $('cfx-title').value.trim() });
  $('cfx-msg').textContent = 'Saved.';
};

// ----- Overview & Master System Switch -----
function updateMasterSystemUI(enabled) {
  const badge = $('sys-status-badge');
  const txt = $('sys-status-text');
  const btn = $('sys-toggle-btn');
  const label = $('sys-btn-text');
  const icon = btn ? btn.querySelector('.sys-btn-icon') : null;
  if (!badge || !btn) return;
  if (enabled) {
    badge.className = 'sys-badge ok';
    if (txt) txt.textContent = 'System Active';
    btn.className = 'sys-master-btn on';
    if (label) label.textContent = 'Turn System OFF';
    if (icon) icon.textContent = '⏸';
  } else {
    badge.className = 'sys-badge paused';
    if (txt) txt.textContent = 'System Paused';
    btn.className = 'sys-master-btn off';
    if (label) label.textContent = 'Turn System ON';
    if (icon) icon.textContent = '⚡';
  }
}

if ($('sys-toggle-btn')) {
  $('sys-toggle-btn').onclick = async () => {
    try {
      const res = await api('POST', '/system/toggle', {});
      updateMasterSystemUI(res.enabled);
    } catch (err) {
      alert('Failed to toggle system: ' + err.message);
    }
  };
}

async function loadOverview() {
  const [s, accs, pxs, st, sys] = await Promise.all([
    api('GET', '/status').catch(() => ({})),
    api('GET', '/accounts').catch(() => []),
    fetchProxies().catch(() => []),
    cfxNav(),
    api('GET', '/system/status').catch(() => ({ enabled: true }))
  ]);
  updateMasterSystemUI(sys.enabled);
  const q = s.queue || {};
  const stat = (k, v, sub) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  $('ov-stats').innerHTML = stat('Accounts connected', `${s.connected || 0}<span class="hint"> / ${accs.length}</span>`, s.suspended ? 'Paused while Mac sleeps' : 'Live on Telegram')
    + stat('Queued jobs', q.pending || 0, `${q.failed || 0} failed`)
    + stat('Proxies working', `${pxs.filter((p) => p.ok).length}<span class="hint"> / ${pxs.length}</span>`, 'Tested through Telegram & Camoufox')
    + stat('Camoufox', st.ready ? 'Ready' : st.running ? 'Setting up' : 'Preparing', 'Built-in stealth browser');
  $('ov-accounts').innerHTML = accs.map((a) => `<div class="mini"><div class="avatar">${esc((a.name || '?')[0].toUpperCase())}</div><div class="grow"><b>${esc(a.name || 'Account')}</b><div class="hint">${esc(a.phone || '')}</div></div><span class="badge ${!a.active ? 'off' : a.connected ? 'ok' : 'bad'}">${!a.active ? 'Paused' : a.connected ? 'Connected' : 'Signed out'}</span></div>`).join('') || '<div class="hint">No accounts yet.</div>';
  $('ov-proxies').innerHTML = pxs.map((p) => `<div class="mini"><div class="grow"><b>${esc(p.label)}</b><div class="hint">${esc(p.accounts.join(', ') || 'Not assigned')}</div></div><span class="badge ${p.ok ? 'ok' : p.last_check ? 'bad' : 'off'}">${p.ok ? 'Working' : p.last_check ? 'Failed' : 'Not tested'}</span></div>`).join('') || '<div class="hint">No proxies yet.</div>';
}

// ----- Accounts -----
let loginToken = null;
const wmsg = (t) => { $('w-msg').textContent = t || ''; };
async function loadAccounts() {
  const noApi = false;
  await fetchProxies();
  const list = await api('GET', '/accounts');
  $('acc-list').innerHTML = list.map((a) => `
    <div class="card acc">
      <div class="avatar">${esc((a.name || '?')[0].toUpperCase())}</div>
      <div class="acc-info"><b>${esc(a.name || 'Account')}</b>
        <div class="hint">${[a.phone, a.username && '@' + a.username, `${a.groups} group${a.groups === 1 ? '' : 's'}`].filter(Boolean).map(esc).join(' · ')}</div></div>
      <span class="badge ${!a.active ? 'off' : a.connected ? 'ok' : 'bad'}">${!a.active ? 'Paused' : a.connected ? 'Connected' : 'Signed out'}</span>
      <select data-px="${a.id}" title="Proxy">${proxyOptions(null, a.proxy_id)}</select>
      <label><input type="checkbox" data-act="${a.id}" ${a.active ? 'checked' : ''}/> On</label>
      <button data-del="${a.id}" data-name="${esc(a.name || 'this account')}">Remove</button>
    </div>`).join('') || (noApi ? '' : '<div class="empty">No accounts yet. Press “+ Add account”.</div>');
  document.querySelectorAll('[data-px]').forEach((el) => el.onchange = async () => {
    await api('POST', '/accounts/' + el.dataset.px, { proxy_id: el.value ? Number(el.value) : null }); loadAccounts();
  });
  document.querySelectorAll('[data-act]').forEach((el) => el.onchange = async () => {
    await api('POST', '/accounts/' + el.dataset.act, { active: el.checked }); loadAccounts(); refreshStatus();
  });
  document.querySelectorAll('[data-del]').forEach((el) => el.onclick = async () => {
    if (!confirm(`Remove ${el.dataset.name}? It will be signed out of this app. Its collected messages stay.`)) return;
    await api('DELETE', '/accounts/' + el.dataset.del); loadAccounts(); loadGroups(); refreshStatus();
  });
}
function wizard(step) {
  $('acc-wizard').classList.toggle('hidden', !step);
  $('w-step-phone').classList.toggle('hidden', step !== 'phone');
  $('w-step-code').classList.toggle('hidden', step !== 'code');
  wmsg('');
}
async function cancelLogin() {
  if (loginToken) api('POST', '/accounts/login/cancel', { token: loginToken }).catch(() => {});
  loginToken = null; wizard(null);
}
$('acc-add').onclick = () => {
  $('w-phone').value = ''; $('w-api-id').value = ''; $('w-api-hash').value = ''; $('w-code').value = ''; $('w-pass').value = '';
  $('w-pass').classList.add('hidden'); $('w-proxy').innerHTML = proxyOptions(null, '');
  $('w-aa-msg').textContent = ''; $('w-aa-code').classList.add('hidden'); wizard('phone'); $('w-phone').focus();
};
$('w-cancel1').onclick = cancelLogin; $('w-cancel2').onclick = cancelLogin;
$('w-send').onclick = async () => {
  const btn = $('w-send'); btn.disabled = true; wmsg('Sending…');
  try {
    const r = await api('POST', '/accounts/login/code', { phone: $('w-phone').value,
      api_id: $('w-api-id').value, api_hash: $('w-api-hash').value, proxy_id: $('w-proxy').value ? Number($('w-proxy').value) : null });
    loginToken = r.token; wizard('code'); $('w-code').focus();
  } catch (e) { wmsg(e.message); } finally { btn.disabled = false; }
};
$('w-verify').onclick = async () => {
  const btn = $('w-verify'); btn.disabled = true; wmsg('Connecting…');
  try {
    const r = await api('POST', '/accounts/login/verify', { token: loginToken, code: $('w-code').value, password: $('w-pass').value });
    if (r.need_password) {
      $('w-pass').classList.remove('hidden'); $('w-code').disabled = true; $('w-pass').focus();
      wmsg('This account has two-step verification. Enter its password.');
    } else { loginToken = null; $('w-code').disabled = false; wizard(null); loadAccounts(); loadGroups(); refreshStatus(); }
  } catch (e) { wmsg(e.message); } finally { btn.disabled = false; }
};
['w-phone', 'w-api-id', 'w-api-hash', 'w-code', 'w-pass'].forEach((id) => $(id).addEventListener('keydown', (e) => {
  if (e.key === 'Enter') (id.startsWith('w-phone') || id.startsWith('w-api') ? $('w-send') : $('w-verify')).click();
}));

// Camoufox creates the API keys for a manually added number
let aaToken = null, aaTimer = null;
$('w-autoapi').onclick = async () => {
  const m = $('w-aa-msg'); m.textContent = 'Opening my.telegram.org in Camoufox…';
  try {
    const r = await api('POST', '/autoapi/start', { phone: $('w-phone').value, proxy_id: $('w-proxy').value ? Number($('w-proxy').value) : null });
    aaToken = r.token; clearInterval(aaTimer);
    aaTimer = setInterval(async () => {
      const s = await api('GET', '/autoapi/' + aaToken).catch(() => ({ error: 'stopped' }));
      if (s.error) { clearInterval(aaTimer); m.textContent = s.error; $('w-aa-code').classList.add('hidden'); }
      else if (s.api_id) { clearInterval(aaTimer); $('w-api-id').value = s.api_id; $('w-api-hash').value = s.api_hash;
        $('w-aa-code').classList.add('hidden'); m.textContent = 'API keys created and filled in. Press “Send code”.'; }
      else if (s.waiting_code) { $('w-aa-code').classList.remove('hidden'); m.textContent = 'Telegram sent a code to this account in the Telegram app. Enter it.'; }
    }, 1500);
  } catch (e) { m.textContent = e.message; }
};
$('w-aa-send').onclick = async () => {
  try { await api('POST', `/autoapi/${aaToken}/code`, { code: $('w-aa-input').value });
    $('w-aa-code').classList.add('hidden'); $('w-aa-msg').textContent = 'Creating the API keys…'; }
  catch (e) { $('w-aa-msg').textContent = e.message; }
};

// Fully automatic account creation
let provTimer = null;
async function pollProv() {
  const s = await api('GET', '/provision/status');
  $('prov-phases').innerHTML = (s.phases || []).map((p, i) => `<div class="phase ${p.status}"><span>${p.status === 'done' ? '✓' : p.status === 'error' ? '!' : i + 1}</span>${esc(p.label)}</div>`).join('');
  $('prov-steps').innerHTML = s.steps.map((x) => `<li>${esc(x.text)}</li>`).join('');
  $('prov-msg').textContent = s.error ? 'Stopped: ' + s.error : s.done ? 'Account created and connected.' : s.running ? 'Working…' : '';
  $('prov-go').disabled = s.running; $('prov-cancel').disabled = !s.running;
  setGenBtn(s.running);
  clearTimeout(provTimer);
  if (s.running) provTimer = setTimeout(pollProv, 2000); else if (s.done) { loadAccounts(); loadGroups(); refreshStatus(); }
}
const provOpts = () => ({ proxy_id: $('prov-proxy').value ? Number($('prov-proxy').value) : null, country: ($('prov-country').value || 'US').trim(), persona_style: $('prov-style').value.trim(), photo: $('prov-photo').checked, clearapp: $('prov-wipe').checked });
$('acc-auto').onclick = async () => {
  await fetchProxies(); $('prov-proxy').innerHTML = proxyOptions(null, '').replace('No proxy', 'Any working proxy');
  $('prov-settings').classList.toggle('hidden');
};
function setGenBtn(running) {
  const b = $('acc-gen'); if (!b) return;
  b.textContent = running ? '■ Stop generation' : '▶ Start generation';
  b.classList.toggle('danger', !!running); b.dataset.running = running ? '1' : '';
}
async function startGen() {
  await fetchProxies();
  if (!$('prov-proxy').options.length) $('prov-proxy').innerHTML = proxyOptions(null, '').replace('No proxy', 'Any working proxy');
  await api('POST', '/provision/start', provOpts());
}
$('acc-gen').onclick = async () => {
  const b = $('acc-gen'); b.disabled = true;
  try {
    if (b.dataset.running) await api('POST', '/provision/cancel');
    else { await startGen(); show('automation'); }
  } catch (e) { show('automation'); $('prov-msg').textContent = e.message; }
  b.disabled = false; setTimeout(pollProv, 600);
};
setInterval(async () => { try { const s = await api('GET', '/provision/status'); setGenBtn(s.running); } catch (_) {} }, 3000);
$('prov-close').onclick = () => $('prov-settings').classList.add('hidden');
$('prov-cancel').onclick = async () => { await api('POST', '/provision/cancel'); setTimeout(pollProv, 800); };
$('prov-go').onclick = async () => {
  try { await api('POST', '/provision/start', provOpts()); pollProv(); }
  catch (e) { $('prov-msg').textContent = e.message; }
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
refreshStatus(); loadGroups(); loadOverview(); cfxNav();
setInterval(() => { cfxNav(); if (!$('view-overview').classList.contains('hidden')) loadOverview(); }, 10000);
setInterval(refreshStatus, 4000);
setInterval(() => { loadFeed(); if (!$('view-queue').classList.contains('hidden')) loadQueue(); }, 8000);

// ----- detailed generation log -----
let dbgAll = [], dbgLast = 0;
const DBG_RANK = { debug: 0, info: 1, warn: 2, error: 3 };
function dbgLine(e) {
  const d = new Date(e.t * 1000);
  const ts = d.toTimeString().slice(0, 8) + '.' + String(d.getMilliseconds()).padStart(3, '0');
  return `${ts} ${e.level.toUpperCase().padEnd(5)} [${e.src}] ${e.text}`;
}
function dbgRender() {
  const src = $('dbg-src').value, lvl = $('dbg-level').value;
  const rows = dbgAll.filter((e) => (!src || e.src === src) && (!lvl || DBG_RANK[e.level] >= DBG_RANK[lvl]));
  $('dbg-log').innerHTML = rows.map((e) => `<span class="l-${e.level}">${esc(dbgLine(e))}</span>`).join('\n');
  $('dbg-count').textContent = `${rows.length} of ${dbgAll.length} lines`;
  if ($('dbg-follow').checked) $('dbg-log').scrollTop = $('dbg-log').scrollHeight;
}
async function dbgPoll() {
  try {
    const r = await api('GET', '/provision/debug?after=' + dbgLast);
    if (r.entries && r.entries.length) { dbgAll = dbgAll.concat(r.entries).slice(-4000); dbgLast = r.entries[r.entries.length - 1].id; dbgRender(); }
    if (r.file) $('dbg-file').textContent = 'Also saved to: ' + r.file;
  } catch (_) {}
}
$('dbg-src').onchange = dbgRender; $('dbg-level').onchange = dbgRender;
$('dbg-copy').onclick = async () => {
  const src = $('dbg-src').value, lvl = $('dbg-level').value;
  const txt = dbgAll.filter((e) => (!src || e.src === src) && (!lvl || DBG_RANK[e.level] >= DBG_RANK[lvl])).map(dbgLine).join('\n');
  try { await navigator.clipboard.writeText(txt); $('dbg-copy').textContent = 'Copied'; }
  catch (_) { const t = document.createElement('textarea'); t.value = txt; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); $('dbg-copy').textContent = 'Copied'; }
  setTimeout(() => ($('dbg-copy').textContent = 'Copy'), 1500);
};
$('dbg-clear').onclick = async () => { await api('POST', '/provision/debug/clear'); dbgAll = []; dbgRender(); };
dbgPoll(); setInterval(dbgPoll, 1500);


// ----- Persona Management Studio (Per User Per Group) -----
let personaData = { matrix: [], accounts: [], personas: [] };
let activePTab = 'roster';
let activeEdTab = 'profile';

function switchPTab(tab) {
  activePTab = tab;
  document.querySelectorAll('.p-tab').forEach((b) => b.classList.toggle('active', b.dataset.ptab === tab));
  $('p-tab-roster').classList.toggle('hidden', tab !== 'roster');
  $('p-tab-matrix').classList.toggle('hidden', tab !== 'matrix');
  if ($('p-tab-cadence')) {
    $('p-tab-cadence').classList.toggle('hidden', tab !== 'cadence');
    if (tab === 'cadence') loadOrchConfig();
  }
}
document.querySelectorAll('.p-tab').forEach((b) => b.onclick = () => switchPTab(b.dataset.ptab));

function switchEdTab(tab) {
  activeEdTab = tab;
  document.querySelectorAll('.p-ed-tab').forEach((b) => b.classList.toggle('active', b.dataset.edtab === tab));
  $('p-sec-profile').classList.toggle('hidden', tab !== 'profile');
  $('p-sec-voice').classList.toggle('hidden', tab !== 'voice');
  $('p-sec-cadence').classList.toggle('hidden', tab !== 'cadence');
  $('p-sec-prompt').classList.toggle('hidden', tab !== 'prompt');
}
document.querySelectorAll('.p-ed-tab').forEach((b) => b.onclick = () => switchEdTab(b.dataset.edtab));

// Color swatches
document.querySelectorAll('.p-swatch').forEach((s) => {
  s.onclick = () => {
    const c = s.dataset.c;
    $('pm-color').value = c;
    $('pm-color-text').value = c;
    updateModalAvatarPreview();
  };
});

function updateModalAvatarPreview() {
  const name = $('pm-name').value.trim() || 'P';
  const col = $('pm-color').value || '#2fc4b2';
  $('pm-head-avatar').textContent = name[0].toUpperCase();
  $('pm-head-avatar').style.background = col;
  $('pm-head-subtitle').textContent = $('pm-role').value.trim() || 'Detailed Voice, Linguistics & Human Cadence';
}

if ($('pm-name')) $('pm-name').oninput = updateModalAvatarPreview;
if ($('pm-role')) $('pm-role').oninput = updateModalAvatarPreview;
if ($('pm-color')) $('pm-color').oninput = (e) => {
  $('pm-color-text').value = e.target.value;
  updateModalAvatarPreview();
};
if ($('pm-color-text')) $('pm-color-text').oninput = (e) => {
  $('pm-color').value = e.target.value;
  updateModalAvatarPreview();
};

if ($('pm-prompt')) {
  $('pm-prompt').oninput = () => {
    $('pm-prompt-charcount').textContent = `${$('pm-prompt').value.length} chars`;
  };
}

async function loadPersonas() {
  try {
    const res = await api('GET', '/personas/matrix');
    personaData = res;
    renderPersonaRoster();
    renderPersonaMatrix();
  } catch (err) {
    console.error('Failed to load personas:', err);
  }
}

// ----- Primary Persona View & Tab Controller -----
let currentStudioPersonaId = null;

function switchPersonaTab(tabKey) {
  const tabs = ['roster', 'studio', 'matrix', 'cadence'];
  tabs.forEach((t) => {
    const el = $('p-tab-' + t);
    if (el) el.classList.toggle('hidden', t !== tabKey);
  });
  if ($('p-main-tabs')) {
    $('p-main-tabs').querySelectorAll('.p-tab').forEach((b) => {
      b.classList.toggle('active', b.dataset.ptab === tabKey);
    });
  }
}

if ($('p-main-tabs')) {
  $('p-main-tabs').querySelectorAll('.p-tab').forEach((b) => {
    b.onclick = () => {
      const tabKey = b.dataset.ptab;
      if (tabKey === 'studio' && !currentStudioPersonaId) {
        openPersonaStudio(null);
      } else {
        switchPersonaTab(tabKey);
      }
    };
  });
}

function renderPersonaRoster() {
  const container = $('p-roster-grid');
  const countEl = $('p-roster-count');
  const personas = personaData.personas || [];
  const matrix = personaData.matrix || [];
  const accounts = personaData.accounts || [];

  if (countEl) countEl.textContent = `${personas.length} persona${personas.length === 1 ? '' : 's'}`;

  // Calculate high-level summary metrics
  const boundAccountIds = new Set(accounts.filter((a) => a.persona_id).map((a) => a.id));
  const activeGroupsCount = matrix.filter((g) => (g.user_assignments || []).some((u) => u.assigned_persona || u.fallback_persona)).length;

  let totalUnhinged = 0;
  let unhingedCount = 0;
  personas.forEach((p) => {
    const d = p.details || {};
    const u = d.unhinged_level != null ? d.unhinged_level : (d.psychometrics && d.psychometrics.unhinged_level);
    if (u != null) {
      totalUnhinged += Number(u);
      unhingedCount++;
    }
  });
  const avgUnhinged = unhingedCount > 0 ? Math.round(totalUnhinged / unhingedCount) : null;

  if ($('p-metric-total')) $('p-metric-total').textContent = personas.length;
  if ($('p-metric-bound')) $('p-metric-bound').textContent = `${boundAccountIds.size} / ${accounts.length}`;
  if ($('p-metric-groups')) $('p-metric-groups').textContent = activeGroupsCount;
  if ($('p-metric-unhinged')) $('p-metric-unhinged').textContent = avgUnhinged != null ? `${avgUnhinged}%` : '--';

  if (!personas || personas.length === 0) {
    container.innerHTML = `
      <div class="empty" style="grid-column: 1 / -1; padding: 36px 20px; text-align: center;">
        <div style="font-size:24px; margin-bottom:8px;">🎛️</div>
        <strong style="font-size:15px; display:block; margin-bottom:4px;">No personas created yet</strong>
        <p class="hint" style="margin-bottom:14px;">Open the Persona Studio to configure your first autonomous agent persona.</p>
        <button type="button" class="primary" id="p-btn-empty-studio" style="padding:8px 18px; font-weight:600;">+ Open Persona Studio</button>
      </div>
    `;
    const emptyBtn = $('p-btn-empty-studio');
    if (emptyBtn) emptyBtn.onclick = () => openPersonaStudio(null);
    return;
  }

  const query = ($('p-roster-search') ? $('p-roster-search').value.toLowerCase().trim() : '');
  const filtered = personas.filter((p) => {
    if (!query) return true;
    const nameMatch = (p.name || '').toLowerCase().includes(query);
    const bioMatch = (p.bio || '').toLowerCase().includes(query);
    const details = JSON.stringify(p.details || {}).toLowerCase();
    return nameMatch || bioMatch || details.includes(query);
  });

  if (filtered.length === 0) {
    container.innerHTML = '<div class="empty" style="grid-column: 1 / -1;">No personas match your search.</div>';
    return;
  }

  container.innerHTML = filtered.map((p) => {
    const d = p.details || {};
    const t = d.typing || {};
    const col = p.color || '#2fc4b2';

    // Check account bindings
    const boundAccs = accounts.filter((a) => String(a.persona_id) === String(p.id));
    const boundAccLabel = boundAccs.length > 0
      ? boundAccs.map((a) => esc(a.name || a.phone)).join(', ')
      : null;

    // Check group assignments
    let assignedInGroups = 0;
    matrix.forEach((g) => {
      (g.user_assignments || []).forEach((u) => {
        if (u.assigned_persona && String(u.assigned_persona.persona_id) === String(p.id)) assignedInGroups++;
      });
    });

    const unhingedVal = d.unhinged_level != null ? d.unhinged_level : (d.psychometrics && d.psychometrics.unhinged_level != null ? d.psychometrics.unhinged_level : 85);
    const unhingedBadge = unhingedVal >= 80
      ? `<span class="p-tag" style="background:rgba(255,45,85,.15); color:#ff2d55; border-color:rgba(255,45,85,.3); font-weight:700;">🚨 ${unhingedVal}% Unhinged</span>`
      : unhingedVal >= 55
      ? `<span class="p-tag" style="background:rgba(255,149,0,.15); color:var(--warn); border-color:rgba(255,149,0,.3); font-weight:600;">⚡ ${unhingedVal}% Edgy</span>`
      : `<span class="p-tag" style="background:rgba(56,212,139,.12); color:var(--ok); border-color:rgba(56,212,139,.3);">🧘 ${unhingedVal}% Composed</span>`;

    const demoLabel = [d.culture, d.age ? d.age + 'yo' : null, d.occupation || d.role_in_group].filter(Boolean).join(' · ');

    // Account binding options
    const accOptions = '<option value="">(None - Unbound)</option>' + accounts.map((a) => {
      const isSelected = String(a.persona_id) === String(p.id);
      return `<option value="${a.id}" ${isSelected ? 'selected' : ''}>${esc(a.name || a.phone)}</option>`;
    }).join('');

    return `
      <div class="card p-card" style="border-top: 3px solid ${esc(col)};">
        <div class="p-card-header">
          <div class="p-avatar" style="background:${esc(col)};">${esc((p.name || 'P')[0].toUpperCase())}</div>
          <div class="p-card-title">
            <b>${esc(p.name)}</b>
            <div class="hint">${esc(p.bio || d.occupation || 'Autonomous Persona')}</div>
          </div>
          <div>${unhingedBadge}</div>
        </div>

        <div class="p-traits">
          ${demoLabel ? `<span class="p-tag" style="background:rgba(88,86,214,.12); color:#9997ff;">${esc(demoLabel)}</span>` : ''}
          ${d.casing_style ? `<span class="p-tag">${esc(d.casing_style.replace(/_/g, ' '))}</span>` : ''}
          ${d.slang_tier ? `<span class="p-tag">${esc(d.slang_tier.replace(/_/g, ' '))}</span>` : ''}
          ${t.chars_per_second ? `<span class="p-tag">${t.chars_per_second} cps</span>` : ''}
        </div>

        <!-- Inline Account Quick-Binding -->
        <div style="background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:6px 10px; display:flex; align-items:center; justify-content:space-between; gap:8px;">
          <span style="font-size:11.5px; font-weight:600; color:var(--muted); white-space:nowrap;">Default Account:</span>
          <select class="p-select" data-roster-bind-pid="${p.id}" style="font-size:12px; padding:3px 6px; flex:1; max-width:180px;">
            ${accOptions}
          </select>
        </div>

        <div class="p-prompt-preview" title="System Directive">
          <div style="font-size:10px; text-transform:uppercase; color:var(--muted); margin-bottom:2px; font-weight:600; display:flex; justify-content:space-between;">
            <span>System Directive</span>
            <span>${p.prompt ? p.prompt.length : 0} chars</span>
          </div>
          ${esc((p.prompt || 'No custom directive compiled.').slice(0, 140))}${p.prompt && p.prompt.length > 140 ? '…' : ''}
        </div>

        <div class="p-card-footer">
          <button type="button" class="primary" data-proster-edit="${p.id}" style="font-size:12px; font-weight:600; padding:5px 12px; background:linear-gradient(135deg, #007aff, #5856d6); color:#fff; border:none; border-radius:5px; cursor:pointer;">
            🎛️ Open Studio
          </button>
          <button type="button" class="ghost" data-proster-dup="${p.id}" style="font-size:12px;" title="Duplicate persona">&#x2398; Clone</button>
          <button type="button" class="ghost danger" data-proster-del="${p.id}" style="font-size:12px; margin-left:auto;" title="Delete permanently">&times;</button>
        </div>
      </div>
    `;
  }).join('');

  // Handle direct account binding change
  container.querySelectorAll('[data-roster-bind-pid]').forEach((sel) => {
    sel.onchange = async () => {
      const pid = sel.dataset.rosterBindPid;
      const aid = sel.value;
      try {
        if (aid) {
          await api('POST', '/personas/account-bind', { account_id: aid, persona_id: pid });
          toast('Account bound to persona and synced to Network!');
        } else {
          // Unbind any account currently bound to this persona
          const bound = (personaData.accounts || []).find((a) => String(a.persona_id) === String(pid));
          if (bound) {
            await api('POST', '/personas/account-bind', { account_id: bound.id, persona_id: null });
            toast('Account unbound from persona.');
          }
        }
        await loadPersonas();
        if (window.loadNetwork) window.loadNetwork();
      } catch (err) {
        alert('Failed to bind account: ' + err.message);
      }
    };
  });

  container.querySelectorAll('[data-proster-edit]').forEach((el) => {
    el.onclick = () => openPersonaStudio(Number(el.dataset.prosterEdit));
  });

  container.querySelectorAll('[data-proster-dup]').forEach((el) => {
    el.onclick = async () => {
      try {
        await api('POST', `/personas/${el.dataset.prosterDup}/duplicate`);
        toast('Persona duplicated!');
        loadPersonas();
      } catch (err) {
        alert('Failed to duplicate persona: ' + err.message);
      }
    };
  });

  container.querySelectorAll('[data-proster-del]').forEach((el) => {
    el.onclick = async () => {
      if (!confirm('Delete this persona permanently? This will remove all account and group assignments using it.')) return;
      try {
        await api('DELETE', `/personas/${el.dataset.prosterDel}`);
        toast('Persona deleted.');
        await loadPersonas();
        if (window.loadNetwork) window.loadNetwork();
      } catch (err) {
        alert('Failed to delete persona: ' + err.message);
      }
    };
  });
}

if ($('p-roster-search')) {
  $('p-roster-search').oninput = renderPersonaRoster;
}


// ----- Dedicated Persona Studio (Full-Page Workspace) -----

function updateStudioUnhingedMeter(val) {
  const badge = $('ps-unhinged-badge');
  const desc = $('ps-unhinged-desc');
  if (!badge) return;

  if (val >= 81) {
    badge.textContent = `${val}% • Extreme Chaotic Wildcard`;
    badge.style.background = 'rgba(255,45,85,.15)';
    badge.style.color = '#ff2d55';
    badge.style.borderColor = 'rgba(255,45,85,.3)';
    desc.innerHTML = `🚨 <b>Extreme Chaos & Volatility:</b> Zero corporate filter. Unpredictable mood swings, fierce skepticism, sudden tangents, blunt dismissal of weak takes, and spontaneous non-sequiturs.`;
  } else if (val >= 56) {
    badge.textContent = `${val}% • Edgy & Unfiltered`;
    badge.style.background = 'rgba(255,149,0,.15)';
    badge.style.color = 'var(--warn)';
    badge.style.borderColor = 'rgba(255,149,0,.3)';
    desc.innerHTML = `⚡ <b>Edgy & High Energy:</b> Sharp, candid, and prone to passionate arguments. Challenges weak claims with biting humor and zero patience for fluff.`;
  } else if (val >= 26) {
    badge.textContent = `${val}% • Grounded Realist`;
    badge.style.background = 'rgba(88,86,214,.15)';
    badge.style.color = '#9997ff';
    badge.style.borderColor = 'rgba(88,86,214,.3)';
    desc.innerHTML = `🧘 <b>Grounded & Authentic:</b> Balanced human persona with natural conversational quirks, mild skepticism, and relatable day-to-day opinions.`;
  } else {
    badge.textContent = `${val}% • Composed & Structured`;
    badge.style.background = 'rgba(56,212,139,.15)';
    badge.style.color = 'var(--ok)';
    badge.style.borderColor = 'rgba(56,212,139,.3)';
    desc.innerHTML = `🧘 <b>Measured & Composed:</b> Thoughtful, low-reactivity tone with constructive takes and structured sentence flow.`;
  }
}

function getStudioPayload() {
  return {
    id: $('ps-id') && $('ps-id').value ? parseInt($('ps-id').value, 10) : null,
    name: ($('ps-name') ? $('ps-name').value.trim() : '') || 'Liam Carter',
    culture: $('ps-culture') ? $('ps-culture').value : 'american',
    gender: $('ps-gender') ? $('ps-gender').value : 'man',
    age: parseInt($('ps-age') ? $('ps-age').value : '25', 10),
    location: ($('ps-loc') ? $('ps-loc').value.trim() : '') || 'Austin, TX',
    timezone: ($('ps-tz') ? $('ps-tz').value.trim() : '') || 'America/Chicago (UTC-6)',
    occupation: ($('ps-occ') ? $('ps-occ').value.trim() : '') || 'On-chain Trader',
    seniority: $('ps-seniority') ? $('ps-seniority').value : 'drop_out',
    education: $('ps-education') ? $('ps-education').value : 'street_smart',
    telegram_bio: ($('ps-bio') ? $('ps-bio').value.trim() : ''),
    color: $('ps-color') ? $('ps-color').value : '#2fc4b2',
    unhinged_level: parseInt($('ps-unhinged') ? $('ps-unhinged').value : '88', 10),
    emotional_volatility: parseInt($('ps-volatility') ? $('ps-volatility').value : '85', 10),
    cynicism: parseInt($('ps-cynicism') ? $('ps-cynicism').value : '90', 10),
    combative: parseInt($('ps-combative') ? $('ps-combative').value : '80', 10),
    impulsive: parseInt($('ps-impulse') ? $('ps-impulse').value : '85', 10),
    casing_style: $('ps-casing') ? $('ps-casing').value : 'all_lowercase',
    punctuation_style: $('ps-punctuation') ? $('ps-punctuation').value : 'none',
    typo_rate: parseFloat($('ps-typo') ? $('ps-typo').value : '6.0'),
    slang_tier: $('ps-slang') ? $('ps-slang').value : 'crypto_degen',
    burstiness: parseInt($('ps-burst') ? $('ps-burst').value : '65', 10),
    emoji_habit: $('ps-emoji-habit') ? $('ps-emoji-habit').value : 'frequent',
    signature_emojis: ($('ps-emojis') ? $('ps-emojis').value : '💀, 🤡, 🫠').split(',').map((s) => s.trim()).filter(Boolean),
    expertise: ($('ps-expertise') ? $('ps-expertise').value.trim() : ''),
    off_topic_obsessions: ($('ps-offtopic') ? $('ps-offtopic').value.trim() : ''),
    polarizing_takes: ($('ps-hottakes') ? $('ps-hottakes').value.trim() : ''),
    trigger_topics: ($('ps-triggers') ? $('ps-triggers').value.trim() : ''),
    reading_cps: parseInt($('ps-reading-cps') ? $('ps-reading-cps').value : '28', 10),
    min_delay: parseFloat($('ps-min-delay') ? $('ps-min-delay').value : '2.0'),
    max_delay: parseFloat($('ps-max-delay') ? $('ps-max-delay').value : '8.0'),
    bind_account_id: $('ps-bind-account') ? $('ps-bind-account').value : ''
  };
}

async function compileStudioPrompt() {
  const payload = getStudioPayload();
  try {
    const res = await api('POST', '/personas/compile-prompt', payload);
    if (res && res.prompt) {
      if ($('ps-compiled-prompt')) $('ps-compiled-prompt').value = res.prompt;
      if ($('ps-prompt-chars')) $('ps-prompt-chars').textContent = `${res.prompt.length} characters`;
    }
  } catch (e) {
    console.warn('Compile prompt error:', e);
  }
}

function openPersonaStudio(pid) {
  currentStudioPersonaId = pid;
  const p = pid ? (personaData.personas || []).find((x) => x.id === pid) : null;
  const d = p && p.details ? p.details : {};
  const t = d.typing || {};
  const psych = d.psychometrics || {};
  const ling = d.linguistic || {};
  const topics = d.topics || {};
  const accounts = personaData.accounts || [];

  // Populate account binding options
  const boundAcc = pid ? accounts.find((a) => String(a.persona_id) === String(pid)) : null;
  const accSel = $('ps-bind-account');
  if (accSel) {
    accSel.innerHTML = '<option value="">None (Library Persona / Manual Group Binding)</option>' +
      accounts.map((a) => {
        const isSel = boundAcc && String(boundAcc.id) === String(a.id);
        return `<option value="${a.id}" ${isSel ? 'selected' : ''}>${esc(a.name || a.phone)} ${a.username ? '(@' + esc(a.username) + ')' : ''}</option>`;
      }).join('');
  }

  if (p) {
    $('ps-id').value = p.id;
    $('ps-studio-title').textContent = `Editing: ${p.name}`;
    $('ps-name').value = p.name || '';
    $('ps-culture').value = d.culture || 'american';
    $('ps-gender').value = d.gender || 'man';
    const ageVal = d.age || 25;
    $('ps-age').value = ageVal;
    $('ps-age-val').textContent = ageVal;
    $('ps-loc').value = d.location || 'Austin, TX';
    $('ps-tz').value = d.timezone || 'America/Chicago (UTC-6)';
    $('ps-occ').value = d.occupation || d.role_in_group || 'On-chain Trader';
    $('ps-seniority').value = d.seniority || 'drop_out';
    $('ps-education').value = d.education || 'street_smart';
    $('ps-bio').value = p.bio || '';

    const col = p.color || '#2fc4b2';
    $('ps-color').value = col;
    $('ps-color-text').value = col;
    $('ps-avatar-preview').style.background = col;
    $('ps-avatar-preview').textContent = (p.name || 'P')[0].toUpperCase();

    const u = d.unhinged_level != null ? d.unhinged_level : (psych.unhinged_level != null ? psych.unhinged_level : 88);
    $('ps-unhinged').value = u;
    updateStudioUnhingedMeter(u);

    const vVol = psych.emotional_volatility != null ? psych.emotional_volatility : 85;
    $('ps-volatility').value = vVol;
    $('ps-volatility-val').textContent = vVol + '%';

    const vCyn = psych.cynicism != null ? psych.cynicism : 90;
    $('ps-cynicism').value = vCyn;
    $('ps-cynicism-val').textContent = vCyn + '%';

    const vCom = psych.combative != null ? psych.combative : 80;
    $('ps-combative').value = vCom;
    $('ps-combative-val').textContent = vCom + '%';

    const vImp = psych.impulsive != null ? psych.impulsive : 85;
    $('ps-impulse').value = vImp;
    $('ps-impulse-val').textContent = vImp + '%';

    $('ps-casing').value = ling.casing || d.casing_style || 'all_lowercase';
    $('ps-punctuation').value = ling.punctuation || 'none';
    const typoVal = ling.typo_rate != null ? ling.typo_rate : 6.0;
    $('ps-typo').value = typoVal;
    $('ps-typo-val').textContent = typoVal + '%';

    $('ps-slang').value = ling.slang_tier || 'crypto_degen';
    const burstVal = ling.burstiness != null ? ling.burstiness : (t.burstiness_percent || 65);
    $('ps-burst').value = burstVal;
    $('ps-burst-val').textContent = burstVal + '%';

    $('ps-emoji-habit').value = ling.emoji_habit || d.emoji_habit || 'frequent';
    $('ps-emojis').value = Array.isArray(ling.signature_emojis) ? ling.signature_emojis.join(', ') : (ling.signature_emojis || '💀, 🤡, 🫠, 🚩');

    $('ps-expertise').value = topics.expertise || 'On-chain token flows, memecoin liquidity pools, smart contract exploits';
    $('ps-offtopic').value = Array.isArray(topics.off_topic) ? topics.off_topic.join(', ') : (topics.off_topic || 'yerba mate, conspiracy rabbit holes, adderall shortages');
    $('ps-hottakes').value = topics.hot_takes || '99% of web3 founders are grifters who never wrote code; centralized exchanges are rigged casinos';
    $('ps-triggers').value = (Array.isArray(topics.triggers) ? topics.triggers.join(', ') : (topics.triggers || 'VC token unlock schedules, sponsored influencer shills'));

    $('ps-reading-cps').value = t.chars_per_second || 28;
    $('ps-min-delay').value = t.min_seconds || 2.0;
    $('ps-max-delay').value = t.max_seconds || 8.0;

    $('ps-compiled-prompt').value = p.prompt || '';
    $('ps-prompt-chars').textContent = `${(p.prompt || '').length} characters`;
  } else {
    // New persona default template
    $('ps-id').value = '';
    $('ps-studio-title').textContent = 'Create New Industrial Persona';
    $('ps-name').value = '';
    $('ps-culture').value = 'american';
    $('ps-gender').value = 'man';
    $('ps-age').value = 25;
    $('ps-age-val').textContent = '25';
    $('ps-loc').value = 'Austin, TX';
    $('ps-tz').value = 'America/Chicago (UTC-6)';
    $('ps-occ').value = 'Quantitative Trader';
    $('ps-seniority').value = 'drop_out';
    $('ps-education').value = 'street_smart';
    $('ps-bio').value = 'on-chain analytics by day, insomnia by night. skeptical of everything.';

    const col = '#2fc4b2';
    $('ps-color').value = col;
    $('ps-color-text').value = col;
    $('ps-avatar-preview').style.background = col;
    $('ps-avatar-preview').textContent = '+';

    $('ps-unhinged').value = 88;
    updateStudioUnhingedMeter(88);
    $('ps-volatility').value = 85;
    $('ps-volatility-val').textContent = '85%';
    $('ps-cynicism').value = 90;
    $('ps-cynicism-val').textContent = '90%';
    $('ps-combative').value = 80;
    $('ps-combative-val').textContent = '80%';
    $('ps-impulse').value = 85;
    $('ps-impulse-val').textContent = '85%';

    $('ps-casing').value = 'all_lowercase';
    $('ps-punctuation').value = 'none';
    $('ps-typo').value = 6.0;
    $('ps-typo-val').textContent = '6.0%';
    $('ps-slang').value = 'crypto_degen';
    $('ps-burst').value = 65;
    $('ps-burst-val').textContent = '65%';
    $('ps-emoji-habit').value = 'frequent';
    $('ps-emojis').value = '💀, 🤡, 🫠, 🚩';

    $('ps-expertise').value = 'On-chain token flows, memecoin liquidity pools, smart contract exploits';
    $('ps-offtopic').value = 'yerba mate, conspiracy rabbit holes, adderall shortages, obscure memecoins';
    $('ps-hottakes').value = '99% of web3 founders are grifters who never wrote code; centralized exchanges are rigged casinos';
    $('ps-triggers').value = 'VC token unlock schedules, sponsored influencer shills, overly polite corporate bots';

    $('ps-reading-cps').value = 28;
    $('ps-min-delay').value = 2.0;
    $('ps-max-delay').value = 8.0;

    compileStudioPrompt();
  }

  switchPersonaTab('studio');
}

async function savePersonaStudio() {
  const p = getStudioPayload();
  const name = p.name || 'Anonymous Persona';
  let prompt = $('ps-compiled-prompt') ? $('ps-compiled-prompt').value.trim() : '';
  if (!prompt) {
    await compileStudioPrompt();
    prompt = $('ps-compiled-prompt') ? $('ps-compiled-prompt').value.trim() : '';
  }

  const bio = p.telegram_bio || `${p.culture} ${p.occupation}`;
  const color = p.color || '#2fc4b2';

  const details = {
    culture: p.culture,
    gender: p.gender,
    age: p.age,
    location: p.location,
    timezone: p.timezone,
    occupation: p.occupation,
    seniority: p.seniority,
    education: p.education,
    unhinged_level: p.unhinged_level,
    psychometrics: {
      unhinged_level: p.unhinged_level,
      emotional_volatility: p.emotional_volatility,
      cynicism: p.cynicism,
      combative: p.combative,
      impulsive: p.impulsive
    },
    linguistic: {
      casing: p.casing_style,
      punctuation: p.punctuation_style,
      typo_rate: p.typo_rate,
      slang_tier: p.slang_tier,
      burstiness: p.burstiness,
      emoji_habit: p.emoji_habit,
      signature_emojis: p.signature_emojis
    },
    topics: {
      expertise: p.expertise,
      off_topic: p.off_topic_obsessions,
      hot_takes: p.polarizing_takes,
      triggers: p.trigger_topics
    },
    typing: {
      chars_per_second: p.reading_cps,
      min_seconds: p.min_delay,
      max_seconds: p.max_delay,
      burstiness_percent: p.burstiness
    },
    role_in_group: p.occupation
  };

  try {
    const res = await api('POST', '/personas', {
      id: p.id || undefined,
      name,
      prompt,
      color,
      bio,
      details
    });

    const savedId = res.id || p.id;

    // Handle direct account binding if selected
    if (p.bind_account_id && savedId) {
      await api('POST', '/personas/account-bind', {
        account_id: p.bind_account_id,
        persona_id: savedId
      });
    }

    toast(`Saved persona “${name}” and synced to Network!`);
    await loadPersonas();
    if (window.loadNetwork) window.loadNetwork();
    switchPersonaTab('roster');
  } catch (err) {
    alert('Failed to save persona: ' + err.message);
  }
}

// Live Studio Input Listeners
if ($('ps-unhinged')) $('ps-unhinged').oninput = (e) => updateStudioUnhingedMeter(parseInt(e.target.value, 10));
if ($('ps-age')) $('ps-age').oninput = (e) => { $('ps-age-val').textContent = e.target.value; };
if ($('ps-volatility')) $('ps-volatility').oninput = (e) => { $('ps-volatility-val').textContent = e.target.value + '%'; };
if ($('ps-cynicism')) $('ps-cynicism').oninput = (e) => { $('ps-cynicism-val').textContent = e.target.value + '%'; };
if ($('ps-combative')) $('ps-combative').oninput = (e) => { $('ps-combative-val').textContent = e.target.value + '%'; };
if ($('ps-impulse')) $('ps-impulse').oninput = (e) => { $('ps-impulse-val').textContent = e.target.value + '%'; };
if ($('ps-typo')) $('ps-typo').oninput = (e) => { $('ps-typo-val').textContent = e.target.value + '%'; };
if ($('ps-burst')) $('ps-burst').oninput = (e) => { $('ps-burst-val').textContent = e.target.value + '%'; };

if ($('ps-color')) $('ps-color').oninput = (e) => {
  $('ps-color-text').value = e.target.value;
  $('ps-avatar-preview').style.background = e.target.value;
};
if ($('ps-color-text')) $('ps-color-text').oninput = (e) => {
  $('ps-color').value = e.target.value;
  $('ps-avatar-preview').style.background = e.target.value;
};
if ($('ps-name')) $('ps-name').oninput = (e) => {
  const n = e.target.value.trim();
  $('ps-avatar-preview').textContent = (n || 'P')[0].toUpperCase();
  $('ps-studio-title').textContent = n ? `Editing: ${n}` : 'Create New Industrial Persona';
};

if ($('ps-btn-recompile')) $('ps-btn-recompile').onclick = compileStudioPrompt;
if ($('ps-btn-save')) $('ps-btn-save').onclick = savePersonaStudio;
if ($('ps-btn-save-side')) $('ps-btn-save-side').onclick = savePersonaStudio;
if ($('ps-btn-cancel')) $('ps-btn-cancel').onclick = () => switchPersonaTab('roster');
if ($('ps-btn-cancel-side')) $('ps-btn-cancel-side').onclick = () => switchPersonaTab('roster');
if ($('ps-btn-back')) $('ps-btn-back').onclick = () => switchPersonaTab('roster');

if ($('p-btn-new')) $('p-btn-new').onclick = () => openPersonaStudio(null);
if ($('p-btn-new-roster')) $('p-btn-new-roster').onclick = () => openPersonaStudio(null);


// ----- Matrix View (Per User Per Group Assignments) -----

function renderPersonaMatrix() {
  const container = $('p-matrix-list');
  const matrix = personaData.matrix || [];
  if (!matrix || matrix.length === 0) {
    container.innerHTML = '<div class="empty">No Telegram groups found yet. Sync groups in Network or open Groups.</div>';
    return;
  }

  const query = ($('p-matrix-search') ? $('p-matrix-search').value.toLowerCase().trim() : '');
  const filtered = matrix.filter((g) => {
    if (!query) return true;
    return (g.title || '').toLowerCase().includes(query) || String(g.chat_id).includes(query);
  });

  if (filtered.length === 0) {
    container.innerHTML = '<div class="empty">No groups match your filter.</div>';
    return;
  }

  container.innerHTML = filtered.map((g) => {
    const userRows = (g.user_assignments || []).map((u) => {
      const assigned = u.assigned_persona;
      const fallback = u.fallback_persona;
      const effective = assigned || fallback;
      const hasBaseline = Boolean(fallback);
      const perOptions = `<option value="">None (Account Default)</option>` +
        (personaData.personas || []).map((p) => `<option value="${p.id}" ${assigned && String(assigned.persona_id) === String(p.id) ? 'selected' : ''}>${esc(p.name)}</option>`).join('');

      let typingInfo = '';
      if (effective && effective.details && effective.details.typing) {
        const t = effective.details.typing;
        typingInfo = `<span class="badge" title="Typing simulation">${t.chars_per_second || 24} cps · ${t.min_seconds || 2}-${t.max_seconds || 8}s</span>`;
      }

      return `
        <div class="p-user-row">
          <div class="p-user-info">
            <span class="p-user-avatar">${esc((u.account_name || 'U')[0].toUpperCase())}</span>
            <div>
              <b>${esc(u.account_name)}</b>
              <div class="hint">${esc(u.account_phone || '')} · ${u.active ? '<span style="color:var(--ok)">Active</span>' : 'Paused'}</div>
            </div>
          </div>
          <div class="p-user-persona-sel">
            <select data-matrix-cid="${g.chat_id}" data-matrix-aid="${u.account_id}" data-has-fallback="${hasBaseline ? '1' : '0'}" class="p-select">
              ${perOptions}
            </select>
          </div>
          <div class="p-user-status">
            ${assigned ? `<span class="badge ok" style="background:rgba(56,212,139,.12); color:var(--ok); border-color:rgba(56,212,139,.25);">Group Custom</span>` 
                       : fallback ? `<span class="badge" style="background:rgba(47,196,178,.12); color:var(--accent);">Account Default: ${esc(fallback.name)}</span>`
                       : `<span class="badge warn" style="background:rgba(245,184,74,.12); color:var(--warn); border-color:rgba(245,184,74,.3);">⚠️ No Persona Connected</span>`}
            ${typingInfo}
            ${!hasBaseline ? `<button class="p-bind-acc-btn" data-bind-aid="${u.account_id}" data-bind-aname="${esc(u.account_name)}" style="margin-top:4px;">Connect Persona to Account</button>` : ''}
          </div>
          <div class="p-user-actions">
            ${effective ? `<button type="button" class="ghost" data-pedit="${effective.id || effective.persona_id}" style="padding:4px 8px; font-size:12px;">Studio</button>` : ''}
          </div>
        </div>
      `;
    }).join('');

    return `
      <div class="card p-group-card">
        <div class="p-group-head">
          <div>
            <h3 style="margin:0; display:flex; align-items:center; gap:8px;">
              ${esc(g.title || 'Untitled Group')}
              <span class="hint" style="font-size:12px; font-weight:normal;">(${g.chat_id})</span>
            </h3>
            <div class="hint" style="margin-top:3px;">${g.user_assignments ? g.user_assignments.length : 0} active accounts in group</div>
          </div>
          <button class="ghost" data-pgen-cid="${g.chat_id}" style="font-size:12px;">&#10024; Generate Personas For Group</button>
        </div>
        <div class="p-group-table-head">
          <span>Telegram Account</span>
          <span>Assigned Persona</span>
          <span>Status &amp; Dynamics</span>
          <span>Action</span>
        </div>
        <div class="p-group-users">${userRows || '<div class="empty">No accounts linked to this group.</div>'}</div>
      </div>
    `;
  }).join('');

  // Handle assignments change
  container.querySelectorAll('select[data-matrix-cid]').forEach((sel) => {
    sel.onchange = async () => {
      const cid = sel.dataset.matrixCid;
      const aid = sel.dataset.matrixAid;
      const pid = sel.value ? Number(sel.value) : null;
      const hasFallback = sel.dataset.hasFallback === '1';
      const shouldAutoBind = (!hasFallback && pid);
      try {
        await api('POST', '/personas/assign-matrix', {
          chat_id: cid,
          account_id: aid,
          persona_id: pid,
          set_as_account_persona: shouldAutoBind
        });
        toast('Assignment saved & synced to Network!');
        await loadPersonas();
        if (window.loadNetwork) window.loadNetwork();
      } catch (err) {
        alert('Failed to update persona assignment: ' + err.message);
      }
    };
  });

  // Handle direct account binding button in matrix
  container.querySelectorAll('[data-bind-aid]').forEach((btn) => {
    btn.onclick = async () => {
      const aid = btn.dataset.bindAid;
      const aname = btn.dataset.bindAname || 'Account';
      const personas = personaData.personas || [];
      if (personas.length === 0) {
        alert('No personas created yet! Click "+ New Persona Studio" first.');
        return;
      }
      const choices = personas.map((p, idx) => `${idx + 1}. ${p.name}`).join('\n');
      const pick = prompt(`Select a default persona to connect to account "${aname}":\n\n${choices}\n\nEnter number (1-${personas.length}):`);
      if (!pick) return;
      const num = parseInt(pick, 10);
      if (isNaN(num) || num < 1 || num > personas.length) {
        alert('Invalid selection');
        return;
      }
      const selectedPersona = personas[num - 1];
      try {
        await api('POST', '/personas/account-bind', { account_id: aid, persona_id: selectedPersona.id });
        toast(`Bound ${selectedPersona.name} to ${aname} and synced to Network!`);
        await loadPersonas();
        if (window.loadNetwork) window.loadNetwork();
      } catch (err) {
        alert('Failed to bind persona: ' + err.message);
      }
    };
  });

  container.querySelectorAll('[data-pedit]').forEach((btn) => {
    btn.onclick = () => openPersonaStudio(Number(btn.dataset.pedit));
  });

  container.querySelectorAll('[data-pgen-cid]').forEach((btn) => {
    btn.onclick = () => openGenModal(btn.dataset.pgenCid);
  });
}

if ($('p-matrix-search')) {
  $('p-matrix-search').oninput = renderPersonaMatrix;
}

// Generation Modal (Study Group)
function openGenModal(preselectedCid) {
  const select = $('pg-group');
  select.innerHTML = (personaData.matrix || []).map((g) =>
    `<option value="${g.chat_id}" ${preselectedCid && String(g.chat_id) === String(preselectedCid) ? 'selected' : ''}>${esc(g.title)}</option>`).join('');
  $('pg-status').textContent = '';
  $('p-gen-modal').classList.remove('hidden');
}

if ($('p-btn-gen')) $('p-btn-gen').onclick = () => openGenModal(null);
if ($('p-gen-close')) $('p-gen-close').onclick = () => $('p-gen-modal').classList.add('hidden');
if ($('pg-cancel')) $('pg-cancel').onclick = () => $('p-gen-modal').classList.add('hidden');

if ($('pg-run')) {
  $('pg-run').onclick = async () => {
    const cid = $('pg-group').value;
    if (!cid) return;
    const count = Number($('pg-count').value) || 3;
    const dir = $('pg-dir').value.trim();
    $('pg-status').textContent = 'Reading group history and generating personas...';
    $('pg-run').disabled = true;
    try {
      const r = await api('POST', `/group-personas/${cid}/generate`, { count, direction: dir });
      $('pg-status').textContent = `Success! Created ${r.ids.length} personas.`;
      setTimeout(() => {
        $('p-gen-modal').classList.add('hidden');
        $('pg-run').disabled = false;
        loadPersonas();
      }, 1200);
    } catch (err) {
      $('pg-status').textContent = 'Failed: ' + err.message;
      $('pg-run').disabled = false;
    }
  };
}

// ----- Direct Messages & Inbound Chats Management -----
let dmChatsList = [];
let activeDmCid = null;
let activeDmAid = '';

async function loadDirectChats() {
  try {
    const chats = await api('GET', '/direct-chats');
    dmChatsList = chats || [];
    renderDmFilters();
    renderDmChatList();
  } catch (err) {
    console.error('Failed to load direct chats:', err);
    $('dm-chat-list').innerHTML = `<div class="empty">Error loading chats: ${esc(err.message)}</div>`;
  }
}

function renderDmFilters() {
  const container = $('dm-account-filters');
  const aids = [...new Set(dmChatsList.map((c) => c.account_id).filter(Boolean))];
  let html = `<button class="dm-filter-btn ${!activeDmAid ? 'active' : ''}" data-aid="">All Accounts</button>`;
  aids.forEach((aid) => {
    const sample = dmChatsList.find((c) => c.account_id === aid);
    const label = sample ? (sample.account_phone || sample.account_name || aid) : aid;
    html += `<button class="dm-filter-btn ${activeDmAid === aid ? 'active' : ''}" data-aid="${esc(aid)}">${esc(label)}</button>`;
  });
  container.innerHTML = html;

  container.querySelectorAll('.dm-filter-btn').forEach((b) => {
    b.onclick = () => {
      activeDmAid = b.dataset.aid || '';
      renderDmFilters();
      renderDmChatList();
    };
  });
}

function renderDmChatList() {
  const container = $('dm-chat-list');
  const query = ($('dm-search') ? $('dm-search').value.toLowerCase().trim() : '');

  const filtered = dmChatsList.filter((c) => {
    if (activeDmAid && c.account_id !== activeDmAid) return false;
    if (!query) return true;
    const searchSpace = [c.title, c.first_name, c.last_name, c.username, c.last_message, c.account_phone].filter(Boolean).join(' ').toLowerCase();
    return searchSpace.includes(query);
  });

  if (filtered.length === 0) {
    container.innerHTML = '<div class="empty">No direct messages found. When users message any of your accounts, they will appear here automatically.</div>';
    return;
  }

  container.innerHTML = filtered.map((c) => {
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.title || (c.username ? `@${c.username}` : `User ${c.peer_id}`);
    const initial = (name || 'U')[0].toUpperCase();
    const isActive = activeDmCid && String(activeDmCid) === String(c.chat_id);
    const timeStr = c.last_message_at ? new Date(c.last_message_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';

    return `
      <div class="dm-chat-item ${isActive ? 'active' : ''}" data-dm-cid="${c.chat_id}">
        <div class="dm-chat-avatar">${esc(initial)}</div>
        <div class="dm-chat-meta">
          <div class="dm-chat-top">
            <span class="dm-chat-name">${esc(name)}</span>
            <span class="dm-chat-time">${esc(timeStr)}</span>
          </div>
          <div class="dm-chat-snippet">${esc(c.last_message || 'No messages yet')}</div>
          <div style="display:flex; justify-content:space-between; align-items:center; margin-top:3px;">
            <span class="hint" style="font-size:10.5px;">on ${esc(c.account_phone || c.account_name || c.account_id)}</span>
            ${c.unread_count > 0 ? `<span class="dm-chat-badge">${c.unread_count}</span>` : ''}
          </div>
        </div>
      </div>
    `;
  }).join('');

  container.querySelectorAll('[data-dm-cid]').forEach((el) => {
    el.onclick = () => selectDirectChat(el.dataset.dmCid);
  });
}

if ($('dm-search')) {
  $('dm-search').oninput = renderDmChatList;
}

if ($('dm-btn-sync')) {
  $('dm-btn-sync').onclick = async () => {
    $('dm-btn-sync').textContent = 'Syncing...';
    try {
      await api('POST', '/graph/sync');
      await loadDirectChats();
      $('dm-btn-sync').textContent = 'Synced ✓';
      setTimeout(() => { $('dm-btn-sync').textContent = '↻ Sync DMs'; }, 1500);
    } catch (err) {
      alert('Failed to sync dialogs: ' + err.message);
      $('dm-btn-sync').textContent = '↻ Sync DMs';
    }
  };
}

async function selectDirectChat(cid) {
  activeDmCid = cid;
  renderDmChatList();

  const chat = dmChatsList.find((c) => String(c.chat_id) === String(cid));
  if (!chat) return;

  $('dm-empty-state').classList.add('hidden');
  $('dm-chat-view').classList.remove('hidden');

  const name = [chat.first_name, chat.last_name].filter(Boolean).join(' ') || chat.title || (chat.username ? `@${chat.username}` : `User ${chat.peer_id}`);
  $('dm-head-name').textContent = name;
  $('dm-head-user').textContent = chat.username ? `@${chat.username}` : '';
  $('dm-head-avatar').textContent = (name || 'U')[0].toUpperCase();
  $('dm-head-account').textContent = chat.account_phone || chat.account_name || chat.account_id;

  // Persona selector
  const pSelect = $('dm-persona-select');
  pSelect.innerHTML = `<option value="">Account Default Persona</option>` +
    (personaData.personas || []).map((p) => `<option value="${p.id}" ${chat.persona_id && String(chat.persona_id) === String(p.id) ? 'selected' : ''}>${esc(p.name)}</option>`).join('');

  pSelect.onchange = async () => {
    const pid = pSelect.value ? Number(pSelect.value) : null;
    try {
      await api('POST', `/direct-chats/${cid}/settings`, { persona_id: pid });
      chat.persona_id = pid;
    } catch (err) {
      alert('Failed to update persona for this conversation: ' + err.message);
    }
  };

  const autoToggle = $('dm-autoreply-toggle');
  autoToggle.checked = !!chat.auto_reply;
  autoToggle.onchange = async () => {
    try {
      await api('POST', `/direct-chats/${cid}/settings`, { auto_reply: autoToggle.checked });
      chat.auto_reply = autoToggle.checked ? 1 : 0;
    } catch (err) {
      alert('Failed to update auto-reply: ' + err.message);
    }
  };

  // Load message history
  await loadDirectMessages(cid);
}

async function loadDirectMessages(cid) {
  const box = $('dm-messages-box');
  box.innerHTML = '<div class="empty">Loading message thread...</div>';
  try {
    const messages = await api('GET', `/direct-chats/${cid}/messages?limit=60`);
    if (!messages || messages.length === 0) {
      box.innerHTML = '<div class="empty">No messages in this conversation yet.</div>';
      return;
    }

    box.innerHTML = messages.map((m) => {
      const isOut = !!m.outgoing;
      const timeStr = m.created_at ? new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
      return `
        <div class="dm-bubble ${isOut ? 'outgoing' : 'incoming'}">
          <div>${esc(m.text || '')}</div>
          <div class="dm-bubble-meta">${esc(timeStr)} ${isOut ? '· Sent' : ''}</div>
        </div>
      `;
    }).join('');

    box.scrollTop = box.scrollHeight;
  } catch (err) {
    box.innerHTML = `<div class="empty">Error loading messages: ${esc(err.message)}</div>`;
  }
}

// Sending reply in DM
async function sendDirectReply() {
  if (!activeDmCid) return;
  const input = $('dm-reply-input');
  const text = input.value.trim();
  if (!text) return;

  const btn = $('dm-btn-send');
  btn.disabled = true;
  btn.textContent = 'Sending...';

  try {
    await api('POST', `/direct-chats/${activeDmCid}/send`, { text });
    input.value = '';
    btn.disabled = false;
    btn.textContent = 'Send';
    await loadDirectMessages(activeDmCid);
    await loadDirectChats();
  } catch (err) {
    alert('Failed to send direct message: ' + err.message);
    btn.disabled = false;
    btn.textContent = 'Send';
  }
}

if ($('dm-btn-send')) $('dm-btn-send').onclick = sendDirectReply;

if ($('dm-reply-input')) {
  $('dm-reply-input').onkeydown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      sendDirectReply();
    }
  };
}

// AI Persona Draft in DM
if ($('dm-btn-ai-draft')) {
  $('dm-btn-ai-draft').onclick = async () => {
    if (!activeDmCid) return;
    const status = $('dm-draft-status');
    status.textContent = 'Drafting reply in persona voice...';
    try {
      const res = await api('POST', `/direct-chats/${activeDmCid}/draft`);
      if (res && res.draft) {
        $('dm-reply-input').value = res.draft;
        status.textContent = 'Draft ready! Review and hit Send.';
      } else {
        status.textContent = 'No draft generated.';
      }
      setTimeout(() => { status.textContent = ''; }, 4000);
    } catch (err) {
      status.textContent = 'Draft failed: ' + err.message;
      setTimeout(() => { status.textContent = ''; }, 4000);
    }
  };
}
// ----- Cadence & Orchestrator Configuration -----
async function loadOrchConfig() {
  try {
    const c = await api('GET', '/orchestrator/config');
    if ($('orch-enabled')) $('orch-enabled').checked = !!c.enabled;
    if ($('orch-spontaneous')) $('orch-spontaneous').checked = !!c.allow_spontaneous;
    if ($('orch-threshold')) {
      $('orch-threshold').value = c.base_threshold || 0.52;
      $('orch-thresh-val').textContent = `(${c.base_threshold || 0.52})`;
    }
    if ($('orch-reading-cps')) $('orch-reading-cps').value = c.reading_cps || 32;
    if ($('orch-circadian')) $('orch-circadian').checked = !!c.circadian_enabled;
    if ($('orch-peak-hour')) $('orch-peak-hour').value = c.circadian_peak_hour != null ? c.circadian_peak_hour : 15;
    if ($('orch-anti-dogpile')) $('orch-anti-dogpile').value = c.anti_dogpile_window || 45;
    if ($('orch-fatigue-tau')) $('orch-fatigue-tau').value = c.fatigue_decay_tau || 240;
  } catch (err) {
    console.error('Failed to load orchestrator config:', err);
  }
}

if ($('orch-threshold')) {
  $('orch-threshold').oninput = (e) => {
    $('orch-thresh-val').textContent = `(${e.target.value})`;
  };
}

if ($('orch-save')) {
  $('orch-save').onclick = async () => {
    const btn = $('orch-save');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      await api('POST', '/orchestrator/config', {
        enabled: $('orch-enabled').checked,
        allow_spontaneous: $('orch-spontaneous').checked,
        base_threshold: parseFloat($('orch-threshold').value),
        reading_cps: parseFloat($('orch-reading-cps').value),
        circadian_enabled: $('orch-circadian').checked,
        circadian_peak_hour: parseInt($('orch-peak-hour').value, 10),
        anti_dogpile_window: parseFloat($('orch-anti-dogpile').value),
        fatigue_decay_tau: parseFloat($('orch-fatigue-tau').value)
      });
      btn.textContent = 'Saved ✓';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Save Orchestration Parameters'; }, 1500);
    } catch (err) {
      alert('Error saving orchestration parameters: ' + err.message);
      btn.disabled = false;
      btn.textContent = 'Save Orchestration Parameters';
    }
  };
}


// ----- Test Group Chat (fal.ai cheap model) -----
window.openGroupTestChatModal = async function(presetChatId, presetGroupTitle) {
  const modal = $('p-testchat-modal');
  if (!modal) return;

  const groupSel = $('ptc-group');
  groupSel.innerHTML = '<option value="">Loading groups...</option>';
  $('ptc-status').textContent = '';
  $('ptc-status').className = 'hint';
  $('ptc-transcript').style.display = 'none';
  $('ptc-transcript').innerHTML = '';
  $('ptc-submit').disabled = false;
  $('ptc-submit').textContent = '⚡ Run Test Chat';

  modal.classList.remove('hidden');

  try {
    const grpRes = await api('GET', '/groups');
    const grps = grpRes.groups || grpRes || [];
    if (grps.length) {
      groupSel.innerHTML = grps.map(g => `<option value="${g.chat_id}" ${String(g.chat_id) === String(presetChatId) ? 'selected' : ''}>${escapeHtml(g.title || 'Group ' + g.chat_id)}</option>`).join('');
    } else if (presetChatId) {
      groupSel.innerHTML = `<option value="${presetChatId}" selected>${escapeHtml(presetGroupTitle || 'Group ' + presetChatId)}</option>`;
    } else {
      groupSel.innerHTML = '<option value="auto">Auto-detect group with active members</option>';
    }
  } catch (e) {
    if (presetChatId) {
      groupSel.innerHTML = `<option value="${presetChatId}" selected>${escapeHtml(presetGroupTitle || 'Group ' + presetChatId)}</option>`;
    } else {
      groupSel.innerHTML = '<option value="auto">Auto-detect group with active members</option>';
    }
  }
};

async function runGroupTestChat() {
  const modal = $('p-testchat-modal');
  const groupVal = $('ptc-group').value;
  const modelVal = $('ptc-model').value;
  const turnsVal = parseInt($('ptc-turns').value, 10) || 4;
  const topicVal = $('ptc-topic').value.trim() || 'Casual natural check-in and banter';
  const sendLiveVal = $('ptc-send-live').checked;

  const statusEl = $('ptc-status');
  const transEl = $('ptc-transcript');
  const submitBtn = $('ptc-submit');

  submitBtn.disabled = true;
  submitBtn.textContent = sendLiveVal ? 'Simulating & Sending to Telegram...' : 'Generating dialogue with fal.ai...';
  statusEl.textContent = 'Calling fal.ai model (' + modelVal + ')...';
  statusEl.className = 'hint';
  transEl.style.display = 'flex';
  transEl.innerHTML = '<div style="text-align:center; padding:12px; color:var(--muted); font-size:12px;"><i>Synthesizing conversation between group personas...</i></div>';

  try {
    const res = await api('POST', '/groups/test-chat', {
      chat_id: groupVal || null,
      model: modelVal,
      turns: turnsVal,
      topic: topicVal,
      send_live: sendLiveVal,
      anti_pattern: ($("ptc-antipattern") ? $("ptc-antipattern").checked : true)
    });

    if (!res.ok) throw new Error(res.error || 'Failed to generate test chat');

    const participants = res.participants || [];
    const partNames = participants.map(p => p.name).join(' & ');
    statusEl.innerHTML = `<span style="color:var(--ok); font-weight:600;">✓ Completed:</span> ${escapeHtml(partNames)} in <b>${escapeHtml(res.group_title || 'Group')}</b> ${res.send_live ? '• <span style="color:var(--ok);">Sent live to Telegram</span>' : '• Preview generated'}`;

    const turns = res.turns || [];
    const pA = participants[0] || { id: 0, name: 'Participant 1' };

    transEl.innerHTML = turns.map(t => {
      const isA = String(t.account_id) === String(pA.id);
      return `<div style="display:flex; flex-direction:column; align-items:${isA ? 'flex-start' : 'flex-end'}; margin-bottom:8px;">
        <span style="font-size:11px; color:var(--muted); margin-bottom:3px; font-weight:600;">
          ${escapeHtml(t.sender || (isA ? pA.name : 'Participant 2'))}
          ${t.sent ? ' <span style="color:var(--ok); font-size:10px;">✓ Sent</span>' : (res.send_live ? ' <span style="color:var(--warn); font-size:10px;">(Pending/Offline)</span>' : '')}
        </span>
        <div style="max-width:82%; padding:8px 12px; border-radius:10px; font-size:13px; line-height:1.4; background:${isA ? 'var(--panel)' : 'rgba(88,86,214,.2)'}; border:1px solid ${isA ? 'var(--border)' : 'rgba(88,86,214,.35)'};">
          ${escapeHtml(t.text)}
        </div>
      </div>`;
    }).join('');

    submitBtn.disabled = false;
    submitBtn.textContent = '⚡ Run Another Test Chat';
    toast(res.send_live ? 'Test chat delivered to Telegram group!' : 'Test dialogue generated!');
  } catch (err) {
    statusEl.innerHTML = `<span style="color:var(--danger); font-weight:600;">Error:</span> ${escapeHtml(err.message)}`;
    submitBtn.disabled = false;
    submitBtn.textContent = '⚡ Run Test Chat';
    toast(err.message, true);
  }
}


if ($('p-btn-test-chat')) $('p-btn-test-chat').onclick = () => window.openGroupTestChatModal();
if ($('ptc-close')) $('ptc-close').onclick = () => $('p-testchat-modal').classList.add('hidden');
if ($('ptc-cancel')) $('ptc-cancel').onclick = () => $('p-testchat-modal').classList.add('hidden');
if ($('ptc-submit')) $('ptc-submit').onclick = runGroupTestChat;
