'use client';

import { useEffect, useState } from 'react';
import { Moon, Sun } from 'lucide-react';

/**
 * Dark/light theme toggle. Reads & writes data-theme on <html> and persists the
 * choice to localStorage. The no-flash init script in layout.tsx applies the
 * stored theme before paint; this button just flips it at runtime.
 *
 * The UI is already authored with Tailwind dark: utilities — the @custom-variant
 * in globals.css points those at [data-theme="dark"], so flipping the attribute
 * is all this needs to do. Reusable across the TAMF tools.
 */
const STORAGE_KEY = 'tamf-theme';

export default function ThemeToggle() {
  const [dark, setDark] = useState(false);

  // Sync initial state from whatever the init script already applied.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- post-mount client-only DOM read
    setDark(document.documentElement.dataset.theme === 'dark');
  }, []);

  const toggle = () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* private mode / storage disabled — theme still applies for this session */
    }
    setDark(next === 'dark');
  };

  const Icon = dark ? Sun : Moon;

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
      title={dark ? 'Light mode' : 'Dark mode'}
      className="flex items-center gap-3 px-3 py-2.5 transition-colors duration-150 text-white/70 hover:bg-white/10 hover:text-white cursor-pointer"
    >
      <Icon size={20} className="shrink-0" strokeWidth={1.75} />
      <span className="text-sm font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-150">
        {dark ? 'Light mode' : 'Dark mode'}
      </span>
    </button>
  );
}
