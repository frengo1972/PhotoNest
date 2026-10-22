// Timeline grid: one section per day, justified rows that keep every photo's proportions.
// Tiles are created only for days near the viewport, so thousands of photos stay light.
// Selection: the circle on a tile, Ctrl+click, Shift+click for a range. Zoom: − / + or Ctrl+wheel.

import { thumbUrl } from './api.js';
import { dayTitle, esc, icon, monthTitle, num } from './format.js';

const GAP = 4;
const MIN_RATIO = 0.4;
const MAX_RATIO = 3.2;
const ZOOM_HEIGHTS = [100, 140, 180, 230, 290, 370, 470];
const ZOOM_DEFAULT = 3;
const ZOOM_KEY = 'photoorg.zoom';

const ratioOf = (it) => (it.w && it.h ? Math.min(MAX_RATIO, Math.max(MIN_RATIO, it.w / it.h)) : 1.5);

function loadZoom() {
  try {
    const raw = localStorage.getItem(ZOOM_KEY);
    const z = Number(raw);
    return raw !== null && Number.isInteger(z) && z >= 0 && z < ZOOM_HEIGHTS.length ? z : ZOOM_DEFAULT;
  } catch {
    return ZOOM_DEFAULT; // storage blocked: just use the default size
  }
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
  constructor(root, scroller, { onOpen, onSelect }) {
    this.root = root;
    this.scroller = scroller;
    this.onOpen = onOpen;
    this.onSelect = onSelect;
    this.items = [];
    this.days = [];
    this.tiles = new Map();
    this.selected = new Set();
    this.anchor = null;
    this.zoom = loadZoom();
    this.dirty = false;

    root.innerHTML = `
      <header class="gallery-head">
        <h1></h1><span class="total"></span>
        <div class="zoom" role="group" aria-label="Dimensione anteprime">
          <button type="button" data-zoom="-1" title="Anteprime più piccole (Ctrl + rotellina)" aria-label="Anteprime più piccole">${icon('i-minus')}</button>
          <button type="button" data-zoom="1" title="Anteprime più grandi (Ctrl + rotellina)" aria-label="Anteprime più grandi">${icon('i-plus')}</button>
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
    this.updateZoomButtons();

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
      const b = e.target.closest('[data-zoom]');
      if (b) this.setZoom(this.zoom + Number(b.dataset.zoom));
    });
    this.body.addEventListener('click', (e) => this.onClick(e));
    this.years.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) this.scroller.scrollTop = this.days[Number(b.dataset.day)].top - this.head.offsetHeight;
    });
  }

  // ---------- data ----------

  setData(items) {
    this.items = items;
    this.tiles.clear();
    this.selected.clear();
    this.anchor = null;
    this.root.classList.remove('selecting');
    this.io.disconnect();
    this.dayOf = new Map();
    this.days = [];
    let cur = null;
    items.forEach((it, idx) => {
      const key = it.d ? it.d.slice(0, 10) : 'none';
      if (!cur || cur.key !== key) {
        cur = { key, date: it.d, start: idx, end: idx, albums: new Set() };
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
      sec.innerHTML = `<header class="day-head"><h2>${esc(dayTitle(day.date))}</h2>${albums ? `<span>${esc(albums)}</span>` : ''}</header><div class="rows"></div>`;
      day.el = sec;
      day.rowsEl = sec.lastElementChild;
      day.label = dayTitle(day.date, true);
      this.dayOf.set(sec, day);
      frag.append(sec);
    }
    this.body.replaceChildren(frag);

    const videos = items.filter((it) => it.k === 'video').length;
    const photos = items.length - videos;
    this.total.textContent = videos ? `${num(photos)} foto, ${num(videos)} video` : `${num(photos)} foto`;
    this.years.innerHTML = this.yearButtons();
    this.layout();
    for (const day of this.days) this.io.observe(day.el);
    this.updateHead();
    this.onSelect?.(this.selection());
  }

  empty(html) {
    this.setData([]);
    this.title.textContent = 'Galleria';
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

  /** Recompute rows for the current width and zoom; call after the view becomes visible. */
  layout() {
    const width = this.days[0]?.rowsEl.clientWidth ?? 0;
    if (!width) { this.dirty = true; return; }
    this.dirty = false;
    const target = this.targetHeight(width);
    for (const day of this.days) {
      const rows = justify(this.items.slice(day.start, day.end + 1), width, target);
      const pos = [];
      let y = 0;
      for (const row of rows) {
        const h = Math.round(row.h);
        let x = 0;
        row.ratios.forEach((r, j) => {
          const w = row.full && j === row.ratios.length - 1 ? width - x : Math.round(r * row.h);
          pos.push({ x, y, w, h });
          x += w + GAP;
        });
        y += h + GAP;
      }
      day.pos = pos;
      day.height = Math.max(0, y - GAP);
      day.rowsEl.style.height = `${day.height}px`;
      day.el.style.containIntrinsicSize = `auto ${day.height + 56}px`;
      if (day.materialized) this.place(day);
    }
    for (const day of this.days) day.top = day.el.offsetTop;
  }

  setZoom(z) {
    z = Math.max(0, Math.min(ZOOM_HEIGHTS.length - 1, z));
    if (z === this.zoom) return;
    // keep the day at the top of the screen in place while the rows change size
    const headH = this.head.offsetHeight;
    const anchorDay = this.days.length ? this.days[this.dayIndexAt(this.scroller.scrollTop + headH + 1)] : null;
    const within = anchorDay?.height ? (this.scroller.scrollTop + headH - anchorDay.top) / (anchorDay.height + 56) : 0;
    this.zoom = z;
    try { localStorage.setItem(ZOOM_KEY, String(z)); } catch { /* private mode */ }
    this.updateZoomButtons();
    this.layout();
    if (anchorDay) this.scroller.scrollTop = anchorDay.top + within * (anchorDay.height + 56) - headH;
    this.updateHead();
  }

  updateZoomButtons() {
    this.zoomButtons[0].disabled = this.zoom === 0;
    this.zoomButtons[1].disabled = this.zoom === ZOOM_HEIGHTS.length - 1;
  }

  shown() {
    if (this.dirty) this.layout();
    this.updateHead();
  }

  materialize(day) {
    if (!day || day.materialized || !day.pos) return;
    day.materialized = true;
    const frag = document.createDocumentFragment();
    for (let k = 0; k <= day.end - day.start; k++) {
      const idx = day.start + k;
      const it = this.items[idx];
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'tile';
      b.dataset.idx = idx;
      b.setAttribute('aria-pressed', String(this.selected.has(idx)));
      b.setAttribute('aria-label', `${it.k === 'video' ? 'Video' : 'Foto'} di ${day.label}`);
      b.innerHTML = `<img alt="" decoding="async" loading="lazy" draggable="false" src="${thumbUrl(it.i, it.v)}">`
        + `<span class="sel" title="Seleziona">${icon('i-check')}</span>`
        + (it.k === 'video' ? `<span class="badge video">${icon('i-play')}</span>` : '')
        + (it.f ? `<span class="badge fav" title="Preferita">${icon('i-star')}</span>` : '')
        + (it.n > 1 ? `<span class="badge copies" title="Unita da ${it.n} copie">${icon('i-copies')}${it.n}</span>` : '');
      const img = b.firstElementChild;
      img.addEventListener('load', () => img.classList.add('loaded'), { once: true });
      this.tiles.set(idx, b);
      frag.append(b);
    }
    day.rowsEl.append(frag);
    this.place(day);
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

  updateHead() {
    if (!this.days.length) return;
    const y = this.scroller.scrollTop + this.head.offsetHeight + 1;
    const day = this.days[this.dayIndexAt(y)];
    this.title.textContent = monthTitle(day.date);
    this.head.classList.toggle('scrolled', this.scroller.scrollTop > 4);
    const year = day.date?.slice(0, 4);
    for (const b of this.years.children) b.setAttribute('aria-current', String(b.dataset.year === year));
  }

  // ---------- selection ----------

  onClick(e) {
    const tile = e.target.closest('.tile');
    if (!tile) return;
    const idx = Number(tile.dataset.idx);
    if (e.target.closest('.sel') || this.selected.size || e.ctrlKey || e.metaKey) {
      this.toggle(idx, e.shiftKey);
      return;
    }
    this.onOpen(idx);
  }

  toggle(idx, range = false) {
    if (range && this.anchor !== null) {
      const [a, b] = [Math.min(this.anchor, idx), Math.max(this.anchor, idx)];
      for (let i = a; i <= b; i++) this.mark(i, true);
    } else {
      this.mark(idx, !this.selected.has(idx));
    }
    this.anchor = idx;
    this.root.classList.toggle('selecting', this.selected.size > 0);
    this.onSelect?.(this.selection());
  }

  mark(idx, on) {
    if (on) this.selected.add(idx); else this.selected.delete(idx);
    this.tiles.get(idx)?.setAttribute('aria-pressed', String(on));
  }

  selectAll() {
    this.items.forEach((_, i) => this.mark(i, true));
    this.root.classList.add('selecting');
    this.onSelect?.(this.selection());
  }

  clearSelection() {
    for (const i of [...this.selected]) this.mark(i, false);
    this.anchor = null;
    this.root.classList.remove('selecting');
    this.onSelect?.(this.selection());
  }

  selection() { return [...this.selected].map((i) => this.items[i]); }

  // ---------- viewer hand-off ----------

  tileFor(idx) {
    return this.tiles.get(idx) ?? null;
  }

  /** Make sure the tile of `idx` exists and is on screen (used when the viewer closes). */
  reveal(idx) {
    const dayIdx = this.days.findIndex((d) => idx >= d.start && idx <= d.end);
    if (dayIdx < 0) return;
    this.materialize(this.days[dayIdx]);
    const tile = this.tiles.get(idx);
    const r = tile.getBoundingClientRect();
    const sr = this.scroller.getBoundingClientRect();
    const top = sr.top + this.head.offsetHeight;
    if (r.top < top || r.bottom > sr.bottom) {
      this.scroller.scrollTop += r.top - top - (sr.bottom - top - r.height) / 2;
      this.updateHead();
    }
  }
}
