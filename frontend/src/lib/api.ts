// API client for the Deep Research Agent FastAPI backend. Calls go to
// `${BASE}/api/...` (+ `${BASE}/health`); Next.js rewrites them to the internal
// FastAPI on :8765 (routes at root). The live run streams over SSE
// (EventSource) from `${BASE}/api/jobs/{id}/stream`.

const BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? '';
const api = (p: string) => `${BASE}/api${p}`;

async function jsonOrThrow(res: Response) {
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json())?.detail || detail; } catch { /* */ }
    throw new Error(detail);
  }
  return res.json();
}

export interface JobCreate {
  query: string;
  no_learn?: boolean;
  parent_report?: string;
  gap_context?: string;
  depth?: 'light' | 'medium' | 'heavy';
}

export interface ReportSummary {
  filename: string;
  title?: string;
  query?: string;
  created?: string;
  tags?: string[];
  [k: string]: unknown;
}

export const health = () => fetch(`${BASE}/health`).then((r) => r.ok);

export async function createJob(body: JobCreate): Promise<{ job_id: string }> {
  return jsonOrThrow(await fetch(api('/jobs'), {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  }));
}
export const cancelJob = (id: string) => fetch(api(`/jobs/${id}/cancel`), { method: 'POST' });
export const resumeJob = (id: string) => fetch(api(`/jobs/${id}/resume`), { method: 'POST' });
export const streamUrl = (id: string) => api(`/jobs/${id}/stream`);
export const jobStatus = (id: string, logOffset = 0) =>
  fetch(api(`/jobs/${id}?log_offset=${logOffset}`)).then(jsonOrThrow) as Promise<Record<string, unknown>>;

export async function listReports(page = 1, pageSize = 50, tags = ''): Promise<{ reports?: ReportSummary[] } & Record<string, unknown>> {
  const p = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (tags) p.set('tags', tags);
  return jsonOrThrow(await fetch(api(`/reports?${p}`)));
}
export const getReport = (filename: string) =>
  fetch(api(`/reports/${encodeURIComponent(filename)}`)).then(jsonOrThrow) as Promise<Record<string, unknown>>;
export const searchReports = (q: string, page = 1, pageSize = 50) =>
  fetch(api(`/reports/search?q=${encodeURIComponent(q)}&page=${page}&page_size=${pageSize}`)).then(jsonOrThrow) as Promise<Record<string, unknown>>;
export const getTags = (filename: string) =>
  fetch(api(`/reports/${encodeURIComponent(filename)}/tags`)).then(jsonOrThrow) as Promise<{ tags: string[] }>;
export async function setTags(filename: string, tags: string[]) {
  return jsonOrThrow(await fetch(api(`/reports/${encodeURIComponent(filename)}/tags`), {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ tags }),
  }));
}
export const deleteReport = (filename: string) =>
  fetch(api(`/reports/${encodeURIComponent(filename)}`), { method: 'DELETE' });
export const exportUrl = (filename: string, format: 'pdf' | 'docx') =>
  api(`/reports/${encodeURIComponent(filename)}/export?format=${format}`);

export const getSettings = () => fetch(api('/settings')).then(jsonOrThrow) as Promise<Record<string, unknown>>;
export async function saveSettings(body: Record<string, unknown>) {
  return jsonOrThrow(await fetch(api('/settings'), {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  }));
}
export const discover = () => fetch(api('/discover')).then(jsonOrThrow) as Promise<Record<string, unknown>>;
