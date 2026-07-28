'use client';

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Moon, Sun } from 'lucide-react';

/**
 * Persistent floating dark/light theme bubble (fleet standard).
 *
 * Reads and writes data-theme on <html> and persists to localStorage; the
 * no-flash init script in layout.tsx applies the stored theme before paint, so
 * this only flips it at runtime.
 *
 * It renders as a bottom-left bubble rather than a row inside the nav rail, so
 * the control is reachable without hovering the rail open and reads as one
 * family with the feedback bubble (bottom-right). Portaled to document.body
 * because the rail is overflow-hidden and would clip a fixed child.
 *
 * Because it is portaled it is NOT a descendant of the rail, so `group-hover:`
 * and inherited CSS variables cannot reach it, and a hardcoded left offset gets
 * covered the moment the tray folds out. A ResizeObserver measures the rail's
 * live width instead and drives `left`, which keeps this pattern-agnostic: it
 * works for hover-expand rails, click-to-collapse rails that persist state in a
 * store, and anything else, because it only cares what the element currently
 * measures. ResizeObserver fires throughout a CSS width transition, so the
 * bubble travels with the tray rather than jumping once it settles.
 */
const STORAGE_KEY = 'tamf-theme';
/** Gap between the right edge of the nav rail and the bubble. */
const RAIL_GUTTER = 20;
/** Fallback if no rail is found (matches the collapsed w-16 rail). */
const RAIL_FALLBACK = 64;

function useRailWidth(): number {
  const [width, setWidth] = useState(RAIL_FALLBACK);

  useEffect(() => {
    // data-nav-rail is explicit; the aside fallback covers tools that have not
    // been annotated yet.
    const rail =
      document.querySelector('[data-nav-rail]') ?? document.querySelector('aside');
    if (!rail) return;

    const measure = () => setWidth(rail.getBoundingClientRect().width);
    measure();

    const ro = new ResizeObserver(measure);
    ro.observe(rail);
    return () => ro.disconnect();
  }, []);

  return width;
}

export default function ThemeToggle() {
  const [dark, setDark] = useState(false);
  const [mounted, setMounted] = useState(false);
  const railWidth = useRailWidth();

  // Portal mount gate: document.body only exists client-side.
  useEffect(() => {
    setMounted(true);
  }, []);

  // Sync initial state from whatever the init script already applied. Reading
  // the DOM can only happen post-mount, so this one-time setState is
  // intentional and cannot cause a hydration mismatch.
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

  if (!mounted) return null;

  const Icon = dark ? Sun : Moon;

  return createPortal(
    <button
      type="button"
      onClick={toggle}
      aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
      title={dark ? 'Light mode' : 'Dark mode'}
      style={{
        left: railWidth + RAIL_GUTTER,
        // `left` must NOT be transitioned. The ResizeObserver already fires each
        // frame of the rail's own width animation, so an additional transition
        // here makes the bubble chase a moving target and trail the rail by
        // roughly its own duration: measured 230px rail vs 139px bubble
        // mid-expand, i.e. the tray visibly sweeps over the bubble before it
        // catches up. Restricting the transition to the hover affordances keeps
        // the horizontal tracking frame-exact. Set inline rather than via a
        // Tailwind arbitrary value so it cannot silently fail to generate.
        transitionProperty: 'background-color, transform',
      }}
      className="fixed bottom-5 z-40 flex items-center justify-center w-12 h-12 rounded-full bg-[#500000] text-white shadow-lg shadow-black/30 ring-1 ring-white/25 hover:bg-[#3c001c] hover:scale-105 duration-150 cursor-pointer"
    >
      <Icon size={21} strokeWidth={1.9} />
    </button>,
    document.body
  );
}
