// Full-screen viewer. The photo grows out of its tile and returns to it; browsing is 1:1 with the
// pointer, flicks are projected, every motion is an interruptible spring. Neighbours are preloaded.

import { api, fileUrl, fullUrl, thumbUrl } from './api.js';
import {
  DATE_SOURCE, REASON, bytes, dayTitle, esc, icon, megapixels, num, plural, sourceLabel, timeOf, tzLabel,
} from './format.js';
import { animate, prefersReducedMotion, project, rubberband, velocityOf } from './spring.js';

const PAD = 28;
const TOP = 68;
const BOTTOM = 44;
const INFO_W = 360;
const FULL_BOX = 2560; // longest side of the server's screen-size preview
const CACHE_LIMIT = 16;
const COMPACT = () => innerWidth <= 820;

/** Where the photo sits inside its tile: tiles either crop (cover) or letterbox (data-fit="contain"). */
function rectInTile(el, ratio) {
  const r = el.getBoundingClientRect();
  const contain = el.dataset.fit === 'contain';
  let w = r.width;
  let h = w / ratio;
  if (contain ? h > r.height : h < r.height) { h = r.height; w = h * ratio; }
  return { x: r.left + (r.width - w) / 2, y: r.top + (r.height - h) / 2, w, h, visible: r.bottom > 0 && r.top < innerHeight && r.width > 0 };
}

