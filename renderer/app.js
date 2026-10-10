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
  if (view === 'queue') view = 'batch';
  document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  ['overview', 'network', 'personas', 'memory', 'usage', 'batch', 'messages', 'groups', 'accounts', 'automation', 'settings'].forEach((v) => {
    const el = $('view-' + v);
    if (el) el.classList.toggle('hidden', v !== view);
  });
  document.querySelector('main').classList.toggle('flush', view === 'network');
  if (view === 'network') window.loadNetwork();
  if (view === 'overview') loadOverview();
  if (view === 'batch') { loadDailyBatchView(); loadQueue(); if (typeof loadOrchConfig === 'function') loadOrchConfig(); }
  if (view === 'settings') loadSettings();
  if (view === 'accounts') loadAccounts();
  if (view === 'automation') loadAutomation();
  if (view === 'personas') loadPersonas();
  if (view === 'memory') loadMemoryView();
  if (view === 'usage') loadUsageView();
  if (view === 'groups') loadGroups();
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

let currentGroupData = null;
let groupSearchQuery = '';

async function loadGroups() {
  try { groups = await api('GET', '/groups'); } catch { return; }
  if (!Array.isArray(groups)) groups = [];
  const multiAcc = new Set(groups.map((g) => g.account_id).filter(Boolean)).size > 1;

  const filtered = groups.filter((g) => {
    if (!groupSearchQuery) return true;
    const q = groupSearchQuery.toLowerCase();
    return (g.title || '').toLowerCase().includes(q) ||
           (g.about || '').toLowerCase().includes(q) ||
           (g.tags || '').toLowerCase().includes(q) ||
           String(g.chat_id).includes(q);
  });

  if (!$('group-list')) return;

  if (filtered.length === 0) {
    $('group-list').innerHTML = '<div class="hint" style="padding: 10px;">No matching groups found.</div>';
  } else {
    $('group-list').innerHTML = filtered.map((g) => {
      const isSel = current === g.chat_id;
      const isBio = (g.title || '').toLowerCase().includes('xbio') || (g.tags || '').toLowerCase().includes('peptide');
      const tagSnippet = isBio ? 'Peptides / Vendor' : (g.tags ? g.tags.split(',')[0].trim() : '');
      return `<div class="gitem ${isSel ? 'sel' : ''}" data-id="${g.chat_id}">
        <div style="flex: 1; min-width: 0;">
          <div style="font-weight: 600; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
            ${esc(g.title || 'Untitled Group')}
          </div>
          <div class="hint" style="font-size: 10.5px; display: flex; gap: 4px; align-items: center; margin-top: 2px;">
            ${tagSnippet ? `<span class="p-tag" style="font-size: 9.5px; padding: 1px 4px;">${esc(tagSnippet)}</span>` : ''}
            ${g.account_name && multiAcc ? `<span>· ${esc(g.account_name)}</span>` : ''}
          </div>
        </div>
        <div style="display: flex; gap: 4px; align-items: center;">
          ${g.auto_reply ? '<span style="font-size: 9px; padding: 1px 4px; border-radius: 3px; background: rgba(47,196,178,.15); color: var(--accent);">AUTO</span>' : ''}
          ${g.watched ? '<span class="dot" title="Watched"></span>' : ''}
          <button class="danger xs g-item-del-btn" data-del-gid="${g.chat_id}" title="Delete Group" style="padding:1px 5px; font-size:9.5px; line-height:1; height:18px;">✕</button>
        </div>
      </div>`;
    }).join('');
  }

  document.querySelectorAll('#group-list .gitem').forEach((el) => {
    el.onclick = () => openGroup(Number(el.dataset.id));
  });

  document.querySelectorAll('.g-item-del-btn').forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const gid = btn.dataset.delGid;
      const grp = groups.find((x) => String(x.chat_id) === String(gid));
      const title = (grp && grp.title) || ('Group ' + gid);
      if (!confirm(`Permanently delete "${title}"?\n\nThis will completely delete the group, its stored messages, summaries, scheduled batch dialogues, and persona bindings from the system.`)) return;
      try {
        await api('DELETE', `/groups/${gid}`);
        if (String(current) === String(gid)) {
          current = null;
          currentGroupData = null;
          $('group-detail').classList.add('hidden');
          $('group-empty').classList.remove('hidden');
        }
        await loadGroups();
        if (window.loadNetwork) window.loadNetwork();
        refreshStatus();
      } catch (err) {
        alert('Failed to delete group: ' + (err.message || err));
      }
    };
  });

  // If none selected but groups exist, open the first one (prefer xbiolabs if present)
  if (!current && groups.length > 0) {
    const xbio = groups.find((g) => (g.title || '').toLowerCase().includes('xbio'));
    openGroup(xbio ? xbio.chat_id : groups[0].chat_id);
  }
}

async function openGroup(id) {
  current = id;
  $('group-empty').classList.add('hidden');
  $('group-detail').classList.remove('hidden');

  try {
    const g = await api('GET', `/groups/${id}`);
    currentGroupData = g;

    $('g-title').textContent = g.title || 'Group ' + g.chat_id;
    $('g-chat-id-badge').textContent = 'ID: ' + g.chat_id;
    $('g-watch').checked = !!g.watched;
    $('g-auto').checked = !!g.auto_reply;

    $('g-about').value = g.about || '';
    $('g-rules').value = g.rules || '';
    $('g-domain-knowledge').value = g.domain_knowledge || '';
    $('g-tags').value = g.tags || '';
    $('g-persona').value = g.persona || '';

    // Render group accounts table
    renderGroupAccounts(g);
  } catch (e) {
    const fallback = groups.find((x) => x.chat_id === id);
    if (fallback) {
      $('g-title').textContent = fallback.title || 'Group ' + fallback.chat_id;
      $('g-chat-id-badge').textContent = 'ID: ' + fallback.chat_id;
      $('g-watch').checked = !!fallback.watched;
      $('g-auto').checked = !!fallback.auto_reply;
      $('g-about').value = fallback.about || '';
      $('g-rules').value = fallback.rules || '';
      $('g-domain-knowledge').value = fallback.domain_knowledge || '';
      $('g-tags').value = fallback.tags || '';
      $('g-persona').value = fallback.persona || '';
    }
  }

  // Refresh active selection highlight in list
  document.querySelectorAll('#group-list .gitem').forEach((el) => {
    el.classList.toggle('sel', Number(el.dataset.id) === id);
  });

  const delBtn = $('g-delete-btn');
  if (delBtn) {
    delBtn.onclick = async () => {
      const title = (currentGroupData && currentGroupData.title) || ('Group ' + id);
      if (!confirm(`Permanently delete group "${title}" (ID: ${id})?\n\nThis will completely delete the group, its stored messages, summaries, scheduled batch dialogues, and persona bindings from the system.`)) return;
      delBtn.disabled = true;
      delBtn.textContent = 'Deleting...';
      try {
        await api('DELETE', `/groups/${id}`);
        current = null;
        currentGroupData = null;
        $('group-detail').classList.add('hidden');
        $('group-empty').classList.remove('hidden');
        await loadGroups();
        if (window.loadNetwork) window.loadNetwork();
        refreshStatus();
      } catch (err) {
        alert('Failed to delete group: ' + (err.message || err));
        delBtn.disabled = false;
        delBtn.textContent = 'Delete Group';
      }
    };
  }

  loadFeed();
}

