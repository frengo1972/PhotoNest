// Screens other than the gallery: overview, duplicates, bursts, dates to check, trash, sources.
// Each render(el, ctx) owns `el` until the next route change.

import { api, q, thumbUrl } from './api.js';
import {
  DATE_SOURCE, REASON, SOURCE, bytes, dateTime, esc, icon, issueSentences, num, plural, sourceLabel, timeOf,
} from './format.js';
import { copyRow } from './lightbox.js';

const PAGE = 20;

const listJoin = (items) => (items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} e ${items.at(-1)}`);

function pageHead(title, text, tools = '') {
  return `<header class="page-head"><div><h1>${title}</h1><p>${text}</p></div>${tools ? `<div class="tools">${tools}</div>` : ''}</header>`;
}

const emptyBlock = (title, text, actions = '') =>
  `<div class="empty"><h2>${title}</h2><p>${text}</p>${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;

/** Viewer entry for an item row returned by the API. */
function entryOf(m, extra = {}) {
  return { item: m.id, v: m.v, w: m.w, h: m.h, kind: m.kind, date: m.date_local, ...extra };
}

/** Viewer origin for a list of entries shown by `tiles` (same order). */
const originOf = (entries, tiles) => ({ tile: (e) => tiles[entries.indexOf(e)] ?? null });

const seg = (attr, value, label, on) => `<button type="button" data-${attr}="${value}" aria-pressed="${on}">${label}</button>`;

// ---------------------------------------------------------------- overview

function nasStep(nas) {
  if (nas === undefined) return { done: false, text: 'Controllo dell’archivio sul NAS…', label: 'Apri' };
  if (!nas?.root) return { done: false, text: 'Scegli dove salvare l’archivio: per ora una cartella su V:, poi il NAS.', label: 'Scegli la destinazione' };
  if (!nas.exists) return { done: false, text: `La destinazione ${nas.root} non è raggiungibile.`, label: 'Controlla' };
  const p = nas.plan;
  const pending = p.copy + p.meta + p.move + p.orphans;
  if (!pending) return { done: true, text: `Archivio aggiornato in ${nas.root}.`, label: 'Apri' };
  return {
    done: false,
    text: p.copy ? `${plural(p.copy, 'foto da copiare', 'foto da copiare')} in ${nas.root} (${bytes(p.bytes_to_copy)}).`
      : `${plural(pending, 'modifica da riportare', 'modifiche da riportare')} sull’archivio in ${nas.root}.`,
    label: p.skip ? 'Aggiorna l’archivio' : 'Crea l’archivio',
  };
}

function aiStep(ai, c) {
  if (ai === undefined) return { done: false, text: 'Controllo delle foto analizzate…', action: '' };
  if (!ai || !ai.total) return { done: false, text: 'Dopo il primo aggiornamento, l’AI può riconoscere volti, luoghi e oggetti.', action: '' };
  if (ai.pending) {
    return {
      done: false,
      text: `${plural(ai.pending, 'foto da analizzare', 'foto da analizzare')}: volti, luoghi e oggetti vengono riconosciuti su questo PC, nessuna foto esce dal computer.`,
      action: `<button class="btn btn-primary" type="button" data-ai-analyze>Analizza le foto</button>`,
    };
  }
  if (c.face_groups) {
    return { done: false, text: `${plural(c.face_groups, 'gruppo di volti', 'gruppi di volti')} da riconoscere: dai un nome e verrà aggiunto a tutte le foto.`, action: 'persone' };
  }
  return { done: true, text: `Foto analizzate: ${num(ai.tagged)} hanno etichette automatiche, ${num(ai.faces)} volti trovati.`, action: '' };
}

/** The overview is shown at once; the NAS plan and the AI counts (slower on a large archive) fill it in after. */
export async function renderOverview(el, ctx) {
  if (!ctx.state.user) { overviewHtml(el, ctx, null, null, null); return; }
  overviewHtml(el, ctx, undefined, undefined, undefined);
  const [nas, ai, vlm] = await Promise.all([
    api.get('/api/nas').catch(() => null), api.get('/api/ai').catch(() => null), api.get('/api/vlm').catch(() => null)]);
  if (ctx.current === 'panoramica' && el.querySelector('.steps')) overviewHtml(el, ctx, nas, ai, vlm);
}

function captionStep(vlm) {
  if (vlm === undefined) return { done: false, text: 'Controllo delle descrizioni…', later: true };
  if (!vlm || !vlm.total) return { done: false, text: 'Dopo il primo aggiornamento, un modello sul tuo PC può scrivere una descrizione di ogni foto.', later: true };
  if (vlm.pending) {
    return {
      done: false,
      text: `${plural(vlm.pending, 'foto senza descrizione', 'foto senza descrizione')}: un modello di linguaggio sul tuo PC scrive una frase e alcune etichette, che si possono cercare.`,
    };
  }
  return { done: true, text: `Tutte le ${plural(vlm.described, 'foto', 'foto')} hanno una descrizione.` };
}

function overviewHtml(el, ctx, nas, ai, vlm) {
  const s = ctx.state;
  const c = s.counts;
  const ns = nasStep(nas);
  const as = aiStep(ai, c);
  const cs = captionStep(vlm);
  const google = s.sources.filter((x) => x.type === 'google' || x.type === 'icloud').length;
  const devices = s.sources.length - google;
  const step = (n, done, title, text, action = '', later = false) => `
    <li class="step ${later ? 'later' : done ? 'done' : 'todo'}">
      <span class="step-mark">${done && !later ? icon('i-check') : n}</span>
      <h3>${title}</h3>${action}<p>${text}</p>
    </li>`;
  const go = (route, label, primary = false) => `<a class="btn ${primary ? 'btn-primary' : ''}" href="#/${route}">${label}</a>`;
  const scanBtn = `<button class="btn ${s.pending.length ? 'btn-primary' : ''}" type="button" data-action="scan" ${s.job.running ? 'disabled' : ''}>${icon('i-refresh')}Aggiorna archivio</button>`;

  el.innerHTML = `<div class="page">
    ${pageHead('Panoramica',
      s.user ? `L’archivio di ${esc(s.user)} si costruisce in pochi passi. Le scelte si possono cambiare in qualsiasi momento: gli originali non vengono mai toccati.`
        : 'Per cominciare, aggiungi le foto da ordinare.')}
    ${s.user ? `<div class="overview-total">
      <div><b>${num(c.assets)}</b><span>foto e video nell’archivio</span></div>
      <div><b>${num(c.copies)}</b><span>copie trovate nelle fonti</span></div>
      <div><b>${num(c.excluded)}</b><span>nel cestino</span></div>
    </div>` : ''}
    <ol class="steps">
      ${step(1, s.sources.length > 0 && !s.pending.length, 'Raccogli le foto',
        s.pending.length ? `${plural(s.pending.length, 'cartella nuova', 'cartelle nuove')} da leggere nella cartella income.`
          : s.sources.length ? `Lette ${plural(google, 'fonte cloud', 'fonti cloud')} e ${plural(devices, 'telefono o disco', 'telefoni o dischi')}. Aggiungine altre quando vuoi.`
            : 'Metti nella cartella income gli zip di Google Takeout e le copie dei telefoni.',
        s.pending.length || !s.sources.length ? scanBtn : go('fonti', 'Vedi le fonti'))}
      ${step(2, s.user && !c.duplicates, 'Controlla i duplicati',
        c.duplicates ? `${plural(c.duplicates, 'foto trovata', 'foto trovate')} in più versioni diverse: conferma quale copia tenere.`
          : 'Nessun gruppo da controllare. Le copie identiche sono già unite.',
        c.duplicates ? go('duplicati', 'Rivedi', true) : '')}
      ${step(3, s.user && !c.variants_todo, 'Scegli tra le raffiche',
        c.variants_todo ? `${plural(c.variants_todo, 'gruppo', 'gruppi')} di scatti fatti nello stesso secondo. Tieni il migliore o tienili tutti.`
          : 'Nessuna raffica da scegliere.',
        c.variants_todo ? go('varianti', 'Scegli', true) : '')}
      ${step(4, s.user && !c.issues, 'Verifica le date',
        c.issues ? `${plural(c.issues, 'foto ha', 'foto hanno')} date incoerenti o mancanti.` : 'Tutte le date sono coerenti.',
        c.issues ? go('verifica', 'Correggi', true) : '')}
      ${step(5, as.done, 'Riconosci persone e cose', esc(as.text),
        as.action === 'persone' ? go('persone', 'Dai un nome', true) : as.action)}
      ${step(6, cs.done, 'Descrivi le foto', esc(cs.text),
        cs.done || cs.later ? go('didascalie', 'Apri') : go('didascalie', 'Descrivi le foto', true), cs.later)}
      ${step(7, false, 'Ordina e sistema',
        'Nella galleria cerca, seleziona le foto per escluderle, metterle tra le preferite, aggiungerle a un album, indicare le persone o correggere la data.',
        go('galleria', 'Apri la galleria'))}
      ${step(8, ns.done, 'Crea l’archivio sul NAS', `${esc(ns.text)} Le foto sono ordinate per anno ed evento, con tutti i dati scritti nei file.`,
        go('nas', ns.label, !ns.done && Boolean(s.user)))}
    </ol></div>`;
  el.onclick = (e) => {
    if (e.target.closest('[data-ai-analyze]')) ctx.startJob('/api/ai/analyze');
  };
}

// ---------------------------------------------------------------- people

const faceImg = (id, cls = '') => `<img class="${cls}" src="/media/face/${id}" alt="" loading="lazy" draggable="false">`;

export async function renderPeople(el, ctx) {
  const [ai, data] = await Promise.all([api.get('/api/ai'), api.get(`/api/people?${q({ user: ctx.user })}`)]);
  const opts = (ctx.viewState.persone ??= { singles: false });
  const multi = data.groups.filter((g) => g.photos >= 2);
  const singles = data.groups.filter((g) => g.photos < 2);
  const shown = opts.singles ? data.groups : multi;
  const groupRow = (g) => `
    <div class="face-group" data-cluster="${g.cluster}">
      <div class="face-row">${g.faces.slice(0, 8).map((id) => `
        <span class="face">${faceImg(id)}<button class="reject" type="button" data-reject="${id}" title="Non fa parte del gruppo" aria-label="Togli questo volto">${icon('i-close')}</button></span>`).join('')}</div>
      <div class="info"><b>${plural(g.photos, 'foto', 'foto')}</b><span>${g.faces.length > 8 ? `e altri ${num(g.faces.length - 8)} volti` : ''}</span></div>
      <div class="tools">
        ${g.suggested ? `<button class="btn btn-primary" type="button" data-name="${esc(g.suggested)}">Sì, è ${esc(g.suggested)}</button>` : ''}
        <button class="btn ${g.suggested ? '' : 'btn-primary'}" type="button" data-ask>${g.suggested ? 'È qualcun altro' : 'Chi è?'}</button>
      </div>
    </div>`;
  const person = (p) => `
    <button class="person" type="button" data-person="${esc(p.name)}">
      <span class="avatar">${p.faces.length ? faceImg(p.faces[0]) : esc(p.name.slice(0, 1).toUpperCase())}</span>
      <b>${esc(p.name)}</b><span>${plural(p.photos, 'foto', 'foto')}</span>
    </button>`;
  const analyzeBtn = ai.pending
    ? `<button class="btn btn-primary" type="button" data-analyze ${ctx.state.job.running ? 'disabled' : ''}>Analizza ${plural(ai.pending, 'foto', 'foto')}</button>`
    : '';

  el.innerHTML = `<div class="page">
    ${pageHead('Persone',
      'I volti vengono riconosciuti su questo PC: nessuna foto esce dal computer. Dai un nome a un gruppo e il nome viene aggiunto a tutte le sue foto, anche nell’archivio sul NAS.',
      analyzeBtn)}
    <div class="jobpanel-slot"></div>
    ${!ai.total ? emptyBlock('Ancora nessuna foto', 'Aggiorna l’archivio, poi analizza le foto.')
      : ai.pending === ai.total ? emptyBlock('Le foto non sono ancora state analizzate', 'L’analisi trova i volti e li raggruppa per persona. Le foto già analizzate non vengono rifatte.')
        : ''}
    ${shown.length ? `<h2 class="section-title">Da riconoscere</h2>${shown.map(groupRow).join('')}` : ''}
    ${singles.length ? `<div class="more"><button class="btn btn-quiet" type="button" data-singles>${opts.singles ? 'Nascondi i volti visti una volta sola' : `Mostra anche i volti visti una volta sola (${num(singles.length)})`}</button></div>` : ''}
    ${data.people.length ? `<h2 class="section-title">Con un nome</h2><div class="people-grid">${data.people.map(person).join('')}</div>` : ''}
  </div>`;
  renderJobPanel(el.querySelector('.jobpanel-slot'), ctx.state.job);

  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    try {
      if ('analyze' in t.dataset) { t.disabled = true; await ctx.startJob('/api/ai/analyze'); return; }
      if ('singles' in t.dataset) { opts.singles = !opts.singles; renderPeople(el, ctx); return; }
      if (t.dataset.person) { ctx.searchFor(t.dataset.person); return; }
      if (t.dataset.reject) {
        await api.post(`/api/faces/${t.dataset.reject}/reject`);
        renderPeople(el, ctx);
        return;
      }
      const row = t.closest('[data-cluster]');
      const g = row && data.groups.find((x) => x.cluster === Number(row.dataset.cluster));
      if (!g) return;
      const name = t.dataset.name ?? await ctx.ask({
        title: 'Chi è?', text: `Il nome verrà aggiunto a ${plural(g.photos, 'foto', 'foto')}.`, label: 'Nome',
        list: 'names-person', confirm: 'Salva', hint: 'Se il nome esiste già, i due gruppi vengono uniti.',
      });
      if (!name) return;
      t.disabled = true;
      await api.post('/api/people/name', { faces: g.faces, name });
      ctx.toast(`${name}: ${plural(g.photos, 'foto', 'foto')}`);
      ctx.galleryChanged();
      ctx.loadNames();
      await ctx.refresh();
      renderPeople(el, ctx);
    } catch (err) {
      t.disabled = false;
      ctx.toast(err.message);
    }
  };
}

