// Timeline: one section per day, as a grid of justified rows that keep every photo's proportions or as
// a details list (preview on the left, everything known about the photo on the right).
// Tiles are created only for days near the viewport, so thousands of photos stay light.
//
// Selection, as in Explorer / Lightroom: the circle on a tile, Ctrl+click toggles one photo, Shift+click
// extends from the last one, dragging draws a rectangle (Shift adds to the selection, Ctrl inverts inside
// it), the circle on a day selects the whole day. "Seleziona" mode (S) makes a plain click select.
// Keyboard: arrows move a cursor across the layout (Shift extends), PgUp/PgDn a screen, Home/End,
// Space selects, Enter opens. Zoom: − / + or Ctrl+wheel.

import { api, thumbUrl } from './api.js';
import {
  DATE_SOURCE, bytes, dayTitle, esc, icon, megapixels, monthTitle, num, plural, sourceLabel, timeOf, tzLabel,
} from './format.js';

const GAP = 4;
const LIST_GAP = 8;
const MIN_RATIO = 0.4;
const MAX_RATIO = 3.2;
const ZOOM_HEIGHTS = [100, 140, 180, 230, 290, 370, 470];
const LIST_HEIGHTS = [96, 124, 156, 196, 240, 300, 380];
const ZOOM_DEFAULT = 3;
const ZOOM_KEY = 'photoorg.zoom';
const VIEW_KEY = 'photoorg.view';
const VIEWS = ['grid', 'list'];
const DRAG_START = 6; // px of movement before a press becomes a selection rectangle
const EDGE = 56; // band at the top / bottom of the screen that scrolls while dragging

const ratioOf = (it) => (it.w && it.h ? Math.min(MAX_RATIO, Math.max(MIN_RATIO, it.w / it.h)) : 1.5);

function loadPref(key, valid, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw !== null && valid(raw) ? raw : fallback;
  } catch {
    return fallback; // storage blocked: just use the default
  }
}

function savePref(key, value) {
  try { localStorage.setItem(key, String(value)); } catch { /* private mode */ }
}

/** Break items into rows that fill `width` exactly; the last row keeps the target height. */
export function justify(items, width, target) {
  const rows = [];
  let row = [];
  let sum = 0;
  for (const it of items) {
    const r = ratioOf(it);
    row.push(r);
    sum += r;
    if (sum * target + GAP * (row.length - 1) >= width) {
      rows.push({ h: (width - GAP * (row.length - 1)) / sum, ratios: row, full: true });
      row = [];
      sum = 0;
    }
  }
  if (row.length) rows.push({ h: target, ratios: row, full: false });
  return rows;
}

