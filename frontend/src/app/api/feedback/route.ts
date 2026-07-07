import { NextResponse } from 'next/server';
import { reportFeedbackToDashboard } from '@/lib/feedbackReport';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * Receive a feedback note from the sidebar Feedback button and forward it to the
 * dashboard's Feedback panel. Identity comes from the dashboard-injected
 * X-Foundation-* headers (null when hit directly). Awaited so a failed forward
 * surfaces to the user instead of being silently dropped.
 */
/** Screenshot data-URL cap: stays comfortably under proxy body limits. */
const SCREENSHOT_MAX_CHARS = 900_000;
const SCREENSHOT_PREFIX = /^data:image\/(jpeg|png);base64,/;

export async function POST(req: Request) {
  let comment = '';
  let screenshot: string | null = null;
  try {
    const body = await req.json();
    comment = typeof body?.comment === 'string' ? body.comment.trim() : '';
    if (typeof body?.screenshot === 'string' && body.screenshot) {
      if (!SCREENSHOT_PREFIX.test(body.screenshot)) {
        return NextResponse.json({ error: 'Bad screenshot format' }, { status: 400 });
      }
      if (body.screenshot.length > SCREENSHOT_MAX_CHARS) {
        return NextResponse.json({ error: 'Screenshot too large' }, { status: 413 });
      }
      screenshot = body.screenshot;
    }
  } catch {
    return NextResponse.json({ error: 'Bad request' }, { status: 400 });
  }
  if (!comment) {
    return NextResponse.json({ error: 'Empty feedback' }, { status: 400 });
  }

  const ok = await reportFeedbackToDashboard({
    userId: req.headers.get('x-foundation-user'),
    userEmail: req.headers.get('x-foundation-email'),
    comment: comment.slice(0, 4000),
    screenshot,
  });

  if (!ok) {
    return NextResponse.json({ error: 'Could not record feedback' }, { status: 502 });
  }
  return NextResponse.json({ ok: true });
}
