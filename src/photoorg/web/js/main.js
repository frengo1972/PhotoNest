// App shell: sidebar, routing, selection actions, dialogs, background scan progress, undo.

import { api, q } from './api.js';
import { esc, icon, num, plural } from './format.js';
import { Gallery } from './gallery.js';
import { Lightbox } from './lightbox.js';
import {
  renderCaptions, renderDuplicates, renderEvents, renderIssues, renderJobPanel, renderNas, renderOverview, renderPeople,
  renderSources, renderTrash, renderVariants,
} from './views.js';

const $ = (s) => document.querySelector(s);
const main = $('#main');
const galleryView = $('#view-galleria');
const otherView = $('#view-other');
const lightbox = new Lightbox();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ctx = {
  state: null,
  user: null,
  current: null,
  lightbox,
  viewState: {},
  leaveHandlers: [],
  onLeave: (fn) => ctx.leaveHandlers.push(fn),
  refresh,
  toast,
  undoable: (text, undo) => undoable(text, undo),
  markPending: () => { $('#pending').hidden = false; },
  galleryChanged: () => { galleryStale = true; },
  ask: (opts) => ask(opts),
  confirm: (title, text, confirmLabel) => ask({ title, text, confirm: confirmLabel }),
  startJob: (url, body) => startJob(url, body),
  gotoDate: (date) => {
    pendingDate = date;
    galleryQuery = '';
    galleryStale = true;
    if (location.hash === '#/galleria') route(); else location.hash = '#/galleria';
  },
  searchFor: (query) => {
    galleryQuery = query;
    galleryStale = true;
    galleryScroll = 0;
    if (location.hash === '#/galleria') route(); else location.hash = '#/galleria';
  },
  loadNames: () => loadNames(),
};
let pendingDate = null;
let galleryQuery = '';

let galleryUser = null;
let galleryStale = false;
let galleryScroll = 0;
// Favourites and exclusions made in the viewer opened on the gallery: applied to it without reloading the
// archive, when the viewer closes (or at once, for an edit that completes after closing).
const viewerEdits = { removed: new Set(), fav: new Map() };
let galleryEntries = new WeakSet();

/** Viewer entry for a gallery item; an item not loaded yet gets its details when the viewer reaches it. */
const toEntry = (it) => ({
  item: it.i, v: it.v, w: it.w, h: it.h, kind: it.k, date: it.d ?? it.day, asset: it.a, fav: Boolean(it.f), excluded: false,
  caption: it.al, version: it.r ? { id: it.r, v: it.rv } : null,
});
const VIEWER_PAGE = 60; // photos on each side whose details are fetched with the one shown

const gallery = new Gallery(galleryView, main, {
  onOpen: (idx) => {
    const items = gallery.items;
    const entries = items.map(toEntry);
    const indexOf = new Map(entries.map((e, i) => [e, i]));
    galleryEntries = new WeakSet(entries);
    lightbox.open(entries, idx, {
      tile: (e) => gallery.tileFor(indexOf.get(e)),
      reveal: (e) => gallery.reveal(indexOf.get(e)),
    }, {
      removeWhen: (e) => e.excluded,
      fill: async (e) => {
        const at = indexOf.get(e);
        const lo = Math.max(0, at - VIEWER_PAGE);
        const hi = Math.min(items.length, at + VIEWER_PAGE + 1);
        await gallery.fetchDetails(items.slice(lo, hi));
        for (let j = lo; j < hi; j++) if (entries[j].item === undefined) Object.assign(entries[j], toEntry(items[j]));
      },
    });
  },
  onSelect: (items, { mode }) => renderSelectionBar(items, mode),
  bottomInset: () => ($('#selbar').hidden ? 0 : $('#selbar').offsetHeight + 22),
  onSearch: (query) => {
    galleryQuery = query;
    galleryScroll = 0;
    reloadGallery().then(() => { main.scrollTop = 0; });
  },
});

// ---------- data ----------