// ---------------------------------------------------------------- NAS archive

const EXAMPLE_TREE = (root, user) => `<b>${esc(root || 'X:\\ArchivioFoto')}</b>
└ ${esc(user || 'utente')}
   ├ 2019
   │  └ 2019-10-09 Gita al mare
   │     ├ 2019-10-09_084004__DSC2630.jpg     data, luogo, persone e album dentro il file
   │     └ 2019-10-09_091512_VID_0042.mp4
   ├ senza-data                               foto di cui non si conosce la data
   ├ _esclusi                                 foto tolte dopo un export precedente (mai cancellate)
   └ _indice.csv                              elenco di tutti i file con le loro fonti, apribile in Excel`;

/** Settings at once; what an export would do takes seconds on a large archive and fills in after. */
export async function renderNas(el, ctx) {
  const quick = await api.get('/api/nas?plan=false');
  nasHtml(el, ctx, quick, quick.exists);
  if (!quick.exists) return;
  const nas = await api.get('/api/nas');
  if (ctx.current === 'nas' && el.querySelector('#nas-root')) nasHtml(el, ctx, nas, false);
}

function nasHtml(el, ctx, nas, planning) {
  const p = nas.plan;
  const last = nas.last;
  const pending = p ? p.copy + p.meta + p.move + p.orphans : 0;
  const status = !nas.root ? '<p class="dest-status">Nessuna destinazione scelta.</p>'
    : nas.exists ? `<p class="dest-status">${bytes(nas.free)} liberi sulla destinazione.</p>`
      : `<p class="dest-status bad">La cartella non è raggiungibile: controlla che l’unità o il NAS sia collegato.</p>`;
  const stats = p ? `<div class="overview-total">
      <div><b>${num(p.copy)}</b><span>da copiare, ${bytes(p.bytes_to_copy)}</span></div>
      <div><b>${num(p.meta + p.move)}</b><span>da aggiornare o spostare</span></div>
      <div><b>${num(p.skip)}</b><span>già nell’archivio</span></div>
      <div><b>${num(p.orphans)}</b><span>da spostare in _esclusi</span></div>
    </div>` : '';
  const action = !nas.exists ? ''
    : `<div class="nas-actions">
        <button class="btn btn-primary" type="button" data-export ${!pending || ctx.state.job.running || !nas.exiftool ? 'disabled' : ''}>
          ${icon('i-refresh')}${!pending ? 'Archivio aggiornato' : p.skip ? 'Aggiorna l’archivio' : 'Crea l’archivio'}</button>
        <button class="btn" type="button" data-open-nas>${icon('i-folder')}Apri la cartella</button>
      </div>`;
  const lastBlock = last ? `<h2 class="section-title">Ultimo aggiornamento</h2>
    <p class="hint">${esc(last.finished_at)} in ${esc(last.root)}: copiati ${num(last.copy)}, aggiornati ${num(last.meta + last.move)},
      spostati in _esclusi ${num(last.orphans)}${last.sidecars ? `, ${num(last.sidecars)} con i dati in un file .xmp accanto` : ''}.
      ${last.n_errors ? `<b>${plural(last.n_errors, 'errore', 'errori')}:</b>` : 'Nessun errore.'}</p>
    ${last.n_errors ? `<ul class="errors-list">${last.errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}` : '';

  el.innerHTML = `<div class="page">
    ${pageHead('Archivio sul NAS',
      'La copia definitiva delle foto incluse, ordinate per anno ed evento, con data, luogo, persone, album ed etichette scritti dentro i file: leggibile da qualsiasi programma. Puoi aggiornarla quando vuoi, copia solo le novità.')}
    <h2 class="section-title">Destinazione</h2>
    <div class="dest">
      <input id="nas-root" value="${esc(nas.root ?? '')}" placeholder="Per esempio X:\\ArchivioFoto oppure \\\\NAS\\foto" aria-label="Cartella dell’archivio" autocomplete="off" spellcheck="false">
      <button class="btn" type="button" data-pick>${icon('i-folder')}Scegli…</button>
      <button class="btn" type="button" data-save>Salva</button>
    </div>
    ${status}
    ${nas.exiftool ? '' : '<p class="dest-status bad">Manca exiftool, necessario per scrivere i dati nei file: installalo con <code>winget install OliverBetz.ExifTool</code>.</p>'}
    ${p ? `<h2 class="section-title">Cosa farà</h2>${stats}${action}`
    : planning ? '<h2 class="section-title">Cosa farà</h2><p class="hint">Confronto tra il catalogo e la destinazione…</p>' : ''}
    <div class="jobpanel-slot"></div>
    ${lastBlock}
    <h2 class="section-title">Come sarà organizzato</h2>
    <div class="tree">${EXAMPLE_TREE(nas.root, ctx.user)}</div>
  </div>`;
  renderJobPanel(el.querySelector('.jobpanel-slot'), ctx.state.job);

  const input = el.querySelector('#nas-root');
  async function save(root) {
    try {
      await api.post('/api/nas', { root });
    } catch (err) {
      if (!/non esiste/.test(err.message)) { ctx.toast(err.message); return; }
      if (!(await ctx.confirm('Creare la cartella?', `${root} non esiste ancora. La creo adesso?`, 'Crea'))) return;
      try { await api.post('/api/nas', { root, create: true }); } catch (e2) { ctx.toast(e2.message); return; }
    }
    ctx.toast('Destinazione salvata');
    renderNas(el, ctx);
  }
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(input.value); });
  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    try {
      if ('save' in t.dataset) await save(input.value);
      if ('pick' in t.dataset) {
        t.disabled = true;
        const { path } = await api.post('/api/pick-folder');
        t.disabled = false;
        if (path) { input.value = path; await save(path); }
      }
      if ('openNas' in t.dataset) await api.post('/api/open-nas');
      if ('export' in t.dataset) { t.disabled = true; await ctx.startJob('/api/export'); }
    } catch (err) {
      t.disabled = false;
      ctx.toast(err.message);
    }
  };
}

// ---------------------------------------------------------------- events

function eventRange(ev) {
  const a = ev.start_local.slice(0, 10);
  const b = ev.end_local.slice(0, 10);
  return a === b ? dateTime(ev.start_local) : `dal ${dateTime(ev.start_local)} al ${dateTime(ev.end_local)}`;
}

const EVENTS_PAGE = 150;

export async function renderEvents(el, ctx) {
  const { events } = await api.get(`/api/events?${q({ user: ctx.user })}`);
  const row = (ev) => `
    <div class="event" data-event="${ev.id}">
      <button class="thumb" type="button" data-goto="${esc(ev.start_local)}" aria-label="Mostra nella galleria">
        ${ev.cover ? `<img src="${thumbUrl(ev.cover, ev.v)}" alt="" loading="lazy">` : ''}</button>
      <div>
        <h3 class="${ev.name ? '' : 'unnamed'}">${esc(ev.name || 'Senza nome')}</h3>
        <p>${esc(eventRange(ev))}, ${plural(ev.n_assets, 'elemento', 'elementi')}${ev.name_source === 'album' ? ', nome preso dall’album' : ''}</p>
      </div>
      <div class="tools"><button class="btn" type="button" data-rename>${ev.name ? 'Rinomina' : 'Dai un nome'}</button></div>
    </div>`;
  el.innerHTML = `<div class="page">
    ${pageHead('Eventi',
      'Le foto vicine nel tempo e nello spazio formano un evento: una gita, una festa, una giornata al mare. Il nome diventa la cartella dell’archivio, per esempio “2019-10-09 Gita al mare”.')}
    <div class="list">${events.length ? '' : emptyBlock('Nessun evento', 'Gli eventi compaiono dopo il primo aggiornamento dell’archivio.')}</div>
    <div class="more"></div>
  </div>`;
  // thousands of events: added a page at a time as the list is scrolled
  const list = el.querySelector('.list');
  const more = el.querySelector('.more');
  let shown = 0;
  const showMore = () => {
    list.insertAdjacentHTML('beforeend', events.slice(shown, shown + EVENTS_PAGE).map(row).join(''));
    shown = Math.min(events.length, shown + EVENTS_PAGE);
    more.innerHTML = shown < events.length
      ? `<button class="btn" type="button" data-more>Mostra altri ${num(Math.min(EVENTS_PAGE, events.length - shown))} di ${num(events.length - shown)}</button>` : '';
  };
  showMore();
  const io = new IntersectionObserver((entries) => {
    if (entries.some((x) => x.isIntersecting) && shown < events.length) showMore();
  }, { root: el.closest('.main'), rootMargin: '1200px 0px' });
  io.observe(more);
  ctx.onLeave(() => io.disconnect());
  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    if ('more' in t.dataset) { showMore(); return; }
    const rowEl = t.closest('[data-event]');
    const ev = rowEl && events.find((x) => x.id === Number(rowEl.dataset.event));
    if (t.dataset.goto) { ctx.gotoDate(t.dataset.goto); return; }
    if ('rename' in t.dataset && ev) {
      const name = await ctx.ask({
        title: 'Nome dell’evento', text: eventRange(ev), label: 'Nome', value: ev.name ?? '', confirm: 'Salva',
        hint: 'Lascia vuoto per tornare al nome automatico.', allowEmpty: true,
      });
      if (name === null) return;
      try {
        await api.post(`/api/events/${ev.id}/rename`, { name });
        ctx.galleryChanged();
        await ctx.refresh();
        renderEvents(el, ctx);
      } catch (err) {
        ctx.toast(err.message);
      }
    }
  };
}

// ---------------------------------------------------------------- duplicates

function itemInfo(m) {
  return `
    <h2>${esc(m.filename)}</h2>
    <p class="sub">${esc(sourceLabel(m.source.type))}, ${esc(m.source.ident)}</p>
    <section><h3>Data</h3><p>${esc(dateTime(m.date_local))}</p><p class="sub">Ricavata ${esc(DATE_SOURCE[m.date_source] ?? 'da nessuna fonte')}.</p></section>
    <section><h3>Questa copia</h3>${copyRow(m)}</section>
    ${m.albums?.length ? `<section><h3>Album</h3><p>${esc(m.albums.join(', '))}</p></section>` : ''}
    ${m.people?.length ? `<section><h3>Persone</h3><p>${esc(m.people.join(', '))}</p></section>` : ''}
    <section><h3>Posizione</h3><p>${m.lat !== null ? `${m.lat.toFixed(5)}, ${m.lon.toFixed(5)}` : 'Nessuna'}</p></section>`;
}

function paneHtml(g, m, k, bestPixels) {
  const kept = m.role === 'winner';
  const pixels = (m.w || 0) * (m.h || 0);
  const note = m.decision === 'winner' ? 'Hai scelto di tenere questa copia.'
    : m.decision === 'separate' ? 'Hai indicato che non è un duplicato.' : '';
  const actions = note
    ? `<div class="pane-note">${esc(note)}<button class="btn btn-quiet" type="button" data-decide="clear" data-sha="${m.sha256}">Annulla</button></div>`
    : m.reason === 'similar'
      ? `<div class="pane-actions">
           <button class="btn" type="button" data-decide="winner" data-sha="${m.sha256}">Tieni questa</button>
           <button class="btn btn-quiet" type="button" data-decide="separate" data-sha="${m.sha256}">${icon('i-split')}Non è un duplicato</button>
         </div>`
      : '';
  return `
    <figure class="pane ${kept ? 'kept' : ''}">
      <button class="pane-img" type="button" data-open="${k}" data-fit="contain" aria-label="Apri ${esc(m.filename)}">
        <img src="${thumbUrl(m.id, m.v)}" alt="" loading="lazy">
      </button>
      <span class="pill ${kept ? 'kept' : ''}">${kept ? icon('i-check') : ''}${esc(REASON[m.reason] ?? m.reason)}</span>
      <dl class="facts">
        <dt>Fonte</dt><dd>${esc(sourceLabel(m.source.type))}<small>${esc(m.source.ident)}</small></dd>
        <dt>File</dt><dd>${esc(m.filename)}</dd>
        <dt>Dimensioni</dt><dd class="${g.n_contents > 1 && pixels === bestPixels ? 'best' : ''}">${m.w ? `${num(m.w)} × ${num(m.h)}` : '—'}</dd>
        <dt>Peso</dt><dd>${bytes(m.size)}</dd>
        <dt>Data</dt><dd>${esc(dateTime(m.date_local))}<small>${esc(DATE_SOURCE[m.date_source] ?? '')}</small></dd>
        <dt>Posizione</dt><dd>${m.lat !== null ? 'Sì' : 'No'}</dd>
        ${m.albums.length ? `<dt>Album</dt><dd>${esc(m.albums.join(', '))}</dd>` : ''}
        ${m.people.length ? `<dt>Persone</dt><dd>${esc(m.people.join(', '))}</dd>` : ''}
      </dl>
      ${actions}
    </figure>`;
}

/** Fold a group away (height and opacity), then remove it. */
function collapse(el) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) { el.remove(); return Promise.resolve(); }
  el.style.height = `${el.offsetHeight}px`;
  el.getBoundingClientRect();
  el.classList.add('leaving');
  el.style.height = '0px';
  return new Promise((done) => { setTimeout(() => { el.remove(); done(); }, 280); });
}

function groupHtml(g) {
  const best = Math.max(...g.members.map((m) => (m.w || 0) * (m.h || 0)));
  const sources = [...new Set(g.members.map((m) => sourceLabel(m.source.type)))];
  const trash = `<button class="btn danger" type="button" data-trash title="Nessuna copia va tenuta: la foto va nel cestino, da cui si può rimettere">${icon('i-trash')}Elimina tutte</button>`;
  const tools = g.excluded
    ? `<span class="muted">${icon('i-trash')}Nel cestino</span><button class="btn" type="button" data-untrash>${icon('i-restore')}Rimetti</button>`
    : g.reviewed
      ? `<span class="ok">${icon('i-check')}Controllato</span><button class="btn btn-quiet" type="button" data-review="0">Riapri</button>${trash}`
      : `${trash}<button class="btn" type="button" data-review="1">${icon('i-check')}Va bene così</button>`;
  return `
    <article class="dgroup ${g.reviewed || g.excluded ? 'reviewed' : ''}" data-group="${g.id}">
      <header class="dgroup-head">
        <div><h3>${esc(dateTime(g.date_local))}</h3><p>${plural(g.n_items, 'copia', 'copie')} da ${esc(listJoin(sources))}</p></div>
        <div class="tools">${tools}</div>
      </header>
      <div class="compare">${g.members.map((m, k) => paneHtml(g, m, k, best)).join('')}</div>
    </article>`;
}

export async function renderDuplicates(el, ctx) {
  const opts = (ctx.viewState.duplicati ??= { kind: 'simili', todo: true });
  el.innerHTML = `<div class="page">
    ${pageHead('Duplicati',
      'Ogni gruppo è la stessa foto trovata più volte. La copia con il bordo blu entra nell’archivio, con i dati di tutte le altre (posizione, album, persone). Se va bene, premi Va bene così; altrimenti scegli un’altra copia. Se la foto non serve (un documento, uno scontrino), Elimina tutte la mette nel cestino.',
      `<div class="segmented" role="group" aria-label="Tipo di copie">
         ${seg('kind', 'simili', 'Simili', opts.kind === 'simili')}${seg('kind', 'identici', 'Identiche', opts.kind === 'identici')}
       </div>
       <div class="segmented" role="group" aria-label="Filtro">
         ${seg('todo', '1', 'Da rivedere', opts.todo)}${seg('todo', '0', 'Tutti', !opts.todo)}
       </div>`)}
    <div class="list"></div><div class="more"></div></div>`;
  const list = el.querySelector('.list');
  const more = el.querySelector('.more');
  const groups = new Map();
  let offset = 0;
  let total = 0;

  const showEmpty = () => {
    list.innerHTML = opts.kind === 'simili'
      ? emptyBlock(opts.todo ? 'Nessun gruppo da rivedere' : 'Nessuna copia simile',
        'Qui compaiono le foto presenti in più versioni diverse, per esempio una ricompressa da Google Foto e l’originale del telefono.')
      : emptyBlock('Nessuna copia identica', 'Qui compaiono i file identici trovati in più fonti. Sono unificati automaticamente.');
  };

  const showMore = () => {
    more.innerHTML = offset < total
      ? `<button class="btn" type="button" data-more>Mostra altri ${num(Math.min(PAGE, total - offset))} di ${num(total - offset)}</button>` : '';
  };

  async function loadMore() {
    const data = await api.get(`/api/groups?${q({ user: ctx.user, kind: opts.kind, todo: opts.todo, offset, limit: PAGE })}`);
    total = data.total;
    if (!offset && !total) showEmpty();
    for (const g of data.groups) groups.set(g.id, g);
    list.insertAdjacentHTML('beforeend', data.groups.map(groupHtml).join(''));
    offset += data.groups.length;
    showMore();
  }

  /** A group that is settled leaves the list, so the next one to check is already under the pointer. */
  async function dismiss(art, g) {
    const next = art.nextElementSibling;
    groups.delete(g.id);
    offset -= 1; // it left the list on the server too
    total -= 1;
    await collapse(art);
    if (!list.querySelector('.dgroup')) {
      if (offset < total) await loadMore(); else showEmpty();
    }
    showMore();
    const target = next?.isConnected ? next : list.querySelector('.dgroup');
    const top = target?.getBoundingClientRect().top;
    if (top !== undefined && (top < 0 || top > innerHeight - 160)) target.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    const art = t.closest('[data-group]');
    const g = art && groups.get(Number(art.dataset.group));
    try {
      if (t.dataset.kind || t.dataset.todo) {
        if (t.dataset.kind) opts.kind = t.dataset.kind;
        if (t.dataset.todo) opts.todo = t.dataset.todo === '1';
        return renderDuplicates(el, ctx);
      }
      if ('more' in t.dataset) { t.disabled = true; return await loadMore(); }
      if (t.dataset.open !== undefined && g) {
        const entries = g.members.map((m) => entryOf(m, {
          caption: `${m.filename}, ${sourceLabel(m.source.type)}`, info: async () => itemInfo(m),
        }));
        return ctx.lightbox.open(entries, Number(t.dataset.open), originOf(entries, [...art.querySelectorAll('.pane-img')]));
      }
      if (t.dataset.decide && g) {
        t.disabled = true;
        await api.post('/api/decide', { sha: t.dataset.sha, action: t.dataset.decide, asset_id: g.id });
        const m = g.members.find((x) => x.sha256 === t.dataset.sha);
        m.decision = t.dataset.decide === 'clear' ? null : t.dataset.decide;
        art.outerHTML = groupHtml(g);
        ctx.markPending();
        return;
      }
      if (('trash' in t.dataset || 'untrash' in t.dataset) && g) {
        t.disabled = true;
        const exclude = 'trash' in t.dataset;
        await api.post('/api/assets/edit', { ids: [g.id], exclude });
        g.excluded = exclude;
        if (exclude && opts.todo) dismiss(art, g).catch((err) => ctx.toast(err.message)); else art.outerHTML = groupHtml(g);
        ctx.galleryChanged();
        ctx.refresh();
        if (exclude) ctx.undoable('Foto e copie nel cestino', () => api.post('/api/assets/edit', { ids: [g.id], exclude: false }));
        return;
      }
      if (t.dataset.review && g) {
        t.disabled = true;
        await api.post('/api/review', { asset_id: g.id, value: t.dataset.review === '1' });
        g.reviewed = t.dataset.review === '1';
        if (g.reviewed && opts.todo) dismiss(art, g).catch((err) => ctx.toast(err.message)); else art.outerHTML = groupHtml(g);
        ctx.refresh();
        if (g.reviewed) ctx.undoable('Gruppo controllato', () => api.post('/api/review', { asset_id: g.id, value: false }));
      }
    } catch (err) {
      t.disabled = false;
      ctx.toast(err.message);
    }
  };
  await loadMore();
}

// ---------------------------------------------------------------- bursts / variants

function sizeStrip(strip) {
  const cells = [...strip.children];
  const ratios = cells.map((c) => Number(c.dataset.ratio));
  const width = strip.clientWidth - 6 * (ratios.length - 1);
  const h = Math.min(260, width / ratios.reduce((a, r) => a + r, 0));
  cells.forEach((c, k) => {
    c.style.width = `${ratios[k] * h}px`;
    c.querySelector('.shot').style.height = `${h}px`;
  });
}

function variantHtml(g) {
  const cells = g.items.map((m, k) => `
    <div class="cell ${m.excluded ? 'excluded' : ''}" data-ratio="${(m.w && m.h ? m.w / m.h : 1.5).toFixed(4)}">
      <button class="shot" type="button" data-open="${k}" aria-label="Apri ${esc(m.filename)}"><img src="${thumbUrl(m.id, m.v)}" alt="" loading="lazy"></button>
      ${m.excluded ? `<span class="pill">${icon('i-trash')}Nel cestino</span>` : m.best && g.items.length > 1 ? `<span class="pill kept">${icon('i-check')}Più nitida</span>` : ''}
      <span class="name">${esc(m.filename)}</span>
      <div class="cell-actions">${m.excluded
        ? `<button class="btn" type="button" data-restore="${m.asset}">${icon('i-restore')}Rimetti</button>`
        : `<button class="btn" type="button" data-keep="${m.asset}">Tieni solo questa</button>`}</div>
    </div>`).join('');
  return `<article class="vgroup ${g.decided ? 'decided' : ''}" data-group="${g.group}">
    <header class="vgroup-head"><h3>${esc(dateTime(g.date_local))}</h3>
      <span class="muted">${plural(g.items.length, 'scatto', 'scatti')} alle ${esc(timeOf(g.date_local, true))}</span>
      <div class="tools">${g.decided ? `<span class="ok">${icon('i-check')}Scelta fatta</span>`
        : `<button class="btn" type="button" data-keep-all>${icon('i-check')}Tienile tutte</button>`}</div></header>
    <div class="strip">${cells}</div>
  </article>`;
}

export async function renderVariants(el, ctx) {
  const opts = (ctx.viewState.varianti ??= { todo: true });
  el.innerHTML = `<div class="page">
    ${pageHead('Raffiche',
      'Scatti fatti nello stesso secondo con la stessa fotocamera. Di solito basta il migliore: quello più nitido è già segnato. Gli altri finiscono nel cestino, da cui si possono rimettere.',
      `<div class="segmented" role="group" aria-label="Filtro">
         ${seg('todo', '1', 'Da scegliere', opts.todo)}${seg('todo', '0', 'Tutte', !opts.todo)}
       </div>`)}
    <div class="list"></div><div class="more"></div></div>`;
  const list = el.querySelector('.list');
  const more = el.querySelector('.more');
  const groups = new Map();
  let offset = 0;

  async function loadMore() {
    const data = await api.get(`/api/variants?${q({ user: ctx.user, todo: opts.todo, offset, limit: PAGE })}`);
    if (!offset && !data.total) {
      list.innerHTML = emptyBlock(opts.todo ? 'Nessuna raffica da scegliere' : 'Nessuna raffica', 'Qui compaiono gli scatti multipli fatti nello stesso secondo.');
    }
    for (const g of data.groups) groups.set(g.group, g);
    list.insertAdjacentHTML('beforeend', data.groups.map(variantHtml).join(''));
    for (const s of list.querySelectorAll('.strip')) sizeStrip(s);
    offset += data.groups.length;
    more.innerHTML = offset < data.total ? `<button class="btn" type="button" data-more>Mostra altri</button>` : '';
  }

  const ro = new ResizeObserver(() => { for (const s of list.querySelectorAll('.strip')) sizeStrip(s); });
  ro.observe(list);
  ctx.onLeave(() => ro.disconnect());

  async function replaceGroup(art, g) {
    if (opts.todo && g.decided) {
      art.remove();
      offset -= 1;
    } else {
      art.outerHTML = variantHtml(g);
      const fresh = list.querySelector(`[data-group="${g.group}"] .strip`);
      if (fresh) sizeStrip(fresh);
    }
    ctx.refresh();
  }

  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    if (t.dataset.todo) { opts.todo = t.dataset.todo === '1'; return renderVariants(el, ctx); }
    if ('more' in t.dataset) { t.disabled = true; return loadMore(); }
    const art = t.closest('[data-group]');
    const g = art && groups.get(Number(art.dataset.group));
    if (!g) return;
    try {
      if (t.dataset.open !== undefined) {
        const entries = g.items.map((m) => entryOf(m, { asset: m.asset, excluded: m.excluded, caption: m.filename }));
        return ctx.lightbox.open(entries, Number(t.dataset.open), originOf(entries, [...art.querySelectorAll('.shot')]));
      }
      if (t.dataset.keep || 'keepAll' in t.dataset) {
        t.disabled = true;
        const keep = t.dataset.keep ? [Number(t.dataset.keep)] : g.items.map((m) => m.asset);
        await api.post('/api/variants/keep', { group: g.group, keep });
        const dropped = g.items.filter((m) => !keep.includes(m.asset)).map((m) => m.asset);
        for (const m of g.items) m.excluded = !keep.includes(m.asset);
        g.decided = true;
        await replaceGroup(art, g);
        ctx.galleryChanged();
        if (dropped.length) {
          ctx.undoable(`${plural(dropped.length, 'scatto messo', 'scatti messi')} nel cestino`,
            () => api.post('/api/assets/edit', { ids: dropped, exclude: false }));
        }
        return;
      }
      if (t.dataset.restore) {
        t.disabled = true;
        await api.post('/api/assets/edit', { ids: [Number(t.dataset.restore)], exclude: false });
        g.items.find((m) => m.asset === Number(t.dataset.restore)).excluded = false;
        await replaceGroup(art, g);
        ctx.galleryChanged();
      }
    } catch (err) {
      t.disabled = false;
      ctx.toast(err.message);
    }
  };
  await loadMore();
}

// ---------------------------------------------------------------- dates to check

const ISSUES_PAGE = 50;

function issueHtml(it, k) {
  return `
      <div class="issue" data-k="${k}">
        <button class="thumb" type="button" data-open="${k}" aria-label="Apri ${esc(it.filename)}"><img src="${thumbUrl(it.id, it.v)}" alt="" loading="lazy"></button>
        <div>
          <h3>${esc(it.filename)}</h3>
          ${issueSentences(it).map((s) => `<p>${s}</p>`).join('')}
          <p class="used">${it.date_local ? `Data scelta: ${esc(dateTime(it.date_local))}, ${esc(DATE_SOURCE[it.date_source] ?? '')}.` : 'Senza una data, la foto finirà nella cartella “senza data”.'}</p>
          <div class="fix">
            <input type="datetime-local" step="1" value="${esc((it.date_local ?? '').replace(' ', 'T'))}" aria-label="Data e ora dello scatto">
            <button class="btn btn-primary" type="button" data-save="${k}">Salva questa data</button>
          </div>
        </div>
      </div>`;
}

export async function renderIssues(el, ctx) {
  el.innerHTML = `<div class="page">
    ${pageHead('Date da verificare',
      'Foto in cui fotocamera, nome del file e Google Foto indicano date diverse, oppure nessuna data. È già stata scelta la più affidabile: se è giusta confermala, altrimenti correggila.')}
    <div class="list"></div><div class="more"></div></div>`;
  const list = el.querySelector('.list');
  const more = el.querySelector('.more');
  const items = [];
  let offset = 0; // position of the next page on the server: rows saved here have left the list there

  async function loadMore() {
    const data = await api.get(`/api/issues?${q({ user: ctx.user, offset, limit: ISSUES_PAGE })}`);
    if (!items.length && !data.items.length) list.innerHTML = emptyBlock('Tutto in ordine', 'Nessuna foto con date incoerenti o mancanti.');
    list.insertAdjacentHTML('beforeend', data.items.map((it, j) => issueHtml(it, items.length + j)).join(''));
    items.push(...data.items);
    offset += data.items.length;
    const left = data.total - offset;
    more.innerHTML = left > 0
      ? `<button class="btn" type="button" data-more>Mostra altre ${num(Math.min(ISSUES_PAGE, left))} di ${num(left)}</button>` : '';
  }

  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    if ('more' in t.dataset) { t.disabled = true; loadMore().catch((err) => { t.disabled = false; ctx.toast(err.message); }); return; }
    if (t.dataset.open !== undefined) {
      const shown = [...el.querySelectorAll('.issue')].map((row) => items[Number(row.dataset.k)]);
      const entries = shown.map((m) => entryOf(m, { asset: m.asset, caption: m.filename }));
      const at = shown.indexOf(items[Number(t.dataset.open)]);
      ctx.lightbox.open(entries, at, originOf(entries, [...el.querySelectorAll('.issue .thumb')]));
      return;
    }
    if (t.dataset.save !== undefined) {
      const row = t.closest('.issue');
      const raw = row.querySelector('input').value;
      if (!raw) { ctx.toast('Scegli una data prima di salvare'); return; }
      t.disabled = true;
      try {
        await api.post('/api/assets/edit', { ids: [items[Number(t.dataset.save)].asset], date_local: raw.replace('T', ' ').padEnd(19, ':00').slice(0, 19) });
        row.remove();
        offset -= 1;
        ctx.toast('Data salvata');
        ctx.galleryChanged();
        ctx.refresh();
      } catch (err) {
        t.disabled = false;
        ctx.toast(err.message);
      }
    }
  };
  await loadMore();
}

// ---------------------------------------------------------------- trash

const TRASH_PAGE = 300;

const trashCell = (it, k) => `
      <div class="trash-cell" style="width:${Math.round(170 * Math.min(3, Math.max(0.5, it.w && it.h ? it.w / it.h : 1.5)))}px">
        <button class="shot" type="button" data-open="${k}" aria-label="Apri"><img src="${thumbUrl(it.i, it.v)}" alt="" loading="lazy"></button>
        <button class="btn restore" type="button" data-restore="${it.a}">${icon('i-restore')}Rimetti</button>
      </div>`;

export async function renderTrash(el, ctx) {
  const first = await api.get(`/api/timeline?${q({ user: ctx.user, excluded: true, limit: TRASH_PAGE })}`);
  const items = [...first.items];
  el.innerHTML = `<div class="page">
    ${pageHead('Cestino',
      'Foto e video esclusi dall’archivio. Non vengono copiati sul NAS, ma restano nella cartella income: puoi rimetterli quando vuoi.',
      items.length ? `<button class="btn" type="button" data-restore-all>${icon('i-restore')}Rimetti tutto (${num(first.total)})</button>` : '')}
    ${items.length ? `<div class="trash-grid">${items.map(trashCell).join('')}</div><div class="more"></div>`
      : emptyBlock('Il cestino è vuoto', 'Le foto escluse dalla galleria, dalle raffiche o dal visualizzatore compaiono qui.')}
  </div>`;
  const grid = el.querySelector('.trash-grid');
  const more = el.querySelector('.more');
  const toEntry = (it) => ({ item: it.i, v: it.v, w: it.w, h: it.h, kind: it.k, date: it.d, asset: it.a, excluded: true });
  const entries = items.map(toEntry);
  const showMore = () => {
    const left = first.total - items.length;
    if (more) more.innerHTML = left > 0 ? `<button class="btn" type="button" data-more>Mostra altri ${num(Math.min(TRASH_PAGE, left))} di ${num(left)}</button>` : '';
  };
  showMore();
  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    if (t.dataset.open !== undefined) {
      ctx.lightbox.open(entries, Number(t.dataset.open), originOf(entries, [...el.querySelectorAll('.trash-cell .shot')]),
        { removeWhen: (x) => !x.excluded });
      return;
    }
    if ('more' in t.dataset) {
      t.disabled = true;
      try {
        const page = await api.get(`/api/timeline?${q({ user: ctx.user, excluded: true, offset: items.length, limit: TRASH_PAGE })}`);
        grid.insertAdjacentHTML('beforeend', page.items.map((it, j) => trashCell(it, items.length + j)).join(''));
        items.push(...page.items);
        entries.push(...page.items.map(toEntry));
        showMore();
      } catch (err) {
        t.disabled = false;
        ctx.toast(err.message);
      }
      return;
    }
    const all = 'restoreAll' in t.dataset;
    if (!all && !t.dataset.restore) return;
    t.disabled = true;
    try {
      const ids = all
        ? (await api.get(`/api/timeline?${q({ user: ctx.user, excluded: true })}`)).items.map((it) => it.a)
        : [Number(t.dataset.restore)];
      await api.post('/api/assets/edit', { ids, exclude: false });
      ctx.toast(`${plural(ids.length, 'elemento rimesso', 'elementi rimessi')} nell’archivio`);
      ctx.galleryChanged();
      await ctx.refresh();
      renderTrash(el, ctx);
    } catch (err) {
      t.disabled = false;
      ctx.toast(err.message);
    }
  };
}

// ---------------------------------------------------------------- captions

export async function renderCaptions(el, ctx) {
  const [vlm, recent] = await Promise.all([api.get('/api/vlm'), api.get(`/api/captions/recent?${q({ user: ctx.user })}`)]);
  const opts = (ctx.viewState.didascalie ??= { check: null });
  const running = ctx.state.job.running;
  const check = opts.check;
  const checkLine = check
    ? `<span class="vlm-check ${check.ok ? 'ok' : 'bad'}" role="status">${check.ok
      ? `${icon('i-check')}Il modello risponde${check.model ? `: ${esc(check.model)}` : ''}` : esc(check.error)}</span>` : '';
  const card = (it) => `
    <article class="caption-card">
      <img src="${thumbUrl(it.i, it.v)}" alt="" loading="lazy" draggable="false">
      <div><p>${esc(it.text)}</p>
        <div class="tagline">${it.tags.map((t) => `<button type="button" class="tag" data-search="${esc(t)}" title="Cerca le foto con ${esc(t)}">${esc(t)}</button>`).join('')}</div>
      </div>
    </article>`;

  el.innerHTML = `<div class="page">
    ${pageHead('Didascalie',
      'Un modello di linguaggio che gira sul tuo PC guarda ogni foto e scrive una frase e alcune etichette in italiano. Si cercano dalla galleria e finiscono nei file dell’archivio sul NAS. Le foto già descritte non vengono rifatte. Per descriverne solo alcune, selezionale nella galleria e premi “Descrivi”.',
      vlm.pending ? `<button class="btn btn-primary" type="button" data-describe ${running ? 'disabled' : ''}>Descrivi ${plural(vlm.pending, 'foto', 'foto')}</button>` : '')}
    <div class="jobpanel-slot"></div>
    <div class="overview-total">
      <div><b>${num(vlm.described)}</b><span>foto descritte</span></div>
      <div><b>${num(vlm.pending)}</b><span>da descrivere</span></div>
    </div>
    <h2 class="section-title">Il modello</h2>
    <form class="vlm-form" autocomplete="off">
      <div class="field"><label for="vlm-url">Indirizzo del server</label>
        <input id="vlm-url" name="url" value="${esc(vlm.url)}" spellcheck="false" required>
        <small>LM Studio, llama.cpp o un altro server con API compatibile OpenAI. Il modello deve saper leggere le immagini.</small></div>
      <div class="field"><label for="vlm-key">Chiave API</label>
        <input id="vlm-key" name="key" type="password" placeholder="${vlm.key_set ? 'Salvata: lascia vuoto per non cambiarla' : 'Se il server la richiede'}">
        ${vlm.key_set ? '<small><button type="button" class="link-btn" data-key-clear>Togli la chiave salvata</button></small>' : ''}</div>
      <div class="field"><label for="vlm-model">Modello</label>
        <input id="vlm-model" name="model" value="${esc(vlm.model)}" placeholder="Quello caricato nel server" spellcheck="false"></div>
      ${vlm.local ? '' : `<p class="vlm-warn">${icon('i-alert')}Questo indirizzo non è su questo PC: le foto, ridotte a 768 pixel, verrebbero inviate a quel computer.</p>`}
      <div class="vlm-actions"><button class="btn btn-primary" type="submit">Salva e verifica</button>${checkLine}</div>
    </form>
    <h2 class="section-title">Ultime descrizioni</h2>
    ${recent.items.length ? `<div class="caption-list">${recent.items.map(card).join('')}</div>`
      : '<p class="hint">Ancora nessuna descrizione. Si può descrivere anche una foto sola: aprila nella galleria e premi “Descrivi con l’AI” nelle informazioni.</p>'}
  </div>`;
  const job = ctx.state.job;  // a finished scan or export is not news here
  renderJobPanel(el.querySelector('.jobpanel-slot'), job.running || job.kind === 'caption' ? job : { running: false, lines: [] });

  const save = async (key) => {
    const data = Object.fromEntries(new FormData(el.querySelector('.vlm-form')));
    await api.post('/api/vlm', { url: data.url, model: data.model, key: key ?? (data.key || null) });
  };
  el.querySelector('.vlm-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = el.querySelector('.vlm-actions .btn');
    btn.disabled = true;
    btn.textContent = 'Verifico…';
    try {
      await save();
      opts.check = await api.post('/api/vlm/check');
    } catch (err) {
      opts.check = { ok: false, error: err.message };
    }
    renderCaptions(el, ctx);
  });
  el.onclick = async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    try {
      if (t.dataset.search) { ctx.searchFor(t.dataset.search); return; }
      if ('keyClear' in t.dataset) {
        await save('');
        opts.check = null;
        renderCaptions(el, ctx);
        return;
      }
      if ('describe' in t.dataset) { t.disabled = true; await ctx.startJob('/api/ai/describe'); }
    } catch (err) {
      t.disabled = false;
      ctx.toast(err.message);
    }
  };
}

// ---------------------------------------------------------------- sources

export function renderJobPanel(slot, job) {
  if (!slot) return;
  if (!job.running && !job.finished_at) { slot.innerHTML = ''; return; }
  const lines = job.lines.map((l) => `<li>${esc(l)}</li>`).join('');
  if (job.running) {
    const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
    slot.innerHTML = `<div class="jobpanel" aria-live="polite">
      <h3>${esc(job.phase ?? 'Avvio')}${job.total ? `, ${num(job.done)} di ${num(job.total)}` : ''}</h3>
      <div class="bar ${job.total ? '' : 'indeterminate'}"><span style="width:${pct}%"></span></div>
      <ol>${lines}</ol>
      ${job.cancellable ? `<div class="jobpanel-actions"><button class="btn" type="button" data-job-cancel ${job.cancelling ? 'disabled' : ''}>${job.cancelling ? 'Sto interrompendo…' : 'Interrompi'}</button></div>` : ''}
    </div>`;
    return;
  }
  const end = new Date(job.finished_at * 1000);
  const secs = Math.max(1, Math.round(job.finished_at - job.started_at));
  slot.innerHTML = `<div class="jobpanel">
    <h3>${job.kind === 'caption' ? (job.error ? 'Descrizioni non riuscite' : 'Descrizioni terminate')
      : job.error ? 'Aggiornamento non riuscito' : 'Archivio aggiornato'}</h3>
    <p class="muted">Terminato alle ${end.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })}, in ${plural(secs, 'secondo', 'secondi')}.</p>
    ${job.error ? `<p class="error">${esc(job.error)}</p>` : ''}
    <ol>${lines}</ol></div>`;
}

function sourceRow(s) {
  const meta = SOURCE[s.type] ?? SOURCE.altro;
  const sidecarPct = s.type === 'google' && s.items ? `${Math.round((100 * s.sidecars) / s.items)}%` : '—';
  return `<div class="source">
    <span class="kind">${icon(meta.icon)}</span>
    <div class="name"><b>${esc(meta.label)}</b><span>${esc(s.ident)}</span></div>
    <div class="num">${num(s.photos + s.raw)}<small>foto</small></div>
    <div class="num">${num(s.videos)}<small>video</small></div>
    <div class="num">${sidecarPct}<small>con dati Google</small></div>
    <div class="num">${bytes(s.bytes)}<small>${s.errors ? `${num(s.errors)} illeggibili` : 'spazio'}</small></div>
  </div>`;
}

export function renderSources(el, ctx) {
  const s = ctx.state;
  const users = [...new Set(s.sources.map((x) => x.user))];
  const sections = users.map((u) => `
    <h2 class="section-title">Archivio di ${esc(u)}</h2>
    ${s.sources.filter((x) => x.user === u).map(sourceRow).join('')}`).join('');
  const pending = s.pending.length ? `
    <h2 class="section-title">Da leggere</h2>
    ${s.pending.map((p) => `<div class="source pending"><span class="kind">${icon((SOURCE[p.type] ?? SOURCE.altro).icon)}</span>
      <div class="name"><b>${esc(sourceLabel(p.type))} di ${esc(p.user)}</b><span>${esc(p.ident)}</span></div></div>`).join('')}
    <p class="hint">Queste cartelle sono nuove: premi Aggiorna archivio per leggerle.</p>` : '';
  const warnings = s.warnings.length ? `
    <h2 class="section-title">Cartelle non riconosciute</h2>
    ${s.warnings.map((w) => `<p class="hint">${esc(w)}</p>`).join('')}` : '';
  el.innerHTML = `<div class="page">
    ${pageHead('Fonti',
      'L’archivio legge le foto dalla cartella income senza modificarla. Per aggiungere un account Google o un telefono, mettine i file lì e aggiorna l’archivio.',
      `<button class="btn" type="button" data-action="open-income">${icon('i-folder')}Apri la cartella income</button>
       <button class="btn btn-primary" type="button" data-action="scan" ${s.job.running ? 'disabled' : ''}>${icon('i-refresh')}Aggiorna archivio</button>`)}
    <div class="jobpanel-slot"></div>
    ${sections || (pending ? '' : emptyBlock('Nessuna fonte', 'La cartella income è vuota.'))}
    ${pending}
    ${warnings}
    <h2 class="section-title">Come aggiungere una fonte</h2>
    <p class="hint">Dentro <code>${esc(s.income)}</code> crea una cartella per persona e, dentro, una per ogni fonte:
      <code>google-indirizzo@gmail.com</code> per gli zip di Google Takeout (lasciali compressi),
      <code>android-modello</code> o <code>iphone-modello</code> per la copia di un telefono,
      <code>icloud-indirizzo</code> per iCloud, <code>altro-descrizione</code> per dischi e chiavette.</p>
  </div>`;
  renderJobPanel(el.querySelector('.jobpanel-slot'), s.job);
}
