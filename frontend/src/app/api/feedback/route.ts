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
export async function POST(req: Request) {
  let comment = '';
  try {
    const body = await req.json();
    comment = typeof body?.comment === 'string' ? body.comment.trim() : '';
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
  });

  if (!ok) {
    return NextResponse.json({ error: 'Could not record feedback' }, { status: 502 });
  }
  return NextResponse.json({ ok: true });
}
