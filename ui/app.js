/* Upscale Video AI – logique de l'interface (communique avec Python via pywebview) */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICONS = {
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  sparkles: '<path d="M12 3l1.8 4.9L19 9.7l-5.2 1.8L12 16.5l-1.8-5L5 9.7l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  wand: '<path d="M15 4V2M15 10V8M11 6h2M17 6h2M3 21l12-12"/><path d="M19 13l.7 1.8 1.8.7-1.8.7L19 18l-.7-1.8-1.8-.7 1.8-.7z"/>',
  brush: '<path d="M9.06 11.9l8.07-8.06a2.85 2.85 0 114.03 4.03l-8.06 8.08"/><path d="M7.07 14.94C5.4 14.94 4 16.3 4 18c0 1.3-1.3 2-2 2 1 1.6 2.6 2 4 2 2.2 0 4-1.8 4-4 0-1.7-1.3-3.06-2.93-3.06z"/>',
  star: '<path d="M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"/>',
};
const SIZE_MAIN = { x2: '×2', x3: '×3', x4: '×4', '720p': '720p', '1080p': '1080p', '1440p': '1440p', '2160p': '4K', '4320p': '8K' };
const SIZE_SUB = { x2: 'double', x3: 'triple', x4: 'quadruple', '720p': 'HD', '1080p': 'Full HD', '1440p': '2K', '2160p': 'Ultra HD', '4320p': 'extrême' };

let API = null;
const S = { settings: {}, models: [], files: [], thumbs: new Map(), running: false, task: '', demo: false };
const cardEls = new Map();

/* ------------------------------------------------------------------ démarrage */
async function getApi() {
  if (new URLSearchParams(location.search).has('demo')) { S.demo = true; return window.MockApi; }
  const t0 = Date.now();
  while (Date.now() - t0 < 20000) {
    const pw = window.pywebview;
    if (pw && pw.api && typeof pw.api.init === 'function') return pw.api;
    if (!pw && Date.now() - t0 > 1200) break;
    await sleep(50);
  }
  S.demo = true;
  return window.MockApi;
}

async function boot() {
  API = await getApi();
  const init = await API.init();
  S.models = init.models;
  S.settings = init.settings;
  $('#version').textContent = 'v' + init.version + (S.demo ? ' · démo' : '');
  buildModels(init.models);
  buildSizes(init.sizes);
  buildEncoders(init.encoders);
  if (init.gpus) setGpus(init.gpus);
  (init.files || []).forEach((f) => f.thumb && S.thumbs.set(f.id, f.thumb));
  applySettings();
  bindUi();
  syncFiles(init.files || []);
  window.__appReady = true;
  pollLoop();
  const demo = new URLSearchParams(location.search).get('demo');
  if (demo === 'preview') setTimeout(() => API.preview(2), 1500);
  if (demo === 'run') setTimeout(() => API.start(), 1500);
}

/* ------------------------------------------------------------------ réglages */
function buildModels(models) {
  const box = $('#models');
  box.innerHTML = models.map((m) => `
    <button class="model-card" data-k="${esc(m.key)}" title="${esc(m.description)}">
      <span class="m-ic"><svg viewBox="0 0 24 24">${ICONS[m.icon] || ICONS.film}</svg></span>
      <span>
        <span class="m-name">${esc(m.name)}${m.tag ? `<span class="m-tag">${esc(m.tag)}</span>` : ''}</span>
        <span class="m-sub">${esc(m.sub)}</span>
      </span>
      <span class="meters">
        <span class="meter" title="Vitesse"><svg viewBox="0 0 24 24"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg><i>${meter(m.speed)}</i></span>
        <span class="meter q" title="Qualité"><svg viewBox="0 0 24 24"><path d="M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"/></svg><i>${meter(m.quality)}</i></span>
      </span>
    </button>`).join('');
  $$('.model-card', box).forEach((el) => el.addEventListener('click', () => setSetting('model', el.dataset.k)));
}
const meter = (n) => [1, 2, 3, 4, 5].map((i) => `<b class="${i <= (n || 0) ? 'f' : ''}"></b>`).join('');

function buildSizes(sizes) {
  $('#sizes').innerHTML = sizes.map((s) =>
    `<button data-k="${esc(s.key)}" title="${esc(s.label)}">${esc(SIZE_MAIN[s.key] || s.key)}<small>${esc(SIZE_SUB[s.key] || '')}</small></button>`).join('');
  $$('#sizes button').forEach((b) => b.addEventListener('click', () => setSetting('output_size', b.dataset.k)));
}

