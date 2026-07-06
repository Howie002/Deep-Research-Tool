'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { marked } from 'marked';
import { Play, Square, Settings as SettingsIcon } from 'lucide-react';
import { createJob, cancelJob, streamUrl } from '@/lib/api';
import { describeEvent, ts, STAGES, type RawEvent } from '@/lib/events';
import MindMap, { type MMNode, type MMLink } from '@/components/research/MindMap';
import SettingsModal from '@/components/research/SettingsModal';
import ReportsHistory from '@/components/research/ReportsHistory';

type ArtTab = 'plan' | 'notes' | 'draft' | 'sources' | 'thoughts' | 'mindmap';

export default function Page() {
  const [query, setQuery] = useState('');
  const [depth, setDepth] = useState<'light' | 'medium' | 'heavy'>('medium');
  const [running, setRunning] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [rows, setRows] = useState<{ icon: string; tone: string; body: React.ReactNode; t: string }[]>([]);
  const [stage, setStage] = useState(0);
  const [report, setReport] = useState<string | null>(null);
  const [artTab, setArtTab] = useState<ArtTab>('plan');
  const [plan, setPlan] = useState('');
  const [draft, setDraft] = useState('');
  const [notes, setNotes] = useState<string[]>([]);
  const [sources, setSources] = useState<{ url: string; title?: string }[]>([]);
  const [thoughts, setThoughts] = useState<{ label: string; rationale?: string }[]>([]);
  const [mmNodes, setMmNodes] = useState<MMNode[]>([]);
  const [mmLinks, setMmLinks] = useState<MMLink[]>([]);
  const [showSettings, setShowSettings] = useState(false);
  const [reportsKey, setReportsKey] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const feedRef = useRef<HTMLDivElement>(null);
  const lastSearch = useRef<string | null>(null);

  useEffect(() => () => esRef.current?.close(), []);
  useEffect(() => { feedRef.current?.scrollTo(0, feedRef.current.scrollHeight); }, [rows]);

  const reset = () => {
    setRows([]); setStage(0); setReport(null); setPlan(''); setDraft('');
    setNotes([]); setSources([]); setThoughts([]); setMmNodes([]); setMmLinks([]);
    lastSearch.current = null;
  };

  const handleEvent = useCallback((ev: RawEvent) => {
    const row = describeEvent(ev);
    if (row) setRows((r) => [...r, { ...row, t: ts(ev) }]);

    const type = ev.type;
    if (type === 'agent_switch' && ev.stage) setStage(Number(ev.stage));
    if (type === 'log' && typeof ev.message === 'string') {
      const m = ev.message;
      for (let i = 1; i <= 4; i++) if (m.includes(`Stage ${i}/4`) && !m.includes('complete')) setStage(i);
    }
    if (type === 'plan_update') setPlan(String(ev.content ?? ''));
    if (type === 'draft_update') setDraft(String(ev.content ?? ''));
    if (type === 'note_add' && ev.content) {
      const c = String(ev.content);
      setNotes((n) => [...n, c]);
      setMmNodes((nodes) => [...nodes, { id: `note-${nodes.length}`, label: c.slice(0, 40), kind: 'note' }]);
      setMmLinks((l) => [...l, { source: 'root', target: `note-${notes.length}` }]);
    }
    if (type === 'search' && ev.query) {
      const sid = `search-${String(ev.query)}`;
      lastSearch.current = sid;
      setMmNodes((nodes) => (nodes.find((n) => n.id === sid) ? nodes : [...nodes, { id: sid, label: String(ev.query), kind: 'search' }]));
      setMmLinks((l) => [...l, { source: 'root', target: sid }]);
    }
    if (type === 'search_result' && Array.isArray(ev.results)) {
      const res = ev.results as { url: string; title?: string }[];
      setSources((s) => [...s, ...res]);
      const parent = lastSearch.current || 'root';
      setMmNodes((nodes) => {
        const add = res.slice(0, 4).filter((r) => !nodes.find((n) => n.id === r.url)).map((r) => ({ id: r.url, label: r.title || r.url, kind: 'source' as const }));
        return [...nodes, ...add];
      });
      setMmLinks((l) => [...l, ...res.slice(0, 4).map((r) => ({ source: parent, target: r.url }))]);
    }
    if (type === 'fetch' && ev.url) setSources((s) => (s.find((x) => x.url === ev.url) ? s : [...s, { url: String(ev.url) }]));
    if (type === 'thought_node' && ev.label) {
      setThoughts((t) => [...t, { label: String(ev.label), rationale: ev.rationale ? String(ev.rationale) : undefined }]);
      setMmNodes((nodes) => [...nodes, { id: `thought-${nodes.length}`, label: String(ev.label).slice(0, 40), kind: 'thought' }]);
      setMmLinks((l) => [...l, { source: 'root', target: `thought-${thoughts.length}` }]);
    }
    if (type === 'done') {
      setRunning(false);
      if (ev.status === 'complete' && ev.result) { setReport(String(ev.result)); setReportsKey((k) => k + 1); }
      else if (ev.status !== 'complete') setError(String(ev.result || 'The pipeline encountered an error.'));
      esRef.current?.close();
    }
  }, [notes.length, thoughts.length]);

  const startStream = (id: string) => {
    const es = new EventSource(streamUrl(id));
    esRef.current = es;
    es.onmessage = (e) => { try { handleEvent(JSON.parse(e.data)); } catch { /* */ } };
    es.onerror = () => { /* keep open; backend closes on done */ };
  };

  const begin = async () => {
    if (!query.trim()) { setError('Enter a research question.'); return; }
    setError(null); reset(); setRunning(true);
    setMmNodes([{ id: 'root', label: query.trim().slice(0, 48), kind: 'root' }]);
    try {
      const { job_id } = await createJob({ query: query.trim(), depth });
      setJobId(job_id);
      startStream(job_id);
    } catch (e) { setError((e as Error).message); setRunning(false); }
  };

  const stop = async () => {
    if (jobId) await cancelJob(jobId);
    esRef.current?.close();
    setRunning(false);
  };

  const reportHtml = report ? marked.parse(report) as string : '';

  const ARTIFACTS: { id: ArtTab; label: string }[] = [
    { id: 'plan', label: 'Plan' }, { id: 'notes', label: `Notes (${notes.length})` },
    { id: 'draft', label: 'Draft' }, { id: 'sources', label: `Sources (${sources.length})` },
    { id: 'thoughts', label: `Thoughts (${thoughts.length})` }, { id: 'mindmap', label: 'Mind Map' },
  ];

  return (
    <div className="max-w-6xl mx-auto px-6 py-8">
      <header className="flex items-start justify-between gap-3 mb-6">
        <div>
          <h1 className="text-xl font-bold text-[#500000] dark:text-[#e0a3a3]">Deep Research Agent</h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400">Multi-agent research on Foundation local inference: plan, search, read, synthesize, and cite.</p>
        </div>
        <button onClick={() => setShowSettings(true)} className="flex items-center gap-1.5 text-sm text-zinc-500 hover:text-[#500000] dark:hover:text-[#e0a3a3]">
          <SettingsIcon size={16} /> Settings
        </button>
      </header>

      <div className="grid lg:grid-cols-[1fr_320px] gap-6 items-start">
        <div className="space-y-5" data-tour-id="dr-main">
          {/* Query */}
          <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-xl p-5 shadow-sm space-y-3">
            <textarea
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              rows={3}
              disabled={running}
              placeholder="What do you want researched? e.g. “Compare endowment spending policies at peer foundations.”"
              className="w-full border border-zinc-300 dark:border-zinc-700 dark:bg-zinc-800 rounded-lg px-3 py-2 text-sm resize-y focus:outline-none focus:border-[#500000]"
            />
            <div className="flex items-center gap-3 flex-wrap">
              <label className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-300">
                Depth
                <select value={depth} onChange={(e) => setDepth(e.target.value as typeof depth)} disabled={running} className="border border-zinc-300 dark:border-zinc-700 dark:bg-zinc-800 rounded px-2 py-1 text-sm">
                  <option value="light">Light</option><option value="medium">Medium</option><option value="heavy">Heavy</option>
                </select>
              </label>
              <div className="flex-1" />
              {running ? (
                <button onClick={stop} className="flex items-center gap-1.5 bg-red-600 hover:bg-red-700 text-white font-semibold px-4 py-2 rounded-lg text-sm">
                  <Square size={14} /> Cancel
                </button>
              ) : (
                <button onClick={() => begin()} className="flex items-center gap-1.5 bg-[#500000] hover:bg-[#3c001c] text-white font-semibold px-4 py-2 rounded-lg text-sm transition-colors">
                  <Play size={14} /> Start research
                </button>
              )}
            </div>
            {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          </div>

          {/* Pipeline */}
          {(running || stage > 0) && (
            <div className="flex items-center gap-2 text-xs">
              {STAGES.map((label, i) => {
                const n = i + 1;
                const active = stage === n; const doneStage = stage > n;
                return (
                  <div key={label} className={`flex-1 text-center py-1.5 rounded-md border ${active ? 'border-[#500000] text-[#500000] dark:text-[#e0a3a3] dark:border-[#e0a3a3] font-semibold' : doneStage ? 'border-emerald-400 text-emerald-600 dark:text-emerald-400' : 'border-zinc-200 dark:border-zinc-800 text-zinc-400'}`}>
                    {doneStage ? '✓ ' : ''}{label}
                  </div>
                );
              })}
            </div>
          )}

          {/* Live stream */}
          {rows.length > 0 && (
            <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-xl p-4 shadow-sm">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500 mb-2">Live stream</h3>
              <div ref={feedRef} className="font-mono text-xs space-y-1 max-h-[420px] overflow-y-auto">
                {rows.map((r, i) => (
                  <div key={i} className={`flex gap-2 ${r.tone}`}>
                    <span className="text-zinc-400 shrink-0">{r.t}</span>
                    <span className="shrink-0">{r.icon}</span>
                    <span className="min-w-0">{r.body}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Artifacts */}
          {(plan || draft || notes.length > 0 || sources.length > 0 || thoughts.length > 0 || mmNodes.length > 1) && (
            <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-xl p-4 shadow-sm">
              <div className="flex flex-wrap gap-1 border-b border-zinc-200 dark:border-zinc-800 mb-3">
                {ARTIFACTS.map((a) => (
                  <button key={a.id} onClick={() => setArtTab(a.id)} className={`px-3 py-1.5 text-xs font-medium border-b-2 -mb-px ${artTab === a.id ? 'border-[#500000] text-[#500000] dark:text-[#e0a3a3] dark:border-[#e0a3a3]' : 'border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200'}`}>{a.label}</button>
                ))}
              </div>
              <div className="text-sm max-h-[420px] overflow-y-auto">
                {artTab === 'plan' && <pre className="whitespace-pre-wrap text-zinc-700 dark:text-zinc-300 text-xs leading-relaxed">{plan || 'No plan yet.'}</pre>}
                {artTab === 'draft' && <pre className="whitespace-pre-wrap text-zinc-700 dark:text-zinc-300 text-xs leading-relaxed">{draft || 'No draft yet.'}</pre>}
                {artTab === 'notes' && (notes.length ? <ul className="space-y-1.5 list-disc pl-4 text-zinc-700 dark:text-zinc-300">{notes.map((n, i) => <li key={i}>{n}</li>)}</ul> : <p className="text-zinc-400">No notes yet.</p>)}
                {artTab === 'sources' && (sources.length ? <ul className="space-y-1">{sources.map((s, i) => <li key={i}><a href={s.url} target="_blank" rel="noreferrer" className="text-sky-500 hover:underline text-xs break-all">{s.title || s.url}</a></li>)}</ul> : <p className="text-zinc-400">No sources yet.</p>)}
                {artTab === 'thoughts' && (thoughts.length ? <ul className="space-y-1.5 text-zinc-700 dark:text-zinc-300">{thoughts.map((t, i) => <li key={i}><span className="font-medium text-purple-500">🧠 {t.label}</span>{t.rationale ? <span className="text-zinc-500 italic">: {t.rationale}</span> : null}</li>)}</ul> : <p className="text-zinc-400">No thoughts yet.</p>)}
                {artTab === 'mindmap' && <MindMap nodes={mmNodes} links={mmLinks} />}
              </div>
            </div>
          )}

          {/* Final report */}
          {report && (
            <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500 mb-3">Research report</h3>
              <div className="prose prose-sm dark:prose-invert max-w-none" dangerouslySetInnerHTML={{ __html: reportHtml }} />
            </div>
          )}
        </div>

        {/* Reports history sidebar */}
        <aside className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-xl p-4 shadow-sm">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500 mb-3">Recent reports</h3>
          <ReportsHistory refreshKey={reportsKey} onOpen={(content) => { setReport(content); window.scrollTo(0, 0); }} />
        </aside>
      </div>

      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </div>
  );
}
