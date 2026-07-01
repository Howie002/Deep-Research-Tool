/**
 * Deep Research Agent — guided-tour manifest.
 * Mirrors the canonical dashboard tour system. Element-anchored steps whose
 * target isn't in the DOM are dropped automatically.
 */

export interface TourStep {
  tourId?: string;
  title: string;
  body: string;
  side?: 'top' | 'bottom' | 'left' | 'right' | 'over';
  align?: 'start' | 'center' | 'end';
  nudgeX?: number;
}

export interface TourSegment { app: string; label: string; url?: string; steps: TourStep[] }

export const TOUR_MANIFEST: TourSegment[] = [
  {
    app: 'deep-research',
    label: 'Deep Research Agent',
    steps: [
      {
        title: 'Welcome to the Deep Research Agent',
        body: 'A multi-agent researcher on Foundation local inference: it plans, searches the web, reads sources, and synthesizes a cited report — streaming every step live. Use Next / Back, or press Esc to leave anytime.',
      },
      {
        tourId: 'dr-main',
        title: 'Ask, then watch it work',
        body: 'Enter a research question and Start — or “Clarify first” to answer a couple of focusing questions. As it runs you’ll see a live stream of searches, reads, notes, and reasoning, a stage pipeline, artifact tabs (Plan / Notes / Draft / Sources / Thoughts / Mind Map), and the final cited report. Past reports (searchable, exportable to PDF/DOCX) are in the sidebar.',
        side: 'top',
        align: 'center',
      },
      {
        tourId: 'tutorial-toggle',
        title: 'Replay anytime',
        body: 'That’s the tour. Start it again from this Tutorial button whenever you need it. Feedback or something off? The Feedback button is right here too.',
        side: 'right',
        align: 'end',
      },
    ],
  },
];

export function getSegment(app: string): TourSegment | undefined {
  return TOUR_MANIFEST.find((s) => s.app === app);
}