function buildEncoders(encs) {
  $('#encoder').innerHTML = encs.map((e) => `<option value="${esc(e.key)}">${esc(e.label)}</option>`).join('');
}

function setGpus(gpus) {
  const chip = $('#gpuChip');
  chip.classList.remove('ok', 'bad');
  if (gpus && gpus.length) {
    chip.classList.add('ok');
    $('#gpuName').textContent = gpus.map((g) => g.name).join(' + ');
  } else {
    chip.classList.add('bad');
    $('#gpuName').textContent = 'Aucun GPU Vulkan détecté';
  }
  const sel = $('#gpu');
  sel.innerHTML = '<option value="auto">Automatique</option>' +
    (gpus || []).map((g) => `<option value="${esc(g.id)}">${esc(g.id)} · ${esc(g.name)}</option>`).join('');
  sel.value = String(S.settings.gpu ?? 'auto');
  if (!sel.value) sel.value = 'auto';
}

function qualityLabel(crf) {
  if (crf <= 14) return 'Quasi sans perte';
  if (crf <= 18) return 'Excellente';
  if (crf <= 22) return 'Très bonne';
  if (crf <= 26) return 'Bonne';
  return 'Fichier léger';
}

function applySettings() {
  const st = S.settings;
  $$('.model-card').forEach((el) => el.classList.toggle('on', el.dataset.k === st.model));
  $$('#sizes button').forEach((el) => el.classList.toggle('on', el.dataset.k === st.output_size));
  const v = 30 - Number(st.quality ?? 18);
  const q = $('#quality');
  q.value = Math.max(0, Math.min(20, v));
  updateRange();
  $('#encoder').value = st.encoder;
  segSet('#container', st.container);
  segSet('#speed', st.speed);
  segSet('#deinterlace', st.deinterlace);
  $('#tile').value = String(st.tile ?? 0);
  $('#tta').checked = !!st.tta;
  const gsel = $('#gpu');
  gsel.value = String(st.gpu ?? 'auto');
  if (!gsel.value) gsel.value = 'auto';
  pathSet('#outdir', '#outdirReset', st.output_dir, "À côté de la vidéo d'origine");
  pathSet('#tmpdir', '#tmpdirReset', st.temp_dir, 'Dossier temporaire de Windows');
  updateSizeHint();
}

function updateRange() {
  const q = $('#quality');
  const crf = 30 - Number(q.value);
  q.style.setProperty('--p', (Number(q.value) / 20) * 100 + '%');
  $('#qualityLabel').textContent = `${qualityLabel(crf)} · CRF ${crf}`;
}

function segSet(sel, v) { $$(sel + ' button').forEach((b) => b.classList.toggle('on', b.dataset.v === v)); }
function pathSet(spanSel, resetSel, value, placeholder) {
  $(spanSel).textContent = value || placeholder;
  $(spanSel).style.color = value ? '' : 'var(--muted)';
  $(resetSel).hidden = !value;
}

async function setSetting(key, value) {
  S.settings[key] = value;
  applySettings();
  try {
    S.settings = await API.set_settings({ [key]: value });
  } catch (e) { console.error(e); }
  applySettings();
}

function updateSizeHint() {
  const f = S.files.find((x) => x.res && x.out);
  $('#sizeHint').textContent = f ? `${f.res} → ${f.out}` : '';
}

