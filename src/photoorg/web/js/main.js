// App shell: sidebar, routing, selection actions, dialogs, background scan progress, undo.

import { api, q } from './api.js';
import { esc, icon, num, plural } from './format.js';
import { Gallery } from './gallery.js';
import { Lightbox } from './lightbox.js';
import {
  renderDuplicates, renderEvents, renderIssues, renderJobPanel, renderNas, renderOverview, renderSources, renderTrash,
  renderVariants,
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
  markPending: () => { $('#pending').hidden = false; },
  galleryChanged: () => { galleryStale = true; },
  ask: (opts) => ask(opts),
  confirm: (title, text, confirmLabel) => ask({ title, text, confirm: confirmLabel }),
  startJob: (url) => startJob(url),
  gotoDate: (date) => {
    pendingDate = date;
    if (location.hash === '#/galleria') route(); else location.hash = '#/galleria';
  },
};
let pendingDate = null;

let galleryUser = null;
let galleryStale = false;
let galleryScroll = 0;

const gallery = new Gallery(galleryView, main, {
  onOpen: (idx) => {
    const entries = gallery.entries;
    const indexOf = new Map(entries.map((e, i) => [e, i]));
    lightbox.open(entries, idx, {
      tile: (e) => gallery.tileFor(indexOf.get(e)),
      reveal: (e) => gallery.reveal(indexOf.get(e)),
    }, { removeWhen: (e) => e.excluded });
  },
  onSelect: (items) => renderSelectionBar(items),
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

async function loadGallery() {
  galleryUser = ctx.user;
  galleryStale = false;
  lightbox.invalidate();
  if (!ctx.user) { gallery.entries = []; gallery.empty(emptyArchive()); return; }
  const { items } = await api.get(`/api/timeline?${q({ user: ctx.user })}`);
  gallery.entries = items.map((it) => ({
    item: it.i, v: it.v, w: it.w, h: it.h, kind: it.k, date: it.d, asset: it.a, fav: Boolean(it.f), excluded: false, caption: it.al,
  }));
  if (items.length) gallery.setData(items); else gallery.empty(emptyArchive());
}

/** Reload after edits, keeping the scroll position. */
async function reloadGallery() {
  const visible = ctx.current === 'galleria';
  const keep = visible ? main.scrollTop : galleryScroll;
  await loadGallery();
  if (visible) { gallery.shown(); main.scrollTop = keep; } else galleryScroll = keep;
}

lightbox.onClose = () => { if (galleryStale && ctx.current === 'galleria') reloadGallery(); };
lightbox.onChange = (change) => {
  if (change.error) { toast(change.error); return; }
  galleryStale = true;
  if (change.tags) loadNames();
  if (change.rebuilt) { refresh().then(route); return; }
  if (change.exclude !== undefined) {
    refresh();
    const { entry } = change;
    toast(change.exclude ? 'Messa nel cestino' : 'Rimessa nell’archivio', {
      action: 'Annulla',
      onAction: async () => {
        await api.post('/api/assets/edit', { ids: [entry.asset], exclude: !change.exclude });
        entry.excluded = !change.exclude;
        change.undo?.();
        galleryStale = true;
        refresh();
        if (lightbox.el.hidden && ctx.current === 'galleria') reloadGallery();
      },
    });
  }
};

// ---------- selection bar ----------

function renderSelectionBar(items) {
  const bar = $('#selbar');
  bar.hidden = !items.length;
  if (!items.length) return;
  const allFav = items.every((it) => it.f);
  bar.innerHTML = `
    <span class="count-label">${plural(items.length, 'selezionata', 'selezionate')}</span>
    <button class="btn" type="button" data-sel="fav">${icon('i-star')}${allFav ? 'Togli preferite' : 'Preferite'}</button>
    <button class="btn" type="button" data-sel="album">${icon('i-album')}Album</button>
    <button class="btn" type="button" data-sel="person">${icon('i-person')}Persone</button>
    <button class="btn" type="button" data-sel="keyword">${icon('i-tag')}Etichette</button>
    <button class="btn" type="button" data-sel="date">${icon('i-calendar')}Data</button>
    <span class="divider"></span>
    <button class="btn danger" type="button" data-sel="exclude">${icon('i-trash')}Escludi</button>
    <button class="btn btn-quiet" type="button" data-sel="all" title="Seleziona tutto (Ctrl+A)">Tutte</button>
    <button class="btn btn-quiet" type="button" data-sel="clear" aria-label="Annulla la selezione (Esc)">${icon('i-close')}</button>`;
}

const TAG_DIALOG = {
  album: { title: 'Aggiungi a un album', label: 'Nome dell’album', confirm: 'Aggiungi' },
  person: { title: 'Indica le persone', label: 'Nomi', hint: 'Separa più nomi con una virgola.', confirm: 'Aggiungi' },
  keyword: { title: 'Aggiungi etichette', label: 'Etichette', hint: 'Per esempio: mare, compleanno. Separa con una virgola.', confirm: 'Aggiungi' },
};

$('#selbar').addEventListener('click', async (e) => {
  const action = e.target.closest('[data-sel]')?.dataset.sel;
  if (!action) return;
  if (action === 'clear') { gallery.clearSelection(); return; }
  if (action === 'all') { gallery.selectAll(); return; }
  const items = gallery.selection();
  const ids = items.map((it) => it.a);
  const n = plural(ids.length, 'foto', 'foto');
  try {
    if (action === 'fav') {
      const value = !items.every((it) => it.f);
      await api.post('/api/assets/edit', { ids, favorite: value });
      toast(value ? `${n} tra le preferite` : `${n} tolte dalle preferite`);
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
    } else if (action === 'exclude') {
      await api.post('/api/assets/edit', { ids, exclude: true });
      toast(`${n} nel cestino`, {
        action: 'Annulla',
        onAction: async () => { await api.post('/api/assets/edit', { ids, exclude: false }); await refresh(); reloadGallery(); },
      });
    }
    await refresh();
    await reloadGallery();
  } catch (err) {
    toast(err.message);
  }
});

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
};

async function route() {
  const name = location.hash.replace(/^#\/?/, '') || 'panoramica';
  for (const a of document.querySelectorAll('.nav a')) {
    if (a.dataset.route === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (ctx.current === 'galleria' && name !== 'galleria') {
    galleryScroll = main.scrollTop;
    gallery.clearSelection();
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

// ---------- scan ----------

let polling = false;
let jobKind = 'scan';
const JOB_DONE = {
  scan: ['Archivio aggiornato', 'Aggiornamento non riuscito: i dettagli sono in Fonti'],
  export: ['Archivio sul NAS aggiornato', 'Copia sul NAS non riuscita: i dettagli sono in Archivio NAS'],
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
      if (ctx.current === 'fonti' || ctx.current === 'nas') renderJobPanel($('.jobpanel-slot'), job);
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
async function startJob(url) {
  jobKind = url.includes('export') ? 'export' : 'scan';
  try {
    ctx.state.job = await api.post(url);
    renderJobMini(ctx.state.job);
    if (['fonti', 'panoramica', 'nas'].includes(ctx.current)) await route();
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
});
$('#scan-btn').addEventListener('click', startScan);

document.addEventListener('keydown', (e) => {
  if (!lightbox.el.hidden || $('#sheet').open || e.target.closest('input, textarea, select')) return;
  if (ctx.current !== 'galleria') return;
  if (e.key === 'Escape' && gallery.selected.size) gallery.clearSelection();
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); gallery.selectAll(); }
  if (e.key === 'Delete' && gallery.selected.size) $('#selbar [data-sel="exclude"]')?.click();
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
})();
