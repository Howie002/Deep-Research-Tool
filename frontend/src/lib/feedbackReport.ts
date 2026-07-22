import 'server-only';
import { createHmac } from 'crypto';

/**
 * Forward a user feedback note to the Foundation AI Dashboard's Feedback panel
 * (single source of truth). Same HMAC scheme as the telemetry shim: sign the
 * raw JSON body with the shared TELEMETRY_HMAC_SECRET and send it as
 * `x-telemetry-sig`. Awaited by the caller so a failed forward surfaces to the
 * user instead of vanishing. No-ops if the secret isn't provisioned.
 */

const DASHBOARD_URL = process.env.DASHBOARD_INTERNAL_URL || 'http://127.0.0.1:3010';
const SECRET = process.env.TELEMETRY_HMAC_SECRET || '';
const TOOL_ID = 'deep-research-agent';

export interface FeedbackReport {
  userId?: string | null;
  userEmail?: string | null;
  comment: string;
  /** Optional data-URL screenshot (image/jpeg or image/png), pre-validated by the route. */
  screenshot?: string | null;
  /** Full URL of the page the user was on, pre-validated by the route. */
  pageUrl?: string | null;
}

export async function reportFeedbackToDashboard(f: FeedbackReport): Promise<boolean> {
  if (!SECRET) return false; // not provisioned
  const body = JSON.stringify({
    toolId: TOOL_ID,
    userId: f.userId ?? null,
    userEmail: f.userEmail ?? null,
    rating: 'note',
    comment: f.comment ?? '',
    ...(f.screenshot ? { screenshot: f.screenshot } : {}),
    ...(f.pageUrl ? { pageUrl: f.pageUrl } : {}),
  });
  const sig = createHmac('sha256', SECRET).update(body).digest('hex');

  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 4000);
  try {
    const res = await fetch(`${DASHBOARD_URL}/api/telemetry/feedback`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-telemetry-sig': sig },
      body,
      signal: ctrl.signal,
    });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
}
