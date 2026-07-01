'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react';
import { usePathname } from 'next/navigation';
import { driver, type Driver, type DriveStep } from 'driver.js';
import 'driver.js/dist/driver.css';
import './tour.css'; // canonical TAMF tutorial popup styling (shared across tools)
import { getSegment, type TourSegment, type TourStep } from '@/lib/tour/manifest';

/**
 * the Deep Research Agent guided-tour engine. Drives a manifest segment with driver.js
 * (spotlight + arrow + sequenced popovers). Mounted once near the app root so
 * the sidebar Tutorial button can start it, and it auto-runs once on a user's
 * first visit. Mirrors the dashboard / K-1 / Chat / Coach TourProvider so all
 * tours match.
 */

const SEEN_KEY = 'tamf-tour-seen-deep-research';
const GATE_PATH = '/';

interface TourContextValue {
  startTour: (app?: string) => void;
  endTour: () => void;
  active: boolean;
}

const TourContext = createContext<TourContextValue | null>(null);

function selectorFor(step: TourStep): string | undefined {
  return step.tourId ? `[data-tour-id="${step.tourId}"]` : undefined;
}

function buildSteps(segment: TourSegment): DriveStep[] {
  return segment.steps
    .filter((step) => {
      const selector = selectorFor(step);
      return !selector || document.querySelector(selector) !== null;
    })
    .map((step) => {
      const selector = selectorFor(step);
      return {
        ...(selector ? { element: selector } : {}),
        popover: {
          title: step.title,
          description: step.body,
          ...(step.side && step.side !== 'over' ? { side: step.side } : {}),
          ...(step.align ? { align: step.align } : {}),
          ...(step.nudgeX
            ? {
                onPopoverRender: (popover: { wrapper: HTMLElement }) => {
                  popover.wrapper.style.marginLeft = `${step.nudgeX}px`;
                },
              }
            : {}),
        },
      } satisfies DriveStep;
    });
}

export default function TourProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [active, setActive] = useState(false);
  const driverRef = useRef<Driver | null>(null);
  const pathname = usePathname();

  const endTour = useCallback(() => {
    driverRef.current?.destroy();
  }, []);

  const startTour = useCallback((app = 'deep-research') => {
    const segment = getSegment(app);
    if (!segment) return;

    driverRef.current?.destroy();

    const steps = buildSteps(segment);
    if (steps.length === 0) return;

    const instance = driver({
      showProgress: true,
      allowClose: true,
      overlayColor: '#1A1A1A',
      overlayOpacity: 0.65,
      stagePadding: 6,
      stageRadius: 0, // square, to match the TAMF brand
      popoverClass: 'tamf-tour',
      nextBtnText: 'Next →',
      prevBtnText: '← Back',
      doneBtnText: 'Done',
      progressText: 'Step {{current}} of {{total}}',
      steps,
      onDestroyed: () => {
        setActive(false);
        driverRef.current = null;
      },
    });

    driverRef.current = instance;
    setActive(true);
    instance.drive();
  }, []);

  useEffect(() => () => driverRef.current?.destroy(), []);

  // First-use auto-trigger: launch the walkthrough once on a user's first visit
  // to the home route (localStorage-gated), then opt-in via the Tutorial button.
  // Gated on the home path so the anchored steps exist.
  useEffect(() => {
    if (pathname !== GATE_PATH) return;
    let seen = true;
    try {
      seen = localStorage.getItem(SEEN_KEY) === '1';
    } catch {
      return;
    }
    if (seen) return;
    const t = setTimeout(() => {
      try {
        localStorage.setItem(SEEN_KEY, '1');
      } catch {
        /* ignore */
      }
      startTour('deep-research');
    }, 900);
    return () => clearTimeout(t);
  }, [pathname, startTour]);

  return (
    <TourContext.Provider value={{ startTour, endTour, active }}>
      {children}
    </TourContext.Provider>
  );
}

export function useTour(): TourContextValue {
  const ctx = useContext(TourContext);
  if (!ctx) {
    throw new Error('useTour must be used within a TourProvider');
  }
  return ctx;
}
