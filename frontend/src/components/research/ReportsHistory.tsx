'use client';

import { useCallback, useEffect, useState } from 'react';
import { FileText, Download, Trash2, Search } from 'lucide-react';
import { listReports, searchReports, getReport, deleteReport, exportUrl, type ReportSummary } from '@/lib/api';

/** Recent-reports panel: list / full-text search / open / export / delete. */
export default function ReportsHistory({
  onOpen, refreshKey,
}: {
  onOpen: (content: string, filename: string) => void;
  refreshKey: number;
}) {
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [q, setQ] = useState('');
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = q.trim() ? await searchReports(q.trim()) : await listReports();
      const list = (d.reports || d.results || []) as ReportSummary[];
      setReports(Array.isArray(list) ? list : []);
    } catch { setReports([]); } finally { setLoading(false); }
  }, [q]);

  useEffect(() => { load(); }, [load, refreshKey]);

  const open = async (f: string) => {
    try {
      const r = await getReport(f);
      onOpen(String(r.content ?? r.markdown ?? r.body ?? ''), f);
    } catch { /* */ }
  };
  const remove = async (f: string) => { await deleteReport(f); load(); };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-2.5 top-2.5 text-zinc-400" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search reports…"
            className="w-full pl-8 pr-2 py-1.5 text-sm border border-zinc-300 dark:border-zinc-700 dark:bg-zinc-800 rounded focus:outline-none focus:border-[#500000]"
          />
        </div>
      </div>
      {loading ? (
        <p className="text-sm text-zinc-400 py-4 text-center">Loading…</p>
      ) : reports.length === 0 ? (
        <p className="text-sm text-zinc-400 py-4 text-center">No reports yet.</p>
      ) : (
        <div className="space-y-1.5">
          {reports.map((r) => (
            <div key={r.filename} className="group border border-zinc-200 dark:border-zinc-800 rounded-lg px-3 py-2 hover:border-[#500000] transition-colors">
              <button onClick={() => open(r.filename)} className="text-left w-full">
                <div className="flex items-center gap-2">
                  <FileText size={14} className="text-[#500000] dark:text-[#e0a3a3] shrink-0" />
                  <span className="text-sm text-zinc-800 dark:text-zinc-100 truncate">{r.title || r.query || r.filename}</span>
                </div>
                {r.created && <div className="text-xs text-zinc-400 mt-0.5">{String(r.created)}</div>}
              </button>
              <div className="flex items-center gap-2 mt-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <a href={exportUrl(r.filename, 'pdf')} className="text-xs text-zinc-500 hover:text-[#500000] flex items-center gap-1"><Download size={12} /> PDF</a>
                <a href={exportUrl(r.filename, 'docx')} className="text-xs text-zinc-500 hover:text-[#500000] flex items-center gap-1"><Download size={12} /> DOCX</a>
                <button onClick={() => remove(r.filename)} className="text-xs text-zinc-500 hover:text-red-600 flex items-center gap-1 ml-auto"><Trash2 size={12} /> Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