function renderGroupAccounts(g) {
  const container = $('g-accounts-table');
  const countLabel = $('g-acc-count');
  if (!container) return;

  const accs = g.accounts || [];
  countLabel.textContent = `${accs.length} account${accs.length === 1 ? '' : 's'} linked`;

  if (accs.length === 0) {
    container.innerHTML = '<div class="hint" style="padding: 12px;">No Telegram accounts currently detected in this group. Sync dialogs or add an account.</div>';
    return;
  }

  // Fetch personas for dropdown
  const personaOpts = (allPersonas || []).map((p) =>
    `<option value="${p.id}">${esc(p.name)}</option>`
  ).join('');

  container.innerHTML = accs.map((a) => {
    return `<div class="g-acc-row">
      <div>
        <div class="acc-name">${esc(a.name || 'Account ' + a.id)}</div>
        <div class="acc-user">${esc(a.username ? '@' + a.username : a.phone || '')}</div>
      </div>
      <div>
        <select class="p-select" data-acc-id="${a.id}" style="height: 28px; font-size: 11.5px;">
          <option value="">No Persona Assigned</option>
          ${(allPersonas || []).map((p) => `<option value="${p.id}" ${p.id === a.persona_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}
        </select>
      </div>
      <div style="text-align: right;">
        <span class="p-tag" style="font-size: 10px;">${a.persona_id ? 'Active Persona' : 'Unassigned'}</span>
      </div>
    </div>`;
  }).join('');

  container.querySelectorAll('select').forEach((sel) => {
    sel.onchange = async (e) => {
      const aid = Number(sel.dataset.accId);
      const pid = sel.value ? Number(sel.value) : null;
      try {
        await api('POST', '/personas/assign-matrix', {
          chat_id: current,
          account_id: aid,
          persona_id: pid
        });
        if (currentGroupData) openGroup(current);
      } catch (err) {
        alert('Failed to update persona assignment: ' + err.message);
      }
    };
  });
}

// Sub Tab Switching for Groups
document.querySelectorAll('[data-gtab]').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('[data-gtab]').forEach((t) => t.classList.toggle('active', t === tab));
    const target = tab.dataset.gtab;
    $('gtab-about-view').classList.toggle('hidden', target !== 'about');
    $('gtab-accounts-view').classList.toggle('hidden', target !== 'accounts');
    $('gtab-feed-view').classList.toggle('hidden', target !== 'feed');
  };
});

// Search input
$('g-search-input')?.addEventListener('input', (e) => {
  groupSearchQuery = e.target.value.trim();
  loadGroups();
});

// Save Group Changes
$('g-save-btn').onclick = async () => {
  if (!current) return;
  const btn = $('g-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    await api('POST', `/groups/${current}`, {
      about: $('g-about').value,
      rules: $('g-rules').value,
      domain_knowledge: $('g-domain-knowledge').value,
      tags: $('g-tags').value,
      persona: $('g-persona').value,
      watched: $('g-watch').checked ? 1 : 0,
      auto_reply: $('g-auto').checked ? 1 : 0
    });
    btn.textContent = 'Saved!';
    setTimeout(() => { btn.textContent = 'Save Changes'; btn.disabled = false; }, 1500);
    loadGroups();
  } catch (err) {
    alert('Failed to save group details: ' + err.message);
    btn.textContent = 'Save Changes';
    btn.disabled = false;
  }
};

// Load xbiolabs Preset Button
$('g-load-xbiolabs-btn').onclick = async () => {
  if (!current) return;
  if (!confirm('Load comprehensive xbiolabs vendor and peptide community domain knowledge into this group?')) return;
  try {
    const res = await api('POST', `/groups/${current}/load-xbiolabs-preset`);
    $('g-about').value = res.about || '';
    $('g-domain-knowledge').value = res.domain_knowledge || '';
    $('g-tags').value = res.tags || '';
    $('g-rules').value = res.rules || '';
    alert('xbiolabs / peptide domain knowledge loaded successfully!');
    loadGroups();
  } catch (err) {
    alert('Could not load preset: ' + err.message);
  }
};

// Test Chat Button
$('g-test-chat-btn').onclick = () => {
  if (current) runGroupTestChat(current);
};

// Sync Dialogs Button
$('g-sync-btn').onclick = async () => {
  const btn = $('g-sync-btn');
  btn.disabled = true;
  btn.textContent = 'Syncing...';
  try {
    await api('POST', '/groups/sync');
    await loadGroups();
  } catch (_) {
    await loadGroups();
  }
  btn.textContent = 'Sync Dialogs';
  btn.disabled = false;
};

// Watch & Auto-Reply Quick Toggles
$('g-watch').onchange = (e) => api('POST', `/groups/${current}`, { watched: e.target.checked ? 1 : 0 }).then(loadGroups);
$('g-auto').onchange = (e) => api('POST', `/groups/${current}`, { auto_reply: e.target.checked ? 1 : 0 }).then(loadGroups);

// Summarize & Draft Actions
$('g-summarize').onclick = () => api('POST', `/groups/${current}/summarize`).then(() => setTimeout(loadFeed, 3000));
$('g-draft').onclick = () => api('POST', `/groups/${current}/draft_reply`).then(() => setTimeout(loadFeed, 3000));

async function loadFeed() {
  if (!current) return;
  try {
    const f = await api('GET', `/groups/${current}/feed`);
    $('g-summaries').innerHTML = f.summaries.map((s) => `<div class="card">${esc(s.body)}<div class="hint">${new Date(s.created * 1000).toLocaleString()}</div></div>`).join('') || '<div class="hint">No notes yet.</div>';
    $('g-messages').innerHTML = f.messages.slice(-60).map((m) => `<div class="msg"><b>${esc(m.sender)}</b> ${esc(m.text)}</div>`).join('') || '<div class="hint">No messages collected yet.</div>';
  } catch (_) {}
}

async function loadQueue() {
  try {
    const rows = await api('GET', '/queue');
    const list = Array.isArray(rows) ? rows : [];
    const el = $('queue-table');
    if (el) {
      el.innerHTML = '<tr><th>#</th><th>Type</th><th>Status</th><th>Tries</th><th>Last error</th></tr>' +
        list.map((r) => `<tr><td>${r.id}</td><td>${esc(r.kind)}</td><td>${esc(r.status)}</td><td>${r.attempts}</td><td>${esc(r.last_error || '')}</td></tr>`).join('');
    }
  } catch (err) {
    console.error('Failed to load queue:', err);
  }
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
async function fetchProxies() { try { const p = await api('GET', '/proxies'); proxyList = Array.isArray(p) ? p : []; } catch { proxyList = []; } return proxyList; }
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
    if (icon) icon.textContent = '';
  } else {
    badge.className = 'sys-badge paused';
    if (txt) txt.textContent = 'System Paused';
    btn.className = 'sys-master-btn off';
    if (label) label.textContent = 'Turn System ON';
    if (icon) icon.textContent = '';
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
  updateMasterSystemUI((sys && sys.enabled !== undefined) ? sys.enabled : true);
  const accountsList = Array.isArray(accs) ? accs : [];
  const proxiesList = Array.isArray(pxs) ? pxs : [];
  const q = (s && s.queue) || {};
  const stat = (k, v, sub) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  const dormantCount = Math.max(0, accountsList.length - ((s && s.connected) || 0));
  $('ov-stats').innerHTML = stat('Scout listener', `${(s && s.connected) || 0}<span class="hint"> / ${accountsList.length}</span>`, `${dormantCount} dormant in RAM (0 sockets)`)
    + stat('Live AI routing', 'Humans Only', 'Bots pre-scheduled daily')
    + stat('Queued jobs', q.pending || 0, `${q.failed || 0} failed`)
    + stat('Proxies working', `${proxiesList.filter((p) => p.ok).length}<span class="hint"> / ${proxiesList.length}</span>`, 'Tested through Telegram & Camoufox');
  $('ov-accounts').innerHTML = accountsList.map((a) => {
    const init = esc((a.name || '?')[0].toUpperCase());
    const av = `<div class="avatar"><img src="/accounts/${a.id}/avatar?t=${Date.now()}" alt="${esc(a.name || '')}" onerror="this.onerror=null; this.remove();" /><span class="av-fallback">${init}</span></div>`;
    return `<div class="mini">${av}<div class="grow"><b>${esc(a.name || 'Account')}</b><div class="hint">${esc(a.phone || '')}</div></div><span class="badge ${!a.active ? 'off' : a.connected ? 'ok' : 'bad'}">${!a.active ? 'Paused' : a.connected ? 'Connected' : 'Signed out'}</span></div>`;
  }).join('') || '<div class="hint">No accounts yet.</div>';
  $('ov-proxies').innerHTML = proxiesList.map((p) => `<div class="mini"><div class="grow"><b>${esc(p.label)}</b><div class="hint">${esc((p.accounts || []).join(', ') || 'Not assigned')}</div></div><span class="badge ${p.ok ? 'ok' : p.last_check ? 'bad' : 'off'}">${p.ok ? 'Working' : p.last_check ? 'Failed' : 'Not tested'}</span></div>`).join('') || '<div class="hint">No proxies yet.</div>';
}

// ----- Accounts -----
let loginToken = null;
const wmsg = (t) => { $('w-msg').textContent = t || ''; };
async function loadAccounts() {
  const el = $('acc-list');
  if (!el) return;
  el.innerHTML = '<div class="hint" style="padding:16px;">Loading accounts...</div>';
  try {
    const [_, res] = await Promise.all([fetchProxies(), api('GET', '/accounts')]);
    const list = Array.isArray(res) ? res : [];
    if (!list.length) {
      el.innerHTML = '<div class="empty">No Telegram accounts connected yet. Click “+ Add account manually” above to connect one.</div>';
      return;
    }
    el.innerHTML = list.map((a) => {
      const badgeClass = !a.active ? 'off' : a.is_scout ? 'ok' : a.connected ? 'ok' : 'standby';
      const badgeLabel = !a.active ? 'Paused' : a.is_scout ? 'Scout (Listening)' : a.connected ? 'Connected' : 'Standby';
      const init = esc((a.name || a.phone || '?')[0].toUpperCase());
      const avHtml = `<div class="avatar"><img src="/accounts/${a.id}/avatar?t=${Date.now()}" alt="${esc(a.name || '')}" onerror="this.onerror=null; this.remove();" /><span class="av-fallback">${init}</span></div>`;
      return `
      <div class="card acc">
        ${avHtml}
        <div class="acc-info"><b>${esc(a.name || 'Account ' + a.id)}</b>
          <div class="hint">${[a.phone, a.username && '@' + a.username, `${a.groups || 0} group${a.groups === 1 ? '' : 's'}`].filter(Boolean).map(esc).join(' · ')}</div></div>
        <span class="badge ${badgeClass}">${badgeLabel}</span>
        <button class="ghost xs" data-sync-acc="${a.id}" title="Sync dialogs and groups for this account">&#8635; Sync</button>
        <select data-px="${a.id}" title="Proxy">${proxyOptions(null, a.proxy_id)}</select>
        <label><input type="checkbox" data-act="${a.id}" ${a.active ? 'checked' : ''}/> On</label>
        <button class="danger xs" data-del="${a.id}" data-name="${esc(a.name || a.phone || 'Account ' + a.id)}">Delete</button>
      </div>`;
    }).join('');

    document.querySelectorAll('[data-sync-acc]').forEach((btn) => btn.onclick = async () => {
      btn.disabled = true;
      const oldText = btn.textContent;
      btn.textContent = 'Syncing...';
      try {
        await api('POST', `/accounts/${btn.dataset.syncAcc}/sync`);
        await loadAccounts();
        await loadOverview();
        await loadGroups();
        if (window.loadNetwork) window.loadNetwork();
      } catch (e) {
        alert('Sync failed: ' + (e.message || e));
      } finally {
        btn.disabled = false;
        btn.textContent = oldText;
      }
    });

    document.querySelectorAll('[data-px]').forEach((sel) => sel.onchange = async () => {
      await api('POST', '/accounts/' + sel.dataset.px, { proxy_id: sel.value ? Number(sel.value) : null });
      loadAccounts();
    });

    document.querySelectorAll('[data-act]').forEach((cb) => cb.onchange = async () => {
      await api('POST', '/accounts/' + cb.dataset.act, { active: cb.checked });
      loadAccounts();
      refreshStatus();
    });

    document.querySelectorAll('[data-del]').forEach((btn) => btn.onclick = async () => {
      const aid = btn.dataset.del;
      const aname = btn.dataset.name;
      if (!confirm(`Permanently delete account "${aname}"?\n\nThis will completely delete the account, log out and destroy its Telegram session, remove it from all groups, and wipe its records.`)) return;
      btn.disabled = true;
      btn.textContent = 'Deleting...';
      try {
        await api('DELETE', '/accounts/' + aid);
        await loadAccounts();
        await loadGroups();
        if (window.loadNetwork) window.loadNetwork();
        refreshStatus();
      } catch (err) {
        alert('Failed to delete account: ' + (err.message || err));
        btn.disabled = false;
        btn.textContent = 'Delete';
      }
    });
  } catch (err) {
    console.error('Failed to load accounts:', err);
    el.innerHTML = `<div class="card err" style="padding:16px;">Failed to load accounts: ${esc(err.message || err)}<br><button class="ghost xs" onclick="loadAccounts()" style="margin-top:8px;">Retry</button></div>`;
  }
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
  $('prov-steps').innerHTML = (s.steps || []).map((x) => `<li>${esc(x.text)}</li>`).join('');
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


// ----- Persona Management Controller (Per User Per Group) -----
let personaData = { matrix: [], accounts: [], personas: [] };
let activePersonaTab = 'roster';
let currentStudioPersonaId = null;

function switchPersonaTab(tabKey) {
  activePersonaTab = tabKey;
  const tabs = ['roster', 'studio'];
  tabs.forEach((t) => {
    const el = $('p-tab-' + t);
    if (el) el.classList.toggle('hidden', t !== tabKey);
  });
  if ($('p-main-tabs')) {
    $('p-main-tabs').querySelectorAll('.p-tab').forEach((b) => {
      b.classList.toggle('active', b.dataset.ptab === tabKey);
    });
  }
  if (tabKey === 'roster') {
    renderPersonaRoster();
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

async function loadPersonas() {
  try {
    const res = await api('GET', '/personas/matrix');
    personaData = res || { matrix: [], accounts: [], personas: [] };
    renderPersonaRoster();
    if (activePersonaTab === 'matrix') renderPersonaMatrix();
  } catch (err) {
    console.error('Failed to load personas:', err);
  }
}

function renderPersonaRoster() {
  const container = $('p-roster-grid');
  const countEl = $('p-roster-count');
  const personas = personaData.personas || [];
  const matrix = personaData.matrix || [];
  const accounts = personaData.accounts || [];

  if (countEl) countEl.textContent = `${personas.length} persona${personas.length === 1 ? '' : 's'}`;

  // Summary metrics (clean without unhinged index)
  const boundAccountIds = new Set(accounts.filter((a) => a.persona_id).map((a) => a.id));
  const activeGroupsCount = matrix.filter((g) => (g.user_assignments || []).some((u) => u.assigned_persona || u.fallback_persona)).length;

  if ($('p-metric-total')) $('p-metric-total').textContent = personas.length;
  if ($('p-metric-bound')) $('p-metric-bound').textContent = `${boundAccountIds.size} / ${accounts.length}`;
  if ($('p-metric-groups')) $('p-metric-groups').textContent = activeGroupsCount;

  if (!personas || personas.length === 0) {
    container.innerHTML = `
      <div class="empty" style="grid-column: 1 / -1; padding: 24px 16px; text-align: center;">
        <div style="font-size:12px; margin-bottom:4px; font-weight:600; color:var(--muted);">No Personas Configured</div>
        <p class="hint" style="margin-bottom:12px; font-size:11.5px;">Open Persona Studio to create an autonomous persona with custom behavior and speech patterns.</p>
        <button type="button" class="primary" id="p-btn-empty-studio" style="font-size:11.5px; padding:4px 12px;">+ Open Persona Studio</button>
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
    container.innerHTML = '<div class="empty" style="grid-column: 1 / -1; padding: 16px; text-align: center; font-size:12px;">No personas match your search.</div>';
    return;
  }

  // Compact mini-containers with persona name, subtitle, and instant click to dedicated settings page
  container.innerHTML = filtered.map((p) => {
    const d = p.details || {};
    const col = p.color || '#2fc4b2';
    const sub = d.occupation || p.bio || (d.culture ? `${d.culture}` : 'Autonomous Persona');

    return `
      <div class="p-card-mini" data-proster-open="${p.id}" style="border-left: 3px solid ${esc(col)};">
        <div class="p-mini-avatar" style="background:${esc(col)};">${esc((p.name || 'P')[0].toUpperCase())}</div>
        <div class="p-mini-body">
          <div class="p-mini-name">${esc(p.name)}</div>
          <div class="p-mini-sub">${esc(sub)}</div>
        </div>
        <div class="p-mini-actions">
          <button type="button" class="ghost p-mini-btn" data-proster-edit="${p.id}" title="Open Persona Settings">Settings</button>
          <button type="button" class="ghost danger p-mini-btn-del" data-proster-del="${p.id}" title="Delete Persona">&times;</button>
        </div>
      </div>
    `;
  }).join('');

  // Clicking anywhere on mini card opens Studio settings
  container.querySelectorAll('.p-card-mini').forEach((card) => {
    card.onclick = (e) => {
      if (e.target.closest('[data-proster-del]')) return;
      const pid = Number(card.dataset.prosterOpen);
      openPersonaStudio(pid);
    };
  });

  container.querySelectorAll('[data-proster-edit]').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      openPersonaStudio(Number(el.dataset.prosterEdit));
    };
  });

  container.querySelectorAll('[data-proster-del]').forEach((el) => {
    el.onclick = async (e) => {
      e.stopPropagation();
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
    emotional_volatility: parseInt($('ps-volatility') ? $('ps-volatility').value : '80', 10),
    cynicism: parseInt($('ps-cynicism') ? $('ps-cynicism').value : '85', 10),
    combative: parseInt($('ps-combative') ? $('ps-combative').value : '75', 10),
    impulsive: parseInt($('ps-impulse') ? $('ps-impulse').value : '80', 10),
    casing_style: $('ps-casing') ? $('ps-casing').value : 'all_lowercase',
    punctuation_style: $('ps-punctuation') ? $('ps-punctuation').value : 'none',
    typo_rate: parseFloat($('ps-typo') ? $('ps-typo').value : '6.0'),
    slang_tier: $('ps-slang') ? $('ps-slang').value : 'crypto_degen',
    burstiness: parseInt($('ps-burst') ? $('ps-burst').value : '65', 10),
    emoji_habit: $('ps-emoji-habit') ? $('ps-emoji-habit').value : 'frequent',
    signature_emojis: ($('ps-emojis') ? $('ps-emojis').value.split(',').map((s) => s.trim()).filter(Boolean) : []),
    expertise: ($('ps-expertise') ? $('ps-expertise').value.trim() : ''),
    off_topic_obsessions: ($('ps-offtopic') ? $('ps-offtopic').value.split(',').map((s) => s.trim()).filter(Boolean) : []),
    polarizing_takes: ($('ps-hottakes') ? $('ps-hottakes').value.trim() : ''),
    trigger_topics: ($('ps-triggers') ? $('ps-triggers').value.split(',').map((s) => s.trim()).filter(Boolean) : []),
    reading_cps: parseInt($('ps-reading-cps') ? $('ps-reading-cps').value : '28', 10),
    min_delay: parseFloat($('ps-min-delay') ? $('ps-min-delay').value : '2.0'),
    max_delay: parseFloat($('ps-max-delay') ? $('ps-max-delay').value : '8.0'),
    bind_account_id: $('ps-bind-account') && $('ps-bind-account').value ? $('ps-bind-account').value : null
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
    console.error('Compile prompt error:', e);
  }
}

window.openPersonaStudioFromNetwork = async function(pid) {
  const navBtn = document.querySelector('nav button[data-view="personas"]');
  if (navBtn) navBtn.click();
  if (!personaData || !personaData.personas || personaData.personas.length === 0) {
    await loadPersonas();
  }
  openPersonaStudio(pid);
};

window.syncPersonaLibraryPrompt = function(pid, name, prompt, color) {
  if (personaData && personaData.personas) {
    const p = personaData.personas.find((x) => x.id === pid);
    if (p) {
      if (name) p.name = name;
      if (prompt !== undefined) p.prompt = prompt;
      if (color) p.color = color;
    }
  }
};

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
    accSel.innerHTML = '<option value="">None (Library Persona / Group Assignment Only)</option>' +
      accounts.map((a) => {
        const isSel = boundAcc && String(boundAcc.id) === String(a.id);
        return `<option value="${a.id}" ${isSel ? 'selected' : ''}>${esc(a.name || a.phone)} ${a.username ? '(@' + esc(a.username) + ')' : ''}</option>`;
      }).join('');
  }

  if (p) {
    $('ps-id').value = p.id;
    $('ps-studio-title').textContent = `Settings: ${p.name}`;
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

    // unhinged meter removed

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
    $('ps-emojis').value = Array.isArray(ling.signature_emojis) ? ling.signature_emojis.join(', ') : (ling.signature_emojis || 'skull, eyes, fire');

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
    $('ps-studio-title').textContent = 'Create Persona';
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

    // unhinged meter removed
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
    $('ps-emojis').value = 'skull, eyes, fire';

    $('ps-expertise').value = 'On-chain token flows, memecoin liquidity pools, smart contract exploits';
    $('ps-offtopic').value = 'yerba mate, conspiracy rabbit holes, adderall shortages';
    $('ps-hottakes').value = '99% of web3 founders are grifters who never wrote code; centralized exchanges are rigged casinos';
    $('ps-triggers').value = 'VC token unlock schedules, sponsored influencer shills';

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
    psychometrics: {
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

    toast(`Saved persona "${name}" and synced to Network!`);
    await loadPersonas();
    if (window.loadNetwork) window.loadNetwork();
    switchPersonaTab('roster');
  } catch (err) {
    alert('Failed to save persona: ' + err.message);
  }
}

