/* Gestion des extensions officielles. Aucune clé de service n'est envoyée à l'interface. */
'use strict';
const EXT = { catalog: null, remote: { installed: false }, role: 'controller', hydrated: false, busy: false, rendered: '', workers: '' };

function initExtensions(init) {
  updateExtensions(init.extensions, init.remote);
  $('#extensionsBtn').addEventListener('click', async () => {
    $('#extensionsModal').hidden = false;
    $('#rConsent').checked = false;
    $('#rPairBox').hidden = true;
    $('#rPairCode').value = '';
    $('#extensionsClose').focus();
    if (!EXT.catalog?.checked) await refreshExtensions();
  });
  $('#extensionsClose').addEventListener('click', closeExtensions);
  $('#extensionsModal').addEventListener('click', (e) => { if (e.target === $('#extensionsModal')) closeExtensions(); });
  $('#extensionsRefresh').addEventListener('click', refreshExtensions);
  $$('#remoteRole button').forEach((b) => b.addEventListener('click', () => setRemoteRole(b.dataset.role)));
  $('#rActivate').addEventListener('click', activateRemote);
  $('#rPair').addEventListener('click', async () => {
    const response = await extensionAction('pair');
    if (response) { $('#rPairCode').value = response.result.code; $('#rPairBox').hidden = false; }
  });
  $('#rCopyPair').addEventListener('click', async () => {
    const el = $('#rPairCode');
    try { await navigator.clipboard.writeText(el.value); }
    catch (_) { el.focus(); el.select(); document.execCommand('copy'); }
    toast('info', 'Code copié', 'Confidentiel : transmettez-le uniquement au PC voulu.');
  });
  $('#rStart').addEventListener('click', () => extensionAction('start'));
  $('#rStop').addEventListener('click', () => {
    const detail = EXT.remote.role === 'worker' ? 'Le calcul sera mis en pause et son avancement conservé.' : 'Le relais sera arrêté. Un calcul déjà lancé sur l’autre PC peut continuer et conserver son résultat jusqu’au retour du relais.';
    if (confirm('Désactiver le service et son démarrage automatique ?\n' + detail)) extensionAction('stop');
  });
  $('#rLogs').addEventListener('click', () => extensionAction('open_logs'));
  $('#rStartup').addEventListener('change', (e) => extensionAction('startup', { enabled: e.target.checked }));
  $('#remoteTarget').addEventListener('change', (e) => extensionAction('select', { worker: e.target.value }));
  setRemoteRole(EXT.remote.role || 'controller');
}

function closeExtensions() {
  $('#extensionsModal').hidden = true;
  // Un code d'appairage ne reste pas dans le DOM après fermeture.
  $('#rPairCode').value = '';
  $('#rJoinCode').value = '';
  $('#rPairBox').hidden = true;
  $('#extensionsBtn').focus();
}

async function refreshExtensions() {
  $('#extensionsRefresh').disabled = true;
  $('#extensionsRefresh').textContent = 'Vérification…';
  try { updateExtensions(await API.extensions_refresh(), EXT.remote); }
  catch (e) { toast('warn', 'Catalogue inaccessible', String(e)); }
  finally { $('#extensionsRefresh').disabled = false; $('#extensionsRefresh').textContent = 'Actualiser'; }
}