async function refresh() {
  const s = await api.get(`/api/state?${q({ user: ctx.user })}`);
  ctx.state = s;
  ctx.user = s.user;
  renderSidebar(s);
  return s;
}

async function loadNames() {
  if (!ctx.user) return;
  const names = await api.get(`/api/names?${q({ user: ctx.user })}`);
  for (const [kind, list] of Object.entries(names)) {
    $(`#names-${kind}`).innerHTML = list.map((n) => `<option value="${esc(n)}"></option>`).join('');
  }
}

function renderSidebar(s) {
  for (const el of document.querySelectorAll('[data-count]')) {
    const n = s.counts[el.dataset.count] ?? 0;
    el.textContent = n ? num(n) : '';
    el.classList.toggle('attention', el.classList.contains('attention-when') && n > 0);
  }
  $('.user-switch').hidden = s.users.length < 2;
  $('#user').innerHTML = s.users.map((u) => `<option value="${esc(u)}" ${u === s.user ? 'selected' : ''}>Archivio di ${esc(u)}</option>`).join('');
  renderJobMini(s.job);
}

function renderJobMini(job) {
  const box = $('#job-mini');
  box.hidden = !job.running;
  $('#scan-btn').disabled = job.running;
  if (!job.running) return;
  box.querySelector('.job-mini-phase').textContent = job.phase ?? 'Avvio';
  const bar = box.querySelector('.bar');
  bar.classList.toggle('indeterminate', !job.total);
  bar.firstElementChild.style.width = job.total ? `${(100 * job.done) / job.total}%` : '';
}

// ---------- gallery ----------

const emptyArchive = () => `<div class="empty">
  <h2>L’archivio è vuoto</h2>
  <p>Metti nella cartella income gli zip di Google Takeout o la copia di un telefono, poi aggiorna l’archivio.</p>
  <div class="actions">
    <button class="btn" type="button" data-action="open-income">${icon('i-folder')}Apri la cartella income</button>
    <button class="btn btn-primary" type="button" data-action="scan">${icon('i-refresh')}Aggiorna archivio</button>
  </div></div>`;

async function loadGallery({ keep = false } = {}) {
  galleryUser = ctx.user;
  galleryStale = false;
  lightbox.invalidate();
  if (!ctx.user) { gallery.empty(emptyArchive()); return; }
  if (galleryQuery) {
    const { items } = await api.get(`/api/search?${q({ user: ctx.user, q: galleryQuery })}`);
    if (items.length) {
      gallery.setData(items, { results: `${plural(items.length, 'risultato', 'risultati')} per «${galleryQuery}»`, query: galleryQuery, keep });
    } else {
      gallery.empty(`<div class="empty"><h2>Nessun risultato</h2>
        <p>Nessuna foto corrisponde a «${esc(galleryQuery)}». Prova con un luogo, una persona, un oggetto o un momento, per esempio “mare”, “compleanno”, “neve”.</p></div>`,
      { results: '', query: galleryQuery });
    }
    return;
  }
  const index = await api.get(`/api/timeline/index?${q({ user: ctx.user })}`);
  if (index.a.length) gallery.setIndex(index, { keep }); else gallery.empty(emptyArchive());
}

/** Reload after edits, keeping the scroll position, the selection and the cursor. */
async function reloadGallery() {
  const visible = ctx.current === 'galleria';
  const scroll = visible ? main.scrollTop : galleryScroll;
  await loadGallery({ keep: true });
  if (visible) { gallery.shown(); main.scrollTop = scroll; } else galleryScroll = scroll;
}

function applyViewerEdits() {
  for (const [asset, value] of viewerEdits.fav) gallery.patch([asset], { f: value ? 1 : 0 });
  if (viewerEdits.removed.size) gallery.remove([...viewerEdits.removed]);
  viewerEdits.fav.clear();
  viewerEdits.removed.clear();
}

