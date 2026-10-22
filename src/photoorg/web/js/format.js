const IT = 'it-IT';
const fmtDay = new Intl.DateTimeFormat(IT, { weekday: 'long', day: 'numeric', month: 'long' });
const fmtDayYear = new Intl.DateTimeFormat(IT, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const fmtMonth = new Intl.DateTimeFormat(IT, { month: 'long', year: 'numeric' });
const fmtTime = new Intl.DateTimeFormat(IT, { hour: '2-digit', minute: '2-digit' });
const fmtTimeSec = new Intl.DateTimeFormat(IT, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const fmtNumber = new Intl.NumberFormat(IT);
const fmtDecimal = new Intl.NumberFormat(IT, { maximumFractionDigits: 1 });

/** "2019-10-09 08:40:04" (local time of the photo) -> Date with the same wall-clock fields. */
export function parseLocal(s) {
  if (!s) return null;
  const [d, t = '00:00:00'] = s.split(' ');
  const [y, mo, da] = d.split('-').map(Number);
  const [h, mi, se] = t.split(':').map(Number);
  return new Date(y, mo - 1, da, h, mi, se);
}

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

export const dayTitle = (s, withYear = false) => (s ? (withYear ? fmtDayYear : fmtDay).format(parseLocal(s)) : 'Senza data');
export const monthTitle = (s) => (s ? cap(fmtMonth.format(parseLocal(s))) : 'Senza data');
export const timeOf = (s, seconds = false) => (s ? (seconds ? fmtTimeSec : fmtTime).format(parseLocal(s)) : '');
export const dateTime = (s) => (s ? `${dayTitle(s, true)}, ${timeOf(s)}` : 'Senza data');
export const num = (n) => fmtNumber.format(n ?? 0);

export function bytes(n) {
  if (!n) return '0 MB';
  if (n >= 1e9) return `${fmtDecimal.format(n / 1e9)} GB`;
  if (n >= 1e6) return `${fmtDecimal.format(n / 1e6)} MB`;
  return `${fmtDecimal.format(n / 1e3)} kB`;
}

export function megapixels(w, h) {
  return w && h ? `${fmtDecimal.format((w * h) / 1e6)} MP` : '';
}

export function tzLabel(minutes) {
  if (minutes === null || minutes === undefined) return '';
  const sign = minutes < 0 ? '−' : '+';
  const m = Math.abs(minutes);
  return `UTC${sign}${Math.floor(m / 60)}${m % 60 ? `:${String(m % 60).padStart(2, '0')}` : ''}`;
}

export const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;

export const SOURCE = {
  google: { label: 'Google Foto', icon: 'i-cloud' },
  icloud: { label: 'iCloud', icon: 'i-cloud' },
  android: { label: 'Android', icon: 'i-phone' },
  iphone: { label: 'iPhone', icon: 'i-phone' },
  altro: { label: 'Altro', icon: 'i-disk' },
};
export const sourceLabel = (type) => SOURCE[type]?.label ?? type;

export const DATE_SOURCE = {
  exif: 'dalla fotocamera',
  filename: 'dal nome del file',
  google: 'da Google Foto',
  mtime: 'dalla data del file',
};

export const REASON = {
  winner: 'Nell’archivio',
  identical: 'Copia identica',
  similar: 'Copia simile',
};

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

export const icon = (id, cls = 'icon') => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;

/** Plain-language explanation of the date checks that failed for a photo. */
export function issueSentences(it) {
  const google = it.google_utc ? `${dayTitle(it.google_utc, true)} alle ${timeOf(it.google_utc)} UTC` : '';
  const out = [];
  for (const f of it.flags) {
    if (f === 'exif_invalid') out.push(`La data della fotocamera non è valida (${esc(it.exif_dt)}).`);
    if (f === 'google_invalid') out.push(`La data di Google Foto non è plausibile (${esc(google)}).`);
    if (f === 'exif_google_conflict') out.push(`Fotocamera e Google Foto non concordano (Google: ${esc(google)}). Forse la data è stata corretta a mano in Google Foto.`);
    if (f === 'filename_google_conflict') out.push(`Il nome del file indica un giorno diverso da Google Foto (${esc(google)}).`);
  }
  if (!it.date_local) out.push('Nessuna data trovata: né nella fotocamera, né nel nome del file, né in Google Foto.');
  return out;
}