// Live Studio Input Listeners
if ($('ps-age')) $('ps-age').oninput = (e) => { $('ps-age-val').textContent = e.target.value; };
if ($('ps-volatility')) $('ps-volatility').oninput = (e) => { $('ps-volatility-val').textContent = e.target.value + '%'; };
if ($('ps-cynicism')) $('ps-cynicism').oninput = (e) => { $('ps-cynicism-val').textContent = e.target.value + '%'; };
if ($('ps-combative')) $('ps-combative').oninput = (e) => { $('ps-combative-val').textContent = e.target.value + '%'; };
if ($('ps-impulse')) $('ps-impulse').oninput = (e) => { $('ps-impulse-val').textContent = e.target.value + '%'; };
if ($('ps-typo')) $('ps-typo').oninput = (e) => { $('ps-typo-val').textContent = e.target.value + '%'; };
if ($('ps-burst')) $('ps-burst').oninput = (e) => { $('ps-burst-val').textContent = e.target.value + '%'; };

if ($('ps-color')) {
  $('ps-color').oninput = (e) => {
    if ($('ps-color-text')) $('ps-color-text').value = e.target.value;
    if ($('ps-avatar-preview')) $('ps-avatar-preview').style.background = e.target.value;
  };
}
if ($('ps-color-text')) {
  $('ps-color-text').oninput = (e) => {
    if ($('ps-color')) $('ps-color').value = e.target.value;
    if ($('ps-avatar-preview')) $('ps-avatar-preview').style.background = e.target.value;
  };
}

