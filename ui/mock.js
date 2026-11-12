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
  const SIZES = [['x2', 'x2'], ['x3', 'x3'], ['x4', 'x4'], ['720p', '720p'], ['1080p', '1080p'], ['1440p', '1440p'], ['2160p', '4K'], ['4320p', '8K']];
  const ENCODERS = [
    ['x264', 'H.264 – CPU (compatible partout)'], ['x265', 'H.265/HEVC – CPU (fichiers plus légers)'],
    ['nvenc_h264', 'H.264 – NVIDIA NVENC (très rapide)'], ['nvenc_hevc', 'H.265 – NVIDIA NVENC (très rapide)'],
    ['amf_h264', 'H.264 – AMD AMF'], ['amf_hevc', 'H.265 – AMD AMF'], ['qsv_h264', 'H.264 – Intel Quick Sync'], ['qsv_hevc', 'H.265 – Intel Quick Sync'],
  ];
  const SAMPLES = [
    { name: 'Tokyo_nuit_2009.mp4', w: 640, h: 360, dur: '00:04:12', size: 187e6, img: 'city' },
    { name: 'Le_voyage_episode01.mkv', w: 854, h: 480, dur: '00:23:40', size: 412e6, img: 'anime' },
    { name: 'Vacances_plage_camescope.avi', w: 720, h: 576, dur: '00:12:05', size: 890e6, img: 'family' },
  ];
  let settings = { model: 'upscayl-lite-4x', output_size: '2160p', encoder: 'x264', quality: 18, speed: 'medium', deinterlace: 'auto', gpu: 'auto', tile: 0, tta: false, container: 'mp4', output_dir: '', temp_dir: '' };
  let files = [];
  let nextId = 1;
  let events = [];
  let logs = [];
  let running = false, task = '', progress = null, timer = null;
  const ts = () => new Date().toTimeString().slice(0, 8);
  const log = (m) => logs.push(`[${ts()}] ${m}`);

  function outFor(f) {
    const k = settings.output_size;
    let fac = k.startsWith('x') ? Number(k.slice(1)) : Number(k.replace('p', '')) / Math.min(f.w, f.h);
    if (fac <= 1) return 'déjà ≥';
    const ev = (x) => Math.max(2, Math.round(x / 2) * 2);
    return `${ev(f.w * fac)}×${ev(f.h * fac)}`;
  }
  function pub(f) { return { ...f, out: outFor(f) }; }

  function addSample(s) {
    const f = { id: nextId++, name: s.name, path: 'C:\\Users\\Vous\\Vidéos\\' + s.name, status: 'Prêt', state: 'ready', progress: 0,
      res: `${s.w}×${s.h}`, dur: s.dur, out: '', size: s.size, output: '', error: '', w: s.w, h: s.h, img: s.img };
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

  window.MockApi = {
    async init() {
      log('Mode démo : interface ouverte dans un navigateur (aucun traitement réel).');
      setTimeout(() => { events.push({ type: 'gpus', gpus: [{ id: '0', name: 'NVIDIA GeForce RTX 4070' }] }); log('Carte(s) graphique(s) : NVIDIA GeForce RTX 4070'); }, 900);
      setTimeout(() => SAMPLES.forEach(addSample), 400);
      return {
        version: '1.1.0', models: MODELS, settings: { ...settings }, gpus: null, files: [], running: false,
        sizes: SIZES.map(([key, label]) => ({ key, label })),
        encoders: ENCODERS.map(([key, label]) => ({ key, label })),
        speeds: [{ key: 'fast', label: 'Rapide' }, { key: 'medium', label: 'Équilibré' }, { key: 'slow', label: 'Lent' }],
      };
    },
    async poll() {
      const ev = events; events = [];
      const lg = logs; logs = [];
      return { events: ev, logs: lg, files: files.map(pub), running, task, progress };
    },
    async pick_files() { return [addSample(SAMPLES[(nextId - 1) % SAMPLES.length])]; },
    async add_paths(names) { return names.map((n, i) => addSample({ ...SAMPLES[i % 3], name: n })); },
    async pick_folder(key) { const p = 'D:\\Vidéos\\Upscale'; settings[key] = p; return p; },
    async remove_file(id) { files = files.filter((f) => f.id !== id); return true; },
    async clear_files() { if (!running) files = []; return true; },
    async set_settings(v) { Object.assign(settings, v); return { ...settings }; },
    async open_output() { events.push({ type: 'toast', kind: 'info', title: 'Mode démo', text: "Dans l'application, le dossier s'ouvre dans l'Explorateur." }); return true; },
    async play() { events.push({ type: 'toast', kind: 'info', title: 'Mode démo', text: "Dans l'application, la vidéo s'ouvre dans votre lecteur." }); return true; },
    async open_url() {},
    async cancel() { if (timer) { clearInterval(timer); timer = null; } files.forEach((f) => { if (f.state === 'running' || f.state === 'queued') Object.assign(f, { state: 'cancelled', status: 'Annulé', progress: 0 }); }); running = false; task = ''; events.push({ type: 'toast', kind: 'info', title: 'Traitement annulé' }); return true; },
    async preview(id) {
      const f = files.find((x) => x.id === id) || files[0];
      if (!f) { events.push({ type: 'toast', kind: 'info', title: 'Aucune vidéo', text: 'Ajoutez une vidéo pour générer un aperçu.' }); return false; }
      running = true; task = 'preview';
      events.push({ type: 'preview_started', id: f.id, name: f.name });
      log(`Aperçu : ${f.name}`);
      const hq = `demo/${f.img}_hq.jpg`;
      blurred(hq, (before, W, H) => setTimeout(() => {
        events.push({ type: 'preview_done', id: f.id, before, after: hq, width: W, height: H, name: f.name, source: f.res, output: outFor(f), model: settings.model });
        running = false; task = '';
      }, 1600));
      return true;
    },
    async start() {
      const todo = files.filter((f) => ['ready', 'error', 'cancelled'].includes(f.state));
      if (!todo.length) { events.push({ type: 'toast', kind: 'info', title: 'Rien à traiter', text: 'Ajoutez une vidéo.' }); return false; }
      todo.forEach((f) => Object.assign(f, { state: 'queued', status: 'En file', progress: 0 }));
      running = true; task = 'queue';
      let i = 0, done = 0; const total = 900; const t0 = Date.now();
      log(`──── [1/${todo.length}] ${todo[0].name}`);
      timer = setInterval(() => {
        const f = todo[i];
        f.state = 'running';
        done += 9 + Math.random() * 6;
        const frac = Math.min(1, done / total);
        f.progress = frac; f.status = Math.round(frac * 100) + ' %';
        const el = (Date.now() - t0) / 1000;
        const fmt = (s) => new Date(Math.max(0, s) * 1000).toISOString().slice(11, 19);
        progress = { done: Math.round(frac * total), total, speed: (11.8 + Math.random()).toFixed(2), eta: fmt((total - done) / 12), elapsed: fmt(el), fraction: frac, stage: frac < 1 ? 'IA' : 'Finalisation', index: i + 1, count: todo.length, name: f.name };
        if (frac >= 1) {
          Object.assign(f, { state: 'done', status: 'Terminé', output: 'C:\\out.mp4' });
          log(`✔ Terminé : ${f.name}`);
          i++; done = 0;
          if (i >= todo.length) {
            clearInterval(timer); timer = null; running = false; task = '';
            events.push({ type: 'queue_done', ok: todo.length, failed: 0, cancelled: false });
            events.push({ type: 'toast', kind: 'success', title: `${todo.length} vidéo(s) terminée(s)`, text: 'Vos vidéos en haute résolution sont prêtes.' });
          } else log(`──── [${i + 1}/${todo.length}] ${todo[i].name}`);
        }
      }, 120);
      return true;
    },
  };
})();