export class Gallery {
  constructor(root, scroller, { onOpen, onSelect, onSearch, bottomInset = () => 0 }) {
    this.root = root;
    this.scroller = scroller;
    this.onOpen = onOpen;
    this.onSelect = onSelect;
    this.onSearch = onSearch;
    this.bottomInset = bottomInset;
    this.results = null; // title of the search results section, null for the timeline
    this.items = [];
    this.days = [];
    this.lines = [];
    this.tiles = new Map();
    this.selected = new Set();
    this.details = new Map(); // asset id -> facts for the details view (null while loading)
    this.cursor = null; // index of the keyboard cursor
    this.anchor = null; // where Shift ranges start
    this.base = null; // selection before the current Shift range
    this.mode = false; // "Seleziona" mode: a plain click selects instead of opening
    this.zoom = Number(loadPref(ZOOM_KEY, (z) => Number.isInteger(Number(z)) && z >= 0 && z < ZOOM_HEIGHTS.length, ZOOM_DEFAULT));
    this.view = loadPref(VIEW_KEY, (v) => VIEWS.includes(v), 'grid');
    this.dirty = false;
    this.gen = 0;

    root.innerHTML = `
      <header class="gallery-head">
        <h1></h1><span class="total"></span>
        <form class="search" role="search">
          <input type="search" placeholder="Cerca persone, luoghi, cose…" aria-label="Cerca nell’archivio (/)" autocomplete="off" list="names-person">
          <button type="button" class="btn btn-quiet clear-search" hidden>Mostra tutte</button>
        </form>
        <div class="head-tools">
          <button type="button" class="btn btn-quiet select-mode" aria-pressed="false" title="Clic sulle foto per selezionarle (S)">${icon('i-select')}<span>Seleziona</span></button>
          <div class="segmented icons" role="group" aria-label="Vista">
            <button type="button" data-view="grid" title="Griglia (G)" aria-label="Griglia">${icon('i-grid')}</button>
            <button type="button" data-view="list" title="Dettagli (D)" aria-label="Dettagli">${icon('i-list')}</button>
          </div>
          <div class="zoom" role="group" aria-label="Dimensione anteprime">
            <button type="button" data-zoom="-1" title="Anteprime più piccole (− o Ctrl + rotellina)" aria-label="Anteprime più piccole">${icon('i-minus')}</button>
            <button type="button" data-zoom="1" title="Anteprime più grandi (+ o Ctrl + rotellina)" aria-label="Anteprime più grandi">${icon('i-plus')}</button>
          </div>
          <button type="button" class="icon-btn" data-action="keys" title="Scorciatoie da tastiera (?)" aria-label="Scorciatoie da tastiera">${icon('i-keyboard')}</button>
        </div>
      </header>
      <div class="gallery-body"></div>
      <nav class="years" aria-label="Anni"></nav>`;
    this.head = root.querySelector('.gallery-head');
    this.title = this.head.querySelector('h1');
    this.total = this.head.querySelector('.total');
    this.body = root.querySelector('.gallery-body');
    this.years = root.querySelector('.years');
    this.zoomButtons = [...this.head.querySelectorAll('[data-zoom]')];
    this.viewButtons = [...this.head.querySelectorAll('[data-view]')];
    this.modeButton = this.head.querySelector('.select-mode');
    this.updateZoomButtons();
    this.updateViewButtons();
    root.classList.toggle('view-list', this.view === 'list');
    this.searchInput = this.head.querySelector('.search input');
    this.clearButton = this.head.querySelector('.clear-search');
    this.head.querySelector('.search').addEventListener('submit', (e) => {
      e.preventDefault();
      this.onSearch?.(this.searchInput.value.trim());
    });
    this.searchInput.addEventListener('search', () => { if (!this.searchInput.value) this.onSearch?.(''); });
    this.searchInput.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !this.searchInput.value) this.searchInput.blur(); });
    this.clearButton.addEventListener('click', () => { this.searchInput.value = ''; this.onSearch?.(''); });

    this.io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) this.materialize(this.dayOf.get(e.target));
    }, { root: scroller, rootMargin: '1600px 0px' });
    new ResizeObserver(() => this.schedule('layout')).observe(this.body);
    scroller.addEventListener('scroll', () => this.schedule('head'), { passive: true });
    scroller.addEventListener('wheel', (e) => {
      if (!e.ctrlKey || this.root.hidden || this.root.closest('[hidden]')) return;
      e.preventDefault();
      const now = performance.now();
      if (now - (this.lastWheel ?? 0) < 120) return; // one step per wheel notch
      this.lastWheel = now;
      this.setZoom(this.zoom + (e.deltaY < 0 ? 1 : -1));
    }, { passive: false });
    this.head.addEventListener('click', (e) => {
      const b = e.target.closest('[data-zoom], [data-view], .select-mode');
      if (!b) return;
      if (b.dataset.zoom) this.setZoom(this.zoom + Number(b.dataset.zoom));
      else if (b.dataset.view) this.setView(b.dataset.view);
      else this.setMode(!this.selecting);
    });
    this.body.addEventListener('click', (e) => this.onClick(e));
    this.body.addEventListener('focusin', (e) => {
      const t = e.target.closest('.tile');
      if (t) this.setCursor(Number(t.dataset.idx), { focus: false, scroll: false });
    });
    this.body.addEventListener('pointerdown', (e) => this.onDown(e));
    this.body.addEventListener('pointermove', (e) => this.onMove(e));
    this.body.addEventListener('pointerup', (e) => this.onUp(e));
    this.body.addEventListener('pointercancel', (e) => this.onUp(e));
    this.years.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) this.scroller.scrollTop = this.days[Number(b.dataset.day)].top - this.head.offsetHeight;
    });
  }

  // ---------- data ----------

  /**
   * items: timeline rows. With `results` (a title) they are shown as one section, in the given order.
   * keep: same list reloaded after an edit, so selection and cursor follow their photos.
   */
  setData(items, { results = null, query = '', keep = false } = {}) {
    const keptSel = keep ? new Set([...this.selected].map((i) => this.items[i]?.a)) : null;
    const keptCursor = keep && this.cursor !== null ? { a: this.items[this.cursor]?.a, at: this.cursor } : null;
    const hadFocus = keep && this.body.contains(document.activeElement);
    this.gen += 1;
    this.items = items;
    this.results = results;
    this.searchInput.value = query;
    this.clearButton.hidden = results === null;
    this.tiles.clear();
    this.details.clear();
    this.selected = new Set();
    this.cursor = null;
    this.anchor = null;
    this.base = null;
    this.io.disconnect();
    this.dayOf = new Map();
    this.days = [];
    let cur = null;
    items.forEach((it, idx) => {
      const key = results !== null ? 'results' : it.d ? it.d.slice(0, 10) : 'none';
      if (!cur || cur.key !== key) {
        cur = { key, date: it.d, start: idx, end: idx, albums: new Set(), nsel: 0 };
        this.days.push(cur);
      }
      cur.end = idx;
      if (it.al) cur.albums.add(it.al);
    });

    const frag = document.createDocumentFragment();
    for (const day of this.days) {
      const sec = document.createElement('section');
      sec.className = 'day';
      const albums = [...day.albums].slice(0, 2).join(', ');
      const title = results !== null ? results : dayTitle(day.date);
      const n = day.end - day.start + 1;
      sec.innerHTML = `<header class="day-head">
          <button type="button" class="day-check" aria-pressed="false" title="Seleziona ${n === 1 ? 'la foto' : `le ${num(n)} foto`} del giorno" aria-label="Seleziona tutto: ${esc(title)}">${icon('i-check')}</button>
          <h2>${esc(title)}</h2>${albums && results === null ? `<span>${esc(albums)}</span>` : ''}
        </header><div class="rows"></div>`;
      day.el = sec;
      day.check = sec.querySelector('.day-check');
      day.rowsEl = sec.lastElementChild;
      day.label = dayTitle(day.date, true);
      this.dayOf.set(sec, day);
      frag.append(sec);
    }
    this.body.replaceChildren(frag);

    const videos = items.filter((it) => it.k === 'video').length;
    const photos = items.length - videos;
    this.total.textContent = videos ? `${num(photos)} foto, ${num(videos)} video` : `${num(photos)} foto`;
    this.years.innerHTML = results !== null ? '' : this.yearButtons();
    this.layout();
    for (const day of this.days) this.io.observe(day.el);
    this.updateHead();

    if (keptSel?.size) this.applySelection(new Set(items.flatMap((it, i) => (keptSel.has(it.a) ? [i] : []))), false);
    if (keptCursor && items.length) {
      const found = items.findIndex((it) => it.a === keptCursor.a);
      const at = found >= 0 ? found : Math.min(keptCursor.at, items.length - 1);
      this.setCursor(at, { focus: hadFocus, scroll: false });
      this.anchor = at;
    }
    this.updateMode();
  }

  empty(html, opts = {}) {
    this.setData([], opts);
    this.title.textContent = opts.results !== undefined && opts.results !== null ? 'Ricerca' : 'Galleria';
    this.total.textContent = '';
    this.body.innerHTML = html;
  }

  yearButtons() {
    const firsts = new Map();
    this.days.forEach((d, i) => {
      const y = d.date ? d.date.slice(0, 4) : null;
      if (y && !firsts.has(y)) firsts.set(y, i);
    });
    if (firsts.size < 2) return '';
    return [...firsts].map(([y, i]) => `<button type="button" data-day="${i}" data-year="${y}">${y}</button>`).join('');
  }

  // ---------- layout ----------

  schedule(what) {
    this.pending ??= new Set();
    this.pending.add(what);
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      if (this.pending.has('layout')) this.layout();
      this.updateHead();
      this.pending.clear();
    });
  }

  targetHeight(width) {
    return Math.min(ZOOM_HEIGHTS[this.zoom], Math.max(90, width / 2.2));
  }

  /** Recompute rows for the current width, zoom and view; call after the view becomes visible. */
  layout() {
    const first = this.days[0];
    const width = first?.rowsEl.clientWidth ?? 0;
    if (!width) { this.dirty = true; return; }
    this.dirty = false;
    // where the rows of a day start, inside its section and inside the scroller (same for every day)
    const sr = this.scroller.getBoundingClientRect();
    const rr = first.rowsEl.getBoundingClientRect();
    this.rowsOff = rr.top - first.el.getBoundingClientRect().top;
    this.rowsLeft = rr.left - sr.left + this.scroller.scrollLeft;

    const list = this.view === 'list';
    const gap = list ? LIST_GAP : GAP;
    const target = list ? LIST_HEIGHTS[this.zoom] : this.targetHeight(width);
    if (list) this.body.style.setProperty('--preview-w', `${Math.round(Math.min(target * 1.5, width * 0.42))}px`);
    this.lines = []; // rows of tiles across all days, for Up / Down and Page Up / Down
    this.lineOf = new Int32Array(this.items.length);
    for (const day of this.days) {
      const pos = [];
      let y = 0;
      const addLine = (start, end) => {
        for (let i = start; i <= end; i++) this.lineOf[i] = this.lines.length;
        this.lines.push({ start, end });
      };
      if (list) {
        for (let idx = day.start; idx <= day.end; idx++) {
          pos.push({ x: 0, y, w: width, h: target });
          addLine(idx, idx);
          y += target + gap;
        }
      } else {
        let idx = day.start;
        for (const row of justify(this.items.slice(day.start, day.end + 1), width, target)) {
          const h = Math.round(row.h);
          let x = 0;
          row.ratios.forEach((r, j) => {
            const w = row.full && j === row.ratios.length - 1 ? width - x : Math.round(r * row.h);
            pos.push({ x, y, w, h });
            x += w + gap;
          });
          addLine(idx, idx + row.ratios.length - 1);
          idx += row.ratios.length;
          y += h + gap;
        }
      }
      day.pos = pos;
      day.height = Math.max(0, y - gap);
      day.rowsEl.style.height = `${day.height}px`;
      day.el.style.containIntrinsicSize = `auto ${day.height + this.rowsOff}px`;
      if (day.materialized) this.place(day);
    }
    this.measureTops();
  }

  measureTops() {
    for (const day of this.days) day.top = day.el.offsetTop;
  }

  /** Change the layout while keeping what is on screen in place: `change` runs between measure and restore. */
  keepPlace(change) {
    const headH = this.head.offsetHeight;
    const cursorShown = this.cursor !== null && this.isOnScreen(this.cursor);
    const anchorDay = this.days.length ? this.days[this.dayIndexAt(this.scroller.scrollTop + headH + 1)] : null;
    const within = anchorDay?.height ? (this.scroller.scrollTop + headH - anchorDay.top) / (anchorDay.height + this.rowsOff) : 0;
    change();
    if (cursorShown) this.ensureVisible(this.cursor, true);
    else if (anchorDay) this.scroller.scrollTop = anchorDay.top + within * (anchorDay.height + this.rowsOff) - headH;
    this.updateHead();
  }

  setZoom(z) {
    z = Math.max(0, Math.min(ZOOM_HEIGHTS.length - 1, z));
    if (z === this.zoom) return;
    this.keepPlace(() => {
      this.zoom = z;
      savePref(ZOOM_KEY, z);
      this.updateZoomButtons();
      this.layout();
    });
  }

  setView(view) {
    if (view === this.view || !VIEWS.includes(view)) return;
    const hadFocus = this.body.contains(document.activeElement);
    this.keepPlace(() => {
      this.view = view;
      savePref(VIEW_KEY, view);
      this.updateViewButtons();
      this.root.classList.toggle('view-list', view === 'list');
      // tiles are built differently in the two views: start again from empty days
      this.io.disconnect();
      this.tiles.clear();
      for (const day of this.days) { day.materialized = false; day.rowsEl.replaceChildren(); }
      this.layout();
      for (const day of this.days) this.io.observe(day.el);
    });
    if (this.cursor !== null) this.setCursor(this.cursor, { focus: hadFocus, scroll: false });
  }

  updateZoomButtons() {
    this.zoomButtons[0].disabled = this.zoom === 0;
    this.zoomButtons[1].disabled = this.zoom === ZOOM_HEIGHTS.length - 1;
  }

  updateViewButtons() {
    for (const b of this.viewButtons) b.setAttribute('aria-pressed', String(b.dataset.view === this.view));
  }

  shown() {
    if (this.dirty) this.layout();
    this.updateHead();
  }

  materialize(day) {
    if (!day || day.materialized || !day.pos) return;
    day.materialized = true;
    const list = this.view === 'list';
    const focusable = this.cursor ?? 0;
    const frag = document.createDocumentFragment();
    for (let idx = day.start; idx <= day.end; idx++) {
      const it = this.items[idx];
      const b = document.createElement('button');
      b.type = 'button';
      b.className = list ? 'tile detail' : 'tile';
      b.dataset.idx = idx;
      b.tabIndex = idx === focusable ? 0 : -1;
      b.setAttribute('aria-pressed', String(this.selected.has(idx)));
      b.setAttribute('aria-label', `${it.k === 'video' ? 'Video' : 'Foto'} di ${day.label}`);
      const media = `<img alt="" decoding="async" loading="lazy" draggable="false" src="${thumbUrl(it.i, it.v)}">`
        + `<span class="sel" title="Seleziona">${icon('i-check')}</span>`
        + (it.k === 'video' ? `<span class="badge video">${icon('i-play')}</span>` : '')
        + (it.f ? `<span class="badge fav" title="Preferita">${icon('i-star')}</span>` : '')
        + (it.n > 1 ? `<span class="badge copies" title="Unita da ${it.n} copie">${icon('i-copies')}${it.n}</span>` : '');
      b.innerHTML = list
        ? `<span class="thumb" data-fit="contain">${media}</span><span class="meta">${this.metaHtml(idx)}</span>`
        : media;
      const img = b.querySelector('img');
      img.addEventListener('load', () => img.classList.add('loaded'), { once: true });
      this.tiles.set(idx, b);
      frag.append(b);
    }
    day.rowsEl.append(frag);
    this.place(day);
    if (list) this.loadDetails(day);
  }

  place(day) {
    for (let k = 0; k < day.pos.length; k++) {
      const p = day.pos[k];
      const s = this.tiles.get(day.start + k).style;
      s.left = `${p.x}px`; s.top = `${p.y}px`; s.width = `${p.w}px`; s.height = `${p.h}px`;
    }
  }

  dayIndexAt(y) {
    let lo = 0;
    let hi = this.days.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.days[mid].top <= y) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  /** The day containing item `idx`. */
  dayOfItem(idx) {
    let lo = 0;
    let hi = this.days.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.days[mid].start <= idx) lo = mid; else hi = mid - 1;
    }
    return this.days[lo];
  }

  /** Position of item `idx` in the scroller's content: {x, y, w, h}. */
  rectOf(idx) {
    const day = this.dayOfItem(idx);
    const p = day.pos[idx - day.start];
    return { x: this.rowsLeft + p.x, y: day.top + this.rowsOff + p.y, w: p.w, h: p.h };
  }

  updateHead() {
    if (!this.days.length) return;
    const y = this.scroller.scrollTop + this.head.offsetHeight + 1;
    const day = this.days[this.dayIndexAt(y)];
    this.title.textContent = this.results !== null ? 'Ricerca' : monthTitle(day.date);
    this.head.classList.toggle('scrolled', this.scroller.scrollTop > 4);
    const year = day.date?.slice(0, 4);
    for (const b of this.years.children) b.setAttribute('aria-current', String(b.dataset.year === year));
  }

  // ---------- details view ----------

  async loadDetails(day) {
    const ids = [];
    for (let idx = day.start; idx <= day.end; idx++) {
      const a = this.items[idx].a;
      if (a && !this.details.has(a)) { ids.push(a); this.details.set(a, null); }
    }
    if (!ids.length) return;
    const gen = this.gen;
    try {
      for (let i = 0; i < ids.length; i += 1000) {
        const { details } = await api.post('/api/assets/details', { ids: ids.slice(i, i + 1000) });
        if (gen !== this.gen) return;
        for (const [a, d] of Object.entries(details)) this.details.set(Number(a), d);
      }
    } catch {
      if (gen === this.gen) for (const a of ids) this.details.delete(a); // retried when the day is built again
      return;
    }
    if (this.view !== 'list') return;
    for (let idx = day.start; idx <= day.end; idx++) {
      const meta = this.tiles.get(idx)?.querySelector('.meta');
      if (meta) meta.innerHTML = this.metaHtml(idx);
    }
  }

  metaHtml(idx) {
    const it = this.items[idx];
    const d = this.details.get(it.a);
    const time = it.d ? [timeOf(it.d), d?.tz !== null && d?.tz !== undefined ? tzLabel(d.tz) : ''].filter(Boolean).join(' · ') : '';
    const facts = [];
    const add = (label, value, cls = '') => {
      if (value) facts.push(`<span class="fact ${cls}"><small>${label}</small><span>${value}</span></span>`);
    };
    const list = (values) => esc(values.join(', '));
    if (d) {
      add('Persone', list(d.people));
      add('Album', list(d.albums));
      add('Etichette', list(d.keywords));
      add('Evento', esc(d.event ?? ''));
      add('Luogo', d.lat !== null && d.lat !== undefined ? `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}` : '');
      add('Riconosciuto', list(d.auto), 'auto');
      add('Fotocamera', esc([d.make, d.model].filter(Boolean).join(' ')));
    }
    add('Dimensioni', it.w ? `${num(it.w)} × ${num(it.h)}${it.k === 'video' ? '' : `, ${megapixels(it.w, it.h)}`}${d ? `, ${bytes(d.size)}` : ''}` : '');
    if (d) {
      add('File', esc(d.file));
      add('Provenienza', `${esc(sourceLabel(d.source))}${it.n > 1 ? `, unita da ${plural(it.n, 'copia', 'copie')}` : ''}`);
      const conf = d.date_conf === 'medium' ? ', da confermare' : d.date_conf === 'low' ? ', poco affidabile' : '';
      add('Data', d.manual ? 'Impostata da te' : it.d ? esc(`Ricavata ${DATE_SOURCE[d.date_source] ?? ''}${conf}`) : '', conf ? 'attention' : '');
      add('Descrizione', esc(d.description ?? ''), 'wide');
    } else {
      facts.push('<span class="fact loading"><small>Dettagli</small><span>…</span></span>');
    }
    return `<span class="meta-head"><b>${esc(it.d ? dayTitle(it.d, true) : 'Senza data')}</b>`
      + `${time ? `<span>${esc(time)}</span>` : ''}${it.k === 'video' ? '<span class="pill">Video</span>' : ''}`
      + `${it.f ? `<span class="star" title="Preferita">${icon('i-star')}</span>` : ''}</span>`
      + `<span class="facts-flow">${facts.join('')}</span>`;
  }

  // ---------- cursor & keyboard ----------

  /** Move the keyboard cursor (a roving tabindex: only the cursor tile is in the Tab order). */
  setCursor(idx, { focus = true, scroll = true } = {}) {
    if (idx === null || idx < 0 || idx >= this.items.length) return;
    const prev = this.tiles.get(this.cursor ?? 0);
    if (prev) prev.tabIndex = -1;
    this.cursor = idx;
    this.materialize(this.dayOfItem(idx));
    const tile = this.tiles.get(idx);
    tile.tabIndex = 0;
    if (scroll) this.ensureVisible(idx);
    if (focus) tile.focus({ preventScroll: true });
  }

  isOnScreen(idx) {
    const r = this.rectOf(idx);
    const top = this.scroller.scrollTop + this.head.offsetHeight;
    return r.y + r.h > top && r.y < this.scroller.scrollTop + this.scroller.clientHeight;
  }

  /** Scroll as little as possible so the tile is fully visible (or centred, with `center`). */
  ensureVisible(idx, center = false) {
    const r = this.rectOf(idx);
    const day = this.dayOfItem(idx);
    const top = this.scroller.scrollTop + this.head.offsetHeight + 12;
    const bottom = this.scroller.scrollTop + this.scroller.clientHeight - 12 - this.bottomInset();
    // in the first row of a day, bring its date into view too
    const y = this.lines[this.lineOf[idx]].start === day.start ? day.top + 12 : r.y;
    if (center) this.scroller.scrollTop += r.y + r.h / 2 - (top + bottom) / 2;
    else if (y < top || r.h > bottom - top) this.scroller.scrollTop -= top - y;
    else if (r.y + r.h > bottom) this.scroller.scrollTop += r.y + r.h - bottom;
    this.updateHead();
  }

  /** First photo whose top is on screen (where the cursor appears when it is not set yet). */
  firstVisible() {
    if (!this.lines.length) return null;
    const top = this.scroller.scrollTop + this.head.offsetHeight;
    const line = this.lineAt(top);
    return this.rectOf(this.lines[line].start).y < top && line + 1 < this.lines.length ? this.lines[line + 1].start : this.lines[line].start;
  }

  /** Index of the last line starting at or above content position `y`. */
  lineAt(y) {
    let lo = 0;
    let hi = this.lines.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.rectOf(this.lines[mid].start).y <= y) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  /** In line `n`, the tile whose centre is closest to `cx`. */
  closestIn(n, cx) {
    const { start, end } = this.lines[n];
    let best = start;
    let dist = Infinity;
    for (let i = start; i <= end; i++) {
      const r = this.rectOf(i);
      const d = Math.abs(r.x + r.w / 2 - cx);
      if (d < dist) { dist = d; best = i; }
    }
    return best;
  }

  targetOf(key, c) {
    const last = this.items.length - 1;
    const r = this.rectOf(c);
    const cx = r.x + r.w / 2;
    const line = this.lineOf[c];
    switch (key) {
      case 'ArrowLeft': return Math.max(0, c - 1);
      case 'ArrowRight': return Math.min(last, c + 1);
      case 'ArrowUp': return line > 0 ? this.closestIn(line - 1, cx) : c;
      case 'ArrowDown': return line < this.lines.length - 1 ? this.closestIn(line + 1, cx) : c;
      case 'Home': return 0;
      case 'End': return last;
      default: { // a screen up or down: the photo at the same place on the next screen
        const page = Math.max(120, this.scroller.clientHeight - this.head.offsetHeight - 80) * (key === 'PageDown' ? 1 : -1);
        const target = this.closestIn(this.lineAt(r.y + page + 1), cx);
        this.scroller.scrollTop += page;
        return target === c ? (page > 0 ? this.closestIn(Math.min(line + 1, this.lines.length - 1), cx) : this.closestIn(Math.max(line - 1, 0), cx)) : target;
      }
    }
  }

  /** Handle a key pressed while the gallery is shown; returns true when it was used. */
  onKey(e) {
    if (!this.items.length) return false;
    const key = e.key;
    const ctrl = e.ctrlKey || e.metaKey;
    if (e.altKey) return false;
    const onControl = !e.target.closest?.('.tile') && e.target.closest?.('button, a');
    const navKeys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'];

    if (navKeys.includes(key)) {
      e.preventDefault();
      let c = this.cursor;
      if (c === null || (!this.isOnScreen(c) && key.startsWith('Arrow'))) {
        // first key press (or the cursor scrolled away): show the cursor where the user is looking
        c = key === 'Home' ? 0 : key === 'End' ? this.items.length - 1 : this.firstVisible();
        if (!key.startsWith('Page')) { this.setCursor(c); this.anchor = c; this.base = null; return true; }
      }
      const t = this.targetOf(key, c);
      if (e.shiftKey) {
        if (this.anchor === null) this.anchor = c;
        this.base ??= new Set(this.selected);
        this.setCursor(t);
        this.selectRange(this.anchor, t);
      } else {
        this.setCursor(t);
        this.anchor = t;
        this.base = null;
      }
      return true;
    }
    if (ctrl && key.toLowerCase() === 'a') { e.preventDefault(); this.selectAll(); return true; }
    if (ctrl && key.toLowerCase() === 'i') { e.preventDefault(); this.invertSelection(); return true; }
    if (key === 'Escape' && !ctrl) {
      if (this.selected.size) this.clearSelection();
      else if (this.mode) this.setMode(false);
      else return false;
      return true;
    }
    if (ctrl || onControl) return false; // Space / Enter on a button press that button
    if (key === ' ') {
      e.preventDefault();
      if (this.cursor === null) { this.setCursor(this.firstVisible()); return true; }
      if (e.shiftKey && this.anchor !== null) { this.base ??= new Set(this.selected); this.selectRange(this.anchor, this.cursor); return true; }
      this.toggle(this.cursor);
      return true;
    }
    if (key === 'Enter' && this.cursor !== null) { e.preventDefault(); this.onOpen(this.cursor); return true; }
    const plain = {
      s: () => this.setMode(!this.selecting),
      g: () => this.setView('grid'),
      d: () => this.setView('list'),
      '+': () => this.setZoom(this.zoom + 1),
      '-': () => this.setZoom(this.zoom - 1),
      '/': () => this.searchInput.focus(),
    }[key.length === 1 ? key.toLowerCase() : key];
    if (!plain) return false;
    e.preventDefault();
    plain();
    return true;
  }

  // ---------- pointer: click, and drag to select an area ----------

  onClick(e) {
    if (this.suppressClick) return;
    const check = e.target.closest('.day-check');
    if (check) {
      this.toggleDay(this.dayOf.get(check.closest('.day')));
      return;
    }
    const tile = e.target.closest('.tile');
    if (!tile) return;
    const idx = Number(tile.dataset.idx);
    if (e.shiftKey) {
      if (this.anchor === null) this.anchor = idx;
      this.base ??= new Set(this.selected);
      this.selectRange(this.anchor, idx);
      return;
    }
    if (e.target.closest('.sel') || this.selecting || e.ctrlKey || e.metaKey) {
      this.toggle(idx);
      return;
    }
    this.anchor = idx;
    this.base = null;
    this.onOpen(idx);
  }

  /** Point of a pointer event in the scroller's content coordinates. */
  contentPoint(x, y) {
    const r = this.scroller.getBoundingClientRect();
    return { x: x - r.left + this.scroller.scrollLeft, y: y - r.top + this.scroller.scrollTop };
  }

  onDown(e) {
    if (e.button !== 0 || e.pointerType !== 'mouse' || !this.items.length) return;
    this.drag = {
      id: e.pointerId, x0: e.clientX, y0: e.clientY, cx: e.clientX, cy: e.clientY,
      start: this.contentPoint(e.clientX, e.clientY), active: false,
      how: e.ctrlKey || e.metaKey ? 'toggle' : e.shiftKey ? 'add' : 'replace',
    };
  }

  onMove(e) {
    const d = this.drag;
    if (!d || e.pointerId !== d.id) return;
    d.cx = e.clientX;
    d.cy = e.clientY;
    if (d.active || Math.hypot(d.cx - d.x0, d.cy - d.y0) < DRAG_START) return;
    d.active = true;
    d.base = d.how === 'replace' ? new Set() : new Set(this.selected);
    this.body.setPointerCapture(d.id);
    this.measureTops();
    d.box = document.createElement('div');
    d.box.className = 'marquee';
    this.root.append(d.box);
    this.root.classList.add('dragging');
    const tick = () => {
      if (!this.drag?.active) return;
      this.autoScroll(d);
      this.updateMarquee(d);
      d.raf = requestAnimationFrame(tick);
    };
    tick();
  }

  onUp(e) {
    const d = this.drag;
    if (!d || e.pointerId !== d.id) return;
    this.drag = null;
    if (!d.active) return;
    cancelAnimationFrame(d.raf);
    d.box.remove();
    this.root.classList.remove('dragging');
    this.anchor = null;
    this.base = null;
    // the click that ends a drag is not a click on a photo
    this.suppressClick = true;
    setTimeout(() => { this.suppressClick = false; }, 0);
  }

  autoScroll(d) {
    const r = this.scroller.getBoundingClientRect();
    const top = r.top + this.head.offsetHeight;
    const speed = (t) => Math.round(2 + 38 * Math.min(1, t) ** 2);
    if (d.cy < top + EDGE) this.scroller.scrollTop -= speed((top + EDGE - d.cy) / EDGE);
    else if (d.cy > r.bottom - EDGE) this.scroller.scrollTop += speed((d.cy - r.bottom + EDGE) / EDGE);
  }

  updateMarquee(d) {
    const p = this.contentPoint(d.cx, d.cy);
    const key = `${p.x},${p.y}`;
    if (key === d.last) return;
    d.last = key;
    const box = {
      x1: Math.min(d.start.x, p.x), y1: Math.min(d.start.y, p.y), x2: Math.max(d.start.x, p.x), y2: Math.max(d.start.y, p.y),
    };
    const s = d.box.style;
    s.left = `${box.x1}px`; s.top = `${box.y1}px`; s.width = `${box.x2 - box.x1}px`; s.height = `${box.y2 - box.y1}px`;
    const next = new Set(d.base);
    for (let n = Math.max(0, this.dayIndexAt(box.y1) - 1); n < this.days.length; n++) {
      const day = this.days[n];
      const y0 = day.top + this.rowsOff;
      if (y0 > box.y2) break;
      if (y0 + day.height < box.y1) continue;
      day.pos.forEach((q, k) => {
        const x = this.rowsLeft + q.x;
        const y = y0 + q.y;
        if (x > box.x2 || x + q.w < box.x1 || y > box.y2 || y + q.h < box.y1) return;
        const idx = day.start + k;
        if (d.how === 'toggle' && d.base.has(idx)) next.delete(idx); else next.add(idx);
      });
    }
    this.applySelection(next);
  }

  // ---------- selection ----------

  /** Selection mode is on when chosen with the button / S, or as soon as something is selected. */
  get selecting() { return this.mode || this.selected.size > 0; }

  setMode(on) {
    this.mode = on;
    if (!on && this.selected.size) { this.clearSelection(); return; }
    this.updateMode();
  }

  updateMode() {
    this.root.classList.toggle('selecting', this.selecting);
    this.modeButton.setAttribute('aria-pressed', String(this.selecting));
    this.onSelect?.(this.selection(), { mode: this.selecting });
  }

  toggle(idx) {
    const next = new Set(this.selected);
    if (next.has(idx)) next.delete(idx); else next.add(idx);
    this.applySelection(next);
    this.anchor = idx;
    this.base = null;
  }

  selectRange(a, b) {
    const next = new Set(this.base ?? this.selected);
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) next.add(i);
    this.applySelection(next);
  }

  toggleDay(day) {
    if (!day) return;
    const next = new Set(this.selected);
    const all = day.nsel === day.end - day.start + 1;
    for (let i = day.start; i <= day.end; i++) if (all) next.delete(i); else next.add(i);
    this.applySelection(next);
  }

  /** Make `next` the selection, touching only the tiles and day headers that change. */
  applySelection(next, notify = true) {
    const touched = new Set();
    const mark = (idx, on) => {
      this.tiles.get(idx)?.setAttribute('aria-pressed', String(on));
      const day = this.dayOfItem(idx);
      day.nsel += on ? 1 : -1;
      touched.add(day);
    };
    for (const i of this.selected) if (!next.has(i)) mark(i, false);
    for (const i of next) if (!this.selected.has(i)) mark(i, true);
    this.selected = next;
    for (const day of touched) day.check.setAttribute('aria-pressed', String(day.nsel === day.end - day.start + 1));
    if (notify) this.updateMode();
  }

  selectAll() {
    this.applySelection(new Set(this.items.keys()));
  }

  invertSelection() {
    this.applySelection(new Set([...this.items.keys()].filter((i) => !this.selected.has(i))));
  }

  clearSelection() {
    this.anchor = null;
    this.base = null;
    this.applySelection(new Set());
  }

  /** Leave selection mode and forget the selection (another screen, or "done"). */
  exitSelection() {
    this.mode = false;
    this.clearSelection();
  }

  selection() { return [...this.selected].sort((a, b) => a - b).map((i) => this.items[i]); }

  /** What keyboard actions apply to: the selection, or else the photo under the cursor. */
  targets() {
    if (this.selected.size) return this.selection();
    return this.cursor !== null && this.isOnScreen(this.cursor) ? [this.items[this.cursor]] : [];
  }

  // ---------- viewer hand-off ----------

  focusCursor() {
    this.tiles.get(this.cursor)?.focus({ preventScroll: true });
  }

  /** Scroll to the first day on or before `date` ("YYYY-MM-DD ..."): the timeline runs newest first. */
  scrollToDate(date) {
    const key = date.slice(0, 10);
    const day = this.days.find((d) => d.key <= key) ?? this.days.at(-1);
    if (day) this.scroller.scrollTop = day.top - this.head.offsetHeight;
    this.updateHead();
  }

  /** Element the viewer grows from: the whole tile in the grid, the preview in the details list. */
  tileFor(idx) {
    const tile = this.tiles.get(idx);
    return (tile?.classList.contains('detail') ? tile.querySelector('.thumb') : tile) ?? null;
  }

  /** Make sure the tile of `idx` exists and is on screen, and put the cursor there (viewer closing). */
  reveal(idx) {
    if (idx === undefined || idx < 0 || idx >= this.items.length) return;
    this.measureTops();
    this.ensureVisible(idx);
    this.setCursor(idx, { focus: false, scroll: false });
    this.anchor = idx;
    this.base = null;
  }
}
