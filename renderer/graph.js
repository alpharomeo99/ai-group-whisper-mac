// Network canvas: Personas -> Accounts -> Groups, node-graph style.
(() => {
  const NW = 232, COLX = { per: 60, acc: 420, grp: 780 }, ROW = 138;
  const COLORS = ['#2fc4b2', '#7c8cff', '#ff7a66', '#f5b84a', '#c77dff', '#38d48b', '#ff5fa2', '#4cc9f0'];
  const g = { data: null, view: { x: 40, y: 40, k: 1 }, layout: {}, sel: null, wireDrag: null, q: '' };
  let root, world, svg, wiresG, ghost, nodesEl, mini, insp, saveT;
  const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
  const E = (s) => { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; };
  const call = (m, r, b) => window.agw.api(m, r, b);
  const toast = (msg, bad) => { const t = el('div', 'gx-toast' + (bad ? ' bad' : ''), E(msg)); root.appendChild(t); setTimeout(() => t.classList.add('out'), 2200); setTimeout(() => t.remove(), 2600); };

  function build() {
    root = document.getElementById('gx');
    root.innerHTML = `
      <div class="gx-bar">
        <div class="gx-title"><b>Network</b><span>Drag from a dot to wire Persona &rarr; Account &rarr; Group</span></div>
        <input id="gx-q" class="gx-search" placeholder="Search nodes" />
        <button class="gx-btn" id="gx-add-per">+ Persona</button>
        <button class="gx-btn ghost" id="gx-sync" title="Pull groups from all accounts">&#8635; Sync groups</button>
        <button class="gx-btn ghost" id="gx-align" title="Tidy columns">&#9638; Align</button>
      </div>
      <div class="gx-canvas" id="gx-canvas">
        <div class="gx-cols"><span>Personas</span><span>Accounts</span><span>Groups</span></div>
        <div class="gx-world" id="gx-world">
          <svg class="gx-wires" id="gx-svg" width="1" height="1"><g id="gx-wg"></g><path id="gx-ghost" class="gx-wire ghost" /></svg>
          <div id="gx-nodes"></div>
        </div>
        <div class="gx-zoom"><button id="gx-zin">+</button><button id="gx-zout">&minus;</button><button id="gx-fit" title="Fit">&#9974;</button></div>
        <canvas class="gx-mini" id="gx-mini" width="200" height="130"></canvas>
        <div class="gx-empty hidden" id="gx-empty">Add a Telegram account and sync groups to see your network.</div>
      </div>
      <div class="gx-insp" id="gx-insp"></div>`;
    world = root.querySelector('#gx-world'); svg = root.querySelector('#gx-svg'); wiresG = root.querySelector('#gx-wg');
    ghost = root.querySelector('#gx-ghost'); nodesEl = root.querySelector('#gx-nodes'); mini = root.querySelector('#gx-mini'); insp = root.querySelector('#gx-insp');
    const cv = root.querySelector('#gx-canvas');

    // pan
    cv.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.gx-node,.gx-zoom,.gx-mini,.gx-wire')) return;
      select(null);
      const sx = e.clientX, sy = e.clientY, ox = g.view.x, oy = g.view.y;
      cv.classList.add('panning'); cv.setPointerCapture(e.pointerId);
      const mv = (ev) => { g.view.x = ox + ev.clientX - sx; g.view.y = oy + ev.clientY - sy; applyView(); };
      const up = () => { cv.classList.remove('panning'); cv.removeEventListener('pointermove', mv); cv.removeEventListener('pointerup', up); };
      cv.addEventListener('pointermove', mv); cv.addEventListener('pointerup', up);
    });
    // zoom at cursor
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      if (!e.ctrlKey && !e.metaKey) { g.view.x -= e.deltaX; g.view.y -= e.deltaY; applyView(); return; } // two-finger pan; pinch or Cmd+scroll zooms
      const r = cv.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)));
    }, { passive: false });
    root.querySelector('#gx-zin').onclick = () => zoomCenter(1.2);
    root.querySelector('#gx-zout').onclick = () => zoomCenter(1 / 1.2);
    root.querySelector('#gx-fit').onclick = () => fit(true);
    root.querySelector('#gx-align').onclick = () => { g.layout = {}; render(); fit(true); saveLayout(); };
    root.querySelector('#gx-q').oninput = (e) => { g.q = e.target.value.trim().toLowerCase(); render(); };
    root.querySelector('#gx-add-per').onclick = addPersona;
    root.querySelector('#gx-sync').onclick = async (e) => {
      e.target.disabled = true; e.target.textContent = 'Syncing...';
      try { await call('POST', '/graph/sync'); await load(); toast('Groups synced'); } catch (err) { toast(err.message, true); }
      e.target.disabled = false; e.target.innerHTML = '&#8635; Sync groups';
    };
    mini.addEventListener('pointerdown', miniJump);
    document.addEventListener('keydown', (e) => {
      if (root.offsetParent === null || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
      if ((e.key === 'Backspace' || e.key === 'Delete') && g.sel && g.sel.wire) { unlink(g.sel.wire); }
      if (e.key === 'Escape') select(null);
      if (e.key === 'f') fit(true);
    });
    window.addEventListener('resize', () => drawMini());
  }

  // ---------- view ----------
  function applyView() {
    world.style.transform = `translate(${g.view.x}px,${g.view.y}px) scale(${g.view.k})`;
    root.querySelector('#gx-canvas').style.backgroundPosition = `${g.view.x}px ${g.view.y}px`;
    root.querySelector('#gx-canvas').style.backgroundSize = `${22 * g.view.k}px ${22 * g.view.k}px`;
    drawMini();
  }
  function zoomAt(px, py, f) {
    const k = Math.min(2, Math.max(0.3, g.view.k * f)); f = k / g.view.k;
    g.view.x = px - (px - g.view.x) * f; g.view.y = py - (py - g.view.y) * f; g.view.k = k; applyView();
  }
  function zoomCenter(f) { const r = root.querySelector('#gx-canvas').getBoundingClientRect(); smooth(); zoomAt(r.width / 2, r.height / 2, f); }
  function smooth() { world.classList.add('anim'); clearTimeout(smooth.t); smooth.t = setTimeout(() => world.classList.remove('anim'), 320); }
  function bounds() {
    const ns = [...nodesEl.children]; if (!ns.length) return null;
    let x1 = 1e9, y1 = 1e9, x2 = -1e9, y2 = -1e9;
    ns.forEach((n) => { const x = +n.dataset.x, y = +n.dataset.y; x1 = Math.min(x1, x); y1 = Math.min(y1, y); x2 = Math.max(x2, x + NW); y2 = Math.max(y2, y + n.offsetHeight); });
    return { x1, y1, x2, y2 };
  }
  function fit(anim) {
    const b = bounds(), r = root.querySelector('#gx-canvas').getBoundingClientRect(); if (!b || !r.width) return;
    const k = Math.min(1.15, Math.max(0.3, Math.min((r.width - 120) / (b.x2 - b.x1), (r.height - 140) / (b.y2 - b.y1))));
    if (anim) smooth();
    g.view = { k, x: (r.width - (b.x2 - b.x1) * k) / 2 - b.x1 * k, y: (r.height - (b.y2 - b.y1) * k) / 2 - b.y1 * k + 16 };
    applyView();
  }

  // ---------- data ----------
  async function load() {
    const d = await call('GET', '/graph');
    g.data = d; if (!Object.keys(g.layout).length) g.layout = d.layout || {};
    render();
    if (!load.fitted) { load.fitted = true; requestAnimationFrame(() => fit(false)); }
    if (g.sel && g.sel.id) select(g.sel.id, true);
  }
  function nodesList() {
    const d = g.data; if (!d) return [];
    const out = [];
    d.personas.forEach((p, i) => out.push({ id: 'per:' + p.id, kind: 'per', i, o: p }));
    d.accounts.forEach((a, i) => out.push({ id: 'acc:' + a.id, kind: 'acc', i, o: a }));
    d.groups.forEach((x, i) => out.push({ id: 'grp:' + x.chat_id, kind: 'grp', i, o: x }));
    return out;
  }
  function edgesList() {
    const d = g.data, out = []; if (!d) return out;
    d.accounts.forEach((a) => { if (a.persona_id) out.push({ from: 'per:' + a.persona_id, to: 'acc:' + a.id }); });
    d.groups.forEach((x) => { if (x.account_id) out.push({ from: 'acc:' + x.account_id, to: 'grp:' + x.chat_id }); });
    return out;
  }
  const label = (n) => n.kind === 'per' ? n.o.name : n.kind === 'acc' ? (n.o.name || n.o.phone || 'Account') : (n.o.title || n.o.chat_id);
  const personaColor = (pid) => { const p = g.data.personas.find((x) => x.id === pid); return p ? p.color : null; };

  function nodeHTML(n) {
    const o = n.o;
    if (n.kind === 'per') {
      const used = g.data.accounts.filter((a) => a.persona_id === o.id).length;
      return `<div class="gx-head"><span class="gx-av" style="background:${E(o.color)}">${E((o.name || '?')[0].toUpperCase())}</span>
        <div class="gx-ht"><b>${E(o.name)}</b><small>Persona</small></div></div>
        <p class="gx-body">${E(o.prompt || 'No instructions yet - click to write how this persona talks.')}</p>
        <div class="gx-foot"><span class="gx-chip">${used} account${used === 1 ? '' : 's'}</span></div>
        <i class="gx-port out" data-port="out"></i>`;
    }
    if (n.kind === 'acc') {
      const c = personaColor(o.persona_id); const ng = g.data.groups.filter((x) => String(x.account_id) === String(o.id)).length;
      return `<i class="gx-port in" data-port="in"></i>
        <div class="gx-head"><span class="gx-av acc" ${c ? `style="box-shadow:0 0 0 2px ${E(c)}"` : ''}>${E((o.name || o.phone || '#')[0].toUpperCase())}</span>
        <div class="gx-ht"><b>${E(o.name || o.phone)}</b><small>${o.username ? '@' + E(o.username) : E(o.phone || '')}</small></div>
        <span class="gx-dot ${o.connected ? 'ok' : o.active ? 'warn' : ''}" title="${o.connected ? 'Connected' : o.active ? 'Not connected' : 'Paused'}"></span></div>
        <div class="gx-foot"><span class="gx-chip">${ng} group${ng === 1 ? '' : 's'}</span>${o.proxy_label ? `<span class="gx-chip">&#8644; ${E(o.proxy_label)}</span>` : '<span class="gx-chip dim">no proxy</span>'}</div>
        <i class="gx-port out" data-port="out"></i>`;
    }
    const acc = g.data.accounts.find((a) => String(a.id) === String(o.account_id));
    const c = acc && personaColor(acc.persona_id);
    return `<i class="gx-port in" data-port="in"></i>
      <div class="gx-head"><span class="gx-av grp" ${c ? `style="background:${E(c)}22;color:${E(c)}"` : ''}>#</span>
      <div class="gx-ht"><b>${E(o.title)}</b><small>${o.messages} msgs saved</small></div></div>
      <div class="gx-toggles">
        <label class="gx-tg"><input type="checkbox" data-k="watched" ${o.watched ? 'checked' : ''}/><span></span>Watch</label>
        <label class="gx-tg"><input type="checkbox" data-k="auto_reply" ${o.auto_reply ? 'checked' : ''}/><span></span>Auto-reply</label>
      </div>`;
  }

  function render() {
    if (!g.data) return;
    const list = nodesList(), seen = new Set();
    root.querySelector('#gx-empty').classList.toggle('hidden', list.length > 0);
    list.forEach((n) => {
      seen.add(n.id);
      let e = nodesEl.querySelector(`[data-id="${CSS.escape(n.id)}"]`);
      if (!e) { e = el('div', 'gx-node ' + n.kind); e.dataset.id = n.id; e.dataset.kind = n.kind; nodesEl.appendChild(e); bindNode(e); e.classList.add('enter'); setTimeout(() => e.classList.remove('enter'), 400); }
      e.innerHTML = nodeHTML(n);
      const p = g.layout[n.id] || { x: COLX[n.kind], y: n.i * (n.kind === 'per' ? ROW + 16 : ROW) };
      e.dataset.x = p.x; e.dataset.y = p.y; e.style.transform = `translate(${p.x}px,${p.y}px)`;
      e.classList.toggle('sel', !!(g.sel && g.sel.id === n.id));
      e.classList.toggle('dim', !!g.q && !label(n).toLowerCase().includes(g.q));
      e.querySelectorAll('.gx-tg input').forEach((cb) => cb.onchange = async () => {
        try { await call('POST', '/groups/' + n.o.chat_id, { [cb.dataset.k]: cb.checked ? 1 : 0 }); n.o[cb.dataset.k] = cb.checked ? 1 : 0; } catch (err) { toast(err.message, true); }
      });
    });
    [...nodesEl.children].forEach((e) => { if (!seen.has(e.dataset.id)) e.remove(); });
    drawWires();
  }

  function portPos(id, side) {
    const e = nodesEl.querySelector(`[data-id="${CSS.escape(id)}"]`); if (!e) return null;
    const p = e.querySelector(`.gx-port.${side}`); const x = +e.dataset.x, y = +e.dataset.y;
    return { x: x + (side === 'out' ? NW : 0), y: y + (p ? p.offsetTop + 6 : 30) };
  }
  const curve = (a, b) => { const dx = Math.max(60, Math.abs(b.x - a.x) * 0.5); return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`; };
  function drawWires() {
    wiresG.innerHTML = '';
    edgesList().forEach((w) => {
      const a = portPos(w.from, 'out'), b = portPos(w.to, 'in'); if (!a || !b) return;
      const key = w.from + '>' + w.to;
      const fromNode = nodesList().find((n) => n.id === w.from);
      const col = w.from.startsWith('per:') ? (fromNode && fromNode.o.color) : (fromNode && personaColor(fromNode.o.persona_id)) || '#7c8cff';
      const hit = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      hit.setAttribute('d', curve(a, b)); hit.setAttribute('class', 'gx-wire hit');
      const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      p.setAttribute('d', curve(a, b)); p.setAttribute('class', 'gx-wire' + (g.sel && g.sel.wire === key ? ' sel' : '')); p.style.stroke = col || '#7c8cff';
      const flow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      flow.setAttribute('d', curve(a, b)); flow.setAttribute('class', 'gx-wire flow'); flow.style.stroke = col || '#7c8cff';
      hit.addEventListener('pointerdown', (e) => { e.stopPropagation(); g.sel = { wire: key }; select(null, false, key); });
      hit.addEventListener('dblclick', (e) => { e.stopPropagation(); unlink(key); });
      wiresG.append(p, flow, hit);
    });
    drawMini();
  }

  function bindNode(e) {
    e.addEventListener('pointerdown', (ev) => {
      if (ev.target.closest('.gx-tg')) return;
      ev.stopPropagation();
      const port = ev.target.closest('.gx-port');
      if (port) return startWire(e, port, ev);
      const sx = ev.clientX, sy = ev.clientY, ox = +e.dataset.x, oy = +e.dataset.y; let moved = false;
      e.setPointerCapture(ev.pointerId); e.classList.add('drag');
      const mv = (m) => {
        const dx = (m.clientX - sx) / g.view.k, dy = (m.clientY - sy) / g.view.k;
        if (!moved && Math.hypot(dx, dy) < 3) return; moved = true;
        const x = Math.round((ox + dx) / 2) * 2, y = Math.round((oy + dy) / 2) * 2;
        e.dataset.x = x; e.dataset.y = y; e.style.transform = `translate(${x}px,${y}px)`;
        g.layout[e.dataset.id] = { x, y }; drawWires();
      };
      const up = () => { e.classList.remove('drag'); e.removeEventListener('pointermove', mv); e.removeEventListener('pointerup', up); if (moved) saveLayout(); else select(e.dataset.id); };
      e.addEventListener('pointermove', mv); e.addEventListener('pointerup', up);
    });
  }

  function startWire(nodeEl, port, ev) {
    const cv = root.querySelector('#gx-canvas'), r = cv.getBoundingClientRect();
    const side = port.dataset.port, id = nodeEl.dataset.id;
    const anchor = portPos(id, side);
    const toWorld = (m) => ({ x: (m.clientX - r.left - g.view.x) / g.view.k, y: (m.clientY - r.top - g.view.y) / g.view.k });
    root.classList.add('wiring', 'from-' + nodeEl.dataset.kind + '-' + side);
    const mv = (m) => { const p = toWorld(m); ghost.setAttribute('d', side === 'out' ? curve(anchor, p) : curve(p, anchor)); };
    mv(ev);
    const up = async (m) => {
      document.removeEventListener('pointermove', mv); document.removeEventListener('pointerup', up);
      ghost.setAttribute('d', ''); root.className = root.className.replace(/\s?(wiring|from-\S+)/g, '');
      const target = document.elementFromPoint(m.clientX, m.clientY);
      const tn = target && target.closest('.gx-node'); if (!tn || tn === nodeEl) return;
      const [from, to] = side === 'out' ? [id, tn.dataset.id] : [tn.dataset.id, id];
      const ok = (from.startsWith('per:') && to.startsWith('acc:')) || (from.startsWith('acc:') && to.startsWith('grp:'));
      if (!ok) return toast('Wire Persona → Account, or Account → Group', true);
      try { await call('POST', '/graph/link', { from, to, on: true }); await load(); toast('Connected'); } catch (err) { toast(err.message, true); }
    };
    document.addEventListener('pointermove', mv); document.addEventListener('pointerup', up);
  }

  async function unlink(key) {
    const [from, to] = key.split('>');
    try { await call('POST', '/graph/link', { from, to, on: false }); g.sel = null; await load(); toast('Disconnected'); } catch (err) { toast(err.message, true); }
  }
  function saveLayout() { clearTimeout(saveT); saveT = setTimeout(() => call('POST', '/graph/layout', { layout: g.layout }).catch(() => {}), 500); }

  async function addPersona() {
    const color = COLORS[(g.data ? g.data.personas.length : 0) % COLORS.length];
    try {
      const r = await call('POST', '/personas', { name: 'New persona', prompt: '', color });
      const b = bounds(); g.layout['per:' + r.id] = { x: COLX.per, y: b ? b.y2 + 30 : 0 };
      saveLayout(); await load(); select('per:' + r.id);
    } catch (err) { toast(err.message, true); }
  }

  // ---------- inspector ----------
  function select(id, keep, wire) {
    insp.style.top = root.querySelector('.gx-bar').offsetHeight + 'px';
    g.sel = id ? { id } : wire ? { wire } : null;
    nodesEl.querySelectorAll('.gx-node').forEach((e) => e.classList.toggle('sel', e.dataset.id === id));
    drawWires();
    if (!id) { if (wire) insp.innerHTML = `<div class="gx-ih"><b>Connection</b><button class="gx-x">&times;</button></div><p class="gx-mut">Press Delete or double-click the wire to disconnect.</p><button class="gx-btn danger" id="gx-unl">Disconnect</button>`, insp.classList.add('open'), insp.querySelector('#gx-unl').onclick = () => unlink(wire), insp.querySelector('.gx-x').onclick = () => select(null); else insp.classList.remove('open'); return; }
    const n = nodesList().find((x) => x.id === id); if (!n) return insp.classList.remove('open');
    if (keep && insp.contains(document.activeElement)) return;
    insp.classList.add('open');
    const o = n.o, head = (t, s) => `<div class="gx-ih"><div><b>${E(t)}</b><small>${E(s)}</small></div><button class="gx-x">&times;</button></div>`;
    if (n.kind === 'per') {
      const groupsOpts = g.data.groups.map((x) => `<option value="${x.chat_id}">${E(x.title)}</option>`).join('');
      insp.innerHTML = head(o.name, 'Persona') + `
        <label class="gx-l">Name</label><input id="pi-name" value="${E(o.name)}" />
        <label class="gx-l">Colour</label><div class="gx-sw">${COLORS.map((c) => `<button data-c="${c}" style="background:${c}" class="${c === o.color ? 'on' : ''}"></button>`).join('')}</div>
        <label class="gx-l">How this persona talks</label><textarea id="pi-prompt" rows="7" placeholder="e.g. 24yo crypto trader from Austin, casual, lowercase, short replies, never uses emojis">${E(o.prompt)}</textarea>
        <div class="gx-row"><button class="gx-btn" id="pi-save">Save</button><button class="gx-btn danger ghost" id="pi-del">Delete</button></div>
        <div class="gx-sep"></div>
        <label class="gx-l">Preview a reply</label>
        <select id="pi-grp"><option value="">Sample chat</option>${groupsOpts}</select>
        <button class="gx-btn ghost wide" id="pi-prev">&#9654; Generate preview</button>
        <div id="pi-out"></div>`;
      let color = o.color;
      insp.querySelectorAll('.gx-sw button').forEach((b) => b.onclick = () => { color = b.dataset.c; insp.querySelectorAll('.gx-sw button').forEach((x) => x.classList.toggle('on', x === b)); });
      insp.querySelector('#pi-save').onclick = async () => {
        try { await call('POST', '/personas', { id: o.id, name: insp.querySelector('#pi-name').value, prompt: insp.querySelector('#pi-prompt').value, color }); document.activeElement.blur(); await load(); toast('Saved'); } catch (err) { toast(err.message, true); }
      };
      insp.querySelector('#pi-del').onclick = async () => { if (!confirm('Delete this persona?')) return; await call('DELETE', '/personas/' + o.id); delete g.layout[id]; saveLayout(); select(null); await load(); };
      insp.querySelector('#pi-prev').onclick = async (e) => {
        const out = insp.querySelector('#pi-out'); e.target.disabled = true; out.innerHTML = '<div class="gx-typing"><i></i><i></i><i></i></div>';
        try {
          const r = await call('POST', `/personas/${o.id}/preview`, { chat_id: insp.querySelector('#pi-grp').value || null });
          out.innerHTML = `<div class="gx-chat">${r.context.map((m) => `<div class="gx-msg"><b>${E(m.sender)}</b>${E(m.text)}</div>`).join('')}<div class="gx-msg me" style="--c:${E(color)}"><b>${E(o.name)}</b>${E(r.text)}</div></div>`;
        } catch (err) { out.innerHTML = `<p class="gx-err">${E(err.message)}</p>`; }
        e.target.disabled = false;
      };
    } else if (n.kind === 'acc') {
      const pers = g.data.personas.map((p) => `<option value="${p.id}" ${p.id === o.persona_id ? 'selected' : ''}>${E(p.name)}</option>`).join('');
      const gs = g.data.groups.filter((x) => String(x.account_id) === String(o.id));
      insp.innerHTML = head(o.name || o.phone, o.username ? '@' + o.username : 'Account') + `
        <div class="gx-kv"><span>Phone</span><b>${E(o.phone || '-')}</b><span>Status</span><b>${o.connected ? '<em class="ok">Connected</em>' : o.active ? 'Not connected' : 'Paused'}</b><span>Proxy</span><b>${E(o.proxy_label || 'None')}</b></div>
        <label class="gx-l">Persona</label><select id="ai-per"><option value="">None</option>${pers}</select>
        <label class="gx-l">Speaks in ${gs.length} group${gs.length === 1 ? '' : 's'}</label>
        <div class="gx-list">${gs.map((x) => `<div>#${E(x.title)}</div>`).join('') || '<p class="gx-mut">Drag from this account\'s right dot to a group.</p>'}</div>`;
      insp.querySelector('#ai-per').onchange = async (e) => {
        const v = e.target.value;
        try {
          if (v) await call('POST', '/graph/link', { from: 'per:' + v, to: id, on: true });
          else if (o.persona_id) await call('POST', '/graph/link', { from: 'per:' + o.persona_id, to: id, on: false });
          await load();
        } catch (err) { toast(err.message, true); }
      };
    } else {
      insp.innerHTML = head(o.title, 'Group') + `
        <div class="gx-kv"><span>Messages saved</span><b>${o.messages}</b><span>Account</span><b>${E((g.data.accounts.find((a) => String(a.id) === String(o.account_id)) || {}).name || 'None')}</b></div>
        <label class="gx-l">Group-specific instructions <small>(overrides persona)</small></label>
        <textarea id="gi-p" rows="4" placeholder="Leave empty to use the account's persona">${E(o.persona || '')}</textarea>
        <button class="gx-btn" id="gi-save">Save</button>
        <div class="gx-sep"></div>
        <label class="gx-l">Recent messages</label><div id="gi-feed" class="gx-chat"><div class="gx-typing"><i></i><i></i><i></i></div></div>`;
      insp.querySelector('#gi-save').onclick = async () => { try { await call('POST', '/groups/' + o.chat_id, { persona: insp.querySelector('#gi-p').value }); o.persona = insp.querySelector('#gi-p').value; toast('Saved'); } catch (err) { toast(err.message, true); } };
      call('GET', `/groups/${o.chat_id}/feed`).then((f) => {
        const box = insp.querySelector('#gi-feed'); if (!box) return;
        box.innerHTML = f.messages.slice(-25).map((m) => `<div class="gx-msg"><b>${E(m.sender)}</b>${E(m.text)}</div>`).join('') || '<p class="gx-mut">Nothing saved yet. Turn on Watch.</p>';
        box.scrollTop = box.scrollHeight;
      }).catch(() => {});
    }
    insp.querySelector('.gx-x').onclick = () => select(null);
  }

  // ---------- minimap ----------
  function drawMini() {
    if (!mini || !g.data) return;
    const ctx = mini.getContext('2d'), W = mini.width, H = mini.height; ctx.clearRect(0, 0, W, H);
    const b = bounds(); if (!b) return;
    const cv = root.querySelector('#gx-canvas').getBoundingClientRect();
    const vx1 = -g.view.x / g.view.k, vy1 = -g.view.y / g.view.k, vx2 = vx1 + cv.width / g.view.k, vy2 = vy1 + cv.height / g.view.k;
    const x1 = Math.min(b.x1, vx1) - 40, y1 = Math.min(b.y1, vy1) - 40, x2 = Math.max(b.x2, vx2) + 40, y2 = Math.max(b.y2, vy2) + 40;
    const s = Math.min(W / (x2 - x1), H / (y2 - y1)); mini._m = { x1, y1, s };
    const tc = { per: '#c77dff', acc: '#2fc4b2', grp: '#7c8cff' };
    [...nodesEl.children].forEach((n) => { ctx.fillStyle = tc[n.dataset.kind]; ctx.globalAlpha = n.classList.contains('sel') ? 1 : 0.7; ctx.fillRect((+n.dataset.x - x1) * s, (+n.dataset.y - y1) * s, NW * s, Math.max(3, n.offsetHeight * s)); });
    ctx.globalAlpha = 1; ctx.strokeStyle = 'rgba(255,255,255,.7)'; ctx.lineWidth = 1;
    ctx.strokeRect((vx1 - x1) * s, (vy1 - y1) * s, (vx2 - vx1) * s, (vy2 - vy1) * s);
  }
  function miniJump(e) {
    e.stopPropagation(); const m = mini._m; if (!m) return;
    const r = mini.getBoundingClientRect(), cv = root.querySelector('#gx-canvas').getBoundingClientRect();
    const go = (ev) => { const wx = (ev.clientX - r.left) / m.s + m.x1, wy = (ev.clientY - r.top) / m.s + m.y1; g.view.x = cv.width / 2 - wx * g.view.k; g.view.y = cv.height / 2 - wy * g.view.k; applyView(); };
    go(e); const up = () => { document.removeEventListener('pointermove', go); document.removeEventListener('pointerup', up); };
    document.addEventListener('pointermove', go); document.addEventListener('pointerup', up);
  }

  let built = false, poll;
  window.loadNetwork = async () => {
    if (!built) { build(); built = true; applyView(); }
    try { await load(); } catch (err) { toast(err.message, true); }
    clearInterval(poll); poll = setInterval(() => { if (root.offsetParent !== null && !g.wireDrag) load().catch(() => {}); else clearInterval(poll); }, 15000);
  };
})();
