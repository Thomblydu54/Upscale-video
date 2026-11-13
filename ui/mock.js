/* Mode démo : simule le moteur Python quand l'interface est ouverte dans un navigateur.
   (Dans l'application Windows, c'est window.pywebview.api qui est utilisé.) */
'use strict';
(function () {
  const MODELS = [
    { key: 'upscayl-lite-4x', name: 'Réaliste Rapide', sub: 'Films, séries, vidéos perso', description: 'Films, séries, vidéos de famille, YouTube…', scales: [4], speed: 5, quality: 3, icon: 'film', tag: 'Recommandé' },
    { key: 'upscayl-standard-4x', name: 'Réaliste Pro', sub: 'Le plus de détails, plus lent', description: 'Plus de détails, 5 à 10x plus lent.', scales: [4], speed: 2, quality: 5, icon: 'sparkles', tag: 'Détails max' },
    { key: 'realesrgan-x4plus', name: 'Real-ESRGAN', sub: 'Vidéos très bruitées / compressées', description: 'Très propre, lisse un peu les textures.', scales: [4], speed: 2, quality: 4, icon: 'wand', tag: 'Débruitage' },
    { key: 'realesr-animevideov3', name: 'Anime Rapide', sub: 'Dessins animés, 2D, x2/x3/x4', description: 'Très rapide.', scales: [2, 3, 4], speed: 5, quality: 4, icon: 'brush', tag: 'Anime rapide' },
    { key: 'realesrgan-x4plus-anime', name: 'Anime Pro', sub: 'Anime net et fidèle, plus lent', description: 'Plus net, plus lent.', scales: [4], speed: 2, quality: 5, icon: 'star', tag: 'Anime HQ' },
  ];
  const FINISHES = [
    ['eclatant', 'Éclatant', { finish_sharp: 45, finish_color: 55, finish_contrast: 35, finish_glow: 15, finish_warmth: 0 }],
    ['glow', 'Glow anime', { finish_sharp: 40, finish_color: 45, finish_contrast: 25, finish_glow: 60, finish_warmth: 0 }],
    ['cinema', 'Cinéma', { finish_sharp: 30, finish_color: 20, finish_contrast: 55, finish_glow: 20, finish_warmth: 15 }],
    ['dore', 'Doré', { finish_sharp: 30, finish_color: 35, finish_contrast: 25, finish_glow: 40, finish_warmth: 45 }],
    ['glace', 'Glacé', { finish_sharp: 35, finish_color: 30, finish_contrast: 30, finish_glow: 35, finish_warmth: -45 }],
    ['net', 'Netteté seule', { finish_sharp: 60, finish_color: 0, finish_contrast: 0, finish_glow: 0, finish_warmth: 0 }],
  ];
  const finActive = () => settings.finish === 'auto' || settings.finish && settings.finish !== 'off'
    && ['finish_sharp', 'finish_color', 'finish_contrast', 'finish_glow', 'finish_warmth'].some((k) => Number(settings[k]) !== 0);
  const finLabel = () => settings.finish === 'auto' ? 'Auto' : (FINISHES.find((x) => x[0] === settings.finish) || [0, 'Personnalisé'])[1];
  // démo du mode auto : finition « choisie » selon l'image d'exemple
  const autoPick = (f) => ({ city: 'glow', anime: 'eclatant', family: 'dore' }[f.img] || 'cinema');
  // démo : simple approximation des finitions avec les filtres du navigateur
  async function finishFrame(src, vals = settings) {
    const settings = vals;  // démo : réglages de la finition appliquée
    const im = await loadImg(src);
    const cv = document.createElement('canvas'); cv.width = im.width; cv.height = im.height;
    const ctx = cv.getContext('2d');
    const w = Number(settings.finish_warmth) / 100;
    ctx.filter = `saturate(${1 + Number(settings.finish_color) / 100}) contrast(${1 + Number(settings.finish_contrast) / 300})`
      + (w > 0 ? ` sepia(${w * 0.35})` : w < 0 ? ` hue-rotate(${w * 12}deg)` : '');
    ctx.drawImage(im, 0, 0);
    const g = Number(settings.finish_glow) / 100;
    if (g > 0) {
      ctx.filter = `blur(${Math.round(im.height * 0.012)}px) brightness(0.9)`;
      ctx.globalCompositeOperation = 'screen'; ctx.globalAlpha = 0.45 * g;
      ctx.drawImage(cv, 0, 0);
    }
    return cv.toDataURL('image/jpeg', 0.88);
  }
  const SIZES = [['x2', 'x2'], ['x3', 'x3'], ['x4', 'x4'], ['720p', '720p'], ['1080p', '1080p'], ['1440p', '1440p'], ['2160p', '4K'], ['4320p', '8K'], ['x1', "Taille d'origine"]];
  const ENCODERS = [
    ['x264', 'H.264 – CPU (compatible partout)'], ['x265', 'H.265/HEVC – CPU (fichiers plus légers)'],
    ['nvenc_h264', 'H.264 – NVIDIA NVENC (très rapide)'], ['nvenc_hevc', 'H.265 – NVIDIA NVENC (très rapide)'],
    ['amf_h264', 'H.264 – AMD AMF'], ['amf_hevc', 'H.265 – AMD AMF'], ['qsv_h264', 'H.264 – Intel Quick Sync'], ['qsv_hevc', 'H.265 – Intel Quick Sync'],
  ];
  const SAMPLES = [
    { name: 'Tokyo_nuit_2009.mp4', w: 640, h: 360, dur: '00:04:12', size: 187e6, img: 'city', fps: 30 },
    { name: 'Le_voyage_episode01.mkv', w: 854, h: 480, dur: '00:23:40', size: 412e6, img: 'anime', fps: 23.976 },
    { name: 'Vacances_plage_camescope.avi', w: 720, h: 576, dur: '00:12:05', size: 890e6, img: 'family', clean: 'moyen' },
    { name: 'Photo_mariage_1998.jpg', w: 800, h: 450, dur: 'Image', size: 2.4e6, img: 'family', kind: 'image', clean: 'léger' },
  ];
  let settings = { model: 'upscayl-lite-4x', output_size: '2160p', encoder: 'x264', quality: 18, speed: 'medium', deinterlace: 'auto', gpu: 'auto', tile: 0, tta: false, container: 'mp4', output_dir: '', temp_dir: '', auto_update: true, interpolation: 'off', image_format: 'auto', finish: 'off', finish_sharp: 0, finish_color: 0, finish_contrast: 0, finish_glow: 0, finish_warmth: 0, clean: 'auto', interp_fps: 60 };
  let files = [];
  let nextId = 1;
  let events = [];
  let logs = [];
  let running = false, task = '', progress = null, timer = null;
  const ts = () => new Date().toTimeString().slice(0, 8);
  const log = (m) => logs.push(`[${ts()}] ${m}`);

  function outFor(f) {
    const k = settings.output_size;
    const isImg = f.kind === 'image';
    // personnalisé : sortie à la fréquence voulue, sauf vidéo déjà aussi rapide (fréquence d'origine)
    const m = settings.interpolation === 'custom' ? (Number(settings.interp_fps) > f.fps ? Number(settings.interp_fps) / f.fps : 1)
      : { x2: 2, x3: 3, x4: 4 }[settings.interpolation] || 1;
    let fac = k.startsWith('x') ? Number(k.slice(1)) : Number(k.replace('p', '')) / Math.min(f.w, f.h);
    if (fac <= 1 && (isImg || m === 1)) return k === 'x1' ? 'inchangé' : 'déjà ≥';
    if (fac < 1) fac = 1;
    const ev = (x) => Math.max(2, Math.round(x / 2) * 2);
    return `${ev(f.w * fac)}×${ev(f.h * fac)}` + (!isImg && m > 1 ? ` · ${String(Math.round(f.fps * m * 100) / 100).replace('.', ',')} i/s` : '');
  }
  function pub(f) {
    const clean = f.cleanLevel && settings.clean !== 'off' ? `Compression détectée · nettoyage ${f.cleanLevel}` : '';
    return { frozen: '', resumable: false, ...f, clean, out: f.frozenOut || outFor(f) };
  }

  function addSample(s) {
    const f = { id: nextId++, name: s.name, path: 'C:\\Users\\Vous\\Vidéos\\' + s.name, status: 'Prêt', state: 'ready', progress: 0,
      res: `${s.w}×${s.h}`, dur: s.dur, out: '', size: s.size, output: '', error: '', w: s.w, h: s.h, img: s.img,
      kind: s.kind || 'video', fps: s.kind ? 0 : (s.fps || 25), cleanLevel: s.clean || '',
      frames: s.kind ? 1 : s.dur.split(':').reduce((a, n) => a * 60 + Number(n), 0) * 25 };
    files.push(f);
    setTimeout(() => events.push({ type: 'thumb', id: f.id, thumb: `demo/${s.img}_thumb.jpg` }), 300 + Math.random() * 600);
    return pub(f);
  }

  function blurred(src, cb) {
    const img = new Image();
    img.onload = () => {
      const W = img.width, H = img.height;
      const small = document.createElement('canvas');
      small.width = Math.round(W / 4.5); small.height = Math.round(H / 4.5);
      small.getContext('2d').drawImage(img, 0, 0, small.width, small.height);
      const big = document.createElement('canvas');
      big.width = W; big.height = H;
      const ctx = big.getContext('2d');
      ctx.imageSmoothingQuality = 'low';
      ctx.drawImage(small, 0, 0, W, H);
      cb(big.toDataURL('image/jpeg', 0.85), W, H);
    };
    img.src = src;
  }

  // images de démo : simulation d'un plan qui bouge (zoom / panoramique) selon l'image choisie
  const imgCache = {};
  function loadImg(src) {
    if (!imgCache[src]) imgCache[src] = new Promise((res) => { const im = new Image(); im.onload = () => res(im); im.src = src; });
    return imgCache[src];
  }
  async function renderFrame(f, index, width) {
    const im = await loadImg(`demo/${f.img}_hq.jpg`);
    const frames = f.frames;
    const p = index / Math.max(1, frames - 1);
    const zoom = 1 + 0.35 * p;
    const W = Math.min(width, 1280), H = Math.round(W * 9 / 16);
    const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
    const ctx = cv.getContext('2d');
    const sw = im.width / zoom, sh = im.height / zoom;
    ctx.drawImage(im, (im.width - sw) * p, (im.height - sh) * (0.5 + 0.3 * Math.sin(p * 6)), sw, sh, 0, 0, W, H);
    return cv.toDataURL('image/jpeg', 0.85);
  }
  const fmtClock = (t) => { const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60; return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`; };

  window.MockApi = {
    async frame_info(id) {
      const f = files.find((x) => x.id === id); if (!f) return {};
      return { id, name: f.name, fps: 25, frames: f.frames, duration: f.frames / 25, width: f.w, height: f.h, res: f.res, out: outFor(f), kind: f.kind };
    },
    async get_frame(id, index, width) {
      const f = files.find((x) => x.id === id); if (!f) return {};
      await new Promise((r) => setTimeout(r, 60));
      return { index, time: fmtClock(index / 25), data: await renderFrame(f, index, width) };
    },
    async get_filmstrip(id, count) {
      const f = files.find((x) => x.id === id); if (!f) return [];
      await new Promise((r) => setTimeout(r, 400));
      const out = [];
      for (let i = 0; i < count; i++) { const idx = Math.floor((i + 0.5) * f.frames / count); out.push({ index: idx, data: await renderFrame(f, idx, 200) }); }
      return out;
    },
    async check_update(manual) {
      events.push({ type: 'update', version: '1.9.0', tag: 'v1.9.0', current: '1.8.0', size: 254e6, can_install: true, page: '#',
        notes: "## Nouveautés\n- **Restauration des visages** par IA\n- Corrections de bugs" });
      return { status: 'available' };
    },
    async install_update() {
      let d = 0; const total = 254e6;
      const t = setInterval(() => {
        d = Math.min(total, d + total / 30);
        events.push({ type: 'update_progress', done: d, total, fraction: d / total });
        if (d >= total) { clearInterval(t); events.push({ type: 'update_ready', version: '1.9.0' });
          setTimeout(() => { events.push({ type: 'update_failed', error: "Mode démo : l'installation est simulée.", page: '#' }); }, 2500); }
      }, 100);
      return true;
    },
    async init() {
      log('Mode démo : interface ouverte dans un navigateur (aucun traitement réel).');
      setTimeout(() => { events.push({ type: 'gpus', gpus: [{ id: '0', name: 'NVIDIA GeForce RTX 4070' }] }); log('Carte(s) graphique(s) : NVIDIA GeForce RTX 4070'); }, 900);
      setTimeout(() => SAMPLES.forEach(addSample), 400);
      setTimeout(() => this.check_update(false), 3500);
      return {
        version: '1.8.0', models: MODELS, settings: { ...settings }, gpus: null, files: [], running: false,
        sizes: SIZES.map(([key, label]) => ({ key, label })),
        encoders: ENCODERS.map(([key, label]) => ({ key, label })),
        speeds: [{ key: 'fast', label: 'Rapide' }, { key: 'medium', label: 'Équilibré' }, { key: 'slow', label: 'Lent' }],
        interpolations: [{ key: 'off', label: 'Désactivée' }, { key: 'x2', label: 'x2' }, { key: 'x3', label: 'x3' }, { key: 'x4', label: 'x4' }],
        finishes: FINISHES.map(([key, label, values]) => ({ key, label, values })),
        image_formats: [{ key: 'auto', label: 'Identique' }, { key: 'png', label: 'PNG' }, { key: 'jpg', label: 'JPEG' }, { key: 'webp', label: 'WebP' }],
      };
    },
    async poll() {
      const ev = events; events = [];
      const lg = logs; logs = [];
      const pending = files.filter((f) => f.frozen && ['queued', 'paused', 'running'].includes(f.state)).length;
      return { events: ev, logs: lg, files: files.map(pub), running, task, progress, pending, pausing: false };
    },
    async pick_files() { return [addSample(SAMPLES[(nextId - 1) % SAMPLES.length])]; },
    async add_paths(names) { return names.map((n, i) => addSample({ ...SAMPLES[i % 3], name: n })); },
    async pick_folder(key) { const p = 'D:\\Vidéos\\Upscale'; settings[key] = p; return p; },
    async remove_file(id) { files = files.filter((f) => f.id !== id); return true; },
    async clear_files() { if (!running) files = []; return true; },
    async set_settings(v) {
      Object.assign(settings, v);
      settings.interp_fps = Math.round(Math.max(1, Math.min(240, Number(settings.interp_fps) || 60)));
      const p = FINISHES.find((x) => x[0] === settings.finish);
      if (p) Object.assign(settings, p[2]);  // comme le moteur : un préréglage impose ses valeurs
      return { ...settings };
    },
    async open_output() { events.push({ type: 'toast', kind: 'info', title: 'Mode démo', text: "Dans l'application, le dossier s'ouvre dans l'Explorateur." }); return true; },
    async play() { events.push({ type: 'toast', kind: 'info', title: 'Mode démo', text: "Dans l'application, la vidéo s'ouvre dans votre lecteur." }); return true; },
    async open_url() {},
    async cancel() {
      if (timer) { clearInterval(timer); timer = null; }
      files.forEach((f) => { if (['running', 'queued', 'paused'].includes(f.state)) Object.assign(f, { state: 'cancelled', status: 'Annulé', progress: 0, frozen: '', frozenOut: '', resumable: false }); });
      running = false; task = ''; progress = null;
      events.push({ type: 'toast', kind: 'info', title: 'Traitement annulé' }); return true;
    },
    async pause() {
      if (!running || task !== 'queue') return false;
      clearInterval(timer); timer = null; running = false; task = '';
      files.forEach((f) => {
        if (f.state === 'running') Object.assign(f, { state: 'paused', status: `En pause · ${Math.round(f.progress * 100)} %`, resumable: true });
        else if (f.state === 'queued') Object.assign(f, { state: 'paused', status: 'En file (en pause)' });
      });
      if (progress) progress = { ...progress, paused: true };
      log('⏸ En pause. Vous pouvez fermer l\'application ou éteindre le PC : le traitement reprendra automatiquement à la même image.');
      events.push({ type: 'queue_done', ok: 0, failed: 0, cancelled: false, paused: true });
      events.push({ type: 'toast', kind: 'info', title: 'Traitement en pause', text: 'Avancement enregistré : vous pouvez quitter ou éteindre le PC, la reprise se fera automatiquement à la même image.' });
      return true;
    },
    async resume() { return this.start(); },
    async preview(id, index) {
      const f = files.find((x) => x.id === id) || files[0];
      if (!f) { events.push({ type: 'toast', kind: 'info', title: 'Aucune vidéo', text: 'Ajoutez une vidéo pour générer un aperçu.' }); return false; }
      if (index == null) index = Math.floor(f.frames / 2);
      running = true; task = 'preview';
      events.push({ type: 'preview_started', id: f.id, name: f.name, index, time: f.kind === 'image' ? '' : fmtClock(index / 25) });
      log(`Aperçu : ${f.name}, image ${index + 1}`);
      const hq = await renderFrame(f, index, 1280);
      const fin = finActive();
      const auto = settings.finish === 'auto' ? FINISHES.find((x) => x[0] === autoPick(f)) : null;
      const finName = auto ? `Auto → ${auto[1]}` : finLabel();
      if (fin) log(`Aperçu : finitions « ${finName} »…`);
      const after = fin ? await finishFrame(hq, auto ? { ...settings, ...auto[2] } : settings) : hq;
      blurred(hq, (before, W, H) => setTimeout(() => {
        events.push({ type: 'preview_done', id: f.id, before, after, finish: fin ? finName : '', clean: settings.clean !== 'off' ? (f.cleanLevel || '') : '', width: W, height: H, name: f.name, source: f.res, output: outFor(f), model: settings.model, index, time: f.kind === 'image' ? '' : fmtClock(index / 25) });
        running = false; task = '';
      }, 1600));
      return true;
    },
    async start() {
      const short = { 'upscayl-lite-4x': 'Réaliste Rapide', 'upscayl-standard-4x': 'Réaliste Pro', 'realesrgan-x4plus': 'Real-ESRGAN', 'realesr-animevideov3': 'Anime Rapide', 'realesrgan-x4plus-anime': 'Anime Pro' };
      const summary = (f) => [short[settings.model] || settings.model, (SIZES.find((x) => x[0] === settings.output_size) || [0, settings.output_size])[1],
        ...(f.kind === 'image' ? [`format ${settings.image_format === 'auto' ? 'identique' : settings.image_format.toUpperCase()}`]
          : [...(settings.interpolation === 'custom' ? [`fluidité ${String(settings.interp_fps).replace('.', ',')} i/s`] : settings.interpolation && settings.interpolation !== 'off' ? [`fluidité ${settings.interpolation}`] : []), `${String(settings.encoder).includes('265') || String(settings.encoder).includes('hevc') ? 'H.265' : 'H.264'} ${String(settings.container || 'mp4').toUpperCase()} · CRF ${settings.quality}`]),
        ...(finActive() ? [`finitions ${finLabel()}`] : [])].join(' · ');
      const fresh = files.filter((f) => ['ready', 'error', 'cancelled'].includes(f.state) && !f.frozen);
      fresh.forEach((f) => Object.assign(f, { state: 'queued', status: 'En file', progress: 0, frozen: summary(f), frozenOut: outFor(f) }));
      if (running) {
        if (fresh.length) events.push({ type: 'toast', kind: 'info', title: `${fresh.length} fichier(s) ajouté(s) à la file`, text: 'Ils seront traités avec les réglages actuels, figés dès maintenant.' });
        return fresh.length > 0;
      }
      files.forEach((f) => { if (f.state === 'paused') Object.assign(f, { state: 'queued', status: 'En file' }); });
      if (!files.some((f) => f.state === 'queued')) { events.push({ type: 'toast', kind: 'info', title: 'Rien à traiter', text: 'Ajoutez une vidéo.' }); return false; }
      running = true; task = 'queue';
      const total = 900; const t0 = Date.now(); let ok = 0, handled = 0, cur = null;
      timer = setInterval(() => {
        if (!cur) {
          cur = files.find((f) => f.state === 'queued');
          if (!cur) {
            clearInterval(timer); timer = null; running = false; task = '';
            events.push({ type: 'queue_done', ok, failed: 0, cancelled: false });
            events.push({ type: 'toast', kind: 'success', title: `${ok} fichier(s) terminé(s)`, text: 'Vos fichiers améliorés sont prêts.' });
            return;
          }
          handled++;
          log(`──── [${handled}/${handled - 1 + files.filter((f) => f.state === 'queued').length}] ${cur.name}`);
          log('Réglages figés : ' + cur.frozen);
          if (cur.progress > 0) log(`↻ Reprise à l'image ${Math.round(cur.progress * total)} (avancement enregistré).`);
        }
        const f = cur;
        f.state = 'running';
        const frac = Math.min(1, (f.progress || 0) + (9 + Math.random() * 6) / total);
        f.progress = frac; f.status = Math.round(frac * 100) + ' %';
        const el = (Date.now() - t0) / 1000;
        const fmt = (s) => new Date(Math.max(0, s) * 1000).toISOString().slice(11, 19);
        progress = { done: Math.round(frac * total), total, speed: (11.8 + Math.random()).toFixed(2), eta: fmt((1 - frac) * total / 12), elapsed: fmt(el), fraction: frac, stage: frac < 1 ? 'IA' : 'Finalisation', index: handled, count: handled - 1 + files.filter((x) => x.state === 'queued' || x === f).length, name: f.name };
        if (frac >= 1) {
          Object.assign(f, { state: 'done', status: 'Terminé', output: 'C:\\out.mp4', frozen: '', resumable: false });
          log(`✔ Terminé : ${f.name}`);
          ok++; cur = null;
        }
      }, 120);
      return true;
    },
  };
})();
