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
    const accMap = new Map();
    d.accounts.forEach((a) => {
      accMap.set(String(a.id), a);
      if (a.persona_id) out.push({ from: 'per:' + a.persona_id, to: 'acc:' + a.id });
    });
    d.groups.forEach((x) => {
      const aids = (x.account_ids && x.account_ids.length) ? x.account_ids : (x.account_id ? [x.account_id] : []);
      aids.forEach((aid) => {
        const acc = accMap.get(String(aid));
        // Accounts without a persona are NOT connected to groups in the network!
        if (acc && acc.persona_id) {
          out.push({ from: 'acc:' + aid, to: 'grp:' + x.chat_id });
        }
      });
    });
    return out;
  }
  const label = (n) => n.kind === 'per' ? n.o.name : n.kind === 'acc' ? (n.o.name || n.o.phone || 'Account') : (n.o.title || n.o.chat_id);
  const personaColor = (pid) => { const p = g.data.personas.find((x) => x.id === pid); return p ? p.color : null; };

  function nodeHTML(n) {
    const o = n.o;
    if (n.kind === 'per') {
      const used = g.data.accounts.filter((a) => a.persona_id === o.id).length;
      let dt = {};
      try { dt = JSON.parse(o.details || '{}'); } catch(e) {}
      const meta = [dt.gender, dt.location, dt.age ? `${dt.age}y` : ''].filter(Boolean).join(' • ');
      const perInit = E((o.name || '?')[0].toUpperCase());
      const perAvMarkup = `<span class="gx-av per" style="background:${E(o.color)}; border-radius:50%; overflow:hidden; position:relative;">
        <img src="/personas/${o.id}/avatar?t=${Date.now()}" alt="${E(o.name || '')}" style="position:absolute; inset:0; width:100%; height:100%; object-fit:cover; border-radius:50%; display:block; z-index:2;" onerror="this.remove();" />
        <span style="width:100%; height:100%; display:flex; align-items:center; justify-content:center; font-weight:700;">${perInit}</span>
      </span>`;
      return `<div class="gx-head">${perAvMarkup}
        <div class="gx-ht"><b>${E(o.name)}</b><small>${E(meta || 'Persona')}</small></div></div>
        <p class="gx-body">${E(o.bio || o.prompt || 'No instructions yet - click to write how this persona talks.')}</p>
        <div class="gx-foot"><span class="gx-chip">${used} account${used === 1 ? '' : 's'}</span>${dt.is_expert ? '<span class="gx-chip" style="background:rgba(47,196,178,.15); color:var(--accent); font-weight:600;">Expert</span>' : ''}</div>
        <i class="gx-port out" data-port="out"></i>`;
    }
    if (n.kind === 'acc') {
      const c = personaColor(o.persona_id);
      const ng = o.persona_id ? g.data.groups.filter((x) => {
        const aids = (x.account_ids && x.account_ids.length) ? x.account_ids : (x.account_id ? [x.account_id] : []);
        return aids.map(String).includes(String(o.id));
      }).length : 0;
      const initial = E((o.name || o.phone || '#')[0].toUpperCase());
      const avMarkup = `<div style="position:relative; width:100%; height:100%; border-radius:50%; overflow:hidden;"><img src="/accounts/${o.id}/avatar?t=${Date.now()}" alt="${E(o.name || '')}" style="position:absolute; inset:0; width:100%; height:100%; object-fit:cover; border-radius:50%; display:block; z-index:2;" onerror="this.onerror=null; this.remove();" /><span style="width:100%; height:100%; display:flex; align-items:center; justify-content:center; font-weight:700;">${initial}</span></div>`;
      return `<i class="gx-port in" data-port="in"></i>
        <div class="gx-head"><span class="gx-av acc" ${c ? `style="box-shadow:0 0 0 2px ${E(c)}"` : ''}>${avMarkup}</span>
        <div class="gx-ht"><b>${E(o.name || o.phone)}</b><small>${o.username ? '@' + E(o.username) : E(o.phone || '')}</small></div>
        <span class="gx-dot ${o.connected ? 'ok' : o.active ? 'warn' : ''}" title="${o.connected ? 'Connected' : o.active ? 'Not connected' : 'Paused'}"></span></div>
        <div class="gx-foot">
          ${o.persona_id ? `<span class="gx-chip">${ng} group${ng === 1 ? '' : 's'}</span>` : `<span class="gx-chip" style="background:rgba(245,184,74,.15); color:var(--warn); border-color:rgba(245,184,74,.3);">No Persona</span>`}
          ${o.proxy_label ? `<span class="gx-chip">&#8644; ${E(o.proxy_label)}</span>` : '<span class="gx-chip dim">no proxy</span>'}
        </div>
        <i class="gx-port out" data-port="out"></i>`;
    }
    const aids = (o.account_ids && o.account_ids.length) ? o.account_ids : (o.account_id ? [o.account_id] : []);
    const memberAccs = g.data.accounts.filter((a) => a.persona_id && aids.map(String).includes(String(a.id)));
    const accCount = memberAccs.length;
    const c = accCount && personaColor(memberAccs[0].persona_id);
    const accLabel = accCount > 0 ? (accCount === 1 ? (memberAccs[0].name || memberAccs[0].phone || '1 account') : `${accCount} accounts active`) : 'No account';
    const accStack = memberAccs.slice(0, 4).map((a) => {
      const pc = personaColor(a.persona_id) || '#5856d6';
      const init = E((a.name || a.phone || 'A')[0].toUpperCase());
      return `<span class="gx-mini-av" style="background:${pc}" title="${E(a.name || a.phone)}">${init}</span>`;
    }).join('');
    return `<i class="gx-port in" data-port="in"></i>
      <div class="gx-head">
        <span class="gx-av grp" ${c ? `style="background:${E(c)}22;color:${E(c)}"` : ''}>#</span>
        <div class="gx-ht">
          <b>${E(o.title || 'Untitled Group')}</b>
          <small>${o.messages} msgs &bull; ${E(accLabel)}</small>
        </div>
        ${accCount > 1 ? `<div class="gx-acc-stack">${accStack}</div>` : ''}
      </div>
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
      if (from.startsWith('per:') && to.startsWith('grp:')) {
        return toast('Connect the Persona to an Account first, then connect the Account to the Group.', true);
      }
      if (from.startsWith('acc:') && to.startsWith('grp:')) {
        const aid = from.replace('acc:', '');
        const acc = g.data.accounts.find((a) => String(a.id) === String(aid));
        if (!acc || !acc.persona_id) {
          return toast('Connect a Persona to this Account first before adding it to a group!', true);
        }
      }
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
      const boundAccounts = (g.data.accounts || []).filter((a) => String(a.persona_id) === String(o.id));
      const boundAccNames = boundAccounts.map((a) => E(a.name || a.phone || 'Account')).join(', ') || 'None';

      let d = {};
      try {
        d = typeof o.details === 'string' ? JSON.parse(o.details) : (o.details || {});
      } catch (err) {}
      const ling = d.linguistic || {};

      const traitsBadge = [
        d.culture ? d.culture : null,
        d.age ? `${d.age}yo` : null,
        d.gender ? d.gender : null,
        d.occupation ? d.occupation : null,
      ].filter(Boolean).join(' • ');

      insp.innerHTML = head(o.name, 'Persona') + `
        <div style="margin-bottom:8px;">
          <button class="gx-btn primary wide" id="pi-open-studio" style="display:flex;align-items:center;justify-content:center;gap:6px;width:100%;height:28px;font-size:11.5px;font-weight:600;">
            Edit in Persona Studio &rarr;
          </button>
        </div>

        <div style="display:flex;gap:8px;align-items:flex-end;margin-bottom:6px;">
          <div style="flex:1;">
            <label class="gx-l" style="margin:2px 0 2px;">Name</label>
            <input id="pi-name" value="${E(o.name)}" style="height:26px;padding:2px 6px;font-size:11.5px;" />
          </div>
          <div>
            <label class="gx-l" style="margin:2px 0 2px;">Colour</label>
            <div class="gx-sw">${COLORS.map((c) => `<button data-c="${c}" style="background:${c}" class="${c === o.color ? 'on' : ''}"></button>`).join('')}</div>
          </div>
        </div>

        <div class="gx-kv" style="font-size:11px;background:#0d1017;padding:6px 8px;border-radius:6px;border:1px solid var(--line);margin:6px 0;">
          <span>Profile</span><b>${traitsBadge ? E(traitsBadge) : 'Custom Persona'}</b>
          <span>Bound Accounts</span><b>${boundAccNames}</b>
          <span>Slang / Casing</span><b>${E(ling.slang_tier || 'casual')} / ${E(ling.casing || 'lowercase')}</b>
        </div>

        <div style="background:#080a10; border:1px solid var(--line2); border-radius:6px; padding:7px 8px; margin:8px 0 6px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
            <b style="font-size:9.5px; text-transform:uppercase; letter-spacing:0.05em; color:var(--accent);">Speech Directive (Decides How It Speaks)</b>
            <span class="badge ok" style="font-size:8.5px;">Active Voice</span>
          </div>
          <div style="font-size:9.5px; color:var(--muted); line-height:1.25; margin-bottom:5px;">
            This system prompt is sent to fal.ai / Telegram daemon. It dictates tone, vocabulary, casing, and replies.
          </div>
          <textarea id="pi-prompt" rows="7" style="font-family:monospace; font-size:10px; line-height:1.35; padding:5px 7px; background:#040608; border:1px solid var(--line); border-radius:4px; color:#c9d1d9; resize:vertical; width:100%; box-sizing:border-box;">${E(o.prompt)}</textarea>
        </div>

        <div class="gx-row" style="margin-top:6px;">
          <button class="gx-btn" id="pi-save" style="height:26px;font-size:11.5px;">Save Directive</button>
          <button class="gx-btn danger ghost" id="pi-del" style="height:26px;font-size:11.5px;">Delete</button>
        </div>

        <div class="gx-sep" style="margin:12px 0 6px;"></div>
        <label class="gx-l" style="margin:6px 0 3px;">Preview a reply</label>
        <select id="pi-grp" style="height:26px;font-size:11px;padding:2px 6px;"><option value="">Sample chat</option>${groupsOpts}</select>
        <button class="gx-btn ghost wide" id="pi-prev" style="height:26px;font-size:11px;margin-top:4px;">&#9654; Generate preview</button>
        <div id="pi-out"></div>`;
      let color = o.color;
      insp.querySelectorAll('.gx-sw button').forEach((b) => b.onclick = () => { color = b.dataset.c; insp.querySelectorAll('.gx-sw button').forEach((x) => x.classList.toggle('on', x === b)); });

      const studioBtn = insp.querySelector('#pi-open-studio');
      if (studioBtn) {
        studioBtn.onclick = () => {
          if (window.openPersonaStudioFromNetwork) {
            window.openPersonaStudioFromNetwork(o.id);
          }
        };
      }

      insp.querySelector('#pi-save').onclick = async () => {
        try {
          const newPrompt = insp.querySelector('#pi-prompt').value;
          const newName = insp.querySelector('#pi-name').value;
          await call('POST', '/personas', { id: o.id, name: newName, prompt: newPrompt, color });
          document.activeElement.blur();
          await load();
          if (window.syncPersonaLibraryPrompt) {
            window.syncPersonaLibraryPrompt(o.id, newName, newPrompt, color);
          }
          toast('Saved and synced to Network & Studio');
        } catch (err) { toast(err.message, true); }
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
        <div class="gx-list">${gs.map((x) => `<div>#${E(x.title)}</div>`).join('') || '<p class="gx-mut">Drag from this account\'s right dot to a group.</p>'}</div>
        <button class="gx-btn danger wide" id="ai-del-acc" style="background:#ff4d4f; color:#fff; font-weight:600; margin:14px 0 0 0; border:none; border-radius:6px; padding:6px; cursor:pointer;">Delete Account</button>`;
      insp.querySelector('#ai-per').onchange = async (e) => {
        const v = e.target.value;
        try {
          if (v) await call('POST', '/graph/link', { from: 'per:' + v, to: id, on: true });
          else if (o.persona_id) await call('POST', '/graph/link', { from: 'per:' + o.persona_id, to: id, on: false });
          await load();
        } catch (err) { toast(err.message, true); }
      };
      const delAccBtn = insp.querySelector('#ai-del-acc');
      if (delAccBtn) {
        delAccBtn.onclick = async () => {
          if (!confirm(`Permanently delete account "${o.name || o.phone}"?\n\nThis will log out the session, remove it from all groups, and wipe all its data from the database.`)) return;
          try {
            await call('DELETE', '/accounts/' + o.id);
            insp.innerHTML = '<p class="gx-mut" style="padding:16px;">Account deleted.</p>';
            await load();
          } catch (err) { toast(err.message, true); }
        };
      }
    } else {
      const aids_insp = (o.account_ids && o.account_ids.length) ? o.account_ids : (o.account_id ? [o.account_id] : []);
      const memberAccs_insp = g.data.accounts.filter((a) => a.persona_id && aids_insp.map(String).includes(String(a.id)));
      insp.innerHTML = head(o.title, 'Group') + `
        <div class="gx-kv">
          <span>Messages saved</span><b>${o.messages}</b>
          <span>Accounts in group</span><b>${memberAccs_insp.length}</b>
        </div>
        <button class="gx-btn wide" id="gi-test-chat" style="background:linear-gradient(135deg, #2fc4b2, #5856d6); color:#fff; font-weight:600; margin:10px 0; border:none; border-radius:6px; padding:8px; cursor:pointer;">
          Test Persona Chat in Group
        </button>
        <label class="gx-l">Active Accounts in this Group</label>
        <div class="gx-list" id="gi-acc-list">
          ${memberAccs_insp.map((a) => {
            const p = g.data.personas.find((x) => x.id === a.persona_id);
            return `<div style="display:flex; justify-content:space-between; align-items:center; padding:6px 8px; border-radius:6px; background:var(--panel2); margin-bottom:4px;">
              <div>
                <b>${E(a.name || a.phone)}</b>
                <small style="display:block; color:var(--muted); font-size:11px;">${p ? 'Persona: ' + E(p.name) : '<span style="color:var(--warn);">No persona</span>'}</small>
              </div>
              <button class="gx-btn danger ghost xs gi-rm-acc" data-aid="${a.id}" style="padding:2px 8px; font-size:11px;">Remove</button>
            </div>`;
          }).join('') || '<p class="gx-mut">No accounts linked yet. Drag an account wire to this group.</p>'}
        </div>
        <label class="gx-l">Personas for this group</label>
        <p class="gx-mut">Studies up to 800 real messages from this group, then designs members who fit it and talk like it.</p>
        <div class="gx-row"><input id="gi-n" type="number" min="1" max="8" value="3" style="width:64px" /><input id="gi-dir" placeholder="Optional direction" /></div>
        <button class="gx-btn wide" id="gi-gen">Study group &amp; design personas</button>
        <div id="gi-prof"></div><div id="gi-pers" class="gx-list"></div>
        <div class="gx-sep"></div>
        <label class="gx-l">Group-specific instructions <small>(overrides persona)</small></label>
        <textarea id="gi-p" rows="4" placeholder="Leave empty to use the account's persona">${E(o.persona || '')}</textarea>
        <button class="gx-btn" id="gi-save">Save</button>
        <div class="gx-sep"></div>
        <label class="gx-l">Recent messages</label><div id="gi-feed" class="gx-chat"><div class="gx-typing"><i></i><i></i><i></i></div></div>
        <button class="gx-btn danger wide" id="gi-del-group" style="background:#ff4d4f; color:#fff; font-weight:600; margin:14px 0 0 0; border:none; border-radius:6px; padding:6px; cursor:pointer;">Delete Group</button>`;
      const testChatBtn = insp.querySelector('#gi-test-chat');
      if (testChatBtn) {
        testChatBtn.onclick = () => {
          if (window.openGroupTestChatModal) {
            window.openGroupTestChatModal(o.chat_id, o.title);
          } else {
            toast('Test chat modal opening...');
          }
        };
      }
      insp.querySelectorAll('.gi-rm-acc').forEach((btn) => {
        btn.onclick = async () => {
          const aid = btn.dataset.aid;
          try {
            await call('POST', '/graph/link', { from: 'acc:' + aid, to: id, on: false });
            await load();
            toast('Account removed from group');
          } catch (err) { toast(err.message, true); }
        };
      });
      insp.querySelector('#gi-save').onclick = async () => { try { await call('POST', '/groups/' + o.chat_id, { persona: insp.querySelector('#gi-p').value }); o.persona = insp.querySelector('#gi-p').value; toast('Saved'); } catch (err) { toast(err.message, true); } };
      const delGrpBtn = insp.querySelector('#gi-del-group');
      if (delGrpBtn) {
        delGrpBtn.onclick = async () => {
          if (!confirm(`Permanently delete group "${o.title}" (ID: ${o.chat_id})?\n\nThis will completely delete the group, its stored messages, summaries, scheduled batch dialogues, and persona bindings from the system.`)) return;
          try {
            await call('DELETE', '/groups/' + o.chat_id);
            insp.innerHTML = '<p class="gx-mut" style="padding:16px;">Group deleted.</p>';
            await load();
          } catch (err) { toast(err.message, true); }
        };
      }
      const accs = g.data.accounts;
      const loadGP = async () => {
        const r = await call('GET', `/group-personas/${o.chat_id}`); const box = insp.querySelector('#gi-pers'); if (!box) return;
        const pf = r.profile || {};
        insp.querySelector('#gi-prof').innerHTML = pf.topic ? `<div class="gx-kv"><span>About</span><b>${E(pf.topic)}</b><span>Style</span><b>${E((pf.writing_style || {}).typical_length || '-')}</b></div>` : '';
        box.innerHTML = r.personas.map((p) => `<div class="gx-gp" data-id="${p.id}"><b>${E(p.name)}</b> <small>${E((JSON.parse(p.details || '{}').role_in_group) || p.bio || '')}</small>
          <div class="gx-row"><select data-p="${p.id}"><option value="">No account</option>${accs.map((a) => `<option value="${a.id}" ${String(a.id) === String(p.account_id) ? 'selected' : ''}>${E(a.name || a.phone)}</option>`).join('')}</select>
          <button class="gx-btn ghost" data-v="${p.id}">View</button><button class="gx-btn danger ghost" data-d="${p.id}">&times;</button></div>
          <pre class="gx-pre hidden" id="gp-${p.id}">${E(p.prompt)}</pre></div>`).join('') || '<p class="gx-mut">No personas designed for this group yet.</p>';
        box.querySelectorAll('select[data-p]').forEach((el) => el.onchange = async () => { await call('POST', `/group-personas/${o.chat_id}/assign`, { persona_id: el.dataset.p, account_id: el.value || null }); toast('Assigned'); loadGP(); });
        box.querySelectorAll('[data-v]').forEach((el) => el.onclick = () => insp.querySelector('#gp-' + el.dataset.v).classList.toggle('hidden'));
        box.querySelectorAll('[data-d]').forEach((el) => el.onclick = async () => { if (!confirm('Delete this persona?')) return; await call('DELETE', `/group-personas/${o.chat_id}/${el.dataset.d}`); loadGP(); });
      };
      loadGP().catch(() => {});
      insp.querySelector('#gi-gen').onclick = async (e) => {
        e.target.disabled = true; e.target.textContent = 'Reading the group and designing personas...';
        try { const r = await call('POST', `/group-personas/${o.chat_id}/generate`, { count: +insp.querySelector('#gi-n').value || 3, direction: insp.querySelector('#gi-dir').value }); toast(`Designed ${r.ids.length} personas from ${r.studied} messages`); await loadGP(); }
        catch (err) { toast(err.message, true); }
        e.target.disabled = false; e.target.textContent = 'Study group & design personas';
      };
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