if ($('ps-name')) {
  $('ps-name').oninput = (e) => {
    const val = e.target.value.trim();
    if ($('ps-avatar-preview')) $('ps-avatar-preview').textContent = (val || 'P')[0].toUpperCase();
  };
}

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
    container.innerHTML = '<div class="empty" style="padding:20px; text-align:center;">No Telegram groups found yet. Sync groups in Network or open Groups view.</div>';
    return;
  }

  const query = ($('p-matrix-search') ? $('p-matrix-search').value.toLowerCase().trim() : '');
  const filtered = matrix.filter((g) => {
    if (!query) return true;
    return (g.title || '').toLowerCase().includes(query) || String(g.chat_id).includes(query);
  });

  if (filtered.length === 0) {
    container.innerHTML = '<div class="empty" style="padding:16px;">No groups match your filter.</div>';
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
        typingInfo = `<span class="hint" style="font-size:10.5px;">${t.chars_per_second || 24} cps</span>`;
      }

      return `
        <div class="p-user-row">
          <div class="p-user-info">
            <span class="p-user-avatar">${esc((u.account_name || 'U')[0].toUpperCase())}</span>
            <div style="min-width:0; overflow:hidden;">
              <b style="font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; display:block;">${esc(u.account_name)}</b>
              <div class="hint" style="font-size:10.5px; white-space:nowrap;">${esc(u.account_phone || '')} · ${u.active ? '<span style="color:var(--ok)">Active</span>' : 'Paused'}</div>
            </div>
          </div>
          <div>
            <select data-matrix-cid="${g.chat_id}" data-matrix-aid="${u.account_id}" data-has-fallback="${hasBaseline ? '1' : '0'}" class="p-select">
              ${perOptions}
            </select>
          </div>
          <div class="p-user-status">
            ${assigned ? `<span class="p-tag" style="background:rgba(56,212,139,.12); color:var(--ok); border-color:rgba(56,212,139,.3);">Group Custom</span>` 
                       : fallback ? `<span class="p-tag" style="background:rgba(47,196,178,.12); color:var(--accent);">Default: ${esc(fallback.name)}</span>`
                       : `<span class="p-tag" style="background:rgba(245,184,74,.12); color:var(--warn); border-color:rgba(245,184,74,.3);">Unassigned</span>`}
            ${typingInfo}
          </div>
          <div>
            ${effective ? `<button type="button" class="ghost" data-pedit="${effective.id || effective.persona_id}" style="padding:3px 8px; font-size:11px;">Studio</button>` : ''}
          </div>
        </div>
      `;
    }).join('');

    return `
      <div class="card p-group-card">
        <div class="p-group-head">
          <div>
            <h3 style="margin:0; font-size:13px; font-weight:600; display:flex; align-items:center; gap:8px;">
              ${esc(g.title || 'Untitled Group')}
              <span class="hint" style="font-size:11px; font-weight:normal;">(${g.chat_id})</span>
            </h3>
            <div class="hint" style="font-size:11px; margin-top:2px;">${g.user_assignments ? g.user_assignments.length : 0} accounts in this group</div>
          </div>
          <button type="button" class="ghost" data-pgen-cid="${g.chat_id}" style="font-size:11.5px; padding:4px 10px;">Generate Group Personas</button>
        </div>
        <div class="p-group-table-head">
          <span>Account</span>
          <span>Assigned Persona</span>
          <span>Resolution</span>
          <span>Action</span>
        </div>
        <div>${userRows || '<div class="empty" style="padding:10px;">No accounts linked to this group.</div>'}</div>
      </div>
    `;
  }).join('');

  // Handle matrix selection change
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


