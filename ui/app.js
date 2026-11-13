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
const SIZE_MAIN = { x2: '×2', x3: '×3', x4: '×4', '720p': '720p', '1080p': '1080p', '1440p': '1440p', '2160p': '4K', '4320p': '8K', x1: "Taille d'origine" };
const SIZE_SUB = { x2: 'double', x3: 'triple', x4: 'quadruple', '720p': 'HD', '1080p': 'Full HD', '1440p': '2K', '2160p': 'Ultra HD', '4320p': 'extrême', x1: 'sans agrandissement · fluidité seule' };

let API = null;
const S = { settings: {}, models: [], files: [], thumbs: new Map(), running: false, task: '', demo: false, pending: 0, pausing: false };
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
  S.version = init.version;
  $('#version').textContent = 'v' + init.version + (S.demo ? ' · démo' : '');
  buildModels(init.models);
  buildSizes(init.sizes);
  buildEncoders(init.encoders);
  buildFinishes(init.finishes || []);
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
  segSet('#interpolation', st.interpolation || 'off');
  segSet('#image_format', st.image_format || 'auto');
  $('#tile').value = String(st.tile ?? 0);
  $('#tta').checked = !!st.tta;
  $('#autoUpdate').checked = st.auto_update !== false;
  $('#cleanAuto').checked = st.clean !== 'off';
  const gsel = $('#gpu');
  gsel.value = String(st.gpu ?? 'auto');
  if (!gsel.value) gsel.value = 'auto';
  pathSet('#outdir', '#outdirReset', st.output_dir, "À côté de la vidéo d'origine");
  pathSet('#tmpdir', '#tmpdirReset', st.temp_dir, 'Dossier temporaire de Windows');
  applyFinish();
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

/* ------------------------------------------------------------------ finitions (optionnelles) */
const FIN_KEYS = ['finish_sharp', 'finish_color', 'finish_contrast', 'finish_glow', 'finish_warmth'];
function buildFinishes(list) {
  S.finishes = list;
  const box = $('#finishPresets');
  box.innerHTML = list.map((p) => `<button data-v="${esc(p.key)}">${esc(p.label)}</button>`).join('')
    + '<button data-v="custom" class="custom" title="Réglé à la main avec les curseurs">Personnalisé</button>';
  $$('button', box).forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.v === 'custom') return;  // « Personnalisé » s'active en bougeant un curseur
    setSettings({ finish: b.dataset.v });
  }));
  $('#finishOn').addEventListener('change', (e) => {
    if (e.target.checked) {
      const hasCustom = FIN_KEYS.some((k) => Number(S.settings[k] || 0) !== 0);
      setSettings({ finish: S.lastFinish || (hasCustom ? 'custom' : (list[0] ? list[0].key : 'custom')) });
    } else {
      if (S.settings.finish && S.settings.finish !== 'off') S.lastFinish = S.settings.finish;
      setSettings({ finish: 'off' });
    }
  });
  $$('.fin-slider').forEach((el) => {
    const input = $('input', el);
    input.addEventListener('input', () => paintFinSlider(el, Number(input.value)));
    input.addEventListener('change', () => {
      const values = { finish: 'custom' };
      FIN_KEYS.forEach((k) => { values[k] = Number(S.settings[k] || 0); });
      values[el.dataset.k] = Number(input.value);
      setSettings(values);
    });
  });
}
function paintFinSlider(el, v) {
  const input = $('input', el);
  const warm = el.classList.contains('warmth');
  if (!warm) input.style.setProperty('--p', ((v - Number(input.min)) / (Number(input.max) - Number(input.min))) * 100 + '%');
  $('.fin-head b', el).textContent = warm ? (v > 0 ? `+${v} chaud` : v < 0 ? `${v} froid` : 'neutre') : String(v);
}
function finishLabel(key) {
  if (!key || key === 'off') return 'Désactivées';
  if (key === 'custom') return 'Personnalisé';
  const p = (S.finishes || []).find((x) => x.key === key);
  return p ? p.label : key;
}
function applyFinish() {
  if (!$('#finishOn')) return;
  const st = S.settings;
  const on = !!st.finish && st.finish !== 'off';
  $('#finishOn').checked = on;
  $('#finishBody').hidden = !on;
  $('#finishSection').classList.toggle('on', on);
  $('#finishHint').textContent = finishLabel(st.finish);
  segSet('#finishPresets', st.finish);
  $$('.fin-slider').forEach((el) => {
    const v = Number(st[el.dataset.k] || 0);
    $('input', el).value = v;
    paintFinSlider(el, v);
  });
}