function updateExtensions(catalog, remote) {
  if (catalog) EXT.catalog = catalog;
  if (remote) {
    if (remote.role && remote.role !== EXT.remote.role) EXT.hydrated = false;
    EXT.remote = remote;
  }
  if (!$('#extensionList')) return;
  const data = EXT.catalog || { items: [] };
  const stamp = JSON.stringify([data.items, data.busy]);
  if (stamp !== EXT.rendered) {
    EXT.rendered = stamp;
    $('#extensionList').innerHTML = (data.items || []).map((item) => `
      <article class="ext-card"><span class="remote-orb">↗</span><h3>${esc(item.name)}</h3>
        <span class="ext-version">${item.installed ? `INSTALLÉE · v${esc(item.installed)}` : `v${esc(item.version)} · GRATUITE`}</span>
        <p>${esc(item.description)}</p><div class="ext-actions">
        ${!item.installed || item.update ? `<button class="btn primary sm" data-install="${esc(item.id)}" ${data.busy ? 'disabled' : ''}>${data.busy === item.id ? 'Installation…' : item.update ? 'Mettre à jour' : 'Installer'}</button>` : ''}
        ${item.installed ? `<button class="btn ghost sm" data-remove="${esc(item.id)}" ${data.busy ? 'disabled' : ''}>Désinstaller</button>` : ''}
        </div></article>`).join('');
    $$('[data-install]', $('#extensionList')).forEach((b) => b.addEventListener('click', async () => {
      const item = data.items.find((x) => x.id === b.dataset.install);
      if (!confirm(`Installer l'extension officielle « ${item.name} » ?\n\nElle contient du code et peut utiliser :\n${(item.permissions || []).map((p) => '• ' + p).join('\n')}\n\nL'installation seule n'active ni le réseau ni le démarrage automatique.`)) return;
      b.disabled = true; b.textContent = 'Téléchargement…';
      try { const ok = await API.extension_install(item.id); if (!ok) { b.disabled = false; b.textContent = 'Installer'; } }
      catch (e) { b.disabled = false; toast('error', 'Installation impossible', String(e)); }
    }));
    $$('[data-remove]', $('#extensionList')).forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Désinstaller cette extension ?\nLe service et son démarrage automatique seront désactivés. Les travaux non livrés doivent d’abord être terminés ou abandonnés.')) return;
      await API.extension_remove(b.dataset.remove);
    }));
  }
  $('#extensionCatalogNote').textContent = data.error || 'Catalogue officiel uniquement. Chaque téléchargement est vérifié par SHA-256 avant installation.';
  const r = EXT.remote;
  $('#remoteSetup').hidden = !r.installed;
  $('#extensionWelcome').hidden = !!r.installed;
  $('#remoteDestination').hidden = !r.installed || r.role !== 'controller';
  $('#privacyLabel').textContent = r.selected ? 'Vos PC · gratuit' : '100 % local · gratuit';
  if (!r.installed) { EXT.hydrated = false; return; }
  if (!EXT.hydrated) {
    $('#rPort').value = r.port || 38443;
    $('#rUrl').value = r.role === 'controller' ? (r.url || '') : '';
    $('#rName').value = r.name || '';
    $('#rWorkerUrl').value = r.role === 'worker' ? (r.url || '') : '';
    $('#rSetupStartup').checked = !!r.startup;
    setRemoteRole(r.role || 'controller');
    EXT.hydrated = true;
  }
  renderRemoteGpus();
  $('#rStatus').textContent = r.running ? (r.role === 'controller' ? 'Relais actif' : 'Service actif') : r.role ? 'Service arrêté' : 'À configurer';
  $('#rStatus').classList.toggle('online', !!r.running);
  $('#rService').hidden = !r.role;
  $('#rServiceMessage').textContent = r.error || r.message || 'Service arrêté';
  $('#rStart').hidden = !!r.running;
  $('#rStop').hidden = !r.running;
  $('#rStartup').checked = !!r.startup;
  $('#rPair').hidden = r.role !== 'controller' || !r.running || EXT.role !== 'controller';
  $('#rLanIp').textContent = (r.local_ips || []).length ? `Adresse(s) détectée(s) : ${r.local_ips.join(', ')}.` : '';
  $('#rActive').hidden = !r.active;
  if (r.active) $('#rActive').textContent = `${r.active.name || ''} · ${r.message || 'Calcul en cours'}`;
  const workerStamp = JSON.stringify([(r.workers || []).map(({ id, name, online, gpus, gpu }) => ({ id, name, online, gpus, gpu })), r.selected, r.role]);
  if (workerStamp !== EXT.workers) {
    EXT.workers = workerStamp;
    const list = r.workers || [];
    const sel = $('#remoteTarget');
    sel.innerHTML = '<option value="">Ce PC · traitement local</option>' + list.map((w) => `<option value="${esc(w.id)}">${esc(w.name)}${w.online ? ' · connecté' : ' · hors ligne'}</option>`).join('');
    sel.value = r.selected || '';
    const worker = list.find((w) => w.id === r.selected);
    $('#remoteTargetNote').textContent = worker
      ? `Calcul et aperçu sur ${worker.name}. GPU : ${(worker.gpus || []).map((g) => g.name).join(' + ') || 'automatique sur l’autre PC'}. Destination figée au lancement.`
      : 'Traitement local inchangé. La destination, comme les réglages, est figée au lancement.';
    $('#rWorkers').innerHTML = r.role !== 'controller' ? '' : '<div class="ext-eyebrow" style="margin-top:24px">PC APPAIRÉS</div>' + (!list.length ? '<p class="help">Créez un code, puis collez-le dans l’extension sur le PC de calcul.</p>' : list.map((w) => `
      <div class="worker-card"><div><b><span class="dot ${w.online ? 'online' : ''}"></span>${esc(w.name)}</b><small>${w.online ? 'Connecté' : 'Hors ligne · reprise à la reconnexion'} · ${esc((w.gpus || []).map((g) => g.name).join(' + ') || 'GPU automatique')}</small></div>
      <div class="worker-actions"><button class="btn ${r.selected === w.id ? 'primary' : 'soft'} sm" data-worker="${esc(w.id)}">${r.selected === w.id ? 'Sélectionné' : 'Utiliser'}</button><button class="icon-btn" data-revoke="${esc(w.id)}" title="Révoquer cet appairage">×</button></div></div>`).join(''));
    $$('[data-worker]').forEach((b) => b.addEventListener('click', () => extensionAction('select', { worker: b.dataset.worker })));
    $$('[data-revoke]').forEach((b) => b.addEventListener('click', () => {
      if (confirm('Révoquer ce PC ? Il devra être appairé à nouveau.\nTerminez d’abord la livraison et le nettoyage de ses travaux.')) extensionAction('revoke', { id: b.dataset.revoke });
    }));
  }
}

