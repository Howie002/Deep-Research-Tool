'use client';

import { useEffect, useState } from 'react';
import { MessageSquarePlus, X } from 'lucide-react';

const BASE = '/DeepResearch';

/**
 * Sidebar Feedback button (fleet standard). Collapses with the rail like the
 * nav items; opens a small modal that posts a free-form note to /api/feedback,
 * which forwards it to the dashboard's Feedback panel.
 */
export default function FeedbackButton() {
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setMessage('');
      setSent(false);
      setSending(false);
      setError(null);
    }
  }, [open]);

  const submit = async () => {
    const text = message.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    try {
      const res = await fetch(`${BASE}/api/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ comment: text }),
      });
      if (res.ok) setSent(true);
      else setError('Could not send feedback');
    } catch {
      setError('Could not send feedback');
    }
    setSending(false);
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="Send feedback to the AI team"
        className="w-full flex items-center gap-3 px-3 py-2.5 rounded-md transition-colors duration-150 text-white/70 hover:bg-white/10 hover:text-white"
      >
        <MessageSquarePlus size={20} className="shrink-0" strokeWidth={1.75} />
        <span className="text-sm font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-150">
          Feedback
        </span>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          <div className="bg-[#FFFFFF] w-full max-w-lg rounded-xl border border-[#D6D3C4] shadow-xl overflow-hidden">
            <div className="flex items-center justify-between px-5 py-4 border-b border-[#D6D3C4]">
              <div className="flex items-center gap-3">
                <span className="w-[3px] h-5 bg-[#500000] rounded-full" />
                <span className="text-[11px] font-bold tracking-[0.16em] uppercase text-[#500000]">
                  Send Feedback
                </span>
              </div>
              <button
                onClick={() => setOpen(false)}
                className="text-[#707070] hover:text-[#500000] transition-colors duration-150"
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>

            {sent ? (
              <div className="px-5 py-8 text-center space-y-3">
                <MessageSquarePlus size={28} className="mx-auto text-[#500000]" />
                <p className="text-sm text-[#1A1A1A]">Thanks — your feedback was recorded.</p>
                <div className="flex justify-center pt-1">
                  <button
                    onClick={() => setOpen(false)}
                    className="bg-[#500000] text-white text-xs font-bold tracking-[0.12em] uppercase px-5 py-2 rounded-md hover:bg-[#3c001c] transition-colors duration-150"
                  >
                    Done
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="px-5 py-5 space-y-3">
                  <p className="text-xs text-[#707070] leading-relaxed">
                    Tell us what is working, what is missing, or anything we could improve
                    about the Deep Research Agent. This goes to the AI team.
                  </p>
                  <textarea
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    rows={6}
                    autoFocus
                    placeholder="Your feedback…"
                    className="w-full resize-none border border-[#D6D3C4] bg-[#FFFFFF] text-[#1A1A1A] text-sm px-4 py-3 rounded-md focus:outline-none focus:border-[#500000] transition-colors duration-150 leading-relaxed"
                  />
                  {error && <p className="text-xs text-[#B91C1C]">{error}</p>}
                </div>
                <div className="flex justify-end gap-3 px-5 pb-5">
                  <button
                    onClick={() => setOpen(false)}
                    className="text-xs font-bold tracking-[0.12em] uppercase text-[#707070] hover:text-[#1A1A1A] transition-colors duration-150 px-3 py-2"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={submit}
                    disabled={!message.trim() || sending}
                    className="bg-[#500000] text-white text-xs font-bold tracking-[0.12em] uppercase px-5 py-2 rounded-md hover:bg-[#3c001c] disabled:opacity-40 transition-colors duration-150"
                  >
                    {sending ? 'Sending…' : 'Send'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
