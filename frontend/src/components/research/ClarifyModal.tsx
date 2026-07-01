'use client';

import { useState } from 'react';
import { X } from 'lucide-react';

export interface ClarifyQ { question: string; placeholder?: string }

/**
 * Pre-research clarification modal: the LLM proposes questions; the user's
 * answers are folded into a `clarifications` block appended to the query.
 */
export default function ClarifyModal({
  questions, onSubmit, onSkip,
}: {
  questions: ClarifyQ[];
  onSubmit: (clarifications: string) => void;
  onSkip: () => void;
}) {
  const [answers, setAnswers] = useState<string[]>(questions.map(() => ''));

  const submit = () => {
    const block = questions
      .map((q, i) => (answers[i].trim() ? `Q: ${q.question}\nA: ${answers[i].trim()}` : ''))
      .filter(Boolean)
      .join('\n\n');
    onSubmit(block);
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) onSkip(); }}>
      <div className="bg-white dark:bg-zinc-900 w-full max-w-lg rounded-xl border border-zinc-200 dark:border-zinc-800 shadow-xl overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-200 dark:border-zinc-800">
          <span className="text-sm font-semibold text-zinc-800 dark:text-zinc-100">A few quick questions to focus the research</span>
          <button onClick={onSkip} aria-label="Close" className="text-zinc-400 hover:text-[#500000]"><X size={16} /></button>
        </div>
        <div className="px-5 py-4 space-y-4 max-h-[60vh] overflow-y-auto">
          {questions.map((q, i) => (
            <div key={i}>
              <label className="block text-sm text-zinc-700 dark:text-zinc-300 mb-1">{q.question}</label>
              <input
                value={answers[i]}
                onChange={(e) => setAnswers((a) => a.map((v, j) => (j === i ? e.target.value : v)))}
                placeholder={q.placeholder || ''}
                className="w-full border border-zinc-300 dark:border-zinc-700 dark:bg-zinc-800 rounded px-2.5 py-1.5 text-sm focus:outline-none focus:border-[#500000]"
              />
            </div>
          ))}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-zinc-200 dark:border-zinc-800">
          <button onClick={onSkip} className="text-sm px-3 py-2 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200">Skip</button>
          <button onClick={submit} className="bg-[#500000] hover:bg-[#3c001c] text-white text-sm font-semibold px-5 py-2 rounded-lg transition-colors">Start research</button>
        </div>
      </div>
    </div>
  );
}