async function setSettings(values) {
  Object.assign(S.settings, values);
  applySettings();
  try {
    S.settings = await API.set_settings(values);
  } catch (e) { console.error(e); }
  applySettings();
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
  $('#sizeHint').textContent = f ? `${f.res} → ${f.out.split(' · ')[0]}` : '';
  // fréquences réelles de TOUTES les vidéos de la liste (chacune garde la sienne)
  const fpsList = [...new Set(S.files.filter((x) => x.kind !== 'image' && x.fps)
    .map((x) => Math.round(x.fps * 100) / 100))].sort((a, b) => a - b);
  const lo = fpsList[0], hi = fpsList[fpsList.length - 1];
  const k = { x2: 2, x3: 3, x4: 4 }[S.settings.interpolation] || 1;
  const span = (m, round) => {
    const f = (x) => (round ? String(Math.round(x * m)) : fmtFps(x * m));
    return lo === hi ? f(lo) : `${f(lo)} à ${f(hi)}`;
  };
  const custom = S.settings.interpolation === 'custom';
  const target = Number(S.settings.interp_fps) || 60;
  $('#fpsHint').textContent = !fpsList.length ? ''
    : custom ? (hi < target ? `${span(1)} → ${fmtFps(target)} i/s` : lo >= target ? `${span(1)} i/s` : `${span(1)} → ${fmtFps(target)} i/s max`)
    : (k > 1 ? `${span(1)} → ${span(k)} i/s` : `${span(1)} i/s`) + (lo !== hi ? ' (selon la vidéo)' : '');
  // fluidité personnalisée
  $('#fpsCustom').hidden = !custom;
  $('small', $('#interpolation button[data-v="custom"]')).textContent = custom ? `${fmtFps(target)} i/s` : 'personnalisé';
  const inp = $('#interpFps');
  if (document.activeElement !== inp) { inp.value = fmtFps(target); inp.classList.remove('bad'); }
  $$('#fpsChips button').forEach((b) => b.classList.toggle('on', Number(b.dataset.f) === target));
  const note = $('#fpsNote');
  const slow = fpsList.filter((x) => x >= target);
  note.classList.toggle('warn', custom && slow.length > 0);
  note.textContent = !custom ? ''
    : slow.length && slow.length === fpsList.length ? `Vos vidéos sont déjà à ${fmtFps(target)} i/s ou plus : leur fréquence d'origine sera conservée.`
    : slow.length ? `Les vidéos déjà à ${fmtFps(target)} i/s ou plus gardent leur fréquence d'origine.`
    : 'Chaque vidéo sort exactement à cette fréquence, sans saccade, avec la même durée.';
  const words = { x2: 'double', x3: 'triple', x4: 'quadruple' };
  $$('#interpolation button').forEach((b) => {
    const m = { x2: 2, x3: 3, x4: 4 }[b.dataset.v];
    if (m) $('small', b).textContent = !fpsList.length ? words[b.dataset.v] : (lo === hi ? `${fmtFps(lo * m)} i/s` : `${span(m, true)}`);
  });
}
const fmtFps = (x) => String(Math.round(x * 100) / 100).replace('.', ',');

/* fréquence personnalisée saisie : nombre entier uniquement (60, 120…) */
function commitCustomFps() {
  const inp = $('#interpFps');
  const txt = String(inp.value).trim();
  const v = Number(txt);
  if (!/^\d+$/.test(txt) || v < 1 || v > 240) {
    inp.classList.add('bad');
    toast('info', 'Fréquence invalide', 'Entrez un nombre entier d\'images par seconde entre 1 et 240 (ex. 60 ou 120).');
    return;
  }
  inp.classList.remove('bad');
  setSetting('interp_fps', v);
}