/* ------------------------------------------------------------------ interactions */
function bindUi() {
  $('#addBtn').addEventListener('click', pickFiles);
  $('#dropzone').addEventListener('click', pickFiles);
  $('#clearBtn').addEventListener('click', async () => { await API.clear_files(); });
  $('#startBtn').addEventListener('click', () => API.start());
  $('#cancelBtn').addEventListener('click', () => API.cancel());
  $('#previewBtn').addEventListener('click', () => API.preview(null));
  $('#quality').addEventListener('input', updateRange);
  $('#quality').addEventListener('change', (e) => setSetting('quality', 30 - Number(e.target.value)));
  $('#encoder').addEventListener('change', (e) => setSetting('encoder', e.target.value));
  $('#gpu').addEventListener('change', (e) => setSetting('gpu', e.target.value));
  $('#tile').addEventListener('change', (e) => setSetting('tile', Number(e.target.value)));
  $('#tta').addEventListener('change', (e) => setSetting('tta', e.target.checked));
  for (const [sel, key] of [['#container', 'container'], ['#speed', 'speed'], ['#deinterlace', 'deinterlace']]) {
    $$(sel + ' button').forEach((b) => b.addEventListener('click', () => setSetting(key, b.dataset.v)));
  }
  $('#outdirField').addEventListener('click', async (e) => {
    if (e.target.closest('.mini-x')) return;
    const p = await API.pick_folder('output_dir');
    if (p) { S.settings.output_dir = p; applySettings(); }
  });
  $('#tmpdirField').addEventListener('click', async (e) => {
    if (e.target.closest('.mini-x')) return;
    const p = await API.pick_folder('temp_dir');
    if (p) { S.settings.temp_dir = p; applySettings(); }
  });
  $('#outdirReset').addEventListener('click', () => setSetting('output_dir', ''));
  $('#tmpdirReset').addEventListener('click', () => setSetting('temp_dir', ''));

  // journal
  $('#logBtn').addEventListener('click', () => { $('#logDrawer').classList.toggle('open'); $('#logDot').hidden = true; });
  $('#closeLog').addEventListener('click', () => $('#logDrawer').classList.remove('open'));
  $('#copyLog').addEventListener('click', () => {
    navigator.clipboard?.writeText($('#log').textContent);
    toast('info', 'Journal copié dans le presse-papiers');
  });

  // modales
  $('#pvClose').addEventListener('click', closePreview);
  $('#errClose').addEventListener('click', () => ($('#errorModal').hidden = true));
  $('#pvStart').addEventListener('click', () => { closePreview(); API.start(); });
  $$('#pvZoom button').forEach((b) => b.addEventListener('click', () => {
    $$('#pvZoom button').forEach((x) => x.classList.toggle('on', x === b));
    cmpZoom(b.dataset.z);
  }));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('#errorModal').hidden) $('#errorModal').hidden = true;
      else if (!$('#previewModal').hidden) closePreview();
      else $('#logDrawer').classList.remove('open');
    }
    if (e.ctrlKey && e.key.toLowerCase() === 'o') { e.preventDefault(); pickFiles(); }
  });

  // glisser-déposer (l'ajout réel est fait côté Python avec les chemins complets)
  let depth = 0;
  const overlay = $('#dropOverlay');
  document.addEventListener('dragenter', (e) => { e.preventDefault(); depth++; overlay.classList.add('show'); });
  document.addEventListener('dragover', (e) => { e.preventDefault(); });
  document.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) overlay.classList.remove('show'); });
  document.addEventListener('drop', (e) => {
    e.preventDefault(); depth = 0; overlay.classList.remove('show');
    if (S.demo && e.dataTransfer?.files?.length) API.add_paths([...e.dataTransfer.files].map((f) => f.name));
  });

  initCompare();
  window.addEventListener('resize', () => { if (!$('#previewModal').hidden) cmpZoom(CMP.mode); });
}

async function pickFiles() {
  try { await API.pick_files(); } catch (e) { console.error(e); }
}

/* ------------------------------------------------------------------ boucle d'état */
async function pollLoop() {
  for (;;) {
    try {
      const r = await API.poll();
      (r.events || []).forEach(handleEvent);
      if (r.logs && r.logs.length) appendLogs(r.logs);
      syncFiles(r.files || []);
      syncRun(r);
    } catch (e) { console.error(e); }
    await sleep(250);
  }
}

function handleEvent(ev) {
  switch (ev.type) {
    case 'toast': toast(ev.kind, ev.title, ev.text); break;
    case 'gpus': setGpus(ev.gpus); break;
    case 'thumb': if (ev.thumb) { S.thumbs.set(ev.id, ev.thumb); const el = cardEls.get(ev.id); if (el) setThumb(el, ev.thumb); } break;
    case 'queue_done': if (ev.ok && !ev.failed && !ev.cancelled) confetti(); break;
    case 'preview_started': openPreview(ev.name); break;
    case 'preview_done': showPreview(ev); break;
    case 'preview_failed':
      closePreview();
      showError('Aperçu impossible', ev.error);
      break;
  }
}

function appendLogs(lines) {
  const pre = $('#log');
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent += lines.join('\n') + '\n';
  if (atBottom) pre.scrollTop = pre.scrollHeight;
  if (!$('#logDrawer').classList.contains('open')) $('#logDot').hidden = false;
}