export class Lightbox {
  constructor() {
    const el = document.createElement('div');
    el.className = 'lb';
    el.hidden = true;
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.setAttribute('aria-label', 'Visualizzatore');
    el.innerHTML = `
      <div class="lb-scrim"></div>
      <div class="lb-stage">
        <img class="lb-media" alt="" draggable="false">
        <video class="lb-media lb-video" controls playsinline hidden></video>
      </div>
      <header class="lb-bar">
        <div class="lb-title"><b></b><span></span></div>
        <div class="lb-actions">
          <button class="lb-btn fav" type="button" data-act="fav" aria-pressed="false" title="Preferita (F)" aria-label="Preferita">${icon('i-star')}</button>
          <button class="lb-btn" type="button" data-act="exclude" title="Escludi dall’archivio (Canc)" aria-label="Escludi dall’archivio">${icon('i-trash')}</button>
          <a class="lb-btn" data-act="original" target="_blank" rel="noopener" title="Apri l’originale" aria-label="Apri l’originale">${icon('i-download')}</a>
          <button class="lb-btn" type="button" data-act="info" aria-pressed="false" title="Informazioni (I)" aria-label="Informazioni">${icon('i-info')}</button>
          <button class="lb-btn" type="button" data-act="close" title="Chiudi (Esc)" aria-label="Chiudi">${icon('i-close')}</button>
        </div>
      </header>
      <button class="lb-btn lb-nav prev" type="button" data-act="prev" aria-label="Precedente">${icon('i-left')}</button>
      <button class="lb-btn lb-nav next" type="button" data-act="next" aria-label="Successiva">${icon('i-right')}</button>
      <div class="lb-count"></div>
      <aside class="lb-info" aria-label="Informazioni" hidden></aside>`;
    document.body.append(el);

    this.el = el;
    this.scrim = el.querySelector('.lb-scrim');
    this.stage = el.querySelector('.lb-stage');
    this.img = el.querySelector('img.lb-media');
    this.video = el.querySelector('video');
    this.media = this.img;
    this.chrome = [...el.querySelectorAll('.lb-bar, .lb-nav, .lb-count')];
    this.titleEl = el.querySelector('.lb-title b');
    this.subEl = el.querySelector('.lb-title span');
    this.countEl = el.querySelector('.lb-count');
    this.prevBtn = el.querySelector('.prev');
    this.nextBtn = el.querySelector('.next');
    this.infoBtn = el.querySelector('[data-act="info"]');
    this.favBtn = el.querySelector('[data-act="fav"]');
    this.excludeBtn = el.querySelector('[data-act="exclude"]');
    this.originalLink = el.querySelector('[data-act="original"]');
    this.onChange = null; // set by the app: (change) => void
    this.info = el.querySelector('.lb-info');
    this.cache = new Map();
    this.assetCache = new Map();
    this.state = { x: 0, y: 0, s: 1, o: 0 };
    this.infoOpen = false;
    this.infoX = INFO_W;
    this.info.style.transform = `translateX(${INFO_W}px)`;

    el.addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act === 'close') this.close();
      else if (act === 'prev') this.step(-1);
      else if (act === 'next') this.step(1);
      else if (act === 'info') this.toggleInfo();
      else if (act === 'fav') this.toggleFavorite();
      else if (act === 'exclude') this.toggleExclude();
    });
    this.info.addEventListener('click', (e) => this.onInfoClick(e));
    this.info.addEventListener('keydown', (e) => this.onInfoKey(e));
    this.stage.addEventListener('pointerdown', (e) => this.onDown(e));
    this.stage.addEventListener('pointermove', (e) => this.onMove(e));
    this.stage.addEventListener('pointerup', (e) => this.onUp(e));
    this.stage.addEventListener('pointercancel', (e) => this.onUp(e, true));
    el.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse') this.showPointer(); });
    document.addEventListener('keydown', (e) => this.onKey(e));
    addEventListener('resize', () => { if (!this.el.hidden) this.refit(false); });
  }

  get entry() { return this.entries[this.index]; }

  // ---------- geometry & rendering ----------

  stageWidth() { return innerWidth - (this.infoOpen && !COMPACT() ? INFO_W : 0); }

  fit(e) {
    const ew = e.w || 1600;
    const eh = e.h || 1200;
    const aw = this.stageWidth() - 2 * PAD;
    const ah = innerHeight - TOP - BOTTOM;
    const s = Math.min(aw / ew, ah / eh, 1);
    const w = ew * s;
    const h = eh * s;
    return { x: PAD + (aw - w) / 2, y: TOP + (ah - h) / 2, w, h };
  }

  place(rect) {
    this.rect = rect;
    this.media.style.width = `${rect.w}px`;
    this.media.style.height = `${rect.h}px`;
  }

  apply() {
    const { x, y, s, o } = this.state;
    this.media.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${s})`;
    this.scrim.style.opacity = o;
    for (const c of this.chrome) c.style.opacity = o;
  }

  to(target, opts = {}) {
    this.anim?.stop();
    this.media.style.willChange = 'transform';
    const anim = animate(this.state, target, {
      response: 0.35,
      precision: { s: 0.001, o: 0.004 },
      ...opts,
      onUpdate: (v) => { this.state = { ...v }; this.apply(); },
    });
    this.anim = anim;
    // dropping will-change after the motion lets the browser re-rasterize sharply at the new scale
    anim.done.then((ok) => { if (ok && this.anim === anim) this.media.style.willChange = 'auto'; });
    return anim.done;
  }

  fitted() { return { x: this.rect.x, y: this.rect.y, s: 1, o: 1 }; }

  // ---------- loading ----------

  fetchFull(e) {
    const url = fullUrl(e.item, e.v);
    let entry = this.cache.get(url);
    if (!entry) {
      const im = new Image();
      im.decoding = 'async';
      im.src = url;
      entry = { ready: false };
      entry.promise = im.decode().then(() => { entry.ready = true; return url; }, () => url);
      entry.im = im;
      this.cache.set(url, entry);
      while (this.cache.size > CACHE_LIMIT) this.cache.delete(this.cache.keys().next().value);
    }
    return entry;
  }

  load(e) {
    const isVideo = e.kind === 'video';
    this.img.hidden = isVideo;
    this.video.hidden = !isVideo;
    this.zoomed = false;
    this.img.classList.remove('zoomed', 'zoomable');
    if (isVideo) {
      this.media = this.video;
      this.video.src = fileUrl(e.item);
      this.video.play().catch(() => {});
    } else {
      this.video.pause();
      this.video.removeAttribute('src');
      this.media = this.img;
      const full = this.fetchFull(e);
      this.img.src = full.ready ? fullUrl(e.item, e.v) : thumbUrl(e.item, e.v);
      if (!full.ready) full.promise.then((url) => { if (this.entry === e) this.img.src = url; });
    }
    this.originalLink.href = fileUrl(e.item);
  }

  preload() {
    for (const d of [1, -1, 2]) {
      const e = this.entries[this.index + d];
      if (e && e.kind !== 'video') this.fetchFull(e);
    }
  }

  updateChrome() {
    const e = this.entry;
    this.titleEl.textContent = e.date ? dayTitle(e.date, true) : 'Senza data';
    this.subEl.textContent = [e.date ? timeOf(e.date) : '', e.caption].filter(Boolean).join(', ');
    this.countEl.textContent = this.entries.length > 1 ? `${num(this.index + 1)} di ${num(this.entries.length)}` : '';
    this.prevBtn.disabled = this.index === 0;
    this.nextBtn.disabled = this.index === this.entries.length - 1;
    const editable = Boolean(e.asset);
    this.favBtn.hidden = !editable;
    this.excludeBtn.hidden = !editable;
    this.favBtn.setAttribute('aria-pressed', String(Boolean(e.fav)));
    const restore = Boolean(e.excluded);
    this.excludeBtn.innerHTML = icon(restore ? 'i-restore' : 'i-trash');
    this.excludeBtn.title = restore ? 'Rimetti nell’archivio (Canc)' : 'Escludi dall’archivio (Canc)';
    this.excludeBtn.setAttribute('aria-label', this.excludeBtn.title);
    const fullW = (e.w || 0) * Math.min(1, FULL_BOX / Math.max(e.w || 1, e.h || 1));
    this.img.classList.toggle('zoomable', e.kind !== 'video' && fullW / this.rect.w > 1.15);
  }

  // ---------- open / navigate / close ----------

  /**
   * entries: [{item, v, w, h, kind, date, caption, asset?, fav?, excluded?, info?}]
   * origin: {tile(entry), reveal?(entry)} to grow from / return to the thumbnail.
   * removeWhen(entry): drop an entry from this list after an edit (e.g. excluded while browsing the archive).
   */
  open(entries, index, origin = null, { removeWhen = null } = {}) {
    this.entries = [...entries];
    this.index = index;
    this.origin = origin;
    this.removeWhen = removeWhen;
    this.editingDate = false;
    this.returnFocus = document.activeElement;
    this.el.hidden = false;
    document.body.classList.add('lb-open');
    const e = this.entry;
    this.load(e);
    const rect = this.fit(e);
    this.place(rect);
    const tile = origin?.tile(e);
    const from = tile && !prefersReducedMotion() ? rectInTile(tile, rect.w / rect.h) : null;
    this.state = from
      ? { x: from.x, y: from.y, s: from.w / rect.w, o: 0 }
      : { x: rect.x, y: rect.y + 24, s: 1, o: 0 };
    this.apply();
    this.to(this.fitted(), { response: 0.38 });
    this.updateChrome();
    this.preload();
    if (this.infoOpen) this.renderInfo();
    this.el.querySelector('[data-act="close"]').focus({ preventScroll: true });
  }

  show(index, { from = 0, velocity = 0 } = {}) {
    this.index = index;
    const e = this.entry;
    this.load(e);
    const rect = this.fit(e);
    this.place(rect);
    this.state = { x: rect.x + from, y: rect.y, s: 1, o: this.state.o };
    this.apply();
    this.to(this.fitted(), { response: 0.3, velocity: { x: velocity } });
    this.updateChrome();
    this.preload();
    if (this.infoOpen) this.renderInfo();
  }

  step(delta) {
    const next = this.index + delta;
    if (next < 0 || next >= this.entries.length) {
      // nothing there: a small push that springs back
      this.to({ ...this.fitted(), x: this.rect.x - delta * 28 }, { response: 0.12 }).then(() => this.to(this.fitted()));
      return;
    }
    this.show(next, { from: delta * 64 });
  }

  commit(delta, velocity) {
    const W = this.stageWidth();
    this.to({ ...this.fitted(), x: this.rect.x - delta * W }, { response: 0.24, velocity: { x: velocity } })
      .then((ok) => { if (ok) this.show(this.index + delta, { from: delta * W * 0.45, velocity }); });
  }

  async close({ velocity } = {}) {
    if (this.closing || this.el.hidden) return;
    this.closing = true;
    this.video.pause();
    const e = this.entry;
    if (e) this.origin?.reveal?.(e);
    const tile = e ? this.origin?.tile(e) : null;
    const r = tile && !prefersReducedMotion() ? rectInTile(tile, this.rect.w / this.rect.h) : null;
    const target = r?.visible && this.media === this.img && this.rect
      ? { x: r.x, y: r.y, s: r.w / this.rect.w, o: 0 }
      : { ...this.state, y: this.state.y + 32, o: 0 };
    await this.to(target, { response: 0.32, velocity: velocity ? { x: velocity.x, y: velocity.y } : {} });
    this.el.hidden = true;
    this.closing = false;
    document.body.classList.remove('lb-open');
    this.video.removeAttribute('src');
    (tile ?? this.returnFocus)?.focus?.({ preventScroll: true });
    this.onClose?.();
  }

  // ---------- edits ----------

  async edit(body) {
    try {
      await api.post('/api/assets/edit', { ids: [this.entry.asset], ...body });
      return true;
    } catch (err) {
      this.onChange?.({ error: err.message });
      return false;
    }
  }

  async toggleFavorite() {
    const e = this.entry;
    if (!e?.asset) return;
    const value = !e.fav;
    if (!(await this.edit({ favorite: value }))) return;
    e.fav = value;
    this.favBtn.setAttribute('aria-pressed', String(value));
    this.onChange?.({ entry: e, favorite: value });
  }

  async toggleExclude() {
    const e = this.entry;
    if (!e?.asset) return;
    const value = !e.excluded;
    const at = this.index;
    if (!(await this.edit({ exclude: value }))) return;
    e.excluded = value;
    this.assetCache.delete(e.asset);
    this.onChange?.({ entry: e, exclude: value, undo: () => this.reinsert(e, at) });
    if (this.removeWhen?.(e)) this.removeCurrent();
    else { this.updateChrome(); if (this.infoOpen) this.renderInfo(); }
  }

  /** Drop the current entry (it no longer belongs to this list) and show its neighbour. */
  removeCurrent() {
    this.entries.splice(this.index, 1);
    if (!this.entries.length) { this.close(); return; }
    this.show(Math.min(this.index, this.entries.length - 1), { from: 64 });
  }

  /** Undo of an exclusion made here: put the entry back where it was, if the viewer is still open. */
  reinsert(entry, at) {
    if (this.el.hidden || this.entries.includes(entry)) return;
    this.entries.splice(Math.min(at, this.entries.length), 0, entry);
    this.show(this.entries.indexOf(entry), { from: -64 });
  }

  async onInfoClick(ev) {
    const t = ev.target.closest('button');
    if (!t || !this.entry?.asset) return;
    const e = this.entry;
    if (t.dataset.tagRemove) {
      await this.changeTags(t.dataset.kind, [], [t.dataset.tagRemove]);
    } else if ('dateEdit' in t.dataset) {
      this.editingDate = true;
      this.renderInfo();
    } else if ('dateCancel' in t.dataset) {
      this.editingDate = false;
      this.renderInfo();
    } else if ('dateSave' in t.dataset) {
      const raw = this.info.querySelector('input[type="datetime-local"]')?.value;
      if (!raw) return;
      const value = raw.replace('T', ' ').padEnd(19, ':00').slice(0, 19);
      if (!(await this.edit({ date_local: value }))) return;
      e.date = value;
      this.editingDate = false;
      this.assetCache.delete(e.asset);
      this.updateChrome();
      this.renderInfo();
      this.onChange?.({ entry: e, date: value });
    } else if ('dateReset' in t.dataset) {
      if (!(await this.edit({ reset_date: true }))) return;
      this.onChange?.({ entry: e, rebuilt: true }); // the automatic date needs a rebuild: ids change
      this.close();
    }
  }

  async onInfoKey(ev) {
    const input = ev.target.closest('input[data-tag-add]');
    if (!input || ev.key !== 'Enter') return;
    ev.preventDefault();
    const names = input.value.split(',').map((s) => s.trim()).filter(Boolean);
    if (names.length) await this.changeTags(input.dataset.tagAdd, names, []);
  }

  async changeTags(kind, add, remove) {
    const e = this.entry;
    try {
      await api.post('/api/assets/tags', { ids: [e.asset], kind, add, remove });
    } catch (err) {
      this.onChange?.({ error: err.message });
      return;
    }
    this.assetCache.delete(e.asset);
    await this.renderInfo();
    this.info.querySelector(`input[data-tag-add="${kind}"]`)?.focus();
    this.onChange?.({ entry: e, tags: kind });
  }

  refit(animated) {
    const rect = this.fit(this.entry);
    if (animated && !this.zoomed) {
      const s = rect.w / this.rect.w;
      this.to({ x: rect.x, y: rect.y, s, o: 1 }).then((ok) => {
        if (!ok) return;
        this.place(rect);
        this.state = this.fitted();
        this.apply();
      });
      return;
    }
    this.place(rect);
    this.zoomed = false;
    this.img.classList.remove('zoomed');
    this.anim?.stop();
    this.state = this.fitted();
    this.apply();
    this.updateChrome();
  }

  // ---------- zoom ----------

  clampPan(st) {
    const w = this.rect.w * st.s;
    const h = this.rect.h * st.s;
    const W = this.stageWidth();
    const x = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, st.x));
    const y = h <= innerHeight ? (innerHeight - h) / 2 : Math.min(0, Math.max(innerHeight - h, st.y));
    return { ...st, x, y };
  }

  toggleZoom(px, py) {
    const e = this.entry;
    if (this.zoomed) {
      this.zoomed = false;
      this.img.classList.remove('zoomed');
      this.to(this.fitted());
      return;
    }
    const fullW = (e.w || 0) * Math.min(1, FULL_BOX / Math.max(e.w || 1, e.h || 1));
    const s = fullW / this.rect.w;
    if (s < 1.15) return;
    this.zoomed = true;
    this.img.classList.add('zoomed');
    const r = this.rect;
    this.to(this.clampPan({ x: px - (px - r.x) * s, y: py - (py - r.y) * s, s, o: 1 }));
  }

  // ---------- pointer: swipe to browse, drag down to close, pan when zoomed ----------

  onDown(ev) {
    if (ev.button !== 0) return;
    if (this.media === this.video && ev.target === this.video) return; // leave video controls alone
    this.anim?.stop();
    this.drag = {
      id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, start: { ...this.state }, mode: null,
      onMedia: ev.target === this.media, hist: [{ x: ev.clientX, y: ev.clientY, t: ev.timeStamp }],
    };
    this.stage.setPointerCapture(ev.pointerId);
  }

  onMove(ev) {
    const d = this.drag;
    if (!d || ev.pointerId !== d.id) return;
    d.hist.push({ x: ev.clientX, y: ev.clientY, t: ev.timeStamp });
    if (d.hist.length > 8) d.hist.shift();
    const dx = ev.clientX - d.x0;
    const dy = ev.clientY - d.y0;
    if (!d.mode) {
      if (Math.hypot(dx, dy) < 8) return;
      d.mode = this.zoomed ? 'pan' : Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    const r = this.rect;
    if (d.mode === 'x') {
      const edge = (this.index === 0 && dx > 0) || (this.index === this.entries.length - 1 && dx < 0);
      this.state = { ...d.start, x: d.start.x + (edge ? rubberband(dx, innerWidth) : dx) };
    } else if (d.mode === 'y') {
      const p = Math.min(1, Math.abs(dy) / (innerHeight * 0.5));
      const s = 1 - p * 0.3;
      this.state = { x: r.x + dx + (r.w * (1 - s)) / 2, y: r.y + dy + (r.h * (1 - s)) / 2, s, o: 1 - p * 0.85 };
    } else {
      this.state = this.clampPan({ ...d.start, x: d.start.x + dx, y: d.start.y + dy });
    }
    this.apply();
  }

  onUp(ev, cancelled = false) {
    const d = this.drag;
    if (!d || ev.pointerId !== d.id) return;
    this.drag = null;
    const v = velocityOf(d.hist);
    if (!d.mode) {
      if (cancelled) return;
      if (d.onMedia && this.media === this.img) this.toggleZoom(ev.clientX, ev.clientY);
      else if (!d.onMedia) this.close();
      return;
    }
    if (d.mode === 'x') {
      const proj = this.state.x - this.rect.x + project(v.x);
      const W = this.stageWidth();
      if (!cancelled && proj < -W * 0.22 && this.index < this.entries.length - 1) return this.commit(1, v.x);
      if (!cancelled && proj > W * 0.22 && this.index > 0) return this.commit(-1, v.x);
      return this.to(this.fitted(), { velocity: { x: v.x } });
    }
    if (d.mode === 'y') {
      const proj = ev.clientY - d.y0 + project(v.y);
      if (!cancelled && Math.abs(proj) > innerHeight * 0.18) return this.close({ velocity: v });
      return this.to(this.fitted(), { velocity: { x: v.x, y: v.y } });
    }
    this.to(this.clampPan({ ...this.state, x: this.state.x + project(v.x, 0.995), y: this.state.y + project(v.y, 0.995) }),
      { velocity: { x: v.x, y: v.y } });
  }

  showPointer() {
    this.el.classList.add('pointer');
    clearTimeout(this.pointerTimer);
    this.pointerTimer = setTimeout(() => this.el.classList.remove('pointer'), 2500);
  }

  onKey(e) {
    if (this.el.hidden || e.altKey || e.ctrlKey || e.metaKey) return;
    const keys = {
      Escape: () => (this.zoomed ? this.toggleZoom() : this.close()),
      ArrowLeft: () => this.step(-1),
      ArrowRight: () => this.step(1),
      Home: () => this.index > 0 && this.show(0, { from: -64 }),
      End: () => this.index < this.entries.length - 1 && this.show(this.entries.length - 1, { from: 64 }),
      i: () => this.toggleInfo(),
      I: () => this.toggleInfo(),
      f: () => this.toggleFavorite(),
      F: () => this.toggleFavorite(),
      Delete: () => this.toggleExclude(),
    };
    if (e.key === 'Tab') {
      const f = [...this.el.querySelectorAll('button:not([disabled]), a[href], video[controls]:not([hidden])')]
        .filter((n) => n.offsetParent !== null);
      const i = f.indexOf(document.activeElement);
      if (i === -1 || (e.shiftKey && i === 0) || (!e.shiftKey && i === f.length - 1)) {
        e.preventDefault();
        f[e.shiftKey ? f.length - 1 : 0]?.focus();
      }
      return;
    }
    if (e.target.closest?.('input, textarea')) { // typing in the info panel
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (keys[e.key] && !(e.target instanceof HTMLVideoElement && e.key.startsWith('Arrow'))) {
      e.preventDefault();
      keys[e.key]();
    }
  }

  // ---------- info panel ----------

  toggleInfo() {
    this.infoOpen = !this.infoOpen;
    this.infoBtn.setAttribute('aria-pressed', String(this.infoOpen));
    if (this.infoOpen) {
      this.info.hidden = false;
      this.renderInfo();
    }
    this.infoAnim?.stop();
    this.infoAnim = animate({ t: this.infoX }, { t: this.infoOpen ? 0 : INFO_W }, {
      response: 0.32,
      onUpdate: ({ t }) => { this.infoX = t; this.info.style.transform = `translateX(${t}px)`; },
    });
    this.infoAnim.done.then((ok) => { if (ok && !this.infoOpen) this.info.hidden = true; });
    this.nextBtn.style.right = this.infoOpen && !COMPACT() ? `${INFO_W + 16}px` : '';
    this.refit(true);
  }

  async renderInfo() {
    const e = this.entry;
    let html;
    try {
      if (e.info) html = await e.info();
      else if (e.asset) {
        if (!this.assetCache.has(e.asset)) this.assetCache.set(e.asset, api.get(`/api/asset/${e.asset}`));
        html = assetInfo(await this.assetCache.get(e.asset), { editingDate: this.editingDate });
      } else html = '<p>Nessuna informazione.</p>';
    } catch (err) {
      this.assetCache.delete(e.asset);
      html = `<p>Informazioni non disponibili: ${esc(err.message)}</p>`;
    }
    if (this.entry === e) this.info.innerHTML = html;
  }

  invalidate() { this.assetCache.clear(); }
}

function where(lat, lon) {
  if (lat === null || lat === undefined) return '<p class="sub">Nessuna posizione registrata.</p>';
  const map = `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=16/${lat}/${lon}`;
  return `<p>${lat.toFixed(5)}, ${lon.toFixed(5)}</p><p><a href="${map}" target="_blank" rel="noopener">Apri sulla mappa</a></p>`;
}

const TAG_UI = {
  album: { title: 'Album', placeholder: 'Aggiungi a un album' },
  person: { title: 'Persone', placeholder: 'Aggiungi una persona' },
  keyword: { title: 'Etichette', placeholder: 'Aggiungi un’etichetta' },
};

/** Editable chips: × removes, typing + Enter adds (comma separates several). */
function tagSection(kind, values) {
  const ui = TAG_UI[kind];
  const chips = values.map((v) => `<span class="chip">${esc(v)}<button type="button" data-kind="${kind}" data-tag-remove="${esc(v)}" aria-label="Togli ${esc(v)}">${icon('i-close')}</button></span>`).join('');
  return `<section><h3>${ui.title}</h3>${chips ? `<div class="chips">${chips}</div>` : ''}
    <input data-tag-add="${kind}" list="names-${kind}" placeholder="${ui.placeholder}" aria-label="${ui.placeholder}" autocomplete="off"></section>`;
}

function dateSection(a, editing) {
  if (editing) {
    const value = (a.date_local ?? '').replace(' ', 'T');
    return `<section><h3>Data dello scatto</h3>
      <input type="datetime-local" step="1" value="${esc(value)}" aria-label="Data e ora dello scatto">
      <div class="row"><button type="button" class="dark-btn" data-date-save>Salva data</button>
      <button type="button" class="link-btn" data-date-cancel>Annulla</button></div></section>`;
  }
  const conf = a.date_conf === 'medium' ? ' Da confermare.' : a.date_conf === 'low' ? ' Poco affidabile.' : '';
  const source = a.date_source === 'manual' ? 'Impostata da te.' : `Ricavata ${esc(DATE_SOURCE[a.date_source] ?? 'da nessuna fonte')}.${conf}`;
  return `<section><h3>Data</h3><p>${source}</p>
    <div class="row"><button type="button" class="link-btn" data-date-edit>Correggi la data</button>
    ${a.date_source === 'manual' ? '<button type="button" class="link-btn" data-date-reset>Torna alla data automatica</button>' : ''}</div></section>`;
}

export function copyRow(m) {
  const kept = m.role === 'winner';
  const dims = m.w ? `${num(m.w)} × ${num(m.h)}, ` : '';
  return `<div class="copy ${kept ? 'kept' : ''}">${icon(kept ? 'i-check' : 'i-copies')}
    <span>${esc(sourceLabel(m.source.type))}, ${esc(REASON[m.reason] ?? '')}</span>
    <small>${esc(m.source.ident)}<br>${esc(m.filename)}<br>${dims}${bytes(m.size)}</small></div>`;
}

export function assetInfo(a, { editingDate = false } = {}) {
  const w = a.items.find((i) => i.role === 'winner') ?? a.items[0];
  const time = a.date_local ? `Alle ${timeOf(a.date_local, true)}${a.tz_offset_min !== null ? `, fuso ${tzLabel(a.tz_offset_min)}` : ''}` : '';
  const camera = [w.make, w.model].filter(Boolean).join(' ');
  return `
    <h2>${esc(dayTitle(a.date_local, true))}</h2>
    ${time ? `<p class="sub">${esc(time)}</p>` : ''}
    ${a.excluded ? '<p class="sub excluded-note">Esclusa dall’archivio: è nel cestino.</p>' : ''}
    ${dateSection(a, editingDate)}
    <section><h3>Luogo</h3>${where(a.lat, a.lon)}</section>
    ${tagSection('album', a.albums)}
    ${tagSection('person', a.people)}
    ${tagSection('keyword', a.keywords ?? [])}
    ${a.description ? `<section><h3>Descrizione</h3><p>${esc(a.description)}</p></section>` : ''}
    <section><h3>Scatto</h3>
      ${camera ? `<p>${esc(camera)}</p>` : ''}
      <p>${w.w ? `${num(w.w)} × ${num(w.h)} pixel, ${megapixels(w.w, w.h)}` : 'Dimensioni sconosciute'}</p>
      <p>${bytes(w.size)}</p></section>
    <section><h3>${a.items.length > 1 ? `Copie trovate (${a.items.length})` : 'Provenienza'}</h3>${a.items.map(copyRow).join('')}</section>
    ${a.variants > 1 ? `<section><h3>Varianti</h3><p>Fa parte di ${plural(a.variants, 'scatto', 'scatti')} fatti nello stesso secondo.</p></section>` : ''}`;
}
