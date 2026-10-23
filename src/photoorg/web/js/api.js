async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    throw new Error(detail || `Errore ${res.status}`);
  }
  return res.json();
}

export const api = {
  get: (url) => request('GET', url),
  post: (url, body = {}) => request('POST', url, body),
};

export const q = (params) =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null)).toString();

// t=2: video thumbnails became frame sheets (the old placeholder is cached forever)
export const thumbUrl = (id, v) => `/media/thumb/${id}?v=${v}&t=2`;
export const fullUrl = (id, v) => `/media/full/${id}?v=${v}`;
export const fileUrl = (id) => `/media/file/${id}`;
export const versionUrl = (id, size, v) => `/media/version/${id}/${size}?v=${v}`;