// ----- Group Persona Generation Modal -----

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
    dmChatsList = Array.isArray(chats) ? chats : [];
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
  $('ptc-submit').textContent = 'Run Test Chat';

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
    submitBtn.textContent = 'Run Another Test Chat';
    toast(res.send_live ? 'Test chat delivered to Telegram group!' : 'Test dialogue generated!');
  } catch (err) {
    statusEl.innerHTML = `<span style="color:var(--danger); font-weight:600;">Error:</span> ${escapeHtml(err.message)}`;
    submitBtn.disabled = false;
    submitBtn.textContent = 'Run Test Chat';
    toast(err.message, true);
  }
}


if ($('p-btn-test-chat')) $('p-btn-test-chat').onclick = () => window.openGroupTestChatModal();
if ($('ptc-close')) $('ptc-close').onclick = () => $('p-testchat-modal').classList.add('hidden');
if ($('ptc-cancel')) $('ptc-cancel').onclick = () => $('p-testchat-modal').classList.add('hidden');
if ($('ptc-submit')) $('ptc-submit').onclick = runGroupTestChat;


// ----- Daily Batch Scheduler & Scout Architecture -----
let dailyBatchData = null;

async function loadDailyBatchView() {
  if (typeof loadOrchConfig === 'function') loadOrchConfig();
  const statusEl = $('batch-gen-status');
  try {
    const [batchRes, matrixRes, accsRes] = await Promise.all([
      api('GET', '/daily-batch/status').catch(() => ({ stats: {}, items: [] })),
      api('GET', '/personas/matrix').catch(() => ({ matrix: [] })),
      api('GET', '/accounts').catch(() => [])
    ]);
    const batch = (batchRes && typeof batchRes === 'object') ? batchRes : {};
    const accounts = Array.isArray(accsRes) ? accsRes : [];
    const matrix = Array.isArray(matrixRes && matrixRes.matrix) ? matrixRes.matrix : [];
    dailyBatchData = batch;

    // Current Date
    if ($('batch-today-date')) $('batch-today-date').textContent = batch.date_str || new Date().toISOString().slice(0, 10);

    // Scout selector
    const scoutSel = $('batch-scout-select');
    if (scoutSel) {
      scoutSel.innerHTML = accounts.filter((a) => a.active).map((a) => `
        <option value="${a.id}" ${batch.scout_account_id === a.id ? 'selected' : ''}>
          ${esc(a.name || 'Account ' + a.id)} (${esc(a.phone || '')})
        </option>
      `).join('') || '<option value="">No active accounts</option>';
    }
    if ($('batch-scout-badge')) {
      const activeScout = accounts.find((a) => a.id === batch.scout_account_id);
      $('batch-scout-badge').textContent = activeScout ? `Scout: ${activeScout.name || activeScout.phone} (1 Socket)` : 'Scout: Auto-Selected';
    }

    // Watched groups dropdown
    const groupSel = $('batch-gen-group');
    if (groupSel) {
      const watched = matrix.filter((g) => g.watched);
      groupSel.innerHTML = watched.map((g) => `
        <option value="${g.chat_id}">${esc(g.title || 'Group ' + g.chat_id)} (${g.user_assignments ? g.user_assignments.length : 0} personas)</option>
      `).join('') || '<option value="">No watched groups</option>';
    }

    // Metrics
    const st = batch.stats || {};
    if ($('batch-metric-total')) $('batch-metric-total').textContent = st.total || 0;
    if ($('batch-metric-pending')) $('batch-metric-pending').textContent = st.pending || 0;
    if ($('batch-metric-sent')) $('batch-metric-sent').textContent = st.sent || 0;
    if ($('batch-metric-postponed')) $('batch-metric-postponed').textContent = st.postponed || 0;

    // Render schedule table
    renderBatchScheduleTable(Array.isArray(batch.items) ? batch.items : []);
  } catch (err) {
    console.error('Failed to load daily batch view:', err);
    if (statusEl) statusEl.textContent = 'Error loading batch status: ' + err.message;
  }
}