function fmtSize(b) {
  if (!b) return '';
  const u = ['o', 'Ko', 'Mo', 'Go', 'To'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(b < 10 && i > 1 ? 1 : 0) + ' ' + u[i];
}

/* ------------------------------------------------------------------ cartes vidéo */
function createCard(f) {
  const el = document.createElement('div');
  el.className = 'card';
  el.innerHTML = `
    <div class="card-progress"><div></div></div>
    <div class="thumb">
      <div class="shimmer"></div>
      <img alt="">
      <div class="res-badge"><span class="from"></span><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg><span class="to"></span></div>
      <div class="dur-badge"></div>
      <button class="card-x" title="Retirer">×</button>
      <div class="overlay">
        <div class="ring"><svg viewBox="0 0 74 74"><circle class="track" cx="37" cy="37" r="32"/><circle class="val" cx="37" cy="37" r="32"/></svg><span>0%</span></div>
        <div class="queued-ic">En file d'attente</div>
        <div class="done-ic"><svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg></div>
      </div>
    </div>
    <div class="card-body">
      <div class="card-name"></div>
      <div class="card-meta"><span class="pill"></span><span class="size"></span></div>
      <div class="card-actions">
        <button class="btn soft play"><svg class="ic" viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg>Lire</button>
        <button class="btn soft folder"><svg class="ic" viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>Dossier</button>
      </div>
    </div>`;
  const img = $('img', el);
  img.addEventListener('load', () => { img.classList.add('loaded'); $('.shimmer', el)?.remove(); });
  $('.card-x', el).addEventListener('click', async (e) => { e.stopPropagation(); await API.remove_file(f.id); });
  $('.play', el).addEventListener('click', () => API.play(f.id));
  $('.folder', el).addEventListener('click', () => API.open_output(f.id));
  $('.pill', el).addEventListener('click', () => {
    const cur = S.files.find((x) => x.id === f.id);
    if (cur && cur.error) showError(cur.name, cur.error);
  });
  el.addEventListener('dblclick', () => { if (!S.running) API.preview(f.id); });
  el.title = 'Double-clic : aperçu IA de cette vidéo';
  const t = S.thumbs.get(f.id);
  if (t) setThumb(el, t);
  return el;
}

function setThumb(el, src) {
  const img = $('img', el);
  if (img.getAttribute('src') !== src) img.src = src;
}

function updateCard(el, f) {
  el.className = `card ${f.state} state-${f.state}`;
  $('.card-name', el).textContent = f.name;
  $('.card-name', el).title = f.path;
  $('.pill', el).textContent = f.status;
  $('.size', el).textContent = fmtSize(f.size);
  $('.from', el).textContent = f.res || '…';
  $('.to', el).textContent = f.out || '…';
  $('.dur-badge', el).textContent = f.dur || '';
  $('.dur-badge', el).hidden = !f.dur;
  const pct = Math.round((f.progress || 0) * 100);
  $('.ring span', el).textContent = pct + '%';
  $('.ring .val', el).style.strokeDashoffset = 201 - 201 * (f.progress || 0);
  $('.card-progress div', el).style.width = (f.state === 'running' ? pct : 0) + '%';
}

function syncFiles(list) {
  S.files = list;
  const ids = new Set(list.map((f) => f.id));
  for (const [id, el] of cardEls) {
    if (!ids.has(id)) { el.remove(); cardEls.delete(id); S.thumbs.delete(id); }
  }
  const box = $('#cards');
  for (const f of list) {
    let el = cardEls.get(f.id);
    if (!el) { el = createCard(f); box.appendChild(el); cardEls.set(f.id, el); }
    updateCard(el, f);
  }
  const n = list.length;
  $('#dropzone').hidden = n > 0;
  $('#clearBtn').hidden = n === 0;
  const done = list.filter((f) => f.state === 'done').length;
  $('#fileCount').textContent = n === 0 ? 'Aucune vidéo pour le moment'
    : `${n} vidéo${n > 1 ? 's' : ''}${done ? ` · ${done} terminée${done > 1 ? 's' : ''}` : ''}`;
  updateSizeHint();
}

/* ------------------------------------------------------------------ progression */
function syncRun(r) {
  S.running = r.running;
  S.task = r.task;
  const start = $('#startBtn');
  start.disabled = r.running;
  $('span', start).textContent = r.running && r.task === 'queue' ? 'Traitement en cours…' : "Lancer l'upscale";
  $('#cancelBtn').hidden = !(r.running && r.task === 'queue');
  $('#previewBtn').disabled = r.running;
  $('#clearBtn').disabled = r.running;
  const bar = $('.bar');
  const p = r.progress;
  bar.classList.toggle('indeterminate', r.running && r.task === 'preview');
  bar.classList.toggle('active', r.running && r.task === 'queue');

  if (r.running && r.task === 'preview') {
    $('#taskTitle').textContent = 'Génération de l\'aperçu IA…';
    return;
  }
  if (p) {
    const frac = p.fraction || 0;
    setRing(frac);
    $('#barFill').style.width = (frac * 100).toFixed(1) + '%';
    $('#taskTitle').textContent = r.running
      ? `${p.count > 1 ? `[${p.index}/${p.count}] ` : ''}${p.name} · ${p.stage === 'Finalisation' ? 'finalisation…' : 'amélioration par l\'IA'}`
      : 'Traitement terminé';
    $('#stSpeed').textContent = p.speed ? `${p.speed} img/s` : '—';
    $('#stElapsed').textContent = p.elapsed || '—';
    $('#stEta').textContent = r.running ? (p.eta || '—') : '00:00:00';
    $('#stFrames').textContent = `${p.done} / ${p.total}`;
  } else if (!r.running) {
    setRing(0);
    $('#barFill').style.width = '0%';
    const ready = S.files.filter((f) => f.state === 'ready').length;
    $('#taskTitle').textContent = ready ? `${ready} vidéo${ready > 1 ? 's' : ''} prête${ready > 1 ? 's' : ''} à être agrandie${ready > 1 ? 's' : ''}` : 'Prêt à agrandir vos vidéos';
  }
}

function setRing(frac) {
  $('#ringVal').style.strokeDashoffset = 125.7 - 125.7 * frac;
  $('#pct').textContent = Math.round(frac * 100) + '%';
}

/* ------------------------------------------------------------------ notifications */
function toast(kind, title, text = '') {
  const icons = { success: '✓', error: '!', warn: '!', info: 'i' };
  const el = document.createElement('div');
  el.className = `toast ${kind || 'info'}`;
  el.innerHTML = `<span class="t-ic">${icons[kind] || 'i'}</span><div><b>${esc(title)}</b>${text ? `<p>${esc(text)}</p>` : ''}</div><button>×</button>`;
  const close = () => { el.classList.add('out'); setTimeout(() => el.remove(), 300); };
  $('button', el).addEventListener('click', close);
  $('#toasts').appendChild(el);
  setTimeout(close, kind === 'error' || kind === 'warn' ? 9000 : 5500);
}

function showError(title, text) {
  $('#errTitle').textContent = title || 'Erreur';
  $('#errText').textContent = text || '';
  $('#errorModal').hidden = false;
}

/* ------------------------------------------------------------------ comparateur */
const CMP = { x: 0.5, scale: 1, tx: 0, ty: 0, w: 1, h: 1, mode: 'fit', fit: 1 };

function openPreview(name) {
  $('#pvName').textContent = '· ' + (name || '');
  $('#pvChips').innerHTML = '';
  $('#pvLoading').hidden = false;
  $('#pvAfter').removeAttribute('src');
  $('#pvBefore').removeAttribute('src');
  $('#previewModal').hidden = false;
}

function closePreview() { $('#previewModal').hidden = true; }

function showPreview(ev) {
  if ($('#previewModal').hidden) openPreview(ev.name);
  const model = S.models.find((m) => m.key === ev.model);
  $('#pvModel').textContent = model ? model.name : '';
  $('#pvChips').innerHTML = `<span>${esc(ev.source)}</span><span class="hl">→ ${esc(ev.output)}</span><span>${esc(model ? model.name : ev.model)}</span>`;
  CMP.w = ev.width; CMP.h = ev.height; CMP.x = 0.5;
  let loaded = 0;
  const done = () => { if (++loaded === 2) { $('#pvLoading').hidden = true; cmpZoom('fit'); } };
  $('#pvAfter').onload = done; $('#pvBefore').onload = done;
  $('#pvAfter').src = ev.after;
  $('#pvBefore').src = ev.before;
  $$('#pvZoom button').forEach((x) => x.classList.toggle('on', x.dataset.z === 'fit'));
}

function cmpZoom(mode) {
  CMP.mode = mode;
  const r = $('#compare').getBoundingClientRect();
  CMP.fit = Math.min(r.width / CMP.w, r.height / CMP.h);
  const cx = r.width / 2, cy = r.height / 2;
  const ix = (cx - CMP.tx) / CMP.scale, iy = (cy - CMP.ty) / CMP.scale; // point image au centre
  if (mode === 'fit') {
    CMP.scale = CMP.fit;
    CMP.tx = (r.width - CMP.w * CMP.scale) / 2;
    CMP.ty = (r.height - CMP.h * CMP.scale) / 2;
  } else {
    CMP.scale = Number(mode) / (window.devicePixelRatio || 1);
    CMP.tx = cx - ix * CMP.scale;
    CMP.ty = cy - iy * CMP.scale;
  }
  cmpApply();
}

function cmpClamp() {
  const r = $('#compare').getBoundingClientRect();
  const W = CMP.w * CMP.scale, H = CMP.h * CMP.scale;
  CMP.tx = W <= r.width ? (r.width - W) / 2 : Math.min(0, Math.max(r.width - W, CMP.tx));
  CMP.ty = H <= r.height ? (r.height - H) / 2 : Math.min(0, Math.max(r.height - H, CMP.ty));
}

function cmpApply() {
  cmpClamp();
  for (const img of [$('#pvAfter'), $('#pvBefore')]) {
    img.style.width = CMP.w + 'px';
    img.style.height = CMP.h + 'px';
    img.style.transform = `translate(${CMP.tx}px, ${CMP.ty}px) scale(${CMP.scale})`;
  }
  const c = $('#compare');
  c.style.setProperty('--x', (CMP.x * 100).toFixed(3) + '%');
  c.classList.toggle('zoomed', CMP.scale > CMP.fit * 1.01);
}

function initCompare() {
  const c = $('#compare');
  let drag = null;
  c.addEventListener('pointerdown', (e) => {
    const r = c.getBoundingClientRect();
    const onHandle = e.target.closest('.handle');
    const zoomed = CMP.scale > CMP.fit * 1.01;
    drag = (onHandle || !zoomed) ? { type: 'slide' } : { type: 'pan', x: e.clientX, y: e.clientY, tx: CMP.tx, ty: CMP.ty };
    if (drag.type === 'slide') { CMP.x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); cmpApply(); }
    else c.classList.add('panning');
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const r = c.getBoundingClientRect();
    if (drag.type === 'slide') CMP.x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    else { CMP.tx = drag.tx + (e.clientX - drag.x); CMP.ty = drag.ty + (e.clientY - drag.y); }
    cmpApply();
  });
  const end = () => { drag = null; c.classList.remove('panning'); };
  c.addEventListener('pointerup', end);
  c.addEventListener('pointercancel', end);
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = c.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const max = 8 / (window.devicePixelRatio || 1);
    const ns = Math.min(max, Math.max(CMP.fit, CMP.scale * (e.deltaY < 0 ? 1.18 : 1 / 1.18)));
    CMP.tx = mx - (mx - CMP.tx) * ns / CMP.scale;
    CMP.ty = my - (my - CMP.ty) * ns / CMP.scale;
    CMP.scale = ns;
    CMP.mode = 'custom';
    $$('#pvZoom button').forEach((x) => x.classList.remove('on'));
    cmpApply();
  }, { passive: false });
}