function setRemoteRole(role) {
  EXT.role = role === 'worker' ? 'worker' : 'controller';
  $$('#remoteRole button').forEach((b) => b.classList.toggle('on', b.dataset.role === EXT.role));
  $('#rControllerForm').hidden = EXT.role !== 'controller';
  $('#rWorkerForm').hidden = EXT.role !== 'worker';
  $('#rActivate').textContent = EXT.role === 'controller' ? 'Activer le relais' : EXT.remote.role === 'worker' ? 'Enregistrer ce PC de calcul' : 'Appairer et activer le calcul';
  $('#rPair').hidden = EXT.role !== 'controller' || EXT.remote.role !== 'controller' || !EXT.remote.running;
  $('#rPairBox').hidden = true;
  $('#rPairCode').value = '';
  renderRemoteGpus(EXT.hydrated ? $('#rGpu').value : (EXT.remote.gpu || 'auto'));
}

function renderRemoteGpus(wanted) {
  const gpu = $('#rGpu');
  const selected = wanted || gpu.value || 'auto';
  const list = (S.gpus || []).length ? S.gpus : (EXT.remote.gpus || []);
  const stamp = JSON.stringify([list, selected]);
  if (gpu.dataset.stamp === stamp) return;
  gpu.dataset.stamp = stamp;
  gpu.innerHTML = '<option value="auto">Automatique</option>' + list.map((g) => `<option value="${esc(g.id)}">${esc(g.id)} · ${esc(g.name)}</option>`).join('');
  if (selected !== 'auto' && !list.some((g) => String(g.id) === selected)) {
    // Garder le GPU enregistré même si sa détection n'est pas encore terminée.
    gpu.add(new Option(`GPU ${selected} · enregistré`, selected));
  }
  gpu.value = selected;
}

