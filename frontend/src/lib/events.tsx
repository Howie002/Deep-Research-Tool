// Presentation for the Deep Research SSE event stream. Maps each typed event
// (agent_switch, search, fetch, step, plan_update, note_add, thought_node,
// strategist, claims_*, verdicts, grounding, done, …) to a styled feed row,
// faithful to the original static UI's icons / colors / labels.
import type { ReactNode } from 'react';

export interface RawEvent {
  type?: string;
  ts?: number | string;
  time?: number | string;
  [k: string]: unknown;
}

const s = (v: unknown) => String(v ?? '');
const clip = (v: unknown, n: number) => { const t = s(v); return t.length > n ? t.slice(0, n) + '…' : t; };

export function ts(ev: RawEvent): string {
  const t = ev.ts ?? ev.time;
  if (t == null) return '';
  try {
    const d = typeof t === 'number' ? new Date(t * (t < 1e12 ? 1000 : 1)) : new Date(t);
    return d.toLocaleTimeString([], { hour12: false });
  } catch { return ''; }
}

export interface Row { icon: string; tone: string; body: ReactNode }

/** Map an event to a feed row. Returns null for no-op/noisy turns. */
export function describeEvent(ev: RawEvent): Row | null {
  const type = ev.type || 'log';
  switch (type) {
    case 'agent_switch':
      return { icon: '▶', tone: 'text-violet-300 font-semibold', body: <>Stage {s(ev.stage)}/3 — <b>{s(ev.agent)}</b></> };
    case 'search':
      return { icon: '⌕', tone: 'text-indigo-300', body: <><span className="font-medium text-indigo-400">Search:</span> {s(ev.query)}</> };
    case 'search_result': {
      const results = (ev.results as { url: string; title?: string }[]) || [];
      return {
        icon: '↳', tone: 'text-zinc-400',
        body: <>
          {s(ev.count)} result{Number(ev.count) !== 1 ? 's' : ''} found
          {results.length > 0 && (
            <div className="ml-4 mt-1 space-y-0.5">
              {results.slice(0, 8).map((r, i) => (
                <div key={i}>↳ <a href={r.url} target="_blank" rel="noreferrer" className="text-indigo-400 hover:underline">{clip(r.title || r.url, 90)}</a></div>
              ))}
            </div>
          )}
        </>,
      };
    }
    case 'fetch':
      return { icon: '↗', tone: 'text-sky-300', body: <><span className="font-medium text-sky-400">Read:</span> <a href={s(ev.url)} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">{clip(ev.url, 90)}</a></> };
    case 'fetch_content':
      return { icon: '│', tone: 'text-zinc-500 italic', body: clip(ev.preview, 300) };
    case 'step':
      return ev.content ? { icon: '·', tone: 'text-zinc-300 whitespace-pre-wrap', body: s(ev.content) } : null;
    case 'plan_update':
      return { icon: '◆', tone: 'text-amber-300 font-medium', body: 'Plan updated' };
    case 'note_add':
      return { icon: '✎', tone: 'text-emerald-300 font-medium', body: <>Note: <span className="text-zinc-400 font-normal">{clip(ev.content, 160)}</span></> };
    case 'draft_update':
      return { icon: '✦', tone: 'text-purple-300 font-medium', body: 'Draft updated' };
    case 'log':
      return ev.message ? { icon: '⚙', tone: 'text-zinc-400', body: s(ev.message) } : null;
    case 'thought_node':
      return ev.label ? { icon: '🧠', tone: 'text-purple-300 font-semibold', body: <>{s(ev.label)}{ev.rationale ? <span className="text-zinc-500 italic font-normal"> — {s(ev.rationale)}</span> : null}</> } : null;
    case 'iteration_tick':
      return { icon: '↺', tone: 'text-zinc-500 text-xs', body: <>Stage {s(ev.stage)}{ev.agent ? ` · ${s(ev.agent)}` : ''}{Number(ev.pass) > 1 ? ` — pass ${s(ev.pass)}` : ''}</> };
    case 'strategist': {
      const parts: string[] = [];
      const pu = ev.priority_updates as unknown[]; const ab = ev.abandoned as unknown[];
      if (pu?.length) parts.push(`${pu.length} re-prioritised`);
      if (ab?.length) parts.push(`${ab.length} abandoned`);
      if (ev.new_claim_count) parts.push(`${s(ev.new_claim_count)} new`);
      return { icon: '🎯', tone: 'text-violet-300 font-medium', body: <><b>Strategist:</b> {clip(s(ev.diagnosis).replace(/\s+/g, ' ').trim(), 260)}{parts.length ? <span className="text-zinc-500"> · {parts.join(' · ')}</span> : null}</> };
    }
    case 'claims_snapshot': {
      const m = (ev.model as Record<string, unknown>) || {};
      const claims = (m.claims as { status: string }[]) || [];
      const counts: Record<string, number> = {};
      claims.forEach((c) => { counts[c.status] = (counts[c.status] || 0) + 1; });
      const parts = Object.entries(counts).map(([k, v]) => `${k}=${v}`).join(', ');
      return { icon: '📋', tone: 'text-zinc-500 text-xs', body: `Claims: ${parts || '(none)'}` };
    }
    case 'claims_update': {
      const upd = (ev.updated_claims as unknown[]) || []; const added = (ev.new_claim_ids as unknown[]) || [];
      if (!upd.length && !added.length && !ev.parse_failed) return null;
      const parts: string[] = [];
      if (upd.length) parts.push(`${upd.length} updated`);
      if (added.length) parts.push(`${added.length} new`);
      return { icon: '↳', tone: 'text-zinc-500 text-xs', body: `${parts.join(' · ') || 'evaluator pass'}${ev.parse_failed ? ' (parse failed)' : ''}` };
    }
    case 'resource_verdict': {
      const useful = ev.verdict === 'useful';
      return { icon: useful ? '✓' : '✗', tone: useful ? 'text-emerald-400' : 'text-red-400', body: <>{useful ? 'useful' : 'reject'} <span className="text-zinc-500">{clip(s(ev.url).replace(/^https?:\/\//, ''), 60)}</span>{ev.reason ? ` — ${clip(ev.reason, 100)}` : ''}</> };
    }
    case 'page_verdict': {
      const on = ev.verdict === 'on_topic';
      return { icon: on ? '●' : '⚠', tone: on ? 'text-emerald-400' : 'text-red-400', body: <>page {s(ev.verdict)} <span className="text-zinc-500">{clip(s(ev.url).replace(/^https?:\/\//, ''), 60)}</span></> };
    }
    case 'stage_collapse':
      return { icon: '🛑', tone: 'text-red-400 font-medium', body: <>{s(ev.agent || 'stage')} output collapsed — {s(ev.signal)}</> };
    case 'grounding': {
      const tone = { high: 'text-emerald-400', medium: 'text-yellow-400', low: 'text-orange-400', very_low: 'text-red-400' }[s(ev.confidence_tier)] || 'text-zinc-400';
      return { icon: '●', tone: `${tone} font-semibold`, body: `Grounding: ${s(ev.confidence_tier).toUpperCase()} (score ${s(ev.confidence_score)})` };
    }
    case 'done':
      return ev.status === 'complete'
        ? { icon: '✓', tone: 'text-emerald-400 font-medium', body: 'Pipeline complete — report ready.' }
        : { icon: '✗', tone: 'text-red-400 font-medium', body: `Pipeline failed: ${clip(ev.result || 'Unknown error', 200)}` };
    default:
      return ev.message || ev.content ? { icon: '·', tone: 'text-zinc-400', body: clip(ev.message || ev.content, 300) } : null;
  }
}

/** The four-stage pipeline labels. */
export const STAGES = ['Research', 'Analysis', 'Gap Analysis', 'Synthesis'];