/* ------------------------------------------------------------------ interactions */
function bindUi() {
  $('#addBtn').addEventListener('click', pickFiles);
  $('#dropzone').addEventListener('click', pickFiles);
  $('#clearBtn').addEventListener('click', async () => { await API.clear_files(); });
  $('#startBtn').addEventListener('click', () => API.start());
  $('#pauseBtn').addEventListener('click', () => API.pause());
  $('#cancelBtn').addEventListener('click', () => {
    if (!S.running && S.pending > 0
      && !confirm('Abandonner les traitements en pause ?\nL\'avancement enregistré sera supprimé.')) return;
    API.cancel();
  });
  $('#previewBtn').addEventListener('click', () => {
    const f = S.files.find((x) => x.state !== 'probing' && x.state !== 'error' && x.res) || S.files.find((x) => x.res);
    if (!f) { toast('info', 'Aucun fichier', 'Ajoutez une vidéo ou une image pour générer un aperçu.'); return; }
    openPicker(f.id);
  });
  $('#quality').addEventListener('input', updateRange);
  $('#quality').addEventListener('change', (e) => setSetting('quality', 30 - Number(e.target.value)));
  $('#encoder').addEventListener('change', (e) => setSetting('encoder', e.target.value));
  $('#gpu').addEventListener('change', (e) => setSetting('gpu', e.target.value));
  $('#tile').addEventListener('change', (e) => setSetting('tile', Number(e.target.value)));
  $('#tta').addEventListener('change', (e) => setSetting('tta', e.target.checked));
  $('#autoUpdate').addEventListener('change', (e) => setSetting('auto_update', e.target.checked));
  $('#cleanAuto').addEventListener('change', (e) => setSetting('clean', e.target.checked ? 'auto' : 'off'));
  $('#version').addEventListener('click', () => API.check_update(true));
  for (const [sel, key] of [['#container', 'container'], ['#speed', 'speed'], ['#deinterlace', 'deinterlace'], ['#interpolation', 'interpolation'], ['#image_format', 'image_format']]) {
    $$(sel + ' button').forEach((b) => b.addEventListener('click', () => setSetting(key, b.dataset.v)));
  }
  $('#interpolation button[data-v="custom"]').addEventListener('click', () => setTimeout(() => { const i = $('#interpFps'); i.focus(); i.select(); }, 30));
  $('#interpFps').addEventListener('change', commitCustomFps);
  $('#interpFps').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
    else if (e.key === 'Escape') { e.target.value = fmtFps(Number(S.settings.interp_fps) || 60); e.target.blur(); }
  });
  $('#interpFps').addEventListener('input', (e) => {
    // chiffres uniquement : virgule, point et lettres sont ignorés à la frappe
    const clean = e.target.value.replace(/\D/g, '').slice(0, 3);
    if (clean !== e.target.value) e.target.value = clean;
    e.target.classList.remove('bad');
  });
  $$('#fpsChips button').forEach((b) => b.addEventListener('click', () => setSetting('interp_fps', Number(b.dataset.f))));
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
  $('#notesClose').addEventListener('click', () => ($('#notesModal').hidden = true));
  $('#pvStart').addEventListener('click', () => { closePreview(); API.start(); });
  $('#pvBack').addEventListener('click', () => setPvMode('pick'));
  $('#pkGo').addEventListener('click', generatePreview);
  $$('#pvZoom button').forEach((b) => b.addEventListener('click', () => {
    $$('#pvZoom button').forEach((x) => x.classList.toggle('on', x === b));
    cmpZoom(b.dataset.z);
  }));
  $('#pkRange').addEventListener('input', (e) => pickFrame(Number(e.target.value), true));
  $('#pkRange').addEventListener('change', (e) => pickFrame(Number(e.target.value), false));
  $$('.tl-btn').forEach((b) => b.addEventListener('click', () => stepFrame(b.dataset.step)));
  $('#pkGoto').addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') {
      const t = parseClock(e.target.value);
      if (t == null) { toast('warn', 'Temps invalide', 'Exemples : 83, 1:23, 00:01:23.5'); return; }
      pickFrame(Math.round(t * PK.fps), false);
      e.target.blur();
    }
  });
  // mises à jour
  $('#ubClose').addEventListener('click', () => ($('#updateBanner').hidden = true));
  $('#ubNotes').addEventListener('click', showNotes);
  $('#ubInstall').addEventListener('click', async () => {
    if (S.update && !S.update.can_install) { API.open_url(S.update.page); return; }
    const ok = await API.install_update();
    if (ok) {
      $('#ubInstall').disabled = true; $('#ubNotes').hidden = true; $('#ubClose').hidden = true;
      $('#ubBar').hidden = false;
      $('#ubTitle').textContent = `Téléchargement de la version ${S.update.version}…`;
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('#errorModal').hidden) $('#errorModal').hidden = true;
      else if (!$('#notesModal').hidden) $('#notesModal').hidden = true;
      else if (!$('#previewModal').hidden) closePreview();
      else $('#logDrawer').classList.remove('open');
    }
    if (!$('#previewModal').hidden && PK.mode === 'pick' && document.activeElement?.id !== 'pkGoto') {
      if (e.key === 'ArrowRight') { e.preventDefault(); stepFrame(e.shiftKey ? '+1s' : '+1'); }
      if (e.key === 'ArrowLeft') { e.preventDefault(); stepFrame(e.shiftKey ? '-1s' : '-1'); }
      if (e.key === 'PageUp') { e.preventDefault(); stepFrame('-10s'); }
      if (e.key === 'PageDown') { e.preventDefault(); stepFrame('+10s'); }
      if (e.key === 'Enter') { e.preventDefault(); generatePreview(); }
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
  window.addEventListener('resize', () => { if (!$('#previewModal').hidden && PK.mode === 'compare') cmpZoom(CMP.mode === 'custom' ? 'fit' : CMP.mode); });
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
    case 'queue_done': if (ev.ok && !ev.failed && !ev.cancelled && !ev.paused) confetti(); break;
    case 'closing': toast('info', 'Mise en pause avant fermeture…', 'L\'avancement est enregistré ; le traitement reprendra automatiquement au prochain lancement.'); break;
    case 'preview_started': onPreviewStarted(ev); break;
    case 'update': showUpdate(ev); break;
    case 'update_progress': {
      const pct = Math.round((ev.fraction || 0) * 100);
      $('#ubBar').hidden = false;
      $('#ubFill').style.width = pct + '%';
      $('#ubSub').textContent = `${pct} % · ${fmtSize(ev.done)} / ${fmtSize(ev.total)}`;
      break;
    }
    case 'update_ready':
      $('#ubTitle').textContent = `Installation de la version ${ev.version}…`;
      $('#ubSub').textContent = "L'application va redémarrer automatiquement.";
      $('#ubFill').style.width = '100%';
      break;
    case 'update_failed':
      $('#ubInstall').disabled = false; $('#ubNotes').hidden = false; $('#ubClose').hidden = false;
      $('#ubBar').hidden = true;
      $('#ubTitle').textContent = 'La mise à jour a échoué';
      $('#ubSub').textContent = ev.error || '';
      $('#ubInstall').textContent = 'Télécharger manuellement';
      S.update = { ...(S.update || {}), can_install: false, page: ev.page };
      break;
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
      <div class="kind-badge img" hidden><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/></svg>Image</div>
      <button class="card-eye" title="Choisir une image et voir le résultat de l'IA"><svg viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>Aperçu</button>
      <button class="card-x" title="Retirer">×</button>
      <div class="overlay">
        <div class="ring"><svg viewBox="0 0 74 74"><circle class="track" cx="37" cy="37" r="32"/><circle class="val" cx="37" cy="37" r="32"/></svg><span>0%</span></div>
        <div class="queued-ic">En file d'attente</div>
        <div class="paused-ic"><svg viewBox="0 0 24 24"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>En pause</div>
        <div class="done-ic"><svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg></div>
      </div>
    </div>
    <div class="card-body">
      <div class="card-name"></div>
      <div class="card-meta"><span class="pill"></span><span class="size"></span></div>
      <div class="card-clean" hidden title="Des défauts de compression (blocs) ont été détectés : ils seront adoucis juste avant l'IA. Désactivable dans les réglages avancés."><svg viewBox="0 0 24 24"><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg><span></span></div>
      <div class="card-frozen" hidden title="Réglages figés au moment du lancement : ils ne changeront plus pour ce fichier, même si vous modifiez les réglages ou redémarrez l'application."><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg><span></span></div>
      <div class="card-actions">
        <button class="btn soft play"><svg class="ic" viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg>Lire</button>
        <button class="btn soft folder"><svg class="ic" viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>Dossier</button>
      </div>
    </div>`;
  const img = $('img', el);
  img.addEventListener('load', () => { img.classList.add('loaded'); $('.shimmer', el)?.remove(); });
  $('.card-x', el).addEventListener('click', async (e) => {
    e.stopPropagation();
    const cur = S.files.find((x) => x.id === f.id);
    if (cur && cur.state === 'paused' && cur.resumable
      && !confirm(`Retirer « ${cur.name} » ?\nL'avancement enregistré de ce traitement en pause sera perdu.`)) return;
    await API.remove_file(f.id);
  });
  $('.card-eye', el).addEventListener('click', (e) => { e.stopPropagation(); if (!S.running) openPicker(f.id); });
  $('.play', el).addEventListener('click', () => API.play(f.id));
  $('.folder', el).addEventListener('click', () => API.open_output(f.id));
  $('.pill', el).addEventListener('click', () => {
    const cur = S.files.find((x) => x.id === f.id);
    if (cur && cur.error) showError(cur.name, cur.error);
  });
  el.addEventListener('dblclick', () => { if (!S.running) openPicker(f.id); });
  el.title = 'Double-clic : choisir une image et voir le résultat de l\'IA';
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
  const isImg = f.kind === 'image';
  $('.dur-badge', el).textContent = isImg ? '' : (f.dur || '') + (f.dur && f.fps ? ` · ${fmtFps(f.fps)} i/s` : '');
  $('.dur-badge', el).hidden = isImg || !f.dur;
  $('.kind-badge.img', el).hidden = !isImg;
  $('.play', el).lastChild.textContent = isImg ? 'Ouvrir' : 'Lire';
  $('.card-eye', el).title = isImg ? "Voir le résultat de l'IA" : "Choisir une image et voir le résultat de l'IA";
  const pct = Math.round((f.progress || 0) * 100);
  $('.ring span', el).textContent = pct + '%';
  $('.ring .val', el).style.strokeDashoffset = 201 - 201 * (f.progress || 0);
  $('.card-progress div', el).style.width = (f.state === 'running' || f.state === 'paused' ? pct : 0) + '%';
  const cleanEl = $('.card-clean', el);
  cleanEl.hidden = !f.clean;
  $('span', cleanEl).textContent = f.clean || '';
  const frozen = $('.card-frozen', el);
  frozen.hidden = !f.frozen;
  $('span', frozen).textContent = f.frozen || '';
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
  const nImg = list.filter((f) => f.kind === 'image').length, nVid = n - nImg;
  const parts = [];
  if (nVid) parts.push(`${nVid} vidéo${nVid > 1 ? 's' : ''}`);
  if (nImg) parts.push(`${nImg} image${nImg > 1 ? 's' : ''}`);
  $('#fileCount').textContent = n === 0 ? 'Aucun fichier pour le moment'
    : `${parts.join(' · ')}${done ? ` · ${done} terminé${done > 1 ? 's' : ''}` : ''}`;
  updateSizeHint();
}