function renderBatchScheduleTable(items) {
  const tbody = $('batch-schedule-tbody');
  if (!tbody) return;
  if (!items || items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty" style="padding:16px; text-align:center;">No batch generated for today. Click \'Generate Batch\' above to pre-bake today\'s conversation.</td></tr>';
    return;
  }

  tbody.innerHTML = items.map((it) => {
    const timeStr = it.scheduled_ts ? new Date(it.scheduled_ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '-';
    let statusClass = 'off';
    let statusLabel = 'Pending';
    if (it.status === 'sent') {
      statusClass = 'ok';
      statusLabel = 'Sent ' + (it.sent_ts ? new Date(it.sent_ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '');
    } else if (it.status === 'postponed') {
      statusClass = 'bad';
      statusLabel = 'Postponed (Human)';
    } else if (it.status === 'cancelled') {
      statusClass = 'off';
      statusLabel = 'Cancelled';
    }

    return `
      <tr style="border-bottom:1px solid var(--border);">
        <td style="padding:8px; font-family:var(--font-mono, monospace); font-size:11.5px; font-weight:600;">${timeStr}</td>
        <td style="padding:8px; font-weight:500;">${esc(it.group_title || ('Group ' + it.chat_id))}</td>
        <td style="padding:8px;">
          <div style="font-weight:600; color:var(--text);">${esc(it.persona_name || it.sender_name || 'Persona')}</div>
          <div class="hint" style="font-size:10.5px;">Acc #${it.account_id} · ${esc(it.account_name || '')}</div>
        </td>
        <td style="padding:8px; max-width:320px; line-height:1.35; color:var(--text-bright);">
          ${esc(it.text || '')}
        </td>
        <td style="padding:8px;">
          <span class="badge ${statusClass}" style="font-size:10.5px; padding:2px 6px;">${statusLabel}</span>
        </td>
      </tr>
    `;
  }).join('');
}

// Daily Batch Controls Event Listeners
if ($('batch-btn-refresh')) $('batch-btn-refresh').onclick = () => loadDailyBatchView();

if ($('batch-scout-set-btn')) {
  $('batch-scout-set-btn').onclick = async () => {
    const sel = $('batch-scout-select');
    if (!sel || !sel.value) return;
    try {
      await api('POST', '/daily-batch/scout', { account_id: parseInt(sel.value, 10) });
      await loadDailyBatchView();
    } catch (e) {
      alert('Failed to set scout: ' + e.message);
    }
  };
}

if ($('batch-hold-minutes')) {
  $('batch-hold-minutes').onchange = async (e) => {
    try {
      await api('POST', '/daily-batch/settings', { human_pause_minutes: parseInt(e.target.value, 10) });
    } catch (err) {
      console.warn('Failed to update pause setting:', err);
    }
  };
}

if ($('batch-btn-generate')) {
  $('batch-btn-generate').onclick = async () => {
    const statusEl = $('batch-gen-status');
    const groupSel = $('batch-gen-group');
    const countSel = $('batch-gen-count');
    const topicInp = $('batch-gen-topic');

    if (!groupSel || !groupSel.value) {
      if (statusEl) statusEl.textContent = 'Please select a watched group first.';
      return;
    }

    const btn = $('batch-btn-generate');
    const oldText = btn.textContent;
    btn.textContent = 'Generating...';
    btn.disabled = true;
    if (statusEl) statusEl.textContent = 'Synthesizing daily multi-persona dialogue with fal.ai...';

    try {
      const res = await api('POST', '/daily-batch/generate', {
        chat_id: parseInt(groupSel.value, 10),
        count: parseInt(countSel ? countSel.value : 6, 10),
        topic: topicInp ? topicInp.value.trim() : ''
      });
      if (statusEl) statusEl.textContent = `Success: Generated ${res.batch ? res.batch.length : 0} scheduled messages across the day.`;
      await loadDailyBatchView();
    } catch (err) {
      if (statusEl) statusEl.textContent = 'Generation failed: ' + err.message;
    } finally {
      btn.textContent = oldText;
      btn.disabled = false;
    }
  };
}

if ($('batch-btn-send-next')) {
  $('batch-btn-send-next').onclick = async () => {
    const statusEl = $('batch-gen-status');
    try {
      const res = await api('POST', '/daily-batch/send-next');
      if (statusEl) statusEl.textContent = 'Triggered next scheduled message (ID ' + res.item_id + '). Account will awaken ephemerally.';
      await loadDailyBatchView();
    } catch (e) {
      if (statusEl) statusEl.textContent = 'Send next failed: ' + e.message;
    }
  };
}

if ($('batch-btn-clear')) {
  $('batch-btn-clear').onclick = async () => {
    if (!confirm('Clear all pending scheduled batch messages for today?')) return;
    const groupSel = $('batch-gen-group');
    const cid = groupSel && groupSel.value ? parseInt(groupSel.value, 10) : null;
    try {
      await api('POST', '/daily-batch/clear', { chat_id: cid });
      await loadDailyBatchView();
    } catch (e) {
      alert('Failed to clear batch: ' + e.message);
    }
  };
}

if ($('batch-btn-refresh-all')) {
  $('batch-btn-refresh-all').onclick = () => {
    loadDailyBatchView();
    loadQueue();
  };
}

if ($('batch-queue-refresh-btn')) {
  $('batch-queue-refresh-btn').onclick = () => {
    loadQueue();
  };
}


/* ==========================================================================
   PERSONA MEMORY CONTROLLER & AUTONOMOUS AGENT
   ========================================================================== */

let activeMemoryPersonaId = null;
let memoryPersonasData = [];

async function loadMemoryView() {
  try {
    const ov = await api('GET', '/memory/overview');
    memoryPersonasData = ov.personas || [];
    renderMemoryPersonaList();

    if (!activeMemoryPersonaId && memoryPersonasData.length > 0) {
      activeMemoryPersonaId = memoryPersonasData[0].id;
    }
    if (activeMemoryPersonaId) {
      await selectMemoryPersona(activeMemoryPersonaId);
    }
  } catch (err) {
    const msg = $('mem-msg');
    if (msg) msg.textContent = 'Failed to load memory overview: ' + err.message;
  }
}

function renderMemoryPersonaList() {
  const container = $('mem-persona-list');
  const countEl = $('mem-persona-count');
  if (!container) return;

  const search = ($('mem-search-input') && $('mem-search-input').value || '').trim().toLowerCase();
  const filtered = memoryPersonasData.filter(p => !search || (p.name || '').toLowerCase().includes(search));

  if (countEl) countEl.textContent = memoryPersonasData.length;

  container.innerHTML = filtered.map(p => {
    const stats = p.stats || { anchors: 0, weekly: 0, short_term: 0 };
    const isActive = p.id === activeMemoryPersonaId;
    return `
      <div class="mem-p-item ${isActive ? 'active' : ''}" data-pid="${p.id}">
        <div class="mem-p-info">
          <span class="dot" style="background:${esc(p.color || '#2fc4b2')};"></span>
          <span class="mem-p-name">${esc(p.name)}</span>
        </div>
        <div class="mem-p-badges">
          <span class="mem-mini-badge mem-mini-long" title="Permanent Anchors">${stats.anchors}</span>
          <span class="mem-mini-badge mem-mini-med" title="Weekly Protocols">${stats.weekly}</span>
          <span class="mem-mini-badge mem-mini-short" title="Transient Notes">${stats.short_term}</span>
        </div>
      </div>
    `;
  }).join('') || '<div class="hint" style="padding:10px;">No personas found</div>';

  container.querySelectorAll('.mem-p-item').forEach(el => {
    el.onclick = () => selectMemoryPersona(parseInt(el.dataset.pid, 10));
  });
}

async function selectMemoryPersona(pid) {
  activeMemoryPersonaId = pid;
  renderMemoryPersonaList();

  try {
    const tree = await api('GET', `/personas/${pid}/memory-tree`);
    renderMemoryTree(tree);
  } catch (err) {
    const msg = $('mem-msg');
    if (msg) msg.textContent = 'Failed to load memory tree: ' + err.message;
  }
}

function renderMemoryTree(data) {
  if (!data) return;
  const nameEl = $('mem-active-name');
  const dotEl = $('mem-active-dot');
  const totalEl = $('mem-active-total');

  if (nameEl) nameEl.textContent = data.persona_name || `Persona #${data.persona_id}`;
  if (dotEl) dotEl.style.background = data.persona_color || '#2fc4b2';
  if (totalEl) totalEl.textContent = `${data.stats.total || 0} memories`;

  if ($('mem-stat-anchors')) $('mem-stat-anchors').textContent = data.stats.anchors || 0;
  if ($('mem-stat-weekly')) $('mem-stat-weekly').textContent = data.stats.weekly || 0;
  if ($('mem-stat-short')) $('mem-stat-short').textContent = data.stats.short_term || 0;

  if ($('mem-badge-long')) $('mem-badge-long').textContent = (data.tree.anchors || []).length;
  if ($('mem-badge-medium')) $('mem-badge-medium').textContent = (data.tree.weekly || []).length;
  if ($('mem-badge-short')) $('mem-badge-short').textContent = (data.tree.short_term || []).length;

  const renderNodes = (items, isLong, isMedium) => {
    if (!items || items.length === 0) {
      return '<div class="hint" style="padding: 6px 8px;">No memory nodes in this tier.</div>';
    }
    return items.map(m => {
      let expiryLabel = '';
      if (m.remaining_hours !== null && m.remaining_hours !== undefined) {
        if (m.remaining_hours <= 0) {
          expiryLabel = '<span class="badge bad">Expired</span>';
        } else if (m.remaining_hours < 24) {
          expiryLabel = `<span class="badge warn">${m.remaining_hours}h left</span>`;
        } else {
          const days = Math.round((m.remaining_hours / 24) * 10) / 10;
          expiryLabel = `<span class="badge" style="background:rgba(255,255,255,0.06);">${days}d left</span>`;
        }
      }

      const salienceBadge = `<span class="badge" style="background:rgba(47,196,178,0.12);color:var(--accent);">Salience ${Math.round((m.salience || 0.7) * 100)}%</span>`;
      const accessesBadge = m.access_count > 0 ? `<span class="hint" style="font-size:10px;">Accessed ${m.access_count}x</span>` : '';

      const promoteBtn = !isLong ? `
        <button type="button" class="ghost btn-xs mem-act-promote" data-mid="${m.id}" title="Promote to permanent anchor">Promote to Anchor</button>
      ` : '';

      return `
        <div class="mem-node-card">
          <div class="mem-node-head">
            <div class="mem-node-meta">
              ${salienceBadge}
              ${expiryLabel}
              ${accessesBadge}
            </div>
            <div class="mem-node-actions">
              ${promoteBtn}
              <button type="button" class="ghost btn-xs mem-act-del" data-mid="${m.id}" style="color:#ff6b6b;" title="Delete memory node">Delete</button>
            </div>
          </div>
          <div class="mem-node-text">${esc(m.content)}</div>
        </div>
      `;
    }).join('');
  };

  const listLong = $('mem-list-long');
  const listMed = $('mem-list-medium');
  const listShort = $('mem-list-short');

  if (listLong) listLong.innerHTML = renderNodes(data.tree.anchors, true, false);
  if (listMed) listMed.innerHTML = renderNodes(data.tree.weekly, false, true);
  if (listShort) listShort.innerHTML = renderNodes(data.tree.short_term, false, false);

  // Wire node action listeners
  document.querySelectorAll('.mem-act-del').forEach(btn => {
    btn.onclick = async () => {
      const mid = parseInt(btn.dataset.mid, 10);
      try {
        await api('DELETE', `/personas/memories/${mid}`);
        if (activeMemoryPersonaId) await selectMemoryPersona(activeMemoryPersonaId);
      } catch (err) {
        alert('Failed to delete memory: ' + err.message);
      }
    };
  });

  document.querySelectorAll('.mem-act-promote').forEach(btn => {
    btn.onclick = async () => {
      const mid = parseInt(btn.dataset.mid, 10);
      try {
        await api('POST', `/personas/memories/${mid}/promote`);
        if (activeMemoryPersonaId) await selectMemoryPersona(activeMemoryPersonaId);
      } catch (err) {
        alert('Failed to promote memory: ' + err.message);
      }
    };
  });
}

// Add memory handlers
if ($('mem-btn-new')) {
  $('mem-btn-new').onclick = () => {
    const f = $('mem-add-form');
    if (f) f.classList.toggle('hidden');
  };
}
if ($('mem-add-close')) {
  $('mem-add-close').onclick = () => {
    const f = $('mem-add-form');
    if (f) f.classList.add('hidden');
  };
}
if ($('mem-add-submit')) {
  $('mem-add-submit').onclick = async () => {
    if (!activeMemoryPersonaId) {
      alert('Select a persona first');
      return;
    }
    const textEl = $('mem-add-content');
    const text = (textEl && textEl.value || '').trim();
    if (!text) {
      alert('Please enter memory statement content');
      return;
    }
    const tier = $('mem-add-tier') ? $('mem-add-tier').value : 'medium';
    const salience = $('mem-add-salience') ? parseFloat($('mem-add-salience').value) : 0.8;

    try {
      await api('POST', `/personas/${activeMemoryPersonaId}/memories`, {
        content: text,
        retention_tier: tier,
        salience: salience
      });
      textEl.value = '';
      const f = $('mem-add-form');
      if (f) f.classList.add('hidden');
      await selectMemoryPersona(activeMemoryPersonaId);
    } catch (err) {
      alert('Failed to add memory node: ' + err.message);
    }
  };
}

// Pruning agent handlers
if ($('mem-btn-prune-all')) {
  $('mem-btn-prune-all').onclick = async () => {
    const btn = $('mem-btn-prune-all');
    btn.disabled = true;
    btn.textContent = 'Agent Pruning...';
    try {
      const res = await api('POST', '/memory/prune-all');
      alert(`Autonomous Memory Pruning Completed:
• Scanned: ${res.scanned}
• Pruned/Expired: ${res.pruned}
• Promoted to Anchors: ${res.promoted}
• Retained Active: ${res.retained}`);
      await loadMemoryView();
    } catch (err) {
      alert('Memory pruning agent failed: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Prune Expired (Agent)';
    }
  };
}

if ($('mem-btn-prune-active')) {
  $('mem-btn-prune-active').onclick = async () => {
    if (!activeMemoryPersonaId) return;
    try {
      const res = await api('POST', `/personas/${activeMemoryPersonaId}/memory/prune`);
      alert(`Persona Memory Cleanup:
• Scanned: ${res.scanned}
• Pruned: ${res.pruned}
• Promoted: ${res.promoted}`);
      await selectMemoryPersona(activeMemoryPersonaId);
    } catch (err) {
      alert('Pruning failed: ' + err.message);
    }
  };
}

if ($('mem-search-input')) {
  $('mem-search-input').oninput = () => {
    renderMemoryPersonaList();
  };
}


/* ==========================================================================
   USAGE, TELEMETRY & COMPUTING RESOURCE CONTROLLER
   ========================================================================== */

async function loadUsageView() {
  const daysSel = $('usage-days-select');
  const days = daysSel ? parseInt(daysSel.value, 10) || 7 : 7;

  try {
    const [summary, timeseries, personas, activity] = await Promise.all([
      api('GET', '/usage/summary'),
      api('GET', `/usage/timeseries?days=${days}`),
      api('GET', '/usage/personas'),
      api('GET', '/usage/activity?limit=30')
    ]);

    renderUsageSummary(summary);
    renderUsageTimeseriesChart(timeseries);
    renderUsagePersonaBreakdown(personas);
    renderUsageActivityTable(activity);
  } catch (err) {
    console.error('Failed to load usage telemetry:', err);
  }
}

function renderUsageSummary(s) {
  if (!s) return;
  const msgs = s.messages || {};
  const aiStats = s.ai || {};
  const comp = s.compute || {};

  if ($('u-msg-sent-today')) $('u-msg-sent-today').textContent = msgs.sent_today || 0;
  if ($('u-msg-sent-7d')) $('u-msg-sent-7d').textContent = msgs.sent_7d || 0;
  if ($('u-msg-sent-total')) $('u-msg-sent-total').textContent = msgs.sent_total || 0;
  if ($('u-msg-recv-today')) $('u-msg-recv-today').textContent = `${msgs.received_today || 0} today`;
  if ($('u-msg-recv-total')) $('u-msg-recv-total').textContent = msgs.received_total || 0;

  if ($('u-ai-tokens-total')) $('u-ai-tokens-total').textContent = (aiStats.total_tokens || 0).toLocaleString();
  if ($('u-ai-tokens-prompt')) $('u-ai-tokens-prompt').textContent = (aiStats.prompt_tokens || 0).toLocaleString();
  if ($('u-ai-tokens-comp')) $('u-ai-tokens-comp').textContent = (aiStats.completion_tokens || 0).toLocaleString();
  if ($('u-ai-cost-est')) $('u-ai-cost-est').textContent = `$${(aiStats.cost_estimate_usd || 0).toFixed(4)}`;

  if ($('u-ai-latency-avg')) $('u-ai-latency-avg').textContent = `${aiStats.avg_latency_ms || 0} ms`;

  if ($('u-comp-sockets')) $('u-comp-sockets').textContent = `${comp.active_sockets || 1} Scout Socket`;
  if ($('u-comp-dormant')) $('u-comp-dormant').textContent = `${comp.dormant_accounts || 0}`;
  if ($('u-comp-savings')) $('u-comp-savings').textContent = `~${comp.ram_savings_percent || 90}% overhead saved`;
}

function renderUsageTimeseriesChart(ts) {
  const container = $('usage-chart-timeseries');
  if (!container) return;
  if (!ts || ts.length === 0) {
    container.innerHTML = '<div class="hint" style="padding:20px;text-align:center;">No activity recorded in this window.</div>';
    return;
  }

  const width = container.clientWidth || 580;
  const height = 180;
  const padLeft = 35;
  const padRight = 15;
  const padTop = 15;
  const padBottom = 25;
  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  const maxMsgs = Math.max(1, ...ts.map(d => Math.max(d.sent || 0, d.received || 0)));
  const maxTok = Math.max(1, ...ts.map(d => d.tokens || 0));

  const n = ts.length;
  const barGroupWidth = chartW / n;
  const barWidth = Math.max(4, Math.min(14, (barGroupWidth - 10) / 2));

  let barsSvg = '';
  let tokenLinePoints = [];

  ts.forEach((d, i) => {
    const xGroupCenter = padLeft + (i * barGroupWidth) + (barGroupWidth / 2);
    const xSent = xGroupCenter - barWidth - 1;
    const xRecv = xGroupCenter + 1;

    const sentH = Math.round(((d.sent || 0) / maxMsgs) * chartH);
    const recvH = Math.round(((d.received || 0) / maxMsgs) * chartH);

    const sentY = padTop + chartH - sentH;
    const recvY = padTop + chartH - recvH;

    barsSvg += `
      <rect x="${xSent}" y="${sentY}" width="${barWidth}" height="${sentH}" fill="var(--accent)" rx="2">
        <title>${esc(d.label)}: ${d.sent} messages sent</title>
      </rect>
      <rect x="${xRecv}" y="${recvY}" width="${barWidth}" height="${recvH}" fill="#3b82f6" rx="2">
        <title>${esc(d.label)}: ${d.received} messages received</title>
      </rect>
      <text x="${xGroupCenter}" y="${height - 6}" font-size="9.5" fill="var(--muted)" text-anchor="middle">${esc(d.label)}</text>
    `;

    const tokY = padTop + chartH - Math.round(((d.tokens || 0) / maxTok) * chartH);
    tokenLinePoints.push(`${xGroupCenter},${tokY}`);
  });

  const polyline = `<polyline fill="none" stroke="#a855f7" stroke-width="2" points="${tokenLinePoints.join(' ')}" />`;
  const dots = tokenLinePoints.map((pt, idx) => {
    const [px, py] = pt.split(',');
    return `<circle cx="${px}" cy="${py}" r="3" fill="#a855f7"><title>${esc(ts[idx].label)}: ${ts[idx].tokens} tokens</title></circle>`;
  }).join('');

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <line x1="${padLeft}" y1="${padTop + chartH}" x2="${width - padRight}" y2="${padTop + chartH}" stroke="var(--line)" />
      <line x1="${padLeft}" y1="${padTop + (chartH/2)}" x2="${width - padRight}" y2="${padTop + (chartH/2)}" stroke="rgba(255,255,255,0.03)" stroke-dasharray="3,3" />
      ${barsSvg}
      ${polyline}
      ${dots}
    </svg>
  `;
}

function renderUsagePersonaBreakdown(list) {
  const container = $('usage-personas-list');
  if (!container) return;
  if (!list || list.length === 0) {
    container.innerHTML = '<div class="hint" style="padding:15px;text-align:center;">No personas configured yet.</div>';
    return;
  }

  container.innerHTML = list.map(p => {
    const color = esc(p.color || '#2fc4b2');
    const share = p.share_pct || 0;
    return `
      <div class="usage-p-row">
        <div class="usage-p-row-top">
          <span class="usage-p-row-name">
            <span class="dot" style="background:${color};"></span>
            ${esc(p.name)}
          </span>
          <span class="usage-p-row-stat">${p.messages_sent} msgs · ${(p.tokens || 0).toLocaleString()} tokens · ${share}%</span>
        </div>
        <div class="usage-p-bar-bg">
          <div class="usage-p-bar-fill" style="width: ${Math.max(2, share)}%; background: ${color};"></div>
        </div>
      </div>
    `;
  }).join('');
}

function renderUsageActivityTable(rows) {
  const tbody = $('usage-activity-rows');
  const countEl = $('u-activity-count');
  if (!tbody) return;

  if (countEl) countEl.textContent = `${rows ? rows.length : 0} events`;

  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="hint" style="text-align:center;padding:16px;">No activity logs recorded yet. Events record automatically as messages send and receive.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const dt = new Date(r.timestamp * 1000);
    const timeStr = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    let eventBadge = '';
    if (r.event_type === 'msg_sent') {
      eventBadge = '<span class="u-badge-sent">Sent Message</span>';
    } else if (r.event_type === 'msg_recv') {
      eventBadge = '<span class="u-badge-recv">Received Msg</span>';
    } else if (r.event_type === 'ai_chat') {
      eventBadge = '<span class="u-badge-ai">Live AI Reply</span>';
    } else if (r.event_type === 'ai_batch') {
      eventBadge = '<span class="u-badge-ai">Daily Batch AI</span>';
    } else {
      eventBadge = `<span>${esc(r.event_type)}</span>`;
    }

    const personaName = r.persona_name ? `
      <span style="display:inline-flex;align-items:center;gap:4px;">
        <span class="dot" style="background:${esc(r.persona_color || '#2fc4b2')};width:6px;height:6px;"></span>
        ${esc(r.persona_name)}
      </span>
    ` : `<span class="hint">Account #${r.account_id || '-'}</span>`;

    const groupTitle = r.group_title ? esc(r.group_title) : (r.chat_id ? `Chat ${r.chat_id}` : '-');
    const tokens = r.tokens_total > 0 ? r.tokens_total.toLocaleString() : '-';
    const latency = r.latency_ms > 0 ? `${r.latency_ms} ms` : '-';
    const cost = r.cost_est > 0 ? `$${parseFloat(r.cost_est).toFixed(4)}` : '-';

    return `
      <tr>
        <td style="padding:6px 8px; color:var(--muted);">${timeStr}</td>
        <td style="padding:6px 8px;">${eventBadge}</td>
        <td style="padding:6px 8px;">${personaName}</td>
        <td style="padding:6px 8px;">${groupTitle}</td>
        <td style="padding:6px 8px; text-align:right;">${tokens}</td>
        <td style="padding:6px 8px; text-align:right;">${latency}</td>
        <td style="padding:6px 8px; text-align:right; color:var(--muted);">${cost}</td>
      </tr>
    `;
  }).join('');
}

// Wire Usage buttons
if ($('usage-btn-refresh')) {
  $('usage-btn-refresh').onclick = () => loadUsageView();
}
if ($('usage-days-select')) {
  $('usage-days-select').onchange = () => loadUsageView();
}

window.openPersonaStudio = openPersonaStudio;

if ($('p-btn-reconcile')) {
  $('p-btn-reconcile').onclick = async () => {
    const btn = $('p-btn-reconcile');
    const oldText = btn.textContent;
    btn.disabled = true; btn.textContent = 'Verifying…';
    try {
      const r = await api('POST', '/personas/reconcile');
      alert(`Verified ${r.verified || 0} personas.
Created & linked ${r.created || 0} name-matched personas to accounts in xbiolabs.`);
      loadPersonas();
      if (window.loadNetwork) window.loadNetwork();
    } catch (e) {
      alert('Reconciliation failed: ' + e.message);
    } finally {
      btn.disabled = false; btn.textContent = oldText;
    }
  };
}

if ($('acc-sync-all-avatars')) {
  $('acc-sync-all-avatars').onclick = async () => {
    const btn = $('acc-sync-all-avatars');
    const oldText = btn.textContent;
    btn.disabled = true; btn.textContent = 'Syncing photos…';
    try {
      const r = await api('POST', '/accounts/sync-avatars');
      let msg = `Profile pictures: ${r.synced || 0} downloaded.`;
      if (r.no_photo) msg += `\n${r.no_photo} account(s) have no profile photo set on Telegram.`;
      if (r.errors && r.errors.length) {
        msg += `\n\n${r.errors.length} account(s) failed to connect:\n` + r.errors.map((e) => `• ${e.name}: ${e.error}`).join('\n');
      }
      alert(msg);
      await loadAccounts();
      await loadOverview();
      if (window.loadNetwork) window.loadNetwork();
    } catch (e) {
      alert('Avatar sync failed: ' + e.message);
    } finally {
      btn.disabled = false; btn.textContent = oldText;
    }
  };
}
