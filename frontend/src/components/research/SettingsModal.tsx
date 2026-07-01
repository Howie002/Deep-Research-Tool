'use client';

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { getSettings, saveSettings, discover } from '@/lib/api';

const input = 'w-full border border-zinc-300 dark:border-zinc-700 dark:bg-zinc-800 rounded px-2.5 py-1.5 text-sm focus:outline-none focus:border-[#500000]';

/** Settings modal: AI endpoint + model + search backend + keys/toggles. */
export default function SettingsModal({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState<Record<string, unknown>>({});
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => { getSettings().then(setS).catch(() => {}); }, []);
  const set = (k: string, v: unknown) => setS((p) => ({ ...p, [k]: v }));
  const val = (k: string) => (s[k] == null ? '' : String(s[k]));

  const save = async () => {
    setStatus('Saving…');
    try { await saveSettings(s); setStatus('Saved.'); } catch (e) { setStatus((e as Error).message); }
  };
  const doDiscover = async () => {
    setStatus('Scanning for local AI servers…');
    try { const d = await discover(); setStatus(`Found: ${JSON.stringify(d).slice(0, 200)}`); } catch (e) { setStatus((e as Error).message); }
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="bg-white dark:bg-zinc-900 w-full max-w-lg rounded-xl border border-zinc-200 dark:border-zinc-800 shadow-xl overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-200 dark:border-zinc-800">
          <span className="text-sm font-semibold text-zinc-800 dark:text-zinc-100">Settings</span>
          <button onClick={onClose} aria-label="Close" className="text-zinc-400 hover:text-[#500000]"><X size={16} /></button>
        </div>
        <div className="px-5 py-4 space-y-3 max-h-[65vh] overflow-y-auto">
          <div>
            <label className="block text-[11px] uppercase tracking-wide text-zinc-500 mb-1">AI endpoint URL</label>
            <input className={input} value={val('endpoint') || val('base_url') || val('lm_studio_base_url')} onChange={(e) => set('base_url', e.target.value)} placeholder="http://host:port/v1" />
          </div>
          <div>
            <label className="block text-[11px] uppercase tracking-wide text-zinc-500 mb-1">Model ID</label>
            <input className={input} value={val('model') || val('model_id')} onChange={(e) => set('model', e.target.value)} />
          </div>
          <div>
            <label className="block text-[11px] uppercase tracking-wide text-zinc-500 mb-1">Search backend</label>
            <select className={input} value={val('search_backend')} onChange={(e) => set('search_backend', e.target.value)}>
              <option value="duckduckgo">DuckDuckGo</option>
              <option value="langsearch">LangSearch</option>
              <option value="brave">Brave</option>
              <option value="serpapi">SerpAPI</option>
            </select>
          </div>
          <div>
            <label className="block text-[11px] uppercase tracking-wide text-zinc-500 mb-1">Search API key (if required)</label>
            <input className={input} value={val('search_api_key')} onChange={(e) => set('search_api_key', e.target.value)} placeholder="key" />
          </div>
          {status && <p className="text-xs text-zinc-500 break-all">{status}</p>}
        </div>
        <div className="flex justify-between gap-2 px-5 py-4 border-t border-zinc-200 dark:border-zinc-800">
          <button onClick={doDiscover} className="text-sm px-3 py-2 text-[#500000] dark:text-[#e0a3a3] hover:underline">Discover local servers</button>
          <button onClick={save} className="bg-[#500000] hover:bg-[#3c001c] text-white text-sm font-semibold px-5 py-2 rounded-lg transition-colors">Save</button>
        </div>
      </div>
    </div>
  );
}
