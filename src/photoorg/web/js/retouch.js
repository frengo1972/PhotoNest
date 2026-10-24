// Retouching inside the viewer. The photo stays where it is; a panel slides in with the tools. Each tool
// adds a step to a draft kept by the server; the result is saved as a new version, the original is never touched.
// Hold "Confronta" (or Space) to see the photo as it was; the brush paints the areas to clean on a canvas over it.
// Turning, cropping and levelling are steps too: instant, and the viewer refits when they change the proportions.

import { api } from './api.js';
import { esc, icon, num } from './format.js';
import { animate } from './spring.js';

const PANEL_W = 360;
const POLL_MS = 250;
const BRUSH = { min: 6, max: 120, start: 28 };
const CROP_MIN = 28;       // smallest crop box, in screen pixels
const HANDLE_PAD = 14;     // the crop overlay reaches this far past the photo, so edge handles stay whole
const MAX_ANGLE = 45;
const RATIOS = [
  { id: 'free', label: 'Libera' }, { id: 'orig', label: 'Originale' }, { id: '1:1', ratio: 1 },
  { id: '4:3', ratio: 4 / 3 }, { id: '3:2', ratio: 3 / 2 }, { id: '16:9', ratio: 16 / 9 },
];
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** The crop box (screen pixels) after dragging handle `h` by (dx, dy); `ratio` (width / height) locks the proportions. */
export function dragBox(h, s, W, H, dx, dy, ratio) {
  if (h === 'move') {
    const l = clamp(s.l + dx, 0, W - (s.r - s.l));
    const t = clamp(s.t + dy, 0, H - (s.b - s.t));
    return { l, t, r: l + (s.r - s.l), b: t + (s.b - s.t) };
  }
  const west = h.includes('w');
  const east = h.includes('e');
  const north = h.includes('n');
  const south = h.includes('s');
  if (!ratio) {
    return {
      l: west ? clamp(s.l + dx, 0, s.r - CROP_MIN) : s.l, r: east ? clamp(s.r + dx, s.l + CROP_MIN, W) : s.r,
      t: north ? clamp(s.t + dy, 0, s.b - CROP_MIN) : s.t, b: south ? clamp(s.b + dy, s.t + CROP_MIN, H) : s.b,
    };
  }
  const min = CROP_MIN * Math.max(1, ratio);
  if ((west || east) && (north || south)) { // corner: the opposite corner stays, the box follows the pointer
    const ax = west ? s.r : s.l;
    const ay = north ? s.b : s.t;
    const px = (west ? s.l : s.r) + dx;
    const py = (north ? s.t : s.b) + dy;
    const room = Math.min(west ? ax : W - ax, (north ? ay : H - ay) * ratio);
    const w = clamp(Math.max(Math.abs(px - ax), Math.abs(py - ay) * ratio), Math.min(min, room), room);
    const ht = w / ratio;
    return { l: west ? ax - w : ax, r: west ? ax : ax + w, t: north ? ay - ht : ay, b: north ? ay : ay + ht };
  }
  if (west || east) { // side: the height follows around the middle
    const cy = (s.t + s.b) / 2;
    const room = Math.min(west ? s.r : W - s.l, 2 * Math.min(cy, H - cy) * ratio);
    const w = clamp((east ? s.r + dx - s.l : s.r - (s.l + dx)), Math.min(min, room), room);
    return { l: west ? s.r - w : s.l, r: west ? s.r : s.l + w, t: cy - w / ratio / 2, b: cy + w / ratio / 2 };
  }
  const cx = (s.l + s.r) / 2;
  const room = Math.min(north ? s.b : H - s.t, (2 * Math.min(cx, W - cx)) / ratio);
  const ht = clamp((south ? s.b + dy - s.t : s.b - (s.t + dy)), Math.min(min / ratio, room), room);
  return { t: north ? s.b - ht : s.t, b: north ? s.b : s.t + ht, l: cx - (ht * ratio) / 2, r: cx + (ht * ratio) / 2 };
}

export const STEP_LABEL = {
  color: () => 'Colori migliorati',
  faces: (s) => `Volti ripristinati${s.faces ? ` (${s.faces})` : ''}`,
  inpaint: () => 'Macchie rimosse',
  upscale: (s) => `Ingrandita ×${s.scale}`,
  rotate: (s) => `Ruotata a ${s.quarters === 1 ? 'destra' : s.quarters === 3 ? 'sinistra' : '180°'}`,
  crop: () => 'Ritagliata',
  straighten: (s) => `Raddrizzata di ${Math.abs(s.angle)}° a ${s.angle > 0 ? 'destra' : 'sinistra'}`.replace('.', ','),
};
export const stepLabel = (s) => STEP_LABEL[s.tool]?.(s) ?? s.tool;