/* ------------------------------------------------------------------ progression */
function syncRun(r) {
  S.running = r.running;
  S.task = r.task;
  S.pending = r.pending || 0;
  S.pausing = !!r.pausing;
  const queue = r.running && r.task === 'queue';
  const pausedQ = !r.running && S.pending > 0;
  const newFiles = S.files.filter((f) => ['ready', 'error', 'cancelled'].includes(f.state) && f.res && !f.frozen).length;
  const start = $('#startBtn');
  start.disabled = (r.running && r.task !== 'queue') || (queue && (!newFiles || S.pausing));
  $('span', start).textContent = queue ? (newFiles ? 'Ajouter à la file' : 'Traitement en cours…')
    : pausedQ ? (newFiles ? 'Reprendre + nouveaux' : 'Reprendre') : "Lancer l'upscale";
  start.title = queue && newFiles ? 'Ajoute les nouveaux fichiers à la file, avec les réglages actuels (figés dès le clic)' : '';
  const pause = $('#pauseBtn');
  pause.hidden = !queue;
  pause.disabled = S.pausing;
  $('span', pause).textContent = S.pausing ? 'Mise en pause…' : 'Pause';
  const cancel = $('#cancelBtn');
  cancel.hidden = !(queue || pausedQ);
  $('span', cancel).textContent = pausedQ ? 'Abandonner' : 'Arrêter';
  $('#previewBtn').disabled = r.running;
  $('#clearBtn').disabled = r.running;
  const bar = $('.bar');
  const p = r.progress;
  bar.classList.toggle('indeterminate', r.running && r.task === 'preview');
  bar.classList.toggle('active', queue && !S.pausing);
  bar.classList.toggle('paused', pausedQ || S.pausing);

  if (r.running && r.task === 'preview') {
    $('#taskTitle').textContent = 'Génération de l\'aperçu IA…';
    return;
  }
  const pausedTitle = `En pause · ${S.pending} fichier${S.pending > 1 ? 's' : ''} en attente — vous pouvez fermer l'application ou éteindre le PC, la reprise se fera à l'image près`;
  if (p && (r.running || p.paused || !pausedQ)) {
    const frac = p.fraction || 0;
    setRing(frac);
    $('#barFill').style.width = (frac * 100).toFixed(1) + '%';
    $('#taskTitle').textContent = S.pausing ? 'Mise en pause… enregistrement de l\'avancement'
      : r.running
        ? `${p.count > 1 ? `[${p.index}/${p.count}] ` : ''}${p.name} · ${p.stage === 'Finalisation' ? 'finalisation…' : 'amélioration par l\'IA'}`
        : pausedQ ? pausedTitle : 'Traitement terminé';
    $('#stSpeed').textContent = r.running && p.speed ? `${p.speed} img/s` : '—';
    $('#stElapsed').textContent = p.elapsed || '—';
    $('#stEta').textContent = r.running ? (p.eta || '—') : pausedQ ? '—' : '00:00:00';
    $('#stFrames').textContent = `${p.done} / ${p.total}`;
  } else if (!r.running) {
    const paused = S.files.find((f) => f.state === 'paused' && f.progress > 0);
    const frac = pausedQ && paused ? paused.progress : 0;
    setRing(frac);
    $('#barFill').style.width = (frac * 100).toFixed(1) + '%';
    const ready = S.files.filter((f) => f.state === 'ready').length;
    $('#taskTitle').textContent = pausedQ ? pausedTitle
      : ready ? `${ready} fichier${ready > 1 ? 's' : ''} prêt${ready > 1 ? 's' : ''} à traiter` : 'Prêt à améliorer vos vidéos et images';
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
const PK = { fid: null, frames: 1, fps: 25, index: 0, seq: 0, mode: 'pick', remember: new Map(), timer: null };

function setPvMode(mode) {
  PK.mode = mode;
  $('#pvBack').hidden = !!PK.isImage;
  const card = $('#pvCard');
  card.classList.toggle('mode-pick', mode === 'pick');
  card.classList.toggle('mode-compare', mode === 'compare');
  $('#pvTitle').textContent = mode === 'pick' ? "Choisir l'image à prévisualiser" : 'Aperçu IA';
  if (mode === 'pick') $('#pvBack').hidden = true;
  if (mode === 'compare') requestAnimationFrame(() => cmpZoom(CMP.mode === 'custom' ? 'fit' : CMP.mode));
}

async function openPicker(fid) {
  const info = await API.frame_info(fid);
  if (!info || !info.frames) { toast('warn', 'Vidéo pas encore analysée', 'Patientez une seconde puis réessayez.'); return; }
  PK.fid = fid; PK.frames = info.frames; PK.fps = info.fps || 25;
  $('#pvName').textContent = '· ' + info.name;
  if (info.kind === 'image') {           // une image : pas de choix d'image, aperçu direct
    PK.index = 0; PK.isImage = true;
    $('#pvChips').innerHTML = `<span>${esc(info.res)}</span><span class="hl">→ ${esc(info.out)}</span>`;
    $('#previewModal').hidden = false;
    generatePreview();
    return;
  }
  PK.isImage = false;
  $('#pvChips').innerHTML = `<span>${esc(info.res)}</span><span class="hl">→ ${esc(info.out)}</span>`;
  $('#pkRange').max = Math.max(0, info.frames - 1);
  $('#pkImg').removeAttribute('src');
  $('#previewModal').hidden = false;
  setPvMode('pick');
  // pellicule
  const strip = $('#filmstrip');
  strip.innerHTML = Array.from({ length: 12 }, () => '<div class="ph"></div>').join('');
  const start = PK.remember.has(fid) ? PK.remember.get(fid) : Math.floor(info.frames * 0.35);
  pickFrame(start, false);
  const thumbs = await API.get_filmstrip(fid, 12);
  if (PK.fid !== fid) return;
  strip.innerHTML = '';
  thumbs.forEach((t) => {
    const d = document.createElement('div');
    if (t.data) d.style.backgroundImage = `url("${t.data}")`;
    strip.appendChild(d);
  });
}

function frameLabel(index) {
  const t = index / PK.fps;
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${sec.toFixed(2).padStart(5, '0')}`;
}

function pickFrame(index, fast) {
  index = Math.max(0, Math.min(PK.frames - 1, Math.round(index)));
  PK.index = index;
  PK.remember.set(PK.fid, index);
  $('#pkRange').value = index;
  const label = `Image ${(index + 1).toLocaleString('fr-FR')} / ${PK.frames.toLocaleString('fr-FR')}`;
  $('#pkFrame').textContent = label;
  $('#pkFrameBig').textContent = `Image ${(index + 1).toLocaleString('fr-FR')}`;
  $('#pkClock').textContent = $('#pkClockBig').textContent = frameLabel(index);
  clearTimeout(PK.timer);
  PK.timer = setTimeout(() => loadFrame(index, fast ? 720 : 1600), fast ? 70 : 0);
}

async function loadFrame(index, width) {
  const seq = ++PK.seq;
  const fid = PK.fid;
  $('#pkLoading').classList.add('show');
  try {
    const r = await API.get_frame(fid, index, width);
    if (seq !== PK.seq || fid !== PK.fid) return; // une requête plus récente est en cours
    if (r && r.data) $('#pkImg').src = r.data;
    else if (r && r.error) toast('error', "Impossible d'afficher cette image", r.error);
  } finally {
    if (seq === PK.seq) $('#pkLoading').classList.remove('show');
  }
}

function stepFrame(step) {
  const fps = PK.fps;
  const map = { '-1': -1, '+1': 1, '-1s': -Math.round(fps), '+1s': Math.round(fps), '-10s': -Math.round(fps * 10), '+10s': Math.round(fps * 10) };
  if (step === 'random') pickFrame(Math.floor(Math.random() * PK.frames), false);
  else pickFrame(PK.index + (map[step] || 0), false);
}

function parseClock(text) {
  const t = String(text || '').trim().replace(',', '.');
  if (!t) return null;
  const parts = t.split(':').map(Number);
  if (parts.some((n) => Number.isNaN(n) || n < 0) || parts.length > 3) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}

function generatePreview() {
  if (S.running) { toast('info', 'Patientez', 'Un traitement est déjà en cours.'); return; }
  $('#pvLoadingSub').textContent = PK.isImage ? "Agrandissement de l'image par l'IA"
    : `Agrandissement de l'image ${PK.index + 1} (${frameLabel(PK.index)})`;
  $('#pvLoading').hidden = false;
  $('#pvAfter').removeAttribute('src');
  $('#pvBefore').removeAttribute('src');
  setPvMode('compare');
  API.preview(PK.fid, PK.index);
}

function onPreviewStarted(ev) {
  if ($('#previewModal').hidden) {
    $('#pvName').textContent = '· ' + (ev.name || '');
    $('#previewModal').hidden = false;
  }
  $('#pvLoading').hidden = false;
  if (ev.time) $('#pvLoadingSub').textContent = `Agrandissement de l'image ${ev.index + 1} (${ev.time})`;
  setPvMode('compare');
}

function closePreview() { $('#previewModal').hidden = true; PK.seq++; }

function showPreview(ev) {
  if ($('#previewModal').hidden) { $('#previewModal').hidden = false; $('#pvName').textContent = '· ' + (ev.name || ''); }
  setPvMode('compare');
  const model = S.models.find((m) => m.key === ev.model);
  $('#pvModel').textContent = model ? model.name : '';
  $('#pvChips').innerHTML = `<span>${esc(ev.source)}</span><span class="hl">→ ${esc(ev.output)}</span>` +
    `<span>${esc(model ? model.name : ev.model)}</span>` + (ev.clean ? `<span>Nettoyage ${esc(ev.clean)}</span>` : '') + (ev.finish ? `<span class="hl">✦ Finitions ${esc(ev.finish)}</span>` : '')
    + (ev.time ? `<span>Image ${ev.index + 1} · ${esc(ev.time)}</span>` : '');
  $('#pvBack').hidden = !!PK.isImage || !ev.time;
  CMP.w = ev.width; CMP.h = ev.height; CMP.x = 0.5;
  let loaded = 0;
  const done = () => { if (++loaded === 2) { $('#pvLoading').hidden = true; cmpZoom('fit'); } };
  $('#pvAfter').onload = done; $('#pvBefore').onload = done;
  $('#pvAfter').src = ev.after;
  $('#pvBefore').src = ev.before;
  $$('#pvZoom button').forEach((x) => x.classList.toggle('on', x.dataset.z === 'fit'));
}

/* ------------------------------------------------------------------ mises à jour */
function showUpdate(info) {
  S.update = info;
  $('#ubTitle').textContent = `Nouvelle version ${info.version} disponible`;
  $('#ubSub').textContent = `Vous utilisez la version ${info.current}` + (info.size ? ` · ${fmtSize(info.size)}` : '');
  $('#ubInstall').textContent = info.can_install ? 'Mettre à jour' : 'Télécharger';
  $('#ubInstall').disabled = false;
  $('#ubNotes').hidden = !info.notes;
  $('#ubClose').hidden = false;
  $('#ubBar').hidden = true;
  $('#updateBanner').hidden = false;
}

function mdToHtml(md) {
  const out = [];
  let inList = false;
  for (const raw of String(md || '').split(/\r?\n/)) {
    const line = raw.trim();
    const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`(.+?)`/g, '<code>$1</code>');
    if (/^[-*] /.test(line)) { if (!inList) { out.push('<ul>'); inList = true; } out.push(`<li>${inline(line.slice(2))}</li>`); continue; }
    if (inList) { out.push('</ul>'); inList = false; }
    if (/^#{1,6} /.test(line)) out.push(`<h3>${inline(line.replace(/^#+ /, ''))}</h3>`);
    else if (line) out.push(`<p>${inline(line)}</p>`);
  }
  if (inList) out.push('</ul>');
  return out.join('');
}

function showNotes() {
  if (!S.update) return;
  $('#notesVersion').textContent = S.update.version;
  $('#notesBody').innerHTML = mdToHtml(S.update.notes);
  $('#notesModal').hidden = false;
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