lightbox.onClose = () => {
  if (ctx.current !== 'galleria') return;
  gallery.focusCursor();
  if (galleryStale) reloadGallery(); else applyViewerEdits();
};
lightbox.onSearch = (query) => ctx.searchFor(query);
lightbox.onUndo = () => runUndo();
lightbox.onChange = (change) => {
  if (change.error) { toast(change.error); return; }
  const fromGallery = Boolean(change.entry) && galleryEntries.has(change.entry);
  if (fromGallery && change.favorite !== undefined) viewerEdits.fav.set(change.entry.asset, change.favorite);
  else if (fromGallery && change.exclude !== undefined) viewerEdits.removed[change.exclude ? 'add' : 'delete'](change.entry.asset);
  else galleryStale = true;
  if (fromGallery && lightbox.el.hidden && !galleryStale) applyViewerEdits();
  if (change.tags) loadNames();
  if (change.rebuilt) { refresh().then(route); return; }
  if (change.version) {
    const { entry, version, replaced, previous } = change;
    undoable('Ritocco salvato come nuova versione: l’originale resta intatto', async () => {
      await api.post(`/api/versions/${version.id}/delete`, { deleted: true });
      if (replaced) await api.post(`/api/versions/${replaced}/delete`, { deleted: false });
      lightbox.restore(entry, previous);
    });
  }
  if (change.version === null) undoable('Tornata all’originale', change.undo);
  if (change.exclude !== undefined) {
    refresh();
    const { entry } = change;
    undoable(change.exclude ? 'Messa nel cestino' : 'Rimessa nell’archivio', async () => {
      await api.post('/api/assets/edit', { ids: [entry.asset], exclude: !change.exclude });
      entry.excluded = !change.exclude;
      change.undo?.();
      viewerEdits.removed.delete(entry.asset);
    });
  }
};

// ---------- selection bar ----------

function renderSelectionBar(items, mode) {
  const bar = $('#selbar');
  bar.hidden = !items.length && !mode;
  if (bar.hidden) return;
  if (!items.length) {
    bar.innerHTML = `
      <span class="count-label hint-label">Clicca le foto o trascina un’area per selezionarle</span>
      <button class="btn btn-quiet" type="button" data-sel="all" title="Seleziona tutto (Ctrl+A)">Tutte</button>
      <button class="btn btn-quiet" type="button" data-action="keys" title="Scorciatoie da tastiera (?)">${icon('i-keyboard')}Tastiera</button>
      <button class="btn" type="button" data-sel="done" title="Esci dalla selezione (Esc)">Fine</button>`;
    return;
  }
  const allFav = items.every((it) => it.f);
  bar.innerHTML = `
    <span class="count-label">${plural(items.length, 'selezionata', 'selezionate')}</span>
    <button class="btn" type="button" data-sel="fav" title="Preferite (F)">${icon('i-star')}${allFav ? 'Togli preferite' : 'Preferite'}</button>
    <button class="btn" type="button" data-sel="album">${icon('i-album')}Album</button>
    <button class="btn" type="button" data-sel="person">${icon('i-person')}Persone</button>
    <button class="btn" type="button" data-sel="keyword">${icon('i-tag')}Etichette</button>
    <button class="btn" type="button" data-sel="date">${icon('i-calendar')}Data</button>
    <button class="btn" type="button" data-sel="describe" title="Scrivi la descrizione con l’AI">${icon('i-caption')}Descrivi</button>
    <span class="divider"></span>
    <button class="btn danger" type="button" data-sel="exclude" title="Metti nel cestino (Canc)">${icon('i-trash')}Escludi</button>
    <button class="btn btn-quiet" type="button" data-sel="all" title="Seleziona tutto (Ctrl+A)">Tutte</button>
    <button class="btn btn-quiet" type="button" data-sel="invert" title="Inverti la selezione (Ctrl+I)">Inverti</button>
    <button class="btn btn-quiet" type="button" data-sel="done" title="Annulla la selezione (Esc)" aria-label="Annulla la selezione">${icon('i-close')}</button>`;
}