/* ------------------------------------------------------------------ confettis */
function confetti() {
  const cv = $('#confetti');
  const ctx = cv.getContext('2d');
  cv.width = innerWidth; cv.height = innerHeight;
  const colors = ['#8b5cf6', '#22d3ee', '#f472b6', '#34d399', '#fbbf24', '#ffffff'];
  const parts = Array.from({ length: 160 }, () => ({
    x: innerWidth / 2 + (Math.random() - 0.5) * 300, y: innerHeight - 60,
    vx: (Math.random() - 0.5) * 16, vy: -Math.random() * 18 - 8,
    s: Math.random() * 7 + 4, r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.3,
    c: colors[(Math.random() * colors.length) | 0],
  }));
  const t0 = performance.now();
  (function frame(t) {
    ctx.clearRect(0, 0, cv.width, cv.height);
    for (const p of parts) {
      p.vy += 0.45; p.vx *= 0.99; p.x += p.vx; p.y += p.vy; p.r += p.vr;
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r);
      ctx.fillStyle = p.c; ctx.globalAlpha = Math.max(0, 1 - (t - t0) / 3200);
      ctx.fillRect(-p.s / 2, -p.s / 4, p.s, p.s / 2); ctx.restore();
    }
    if (t - t0 < 3200) requestAnimationFrame(frame); else ctx.clearRect(0, 0, cv.width, cv.height);
  })(t0);
}

boot();
