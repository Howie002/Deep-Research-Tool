'use client';

import { GraduationCap, X } from 'lucide-react';
import { useTour } from './TourProvider';

/**
 * Canonical sidebar Tutorial button (fleet standard). Sits in the sidebar's
 * bottom cluster next to Feedback + theme, collapsing with the rail like the
 * nav items. Starts / ends the guided walkthrough via the shared TourProvider.
 * Tagged `data-tour-id="tutorial-toggle"` so the tour's closing step points at it.
 */
export default function SidebarTutorialButton() {
  const { startTour, endTour, active } = useTour();

  return (
    <button
      type="button"
      data-tour-id="tutorial-toggle"
      onClick={() => (active ? endTour() : startTour('deep-research'))}
      aria-pressed={active}
      title={active ? 'End the walkthrough' : 'Start the tutorial'}
      className="w-full flex items-center gap-3 px-3 py-2.5 rounded-md transition-colors duration-150 text-white/70 hover:bg-white/10 hover:text-white"
    >
      {active ? (
        <X size={20} className="shrink-0" strokeWidth={1.9} />
      ) : (
        <GraduationCap size={20} className="shrink-0" strokeWidth={1.75} />
      )}
      <span className="text-sm font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-150">
        {active ? 'End tutorial' : 'Tutorial'}
      </span>
    </button>
  );
}