const TAG_DIALOG = {
  album: { title: 'Aggiungi a un album', label: 'Nome dell’album', confirm: 'Aggiungi' },
  person: { title: 'Indica le persone', label: 'Nomi', hint: 'Separa più nomi con una virgola.', confirm: 'Aggiungi' },
  keyword: { title: 'Aggiungi etichette', label: 'Etichette', hint: 'Per esempio: mare, compleanno. Separa con una virgola.', confirm: 'Aggiungi' },
};

$('#selbar').addEventListener('click', (e) => {
  const action = e.target.closest('[data-sel]')?.dataset.sel;
  if (action === 'done') gallery.exitSelection();
  else if (action === 'all') gallery.selectAll();
  else if (action === 'invert') gallery.invertSelection();
  else if (action) runAction(action, gallery.selection());
});

/** An edit on photos of the gallery: from the selection bar, or a key on the selection / the cursor. */
async function runAction(action, items) {
  if (!items.length) return;
  const ids = items.map((it) => it.a);
  const n = plural(ids.length, 'foto', 'foto');
  try {
    if (action === 'fav') {
      const value = !items.every((it) => it.f);
      await api.post('/api/assets/edit', { ids, favorite: value });
      gallery.patch(ids, { f: value ? 1 : 0 });
      const was = items.filter((it) => it.f).map((it) => it.a);
      undoable(value ? `${n} tra le preferite` : `${n} tolte dalle preferite`, async () => {
        const wasNot = ids.filter((id) => !was.includes(id));
        if (was.length) await api.post('/api/assets/edit', { ids: was, favorite: true });
        if (wasNot.length) await api.post('/api/assets/edit', { ids: wasNot, favorite: false });
      });
      return;
    } else if (TAG_DIALOG[action]) {
      const d = TAG_DIALOG[action];
      const value = await ask({ ...d, text: `Per ${n}.`, list: `names-${action}` });
      if (!value) return;
      const add = value.split(',').map((s) => s.trim()).filter(Boolean);
      await api.post('/api/assets/tags', { ids, kind: action, add });
      loadNames();
      toast(`Aggiunto a ${n}`);
    } else if (action === 'date') {
      const value = await ask({
        title: 'Imposta la data', text: `La stessa data e ora per ${n}.`, label: 'Data e ora dello scatto',
        type: 'datetime-local', value: (items[0].d ?? '').replace(' ', 'T'), confirm: 'Imposta',
      });
      if (!value) return;
      await api.post('/api/assets/edit', { ids, date_local: value.replace('T', ' ').padEnd(19, ':00').slice(0, 19) });
      toast(`Data impostata su ${n}`);
    } else if (action === 'describe') {
      // one photo: describe it again even if it has a caption; several: only the ones that have none
      await startJob('/api/ai/describe', { ids, redo: ids.length === 1 });
      return;
    } else if (action === 'exclude') {
      await api.post('/api/assets/edit', { ids, exclude: true });
      gallery.remove(ids); // at once, without reloading the archive
      undoable(`${n} nel cestino`, () => api.post('/api/assets/edit', { ids, exclude: false }));
      refresh(); // sidebar counts
      return;
    }
    await refresh();
    await reloadGallery();
  } catch (err) {
    toast(err.message);
  }
}

function showKeys() {
  const d = $('#keys');
  if (!d.open) d.showModal();
}

// ---------- dialog ----------

/**
 * Small modal. With `label` it asks for one value and resolves with it (null if cancelled; "" only
 * with allowEmpty). Without `label` it is a confirmation and resolves true / false.
 */
function ask({ title, text = '', label = null, hint = '', type = 'text', list = '', value = '', confirm, allowEmpty = false }) {
  const d = $('#sheet');
  d.querySelector('h2').textContent = title;
  d.querySelector('.sheet-text').textContent = text;
  d.querySelector('.sheet-body').innerHTML = label === null ? '' : `<div class="field">
    <label for="sheet-input">${esc(label)}</label>
    <input id="sheet-input" type="${type}" ${type === 'datetime-local' ? 'step="1"' : ''} ${list ? `list="${list}"` : ''} value="${esc(value)}" autocomplete="off">
    ${hint ? `<small>${esc(hint)}</small>` : ''}</div>`;
  const ok = d.querySelector('button[value="ok"]');
  ok.textContent = confirm;
  const input = d.querySelector('#sheet-input');
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); d.close('ok'); } // Enter confirms (not the first button)
  });
  d.returnValue = '';
  d.showModal();
  (input ?? ok).focus();
  return new Promise((resolve) => {
    d.addEventListener('close', () => {
      const confirmed = d.returnValue === 'ok';
      if (!input) { resolve(confirmed); return; }
      const v = input.value.trim();
      resolve(confirmed && (v || allowEmpty) ? v : null);
    }, { once: true });
  });
}

