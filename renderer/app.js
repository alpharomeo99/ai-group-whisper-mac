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
  ['overview', 'network', 'personas', 'groups', 'accounts', 'automation', 'queue', 'settings'].forEach((v) => $('view-' + v).classList.toggle('hidden', v !== view));
  document.querySelector('main').classList.toggle('flush', view === 'network');
  if (view === 'network') window.loadNetwork();
  if (view === 'overview') loadOverview();
  if (view === 'queue') loadQueue();
  if (view === 'settings') loadSettings();
  if (view === 'accounts') loadAccounts();
  if (view === 'automation') loadAutomation();
  if (view === 'personas') loadPersonas();
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

// ----- Overview -----
async function loadOverview() {
  const [s, accs, pxs, st] = await Promise.all([api('GET', '/status').catch(() => ({})), api('GET', '/accounts').catch(() => []), fetchProxies().catch(() => []), cfxNav()]);
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


// ----- Persona Management (Per User Per Group) -----
let personaData = { matrix: [], accounts: [], personas: [] };
let activePTab = 'matrix';

function switchPTab(tab) {
  activePTab = tab;
  document.querySelectorAll('.p-tab').forEach((b) => b.classList.toggle('active', b.dataset.ptab === tab));
  $('p-tab-matrix').classList.toggle('hidden', tab !== 'matrix');
  $('p-tab-roster').classList.toggle('hidden', tab !== 'roster');
}
document.querySelectorAll('.p-tab').forEach((b) => b.onclick = () => switchPTab(b.dataset.ptab));

async function loadPersonas() {
  try {
    const res = await api('GET', '/personas/matrix');
    personaData = res;
    renderPersonaMatrix();
    renderPersonaRoster();
  } catch (err) {
    console.error('Failed to load personas:', err);
  }
}

function renderPersonaMatrix() {
  const container = $('p-matrix-list');
  if (!personaData.matrix || personaData.matrix.length === 0) {
    container.innerHTML = '<div class="empty">No Telegram groups found yet. Sync groups in Network or open Groups.</div>';
    return;
  }

  container.innerHTML = personaData.matrix.map((g) => {
    const userRows = g.user_assignments.map((u) => {
      const assigned = u.assigned_persona;
      const fallback = u.fallback_persona;
      const effective = assigned || fallback;
      const perOptions = `<option value="">None (Account Default)</option>` +
        personaData.personas.map((p) => `<option value="${p.id}" ${assigned && String(assigned.persona_id) === String(p.id) ? 'selected' : ''}>${esc(p.name)}</option>`).join('');

      let typingInfo = '';
      if (effective && effective.details && effective.details.typing) {
        const t = effective.details.typing;
        typingInfo = `<span class="badge" title="Typing simulation">${t.chars_per_second || 20} cps · ${t.min_seconds || 2}-${t.max_seconds || 8}s</span>`;
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
            <select data-matrix-cid="${g.chat_id}" data-matrix-aid="${u.account_id}" class="p-select">
              ${perOptions}
            </select>
          </div>
          <div class="p-user-status">
            ${assigned ? `<span class="badge ok" style="background:rgba(56,212,139,.12); color:var(--ok); border-color:rgba(56,212,139,.25);">Group Custom</span>` 
                       : fallback ? `<span class="badge" style="background:rgba(47,196,178,.12); color:var(--accent);">Fallback: ${esc(fallback.name)}</span>`
                       : `<span class="badge off">No Persona</span>`}
            ${typingInfo}
          </div>
          <div class="p-user-actions">
            ${effective ? `<button class="ghost" data-pedit="${effective.id || effective.persona_id}" style="padding:4px 8px; font-size:12px;">Edit</button>` : ''}
          </div>
        </div>
      `;
    }).join('');

    return `
      <div class="card p-group-card">
        <div class="p-group-head">
          <div>
            <h3 style="margin:0; display:flex; align-items:center; gap:8px;">
              ${esc(g.title)}
              ${g.watched ? '<span class="badge ok">Watched</span>' : ''}
              ${g.auto_reply ? '<span class="badge" style="background:rgba(47,196,178,.15); color:var(--accent);">Auto-Reply</span>' : ''}
            </h3>
            <div class="hint" style="margin-top:2px;">Chat ID: ${g.chat_id} · ${g.profile && g.profile.topic ? esc(g.profile.topic) : 'General discussion'}</div>
          </div>
          <button class="ghost" data-gen-for="${g.chat_id}" style="font-size:12px;">+ Design Personas for Group</button>
        </div>
        <div class="p-group-users">
          <div class="p-group-table-head">
            <span>User / Account</span>
            <span>Assigned Persona for this Group</span>
            <span>Status &amp; Cadence</span>
            <span></span>
          </div>
          ${userRows || '<div class="hint" style="padding:10px;">No accounts assigned.</div>'}
        </div>
      </div>
    `;
  }).join('');

  // Attach matrix change listeners
  container.querySelectorAll('select[data-matrix-cid]').forEach((el) => {
    el.onchange = async () => {
      const cid = el.dataset.matrixCid;
      const aid = el.dataset.matrixAid;
      const pid = el.value || null;
      try {
        await api('POST', '/personas/assign-matrix', { chat_id: cid, account_id: aid, persona_id: pid });
        loadPersonas();
      } catch (err) {
        alert('Failed to update persona assignment: ' + err.message);
      }
    };
  });

  container.querySelectorAll('[data-gen-for]').forEach((el) => {
    el.onclick = () => openGenModal(Number(el.dataset.genFor));
  });

  container.querySelectorAll('[data-pedit]').forEach((el) => {
    el.onclick = () => openPersonaModal(Number(el.dataset.pedit));
  });
}

function renderPersonaRoster() {
  const container = $('p-roster-grid');
  $('p-roster-count').textContent = `${personaData.personas ? personaData.personas.length : 0} personas`;
  if (!personaData.personas || personaData.personas.length === 0) {
    container.innerHTML = '<div class="empty">No personas created yet. Click "+ New Persona" or study a group.</div>';
    return;
  }

  container.innerHTML = personaData.personas.map((p) => {
    const d = p.details || {};
    const t = d.typing || {};
    const col = p.color || '#2fc4b2';
    return `
      <div class="card p-card">
        <div class="p-card-header">
          <div class="p-avatar" style="background:${esc(col)};">${esc((p.name || 'P')[0].toUpperCase())}</div>
          <div class="p-card-title">
            <b>${esc(p.name)}</b>
            <div class="hint">${esc(p.bio || d.role_in_group || 'AI Persona')}</div>
          </div>
        </div>
        <div class="p-traits">
          ${d.role_in_group ? `<span class="p-tag">${esc(d.role_in_group)}</span>` : ''}
          ${d.voice && d.voice.typical_length ? `<span class="p-tag">${esc(d.voice.typical_length)}</span>` : ''}
          <span class="p-tag">${t.chars_per_second || 20} chars/s</span>
        </div>
        <div class="p-prompt-preview">${esc((p.prompt || '').slice(0, 140))}${p.prompt && p.prompt.length > 140 ? '…' : ''}</div>
        <div class="p-card-footer">
          <button class="ghost" data-proster-edit="${p.id}" style="font-size:12px;">Edit</button>
          <button class="ghost" data-proster-test="${p.id}" style="font-size:12px;">Test</button>
          <button class="ghost danger" data-proster-del="${p.id}" style="font-size:12px; margin-left:auto;">&times;</button>
        </div>
      </div>
    `;
  }).join('');

  container.querySelectorAll('[data-proster-edit]').forEach((el) => el.onclick = () => openPersonaModal(Number(el.dataset.prosterEdit)));
  container.querySelectorAll('[data-proster-test]').forEach((el) => el.onclick = () => {
    openPersonaModal(Number(el.dataset.prosterTest));
    $('pm-test-btn').click();
  });
  container.querySelectorAll('[data-proster-del]').forEach((el) => el.onclick = async () => {
    if (!confirm('Delete this persona permanently?')) return;
    await api('DELETE', `/personas/${el.dataset.prosterDel}`);
    loadPersonas();
  });
}

function openPersonaModal(pid) {
  const p = pid ? (personaData.personas || []).find((x) => x.id === pid) : null;
  const d = p && p.details ? p.details : {};
  const t = d.typing || {};
  $('pm-id').value = pid || '';
  $('p-modal-title').textContent = pid ? 'Edit Persona' : 'Create New Persona';
  $('pm-name').value = p ? p.name : '';
  $('pm-color').value = p ? p.color || '#2fc4b2' : '#2fc4b2';
  $('pm-color-text').value = $('pm-color').value;
  $('pm-bio').value = p ? p.bio || '' : '';
  $('pm-role').value = d.role_in_group || '';
  $('pm-tone').value = d.voice ? d.voice.typical_length || '' : '';
  $('pm-emoji').value = d.voice ? d.voice.emojis || '' : '';
  $('pm-offtopic').value = d.off_topic ? d.off_topic.join(', ') : '';
  $('pm-cps').value = t.chars_per_second || 20;
  $('pm-min-sec').value = t.min_seconds || 2.0;
  $('pm-max-sec').value = t.max_seconds || 8.0;
  $('pm-prompt').value = p ? p.prompt || '' : '';
  $('pm-test-output').textContent = 'Click "Test Reply" to see how this persona responds to group context.';
  $('p-modal').classList.remove('hidden');
}

$('pm-color').oninput = (e) => { $('pm-color-text').value = e.target.value; };
$('pm-color-text').oninput = (e) => { $('pm-color').value = e.target.value; };
$('p-modal-close').onclick = () => $('p-modal').classList.add('hidden');
$('pm-cancel').onclick = () => $('p-modal').classList.add('hidden');
$('p-btn-new').onclick = () => openPersonaModal(null);

$('pm-save').onclick = async () => {
  const pid = $('pm-id').value ? Number($('pm-id').value) : null;
  const existing = pid ? (personaData.personas || []).find((x) => x.id === pid) : null;
  const details = existing && existing.details ? { ...existing.details } : {};
  details.role_in_group = $('pm-role').value;
  details.voice = details.voice || {};
  details.voice.typical_length = $('pm-tone').value;
  details.voice.emojis = $('pm-emoji').value;
  details.off_topic = $('pm-offtopic').value ? $('pm-offtopic').value.split(',').map((s) => s.trim()).filter(Boolean) : [];
  details.typing = {
    chars_per_second: parseFloat($('pm-cps').value) || 20,
    min_seconds: parseFloat($('pm-min-sec').value) || 2,
    max_seconds: parseFloat($('pm-max-sec').value) || 8,
  };

  const payload = {
    id: pid,
    name: $('pm-name').value.trim() || 'New Persona',
    prompt: $('pm-prompt').value.trim(),
    color: $('pm-color').value,
    bio: $('pm-bio').value.trim(),
    details: details
  };

  try {
    await api('POST', '/personas', payload);
    $('p-modal').classList.add('hidden');
    loadPersonas();
  } catch (err) {
    alert('Failed to save persona: ' + err.message);
  }
};

$('pm-test-btn').onclick = async () => {
  const pid = $('pm-id').value;
  if (!pid) {
    $('pm-test-output').textContent = 'Please save this persona first before testing.';
    return;
  }
  $('pm-test-output').textContent = 'Generating preview reply...';
  try {
    const res = await api('POST', `/personas/${pid}/preview`, {});
    $('pm-test-output').innerHTML = `
      <div style="font-size:11.5px; color:var(--muted); margin-bottom:4px;">Test Reply Sample:</div>
      <div style="color:var(--text); font-weight:500;">${esc(res.text)}</div>
    `;
  } catch (err) {
    $('pm-test-output').textContent = 'Preview error: ' + err.message;
  }
};

// Generation Modal
function openGenModal(preselectedCid) {
  const select = $('pg-group');
  select.innerHTML = (personaData.matrix || []).map((g) =>
    `<option value="${g.chat_id}" ${preselectedCid && String(g.chat_id) === String(preselectedCid) ? 'selected' : ''}>${esc(g.title)}</option>`).join('');
  $('pg-status').textContent = '';
  $('p-gen-modal').classList.remove('hidden');
}

$('p-btn-gen').onclick = () => openGenModal(null);
$('p-gen-close').onclick = () => $('p-gen-modal').classList.add('hidden');
$('pg-cancel').onclick = () => $('p-gen-modal').classList.add('hidden');

$('pg-run').onclick = async () => {
  const cid = $('pg-group').value;
  if (!cid) return;
  const count = Number($('pg-count').value) || 3;
  const dir = $('pg-dir').value.trim();
  $('pg-status').textContent = 'Reading real group chat messages and generating personas with Claude Sonnet 4.5... This takes 10-25 seconds.';
  $('pg-run').disabled = true;
  try {
    const r = await api('POST', `/group-personas/${cid}/generate`, { count, direction: dir });
    $('pg-status').textContent = `Success! Designed ${r.ids.length} personas based on ${r.studied} messages.`;
    setTimeout(() => {
      $('p-gen-modal').classList.add('hidden');
      $('pg-run').disabled = false;
      loadPersonas();
    }, 1500);
  } catch (err) {
    $('pg-status').textContent = 'Failed: ' + err.message;
    $('pg-run').disabled = false;
  }
};