const TOOLS = [
  { tool: 'color', icon: 'i-sun', title: 'Colori ed esposizione', text: 'Toglie le dominanti e ravviva le foto sbiadite o scure.', strength: 0.8 },
  { tool: 'faces', icon: 'i-face', title: 'Ripristina i volti', text: 'Rende nitidi i volti sfocati, piccoli o rovinati.', strength: 0.8 },
  { tool: 'inpaint', icon: 'i-brush', title: 'Rimuovi macchie e graffi', text: 'Pennella macchie, graffi, pieghe o scritte: vengono ricostruiti.' },
];

const sleep = (ms) => new Promise((r) => { setTimeout(r, ms); });

export class RetouchEditor {
  constructor(lb) {
    this.lb = lb;
    this.panel = lb.el.querySelector('.lb-edit');
    this.canvas = lb.el.querySelector('.lb-brush');
    this.cursor = lb.el.querySelector('.lb-brush-cursor');
    this.cropEl = lb.el.querySelector('.lb-crop');
    this.boxEl = this.cropEl.querySelector('.crop-box');
    this.liveImg = this.cropEl.querySelector('.crop-live img');
    this.ctx = this.canvas.getContext('2d');
    this.active = false;
    this.mode = null;            // brush | crop | level: the tool that has the photo
    this.box = { l: 0, t: 0, r: 1, b: 1 };   // crop box, as fractions of the photo
    this.ratio = { id: 'free', flip: false };
    this.nudging = false;
    this.panelX = PANEL_W;
    this.panel.style.transform = `translateX(${PANEL_W}px)`;
    this.brushSize = BRUSH.start;
    this.strengths = {};

    this.panel.addEventListener('click', (e) => this.onClick(e));
    this.panel.addEventListener('change', (e) => this.onSlider(e));
    this.panel.addEventListener('input', (e) => {
      if (e.target.matches('[data-brush-size]')) this.setBrush(Number(e.target.value), false);
      else if (e.target.matches('[data-angle]')) this.slideAngle(Number(e.target.value));
    });
    this.cropEl.addEventListener('pointerdown', (e) => this.onCropDown(e));
    this.cropEl.addEventListener('pointermove', (e) => this.onCropMove(e));
    this.cropEl.addEventListener('pointerup', (e) => this.onCropUp(e));
    this.cropEl.addEventListener('pointercancel', (e) => this.onCropUp(e));
    this.canvas.addEventListener('pointerdown', (e) => this.onDraw(e));
    this.canvas.addEventListener('pointermove', (e) => this.onDraw(e));
    this.canvas.addEventListener('pointerup', (e) => this.onDraw(e));
    this.canvas.addEventListener('pointercancel', (e) => this.onDraw(e));
    this.canvas.addEventListener('pointerleave', () => { this.cursor.hidden = true; });
    this.canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.setBrush(this.brushSize * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
    }, { passive: false });
    document.addEventListener('keyup', (e) => { if (this.active && e.key === ' ') this.compare(false); });
  }

  get asset() { return this.entry?.asset; }
  get brush() { return this.mode === 'brush'; }

  // ---------- open / close ----------

  async open() {
    const e = this.lb.entry;
    if (!e?.asset || e.kind !== 'image' || this.active) return;
    this.entry = e;
    this.active = true;
    this.mode = null;
    this.confirming = false;
    this.error = null;
    this.state = { busy: true, phase: 'Apro la foto', steps: [], downloads: {} };
    this.origDims = { w: e.w, h: e.h };
    this.baseSrc = this.lb.img.currentSrc || this.lb.img.src;
    this.previewSrc = null;
    this.lb.el.classList.add('editing');
    if (this.lb.infoOpen) this.lb.toggleInfo();
    this.slide(true);
    this.render();
    this.lb.updateChrome();
    this.lb.refit(true);
    try {
      this.state = await api.post(`/api/retouch/${e.asset}/open`);
    } catch (err) {
      this.lb.onChange?.({ error: err.message });
      this.close();
      return;
    }
    this.render();
    this.panel.querySelector('.tool')?.focus({ preventScroll: true });
  }

  close() {
    if (!this.active) return;
    api.post('/api/retouch/close').catch(() => {});
    this.active = false;
    this.setMode(null);
    if (this.origDims) Object.assign(this.entry, this.origDims); // the preview may have changed the proportions
    this.origDims = null;
    this.lb.el.classList.remove('editing');
    this.slide(false);
    this.lb.load(this.lb.entry);
    this.lb.refit(true);
    this.lb.updateChrome();
    this.lb.el.querySelector('[data-act="retouch"]')?.focus({ preventScroll: true });
  }

  /** Leave without saving: ask first when there is something to lose. */
  requestCancel() {
    if (this.state.busy) return;
    if (this.state.steps?.length && !this.confirming) {
      this.confirming = true;
      this.render();
      this.panel.querySelector('[data-edit="discard"]')?.focus();
      return;
    }
    this.close();
  }

  slide(open) {
    if (open) this.panel.hidden = false;
    this.anim?.stop();
    this.anim = animate({ t: this.panelX }, { t: open ? 0 : PANEL_W }, {
      response: 0.32,
      onUpdate: ({ t }) => { this.panelX = t; this.panel.style.transform = `translateX(${t}px)`; },
    });
    this.anim.done.then((ok) => { if (ok && !open) this.panel.hidden = true; });
  }

  // ---------- tools ----------

  /** `quiet`: refresh only the history and the buttons at the bottom, so a slider being dragged is not rebuilt. */
  async run(tool, params = {}, replaceLast = false, quiet = false) {
    if (this.state.busy) return;
    const draw = () => (quiet ? this.renderParts() : this.render());
    this.error = null;
    this.confirming = false;
    try {
      this.state = await api.post(`/api/retouch/${this.asset}/apply`, { tool, params, replace_last: replaceLast });
    } catch (err) {
      this.error = err.message;
      draw();
      return;
    }
    draw();
    while (this.state.busy && this.active) {
      await sleep(POLL_MS);
      try { this.state = await api.get('/api/retouch'); } catch { continue; }
      this.render();
    }
    if (!this.active) return;
    if (this.state.error) this.error = this.state.error;
    else await this.showPreview();
    draw();
  }

  /** Take a step back (undo) or forward again (redo). */
  async travel(way) {
    const can = way === 'undo' ? this.state.steps?.length : this.state.redo;
    if (this.state.busy || !can) return;
    this.error = null;
    this.state = await api.post(`/api/retouch/${this.asset}/${way}`);
    await this.showPreview();
    this.render();
  }

  undo() { return this.travel('undo'); }
  redo() { return this.travel('redo'); }

  async showPreview() {
    const url = this.state.steps.length ? `/media/retouch/${this.asset}?rev=${this.state.rev}` : this.baseSrc;
    const im = new Image();
    im.src = url;
    try { await im.decode(); } catch { /* shown anyway */ }
    if (!this.active) return;
    this.previewSrc = url;
    if (!this.comparing) this.lb.img.src = url;
    this.fitTo(this.state);
  }

  /** The preview may have other proportions than the photo (turned, cropped): make the viewer and the overlays follow. */
  fitTo({ w, h }) {
    const e = this.entry;
    if (!w || (e.w === w && e.h === h)) return;
    Object.assign(e, { w, h });
    this.lb.refit(false);
    if (this.mode === 'crop') this.resetBox();
    this.layout(true);
  }

  compare(on) {
    if (!this.active || this.comparing === on || !this.previewSrc) return;
    this.comparing = on;
    this.lb.img.src = on ? this.baseSrc : this.previewSrc;
    this.fitTo(on ? this.origDims : this.state);
    this.lb.el.classList.toggle('comparing', on);
    this.panel.querySelector('[data-edit="compare"]')?.setAttribute('aria-pressed', String(on));
  }

  async save() {
    if (this.state.busy || !this.state.steps?.length) return;
    let version;
    let replaced;
    try {
      ({ version, replaced } = await api.post(`/api/retouch/${this.asset}/save`));
    } catch (err) {
      this.error = err.message;
      this.render();
      return;
    }
    const e = this.entry;
    const previous = { version: e.version ?? null, ...this.origDims };
    e.version = { id: version.id, v: version.v };
    e.w = version.w;
    e.h = version.h;
    this.lb.showOriginal = false;
    this.lb.assetCache.delete(e.asset);
    this.state.steps = [];
    this.origDims = null; // the entry has the proportions of the new version now
    this.close();
    this.lb.onChange?.({ entry: e, version, replaced, previous });
  }

  // ---------- brush ----------

  /** One tool at a time has the photo: the brush paints on it, the crop box and the levelling grid sit over it. */
  setMode(mode) {
    if (this.mode === mode) return;
    this.mode = mode;
    this.canvas.hidden = mode !== 'brush';
    this.cursor.hidden = true;
    this.cropEl.hidden = mode !== 'crop' && mode !== 'level';
    this.cropEl.classList.toggle('level', mode === 'level');
    this.lb.el.classList.toggle('brushing', mode === 'brush');
    this.cdrag = null;
    this.cropEl.classList.remove('live');
    this.liveKey = null;
    if (mode === 'level') this.liveBase(); // fetch the photo to turn while the user is still reading the panel
    if (mode === 'brush') api.post('/api/retouch/prepare/inpaint').catch(() => {}); // load the model while the user paints
    if (mode === 'crop') this.resetBox();
    this.layout(true);
    this.strokes = 0;
    this.render();
  }

  setBrushMode(on) { this.setMode(on ? 'brush' : null); }

  /** Keep the overlays exactly over the photo; the brush canvas keeps its pixels, so a resize only scales the strokes. */
  layout(reset = false) {
    const r = this.lb.rect;
    if (!r || !this.mode) return;
    if (this.mode === 'brush') {
      Object.assign(this.canvas.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
      if (reset || !this.canvas.width) {
        const dpr = devicePixelRatio || 1;
        this.canvas.width = Math.round(r.w * dpr);
        this.canvas.height = Math.round(r.h * dpr);
        this.clearStrokes();
      }
      return;
    }
    Object.assign(this.cropEl.style, {
      left: `${r.x - HANDLE_PAD}px`, top: `${r.y - HANDLE_PAD}px`,
      width: `${r.w + 2 * HANDLE_PAD}px`, height: `${r.h + 2 * HANDLE_PAD}px`,
    });
    this.cropEl.style.setProperty('--pad', `${HANDLE_PAD}px`);
    this.paintBox();
  }

  // ---------- crop ----------

  /** Width / height the crop box must keep, or null when it is free. */
  lockedRatio() {
    const { id, flip } = this.ratio;
    if (id === 'free') return null;
    const base = id === 'orig' ? this.state.w / this.state.h : RATIOS.find((x) => x.id === id).ratio;
    return flip ? 1 / base : base;
  }

  resetBox() {
    this.box = { l: 0, t: 0, r: 1, b: 1 };
    if (this.lockedRatio()) this.fitRatio();
    this.paintBox();
  }

  chooseRatio(id) {
    const portrait = this.state.h > this.state.w;
    this.ratio = id === this.ratio.id && id !== 'free' && id !== 'orig' ? { id, flip: !this.ratio.flip } : { id, flip: portrait };
    if (id === 'orig') this.ratio.flip = false;
    this.fitRatio();
    this.render();
  }

  /** Give the box the chosen proportions: the same middle and width as far as the photo allows. */
  fitRatio() {
    const ratio = this.lockedRatio();
    const r = this.lb.rect;
    if (!ratio || !r) return;
    const { l, t, r: right, b } = this.box;
    const photo = r.w / r.h;
    let w = Math.min(right - l, 1);
    let h = (w * photo) / ratio;   // fractions of the photo's height
    if (h > 1) { h = 1; w = (h * ratio) / photo; }
    const cx = clamp((l + right) / 2, w / 2, 1 - w / 2);
    const cy = clamp((t + b) / 2, h / 2, 1 - h / 2);
    this.box = { l: cx - w / 2, r: cx + w / 2, t: cy - h / 2, b: cy + h / 2 };
    this.paintBox();
  }

  paintBox() {
    const r = this.lb.rect;
    if (!r || this.mode !== 'crop') return;
    const { l, t, r: right, b } = this.box;
    Object.assign(this.boxEl.style, {
      left: `${HANDLE_PAD + l * r.w}px`, top: `${HANDLE_PAD + t * r.h}px`,
      width: `${(right - l) * r.w}px`, height: `${(b - t) * r.h}px`,
    });
    const size = this.panel.querySelector('[data-crop-size]');
    if (size && this.state.w) size.textContent = `${num(Math.round((right - l) * this.state.w))} × ${num(Math.round((b - t) * this.state.h))} pixel`;
  }

  onCropDown(ev) {
    const handle = ev.target.closest('[data-h]');
    if (!handle || ev.button !== 0 || this.mode !== 'crop') return;
    ev.preventDefault();
    this.cropEl.setPointerCapture(ev.pointerId);
    const r = this.lb.rect;
    const { l, t, r: right, b } = this.box;
    this.cdrag = {
      id: ev.pointerId, h: handle.dataset.h, x: ev.clientX, y: ev.clientY, W: r.w, H: r.h,
      start: { l: l * r.w, t: t * r.h, r: right * r.w, b: b * r.h },
    };
    this.cropEl.classList.add('dragging');
  }

  onCropMove(ev) {
    const d = this.cdrag;
    if (!d || ev.pointerId !== d.id) return;
    const px = dragBox(d.h, d.start, d.W, d.H, ev.clientX - d.x, ev.clientY - d.y, this.lockedRatio());
    this.box = { l: px.l / d.W, t: px.t / d.H, r: px.r / d.W, b: px.b / d.H };
    this.paintBox();
  }

  onCropUp(ev) {
    if (this.cdrag?.id !== ev.pointerId) return;
    this.cdrag = null;
    this.cropEl.classList.remove('dragging');
  }

  async applyCrop() {
    if (this.mode !== 'crop' || this.state.busy) return;
    const { l, t, r, b } = this.box;
    if (r - l > 0.995 && b - t > 0.995) { this.error = 'Trascina i bordi per scegliere l’area da tenere.'; this.render(); return; }
    await this.run('crop', { x: l, y: t, w: r - l, h: b - t });
    if (!this.error) this.setMode(null);
  }

  // ---------- turn and level ----------

  turn(quarters) { return this.run('rotate', { quarters }); }

  /** The photo a levelling starts from, as a second image that the browser can turn at once (and in the GPU). */
  liveBase() {
    const key = `${this.asset}:${this.state.rev}`;
    if (this.liveKey !== key) {
      this.liveKey = key;
      this.liveImg.src = `/media/retouch/${this.asset}?base=1&rev=${this.state.rev}`;
      this.liveReady = this.liveImg.decode().catch(() => {});
    }
    return this.liveReady;
  }

  /** While the slider moves: turn the copy of the photo to the angle, scaled just enough to fill the frame (what the
   *  crop of the result will show). Nothing goes to the server until the slider is released. */
  async slideAngle(angle) {
    this.liveAngle = angle;
    const out = this.panel.querySelector('[data-angle-out]');
    if (out) out.textContent = angleText(angle);
    await this.liveBase();
    const r = this.lb.rect;
    if (this.liveAngle !== angle || this.mode !== 'level' || !r) return;
    const rad = (Math.abs(angle) * Math.PI) / 180;
    const [sin, cos] = [Math.sin(rad), Math.cos(rad)];
    const fit = Math.min(r.w / (r.w * cos + r.h * sin), r.h / (r.w * sin + r.h * cos));
    this.liveImg.style.transform = `rotate(${angle}deg) scale(${1 / fit})`;
    this.cropEl.classList.add('live');
  }

  /** The slider and the ±1° buttons: the newest value wins while the server is still busy with the previous one. */
  level(angle) {
    this.angle = clamp(Math.round(angle * 10) / 10, -MAX_ANGLE, MAX_ANGLE);
    const out = this.panel.querySelector('[data-angle-out]');
    if (out) out.textContent = angleText(this.angle);
    const slider = this.panel.querySelector('[data-angle]');
    if (slider && Number(slider.value) !== this.angle) slider.value = String(this.angle);
    this.queued = this.angle;
    if (!this.nudging) this.flushLevel();
  }

  async flushLevel() {
    this.nudging = true;
    try {
      while (this.queued !== undefined && this.active) {
        const angle = this.queued;
        this.queued = undefined;
        const last = this.state.steps?.[this.state.steps.length - 1];
        const again = last?.tool === 'straighten';
        if (!again && !angle) continue;
        await this.run('straighten', { angle }, again, true);
      }
    } finally {
      this.nudging = false;
    }
    if (this.queued === undefined && this.mode === 'level') {
      this.liveBase();   // the next move starts from the new state
      requestAnimationFrame(() => requestAnimationFrame(() => { if (!this.nudging) this.cropEl.classList.remove('live'); }));
    }
  }

  /** The angle of the levelling step on top of the draft (0 when there is none), or the one just asked for. */
  angleNow() {
    if (this.nudging) return this.angle;
    const last = this.state.steps?.[this.state.steps.length - 1];
    return last?.tool === 'straighten' ? last.angle : 0;
  }

  clearStrokes() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.strokes = 0;
  }

  setBrush(size, sync = true) {
    this.brushSize = Math.round(Math.min(BRUSH.max, Math.max(BRUSH.min, size)));
    const d = this.brushSize;
    Object.assign(this.cursor.style, { width: `${d}px`, height: `${d}px` });
    if (sync) {
      const input = this.panel.querySelector('[data-brush-size]');
      if (input) input.value = String(this.brushSize);
    }
  }

  onDraw(ev) {
    const box = this.canvas.getBoundingClientRect();
    const k = this.canvas.width / box.width;
    const x = (ev.clientX - box.left) * k;
    const y = (ev.clientY - box.top) * k;
    this.cursor.hidden = false;
    this.cursor.style.transform = `translate(${ev.clientX - this.brushSize / 2}px, ${ev.clientY - this.brushSize / 2}px)`;
    const c = this.ctx;
    if (ev.type === 'pointerdown' && ev.button === 0 && !this.state.busy) {
      this.canvas.setPointerCapture(ev.pointerId);
      this.drawing = ev.pointerId;
      Object.assign(c, { lineCap: 'round', lineJoin: 'round', strokeStyle: '#ff5a4f', fillStyle: '#ff5a4f', lineWidth: this.brushSize * k });
      c.beginPath();
      c.arc(x, y, (this.brushSize * k) / 2, 0, Math.PI * 2);
      c.fill();
      c.beginPath();
      c.moveTo(x, y);
    } else if (ev.type === 'pointermove' && this.drawing === ev.pointerId) {
      c.lineTo(x, y);
      c.stroke();
      c.beginPath();
      c.moveTo(x, y);
    } else if ((ev.type === 'pointerup' || ev.type === 'pointercancel') && this.drawing === ev.pointerId) {
      this.drawing = null;
      this.strokes += 1;
      if (this.strokes === 1) this.render();
    }
  }

  async clean() {
    if (!this.strokes || this.state.busy) return;
    const mask = this.canvas.toDataURL('image/png');
    await this.run('inpaint', { mask });
    if (!this.error) this.clearStrokes();
    this.render();
  }

  // ---------- events ----------

  onClick(ev) {
    const t = ev.target.closest('[data-edit], [data-tool], [data-scale], [data-turn], [data-ratio], [data-nudge]');
    if (!t || t.disabled) return;
    if (t.dataset.scale) { this.run('upscale', { scale: Number(t.dataset.scale) }); return; }
    if (t.dataset.turn) { this.turn(Number(t.dataset.turn)); return; }
    if (t.dataset.ratio) { this.chooseRatio(t.dataset.ratio); return; }
    if (t.dataset.nudge) {
      const angle = clamp(Math.round((this.angleNow() + Number(t.dataset.nudge)) * 10) / 10, -MAX_ANGLE, MAX_ANGLE);
      this.slideAngle(angle);
      this.level(angle);
      return;
    }
    if (t.dataset.tool === 'inpaint') { this.setMode(this.brush ? null : 'brush'); return; }
    if (t.dataset.tool === 'crop' || t.dataset.tool === 'level') {
      this.setMode(this.mode === t.dataset.tool ? null : t.dataset.tool);
      return;
    }
    if (t.dataset.tool) {
      const def = TOOLS.find((x) => x.tool === t.dataset.tool);
      this.strengths[def.tool] = def.strength;
      this.run(def.tool, { strength: def.strength });
      return;
    }
    const act = t.dataset.edit;
    if (act === 'save') this.save();
    else if (act === 'cancel') this.requestCancel();
    else if (act === 'discard') this.close();
    else if (act === 'keep') { this.confirming = false; this.render(); }
    else if (act === 'undo') this.undo();
    else if (act === 'redo') this.redo();
    else if (act === 'crop-apply') this.applyCrop();
    else if (act === 'tool-done') this.setMode(null);
    else if (act === 'level-reset') { this.slideAngle(0); this.level(0); }
    else if (act === 'clean') this.clean();
    else if (act === 'clear') { this.clearStrokes(); this.render(); }
    else if (act === 'brush-done') this.setBrushMode(false);
  }

  onSlider(ev) {
    if (ev.target.matches('[data-angle]')) { this.level(Number(ev.target.value)); return; } // released: the server renders it
    const input = ev.target.closest('[data-strength]');
    if (!input) return;
    this.strengths[input.dataset.strength] = Number(input.value) / 100;
    this.run(input.dataset.strength, { strength: Number(input.value) / 100 }, true);
  }

  /** Keys while retouching; returns true when handled. */
  onKey(e) {
    if (e.target.closest?.('input[type="range"]') && e.key.startsWith('Arrow')) return true;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) this.redo(); else this.undo(); return true; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); this.redo(); return true; }
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    if (e.key === ' ') { e.preventDefault(); if (!e.repeat) this.compare(true); return true; }
    if (e.key === 'Escape') {
      e.preventDefault();
      if (this.confirming) { this.confirming = false; this.render(); } else if (this.mode) this.setMode(null); else this.requestCancel();
      return true;
    }
    if (this.brush && (e.key === '[' || e.key === ']')) { this.setBrush(this.brushSize * (e.key === ']' ? 1.2 : 1 / 1.2)); return true; }
    if (!this.brush && (e.key === '[' || e.key === ']')) { this.turn(e.key === ']' ? 1 : -1); return true; }
    if (this.brush && e.key === 'Enter' && !e.target.closest?.('button')) { e.preventDefault(); this.clean(); return true; }
    if (this.mode === 'crop' && e.key === 'Enter' && !e.target.closest?.('button')) { e.preventDefault(); this.applyCrop(); return true; }
    if (e.key === 'b' || e.key === 'B') { this.setMode(this.brush ? null : 'brush'); return true; }
    if (e.key === 'c' || e.key === 'C') { this.setMode(this.mode === 'crop' ? null : 'crop'); return true; }
    return e.key.startsWith('Arrow') || e.key === 'Home' || e.key === 'End' || e.key === 'Delete'; // no browsing while editing
  }

  // ---------- panel ----------

  geometryCards(busy) {
    const open = (tool) => this.mode === tool;
    const turnBtn = (quarters, name, key) => `<button type="button" class="seg" data-turn="${quarters}" ${busy ? 'disabled' : ''}
      title="Ruota a ${name} (${key})" aria-label="Ruota a ${name}">${icon(quarters > 0 ? 'i-rotate-right' : 'i-rotate-left')}</button>`;
    const turn = `<div class="tool-wrap"><div class="tool static">${icon('i-rotate-right')}<span><b>Ruota</b>
      <small>Di 90° a sinistra o a destra.</small></span>
      <div class="segs">${turnBtn(-1, 'sinistra', '[')}${turnBtn(1, 'destra', ']')}</div></div></div>`;

    const ratios = RATIOS.map((r) => {
      const on = this.ratio.id === r.id;
      const turnable = on && r.ratio && r.ratio !== 1;
      const text = turnable && this.ratio.flip ? r.id.split(':').reverse().join(':') : r.label ?? r.id;
      return `<button type="button" class="seg wide" data-ratio="${r.id}" aria-pressed="${on}"
        ${turnable ? 'title="Tocca ancora per girare il formato"' : ''}>${text}</button>`;
    }).join('');
    const cropBox = open('crop') ? `<div class="brush-box">
        <p class="hint">Trascina i bordi o gli angoli, oppure sposta l’area. Un formato fisso si gira toccandolo ancora.</p>
        <div class="ratios">${ratios}</div>
        <p class="dims" data-crop-size></p>
        <div class="row">
          <button type="button" class="dark-btn" data-edit="crop-apply" ${busy ? 'disabled' : ''}>Ritaglia</button>
          <button type="button" class="link-btn" data-edit="tool-done">Fine</button>
        </div></div>` : '';
    const crop = `<div class="tool-wrap${open('crop') ? ' open' : ''}">
      <button type="button" class="tool" data-tool="crop" aria-pressed="${open('crop')}" ${busy ? 'disabled' : ''}>
        ${icon('i-crop')}<span><b>Ritaglia</b><small>Tieni solo l’area che conta, con le proporzioni che vuoi.</small></span></button>${cropBox}</div>`;

    const angle = this.angleNow();
    const levelBox = open('level') ? `<div class="brush-box">
        <p class="hint">Allinea l’orizzonte o un bordo alle linee della griglia. I margini vuoti vengono tagliati.</p>
        <div class="angle"><output data-angle-out>${angleText(angle)}</output>
          <input type="range" min="${-MAX_ANGLE}" max="${MAX_ANGLE}" step="0.1" value="${angle}" data-angle aria-label="Inclinazione in gradi"></div>
        <div class="row">
          <button type="button" class="seg wide" data-nudge="-1" title="Un grado a sinistra">−1°</button>
          <button type="button" class="seg wide" data-nudge="1" title="Un grado a destra">+1°</button>
          <button type="button" class="link-btn" data-edit="level-reset">Azzera</button>
          <button type="button" class="link-btn" data-edit="tool-done">Fine</button>
        </div></div>` : '';
    const level = `<div class="tool-wrap${open('level') ? ' open' : ''}">
      <button type="button" class="tool" data-tool="level" aria-pressed="${open('level')}" ${busy ? 'disabled' : ''}>
        ${icon('i-level')}<span><b>Raddrizza</b><small>Corregge una foto storta, anche di pochi gradi.</small></span></button>${levelBox}</div>`;
    return turn + crop + level;
  }

  stepsHtml(busy) {
    const steps = this.state.steps ?? [];
    if (!steps.length && !this.state.redo) return '';
    const undo = steps.length
      ? `<button type="button" class="link-btn" data-edit="undo" ${busy ? 'disabled' : ''} title="Annulla (Ctrl+Z)">${icon('i-undo')}Annulla l’ultimo</button>` : '';
    const redo = this.state.redo
      ? `<button type="button" class="link-btn" data-edit="redo" ${busy ? 'disabled' : ''} title="Ripristina (Ctrl+Maiusc+Z)">Ripristina</button>` : '';
    const compare = steps.length
      ? '<button type="button" class="link-btn" data-edit="compare" aria-pressed="false" title="Tieni premuto (o tieni premuto Spazio)">Confronta con prima</button>' : '';
    return `<section class="steps"><h3>Passaggi</h3>${steps.length ? `<ol>${steps.map((st) => `<li>${esc(st.label)}</li>`).join('')}</ol>` : ''}
        <div class="row">${undo}${redo}${compare}</div></section>`;
  }

  footHtml(busy) {
    const steps = this.state.steps ?? [];
    return this.confirming
      ? `<div class="edit-foot confirm"><p>Scartare le modifiche a questa foto?</p>
          <button type="button" class="dark-btn danger" data-edit="discard">Scarta</button>
          <button type="button" class="link-btn" data-edit="keep">Continua a ritoccare</button></div>`
      : `<div class="edit-foot">
          <button type="button" class="dark-btn" data-edit="save" ${!steps.length || busy ? 'disabled' : ''}>Salva come nuova versione</button>
          <button type="button" class="link-btn" data-edit="cancel" ${busy ? 'disabled' : ''}>Annulla</button></div>`;
  }

  errorHtml() {
    return this.error ? `<p class="edit-error" role="alert">${esc(this.error)}</p>` : '';
  }

  bindCompare() {
    const cmp = this.panel.querySelector('[data-edit="compare"]');
    if (!cmp) return;
    cmp.addEventListener('pointerdown', () => this.compare(true));
    for (const t of ['pointerup', 'pointerleave', 'pointercancel']) cmp.addEventListener(t, () => this.compare(false));
  }

  /** Only the history, the error and the buttons at the bottom: used while a slider is being dragged. */
  renderParts() {
    if (!this.active) return;
    const busy = Boolean(this.state.busy);
    const set = (part, html) => { const n = this.panel.querySelector(`[data-part="${part}"]`); if (n) n.innerHTML = html; };
    set('steps', this.stepsHtml(busy));
    set('foot', this.footHtml(busy));
    set('error', this.errorHtml());
    this.bindCompare();
  }

  render() {
    if (!this.active) return;
    const s = this.state;
    const busy = Boolean(s.busy);
    const steps = s.steps ?? [];
    const last = steps[steps.length - 1];
    const toolCard = (def) => {
      const pressed = def.tool === 'inpaint' && this.brush;
      const dl = s.downloads?.[def.tool];
      let extra = '';
      if (def.strength && last?.tool === def.tool) {
        extra = `<label class="strength"><span>Intensità</span>
          <input type="range" min="20" max="100" step="5" value="${Math.round((this.strengths[def.tool] ?? def.strength) * 100)}"
            data-strength="${def.tool}" ${busy ? 'disabled' : ''} aria-label="Intensità: ${esc(def.title)}"></label>`;
      }
      if (pressed) {
        extra = `<div class="brush-box">
          <label class="strength"><span>Pennello</span>
            <input type="range" min="${BRUSH.min}" max="${BRUSH.max}" value="${this.brushSize}" data-brush-size aria-label="Dimensione del pennello"></label>
          <p class="hint">Dipingi sulla foto le zone da ripulire. Rotellina o [ ] per cambiare la dimensione.</p>
          <div class="row">
            <button type="button" class="dark-btn" data-edit="clean" ${!this.strokes || busy ? 'disabled' : ''}>Ripulisci</button>
            <button type="button" class="link-btn" data-edit="clear" ${!this.strokes || busy ? 'disabled' : ''}>Cancella i tratti</button>
            <button type="button" class="link-btn" data-edit="brush-done">Fine</button>
          </div></div>`;
      }
      return `<div class="tool-wrap${pressed ? ' open' : ''}">
        <button type="button" class="tool" data-tool="${def.tool}" ${busy ? 'disabled' : ''} ${def.tool === 'inpaint' ? `aria-pressed="${pressed}"` : ''}>
          ${icon(def.icon)}<span><b>${def.title}</b><small>${def.text}</small>
          ${dl ? `<small class="dl">${icon('i-download')}La prima volta scarica ${dl} MB</small>` : ''}</span></button>${extra}</div>`;
    };
    const scales = [2, 4].map((k) => {
      const ok = s.upscale?.includes(k);
      const dl = s.downloads?.[`upscale${k}`];
      return `<button type="button" class="seg" data-scale="${k}" ${!ok || busy ? 'disabled' : ''}
        title="${ok ? `${num(s.w * k)} × ${num(s.h * k)} pixel${dl ? ` · la prima volta scarica ${dl} MB` : ''}` : 'La foto è già abbastanza grande'}">×${k}</button>`;
    }).join('');
    const upscale = `<div class="tool-wrap"><div class="tool static">${icon('i-expand')}<span><b>Ingrandisci</b>
      <small>${!s.upscale || s.upscale.length ? 'Più pixel per le foto piccole o vecchie, con dettagli ricostruiti.' : 'La foto è già abbastanza grande per stampe e schermi.'}</small>
      ${s.w ? `<small class="dims">Ora ${num(s.w)} × ${num(s.h)} pixel</small>` : ''}</span>
      <div class="segs">${scales}</div></div></div>`;

    const progress = busy ? `<div class="progress" role="status">
        <p>${esc(s.phase || 'Elaboro…')}</p>
        <div class="bar${s.total ? '' : ' indeterminate'}"><i style="width:${s.total ? Math.round((100 * s.done) / s.total) : 40}%"></i></div></div>` : '';

    const focused = document.activeElement?.closest?.('.lb-edit button, .lb-edit input');
    const focusKey = focused && ['tool', 'edit', 'scale', 'strength', 'turn', 'ratio', 'nudge']
      .map((k) => focused.dataset[k] !== undefined && `[data-${k}="${focused.dataset[k]}"]`).find(Boolean);
    this.panel.innerHTML = `
      <h2>Ritocco</h2>
      <p class="sub">${s.from_version ? 'Parti dalla versione ritoccata. ' : ''}L’originale resta com’è: il risultato si aggiunge come nuova versione.</p>
      <div class="tools">${this.geometryCards(busy)}${TOOLS.map(toolCard).join('')}${upscale}</div>
      ${progress}<div data-part="error">${this.errorHtml()}</div>
      <div data-part="steps">${this.stepsHtml(busy)}</div><div data-part="foot">${this.footHtml(busy)}</div>`;
    if (focusKey) this.panel.querySelector(focusKey)?.focus({ preventScroll: true }); // re-rendering must not lose the keyboard focus
    this.bindCompare();
    this.paintBox();
  }
}

const angleText = (angle) => `${angle > 0 ? '+' : ''}${String(angle).replace('.', ',')}°`;