// ---------- routing ----------

const SCREENS = {
  panoramica: renderOverview,
  eventi: renderEvents,
  duplicati: renderDuplicates,
  varianti: renderVariants,
  verifica: renderIssues,
  cestino: renderTrash,
  fonti: renderSources,
  nas: renderNas,
  persone: renderPeople,
  didascalie: renderCaptions,
};

async function route() {
  const name = location.hash.replace(/^#\/?/, '') || 'panoramica';
  for (const a of document.querySelectorAll('.nav a')) {
    if (a.dataset.route === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (ctx.current === 'galleria' && name !== 'galleria') {
    galleryScroll = main.scrollTop;
    gallery.exitSelection();
  }
  ctx.leaveHandlers.splice(0).forEach((fn) => fn());
  ctx.current = name;
  try {
    if (name === 'galleria' || !SCREENS[name]) {
      otherView.hidden = true;
      galleryView.hidden = false;
      if (galleryUser !== ctx.user || galleryStale) await loadGallery();
      gallery.shown();
      main.scrollTop = galleryScroll;
      if (pendingDate) { gallery.scrollToDate(pendingDate); pendingDate = null; }
    } else {
      galleryView.hidden = true;
      otherView.hidden = false;
      main.scrollTop = 0;
      await SCREENS[name](otherView, ctx);
    }
  } catch (err) {
    toast(`Impossibile caricare: ${err.message}`);
  }
}

// ---------- feedback ----------

let toastTimer;
function toast(text, { action, onAction } = {}) {
  const t = $('#toast');
  t.replaceChildren(Object.assign(document.createElement('span'), { textContent: text }));
  if (action) {
    const b = Object.assign(document.createElement('button'), { type: 'button', className: 'btn btn-quiet', textContent: action });
    b.addEventListener('click', async () => {
      t.hidden = true;
      try { await onAction(); } catch (err) { toast(err.message); }
    });
    t.append(b);
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, action ? 7000 : 4000);
}

// ---------- undo ----------

const UNDO_LIMIT = 30;
const undoStack = [];

/** Tell what was done and keep a way back: the toast offers Annulla, Ctrl+Z takes the latest actions back one by one. */
function undoable(text, undo) {
  const item = { text, undo };
  undoStack.push(item);
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  toast(text, { action: 'Annulla', onAction: () => runUndo(item) });
}

async function runUndo(item = undoStack[undoStack.length - 1]) {
  if (!item) { toast('Non c’è niente da annullare'); return; }
  undoStack.splice(undoStack.indexOf(item), 1);
  try {
    await item.undo();
    await refresh();
  } catch (err) {
    toast(err.message);
    return;
  }
  galleryStale = true;
  lightbox.invalidate();
  if (lightbox.el.hidden) await (ctx.current === 'galleria' ? reloadGallery() : route());
  toast(`Annullato: ${item.text.charAt(0).toLowerCase()}${item.text.slice(1)}`);
}

// ---------- scan ----------

let polling = false;
let jobKind = 'scan';
const JOB_DONE = {
  scan: ['Archivio aggiornato', 'Aggiornamento non riuscito: i dettagli sono in Fonti'],
  export: ['Archivio sul NAS aggiornato', 'Copia sul NAS non riuscita: i dettagli sono in Archivio NAS'],
  ai: ['Analisi completata: volti ed etichette pronti', 'Analisi non riuscita: i dettagli sono in Persone'],
  caption: ['Descrizioni pronte', 'Descrizioni non riuscite: i dettagli sono in Didascalie'],
};

async function pollJob() {
  if (polling) return;
  polling = true;
  let job;
  try {
    for (;;) {
      job = await api.get('/api/job');
      ctx.state.job = job;
      renderJobMini(job);
      if (['fonti', 'nas', 'persone', 'didascalie'].includes(ctx.current)) renderJobPanel($('.jobpanel-slot'), job);
      if (!job.running) break;
      await sleep(700);
    }
    await refresh();
    galleryStale = true;
    loadNames();
    await route();
    const [ok, failed] = JOB_DONE[jobKind] ?? JOB_DONE.scan;
    toast(job.error ? failed : ok);
  } finally {
    polling = false;
  }
}

/** Start a background operation (scan or export) and follow its progress. */
async function startJob(url, body) {
  jobKind = url.includes('export') ? 'export' : url.includes('/describe') ? 'caption' : url.includes('/ai/') ? 'ai' : 'scan';
  try {
    ctx.state.job = await api.post(url, body);
    renderJobMini(ctx.state.job);
    if (['fonti', 'panoramica', 'nas', 'persone', 'didascalie'].includes(ctx.current)) await route();
    pollJob();
  } catch (err) {
    toast(err.message);
  }
}

const startScan = () => startJob('/api/scan');

// ---------- global events ----------

document.addEventListener('click', (e) => {
  const action = e.target.closest('[data-action]')?.dataset.action;
  if (action === 'scan') startScan();
  if (action === 'open-income') api.post('/api/open-income').catch((err) => toast(err.message));
  if (action === 'keys') showKeys();
  if (e.target.closest('[data-job-cancel]')) {
    e.target.closest('button').disabled = true;
    api.post('/api/job/cancel').catch((err) => toast(err.message));
  }
});
$('#scan-btn').addEventListener('click', startScan);

document.addEventListener('keydown', (e) => {
  if (!lightbox.el.hidden || $('#sheet').open || $('#keys').open || e.target.closest('input, textarea, select')) return;
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'z') { e.preventDefault(); runUndo(); return; }
  if (ctx.current !== 'galleria') return;
  const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
  if (plain && e.key === '?') { e.preventDefault(); showKeys(); return; }
  if (plain && (e.key === 'Delete' || e.key.toLowerCase() === 'f')) {
    e.preventDefault();
    if (!e.repeat) runAction(e.key === 'Delete' ? 'exclude' : 'fav', gallery.targets());
    return;
  }
  gallery.onKey(e);
});

$('#apply-btn').addEventListener('click', async (e) => {
  e.target.disabled = true;
  try {
    await api.post('/api/rebuild');
    $('#pending').hidden = true;
    await refresh();
    galleryStale = true;
    await route();
    toast('Scelte applicate');
  } catch (err) {
    toast(err.message);
  } finally {
    e.target.disabled = false;
  }
});

$('#user').addEventListener('change', async (e) => {
  ctx.user = e.target.value;
  ctx.viewState = {};
  galleryScroll = 0;
  await refresh();
  loadNames();
  await route();
});

addEventListener('hashchange', route);
setInterval(() => api.get('/api/ping').catch(() => {}), 20000); // keeps the local server alive while the window is open

/** Development mode (`photoorg dev`): reload the page when the server restarts or a web file changes. */
async function watchDevBuild() {
  let known = null;
  for (;;) {
    try {
      const { build } = await api.get('/api/dev');
      if (known && build !== known && lightbox.el.hidden && !$('#sheet').open) location.reload();
      known ??= build;
    } catch {
      // server restarting: keep polling until it answers again
    }
    await sleep(1500);
  }
}

(async () => {
  try {
    await refresh();
  } catch (err) {
    toast(`Server non raggiungibile: ${err.message}`);
    return;
  }
  loadNames();
  await route();
  if (ctx.state.job.running) pollJob();
  if (ctx.state.dev) watchDevBuild();
})();