async function activateRemote() {
  if (!$('#rConsent').checked) { toast('warn', 'Autorisation nécessaire', 'Confirmez que vous autorisez ce PC à participer.'); return; }
  let values = { consent: true, startup: $('#rSetupStartup').checked };
  if (EXT.role === 'controller') {
    const port = Number($('#rPort').value);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) { toast('warn', 'Port invalide', 'Choisissez un port entier entre 1024 et 65535, dans votre plage Freebox.'); return; }
    values = { ...values, port, url: $('#rUrl').value.trim() };
  } else values = { ...values, code: $('#rJoinCode').value.trim(), name: $('#rName').value.trim(), gpu: $('#rGpu').value, url: $('#rWorkerUrl').value.trim() };
  const action = EXT.role === 'controller' ? 'configure_controller' : EXT.remote.role === 'worker' && !values.code ? 'update_worker' : 'join';
  const response = await extensionAction(action, values);
  if (response) {
    $('#rJoinCode').value = ''; $('#rConsent').checked = false;
    toast('success', EXT.role === 'controller' ? 'Relais activé' : 'PC appairé', EXT.role === 'controller' ? 'Configurez le port sur la Freebox, puis créez un code pour l’autre PC.' : 'Vous pouvez fermer la fenêtre : le service reste actif en arrière-plan.');
  }
}

async function extensionAction(action, values = {}) {
  if (EXT.busy) return null;
  EXT.busy = true;
  const buttons = $$('#remoteSetup button');
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const response = await API.remote_action(action, values);
    if (!response.ok) { showError('Calcul distant', response.error || 'Action impossible.'); return null; }
    updateExtensions(EXT.catalog, response.remote);
    if (action === 'startup' || action === 'stop') $('#rSetupStartup').checked = !!response.remote.startup;
    return response;
  } catch (e) { showError('Calcul distant', String(e)); return null; }
  finally { EXT.busy = false; $$('#remoteSetup button').forEach((b) => { b.disabled = false; }); }
}

/* Démo navigateur uniquement : aucune connexion ni aucun service réel. */
if (window.MockApi) {
  (() => {
    const mock = window.MockApi;
    let installed = false;
    let remote = { installed: false, role: '', running: false, startup: false, workers: [], selected: '' };
    const catalog = () => ({ checked: true, busy: '', error: '', items: [{ id: 'remote-compute', name: 'Calcul à distance', version: '1.0.0', installed: installed ? '1.0.0' : '', available: true,
      description: 'Confiez vos vidéos au GPU d’un autre PC, suivez le calcul et récupérez le résultat automatiquement.', permissions: ['Réseau et médias choisis', 'Calcul GPU en arrière-plan', 'Démarrage Windows facultatif'] }] });
    const init = mock.init.bind(mock), poll = mock.poll.bind(mock);
    mock.init = async () => ({ ...await init(), extensions: catalog(), remote });
    mock.poll = async () => ({ ...await poll(), extensions: catalog(), remote });
    mock.extensions_refresh = async () => catalog();
    mock.extension_install = async () => { installed = true; remote = { ...remote, installed: true }; return true; };
    mock.extension_remove = async () => { installed = false; remote = { ...remote, installed: false }; return true; };
    mock.remote_action = async (action, values) => {
      if (action === 'configure_controller') remote = { ...remote, ...values, role: 'controller', enabled: true, running: true, message: 'Mode démo · aucun serveur réel', local_ips: [], workers: [{ id: 'demo-worker', name: 'PC de calcul · démo', online: true, gpu: 'auto', gpus: [{ id: '0', name: 'GPU distant (démonstration)' }] }] };
      if (action === 'join' || action === 'update_worker') remote = { ...remote, role: 'worker', enabled: true, running: true, name: values.name, url: values.url, gpu: values.gpu, startup: values.startup, selected: '', workers: [], message: 'Mode démo · aucun calcul distant réel' };
      if (action === 'select') remote = { ...remote, selected: values.worker };
      if (action === 'startup') remote = { ...remote, startup: values.enabled };
      if (action === 'start') remote = { ...remote, running: true };
      if (action === 'stop') remote = { ...remote, running: false, startup: false };
      if (action === 'revoke') remote = { ...remote, workers: remote.workers.filter((w) => w.id !== values.id), selected: '' };
      return { ok: true, remote, result: action === 'pair' ? { code: 'DÉMONSTRATION UNIQUEMENT — aucun code d’appairage réel.', expires: Date.now() / 1000 + 600 } : remote };
    };
  })();
}
