// Retouching inside the viewer. The photo stays where it is; a panel slides in with the tools. Each tool
// adds a step to a draft kept by the server; the result is saved as a new version, the original is never touched.
// Hold "Confronta" (or Space) to see the photo as it was; the brush paints the areas to clean on a canvas over it.

import { api } from './api.js';
import { esc, icon, num } from './format.js';
import { animate } from './spring.js';

const PANEL_W = 360;
const POLL_MS = 250;
const BRUSH = { min: 6, max: 120, start: 28 };

export const STEP_LABEL = {
  color: () => 'Colori migliorati',
  faces: (s) => `Volti ripristinati${s.faces ? ` (${s.faces})` : ''}`,
  inpaint: () => 'Macchie rimosse',
  upscale: (s) => `Ingrandita ×${s.scale}`,
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
    this.ctx = this.canvas.getContext('2d');
    this.active = false;
    this.panelX = PANEL_W;
    this.panel.style.transform = `translateX(${PANEL_W}px)`;
    this.brushSize = BRUSH.start;
    this.strengths = {};

    this.panel.addEventListener('click', (e) => this.onClick(e));
    this.panel.addEventListener('change', (e) => this.onSlider(e));
    this.panel.addEventListener('input', (e) => {
      if (e.target.matches('[data-brush-size]')) this.setBrush(Number(e.target.value), false);
    });
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

  // ---------- open / close ----------

  async open() {
    const e = this.lb.entry;
    if (!e?.asset || e.kind !== 'image' || this.active) return;
    this.entry = e;
    this.active = true;
    this.brush = false;
    this.confirming = false;
    this.error = null;
    this.state = { busy: true, phase: 'Apro la foto', steps: [], downloads: {} };
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
    this.setBrushMode(false);
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

  async run(tool, params = {}, replaceLast = false) {
    if (this.state.busy) return;
    this.error = null;
    this.confirming = false;
    try {
      this.state = await api.post(`/api/retouch/${this.asset}/apply`, { tool, params, replace_last: replaceLast });
    } catch (err) {
      this.error = err.message;
      this.render();
      return;
    }
    this.render();
    while (this.state.busy && this.active) {
      await sleep(POLL_MS);
      try { this.state = await api.get('/api/retouch'); } catch { continue; }
      this.render();
    }
    if (!this.active) return;
    if (this.state.error) this.error = this.state.error;
    else await this.showPreview();
    this.render();
  }

  async undo() {
    if (this.state.busy || !this.state.steps?.length) return;
    this.state = await api.post(`/api/retouch/${this.asset}/undo`);
    await this.showPreview();
    this.render();
  }

  async showPreview() {
    const url = this.state.steps.length ? `/media/retouch/${this.asset}?rev=${this.state.rev}` : this.baseSrc;
    const im = new Image();
    im.src = url;
    try { await im.decode(); } catch { /* shown anyway */ }
    if (!this.active) return;
    this.previewSrc = url;
    if (!this.comparing) this.lb.img.src = url;
  }

  compare(on) {
    if (!this.active || this.comparing === on || !this.previewSrc) return;
    this.comparing = on;
    this.lb.img.src = on ? this.baseSrc : this.previewSrc;
    this.lb.el.classList.toggle('comparing', on);
    this.panel.querySelector('[data-edit="compare"]')?.setAttribute('aria-pressed', String(on));
  }

  async save() {
    if (this.state.busy || !this.state.steps?.length) return;
    let version;
    try {
      ({ version } = await api.post(`/api/retouch/${this.asset}/save`));
    } catch (err) {
      this.error = err.message;
      this.render();
      return;
    }
    const e = this.entry;
    e.version = { id: version.id, v: version.v };
    e.w = version.w;
    e.h = version.h;
    this.lb.showOriginal = false;
    this.lb.assetCache.delete(e.asset);
    this.state.steps = [];
    this.close();
    this.lb.onChange?.({ entry: e, version });
  }

  // ---------- brush ----------

  setBrushMode(on) {
    this.brush = on;
    this.canvas.hidden = !on;
    this.cursor.hidden = true;
    this.lb.el.classList.toggle('brushing', on);
    if (on) {
      this.layout(true);
      api.post('/api/retouch/prepare/inpaint').catch(() => {}); // load the model while the user paints
    }
    this.strokes = 0;
    this.render();
  }

  /** Keep the canvas exactly over the photo; its pixels are kept, so a resize only scales the strokes. */
  layout(reset = false) {
    const r = this.lb.rect;
    if (!r || !this.brush) return;
    Object.assign(this.canvas.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
    if (reset || !this.canvas.width) {
      const dpr = devicePixelRatio || 1;
      this.canvas.width = Math.round(r.w * dpr);
      this.canvas.height = Math.round(r.h * dpr);
      this.clearStrokes();
    }
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
    const t = ev.target.closest('[data-edit], [data-tool], [data-scale]');
    if (!t || t.disabled) return;
    if (t.dataset.scale) { this.run('upscale', { scale: Number(t.dataset.scale) }); return; }
    if (t.dataset.tool === 'inpaint') { this.setBrushMode(!this.brush); return; }
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
    else if (act === 'clean') this.clean();
    else if (act === 'clear') { this.clearStrokes(); this.render(); }
    else if (act === 'brush-done') this.setBrushMode(false);
  }

  onSlider(ev) {
    const input = ev.target.closest('[data-strength]');
    if (!input) return;
    this.strengths[input.dataset.strength] = Number(input.value) / 100;
    this.run(input.dataset.strength, { strength: Number(input.value) / 100 }, true);
  }

  /** Keys while retouching; returns true when handled. */
  onKey(e) {
    if (e.target.closest?.('input[type="range"]') && e.key.startsWith('Arrow')) return true;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); this.undo(); return true; }
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    if (e.key === ' ') { e.preventDefault(); if (!e.repeat) this.compare(true); return true; }
    if (e.key === 'Escape') {
      e.preventDefault();
      if (this.confirming) { this.confirming = false; this.render(); } else if (this.brush) this.setBrushMode(false); else this.requestCancel();
      return true;
    }
    if (this.brush && (e.key === '[' || e.key === ']')) { this.setBrush(this.brushSize * (e.key === ']' ? 1.2 : 1 / 1.2)); return true; }
    if (this.brush && e.key === 'Enter' && !e.target.closest?.('button')) { e.preventDefault(); this.clean(); return true; }
    if (e.key === 'b' || e.key === 'B') { this.setBrushMode(!this.brush); return true; }
    return e.key.startsWith('Arrow') || e.key === 'Home' || e.key === 'End' || e.key === 'Delete'; // no browsing while editing
  }

  // ---------- panel ----------

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
    const error = this.error ? `<p class="edit-error" role="alert">${esc(this.error)}</p>` : '';
    const history = steps.length ? `<section class="steps"><h3>Passaggi</h3><ol>${steps.map((st) => `<li>${esc(st.label)}</li>`).join('')}</ol>
        <div class="row"><button type="button" class="link-btn" data-edit="undo" ${busy ? 'disabled' : ''}>${icon('i-undo')}Annulla l’ultimo</button>
        <button type="button" class="link-btn" data-edit="compare" aria-pressed="false" title="Tieni premuto (o tieni premuto Spazio)">Confronta con prima</button></div></section>` : '';
    const footer = this.confirming
      ? `<div class="edit-foot confirm"><p>Scartare le modifiche a questa foto?</p>
          <button type="button" class="dark-btn danger" data-edit="discard">Scarta</button>
          <button type="button" class="link-btn" data-edit="keep">Continua a ritoccare</button></div>`
      : `<div class="edit-foot">
          <button type="button" class="dark-btn" data-edit="save" ${!steps.length || busy ? 'disabled' : ''}>Salva come nuova versione</button>
          <button type="button" class="link-btn" data-edit="cancel" ${busy ? 'disabled' : ''}>Annulla</button></div>`;

    const focused = document.activeElement?.closest?.('.lb-edit [data-tool], .lb-edit [data-edit], .lb-edit [data-scale], .lb-edit input');
    const focusKey = focused && ['tool', 'edit', 'scale', 'strength'].map((k) => focused.dataset[k] && `[data-${k}="${focused.dataset[k]}"]`).find(Boolean);
    this.panel.innerHTML = `
      <h2>Ritocco</h2>
      <p class="sub">${s.from_version ? 'Parti dalla versione ritoccata. ' : ''}L’originale resta com’è: il risultato si aggiunge come nuova versione.</p>
      <div class="tools">${TOOLS.map(toolCard).join('')}${upscale}</div>
      ${progress}${error}${history}${footer}`;
    if (focusKey) this.panel.querySelector(focusKey)?.focus({ preventScroll: true }); // re-rendering must not lose the keyboard focus
    const cmp = this.panel.querySelector('[data-edit="compare"]');
    if (cmp) {
      cmp.addEventListener('pointerdown', () => this.compare(true));
      for (const t of ['pointerup', 'pointerleave', 'pointercancel']) cmp.addEventListener(t, () => this.compare(false));
    }
  }
}
