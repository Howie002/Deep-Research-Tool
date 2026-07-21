// middleware.ts — Foundation AI gate HMAC verification.
// Rejects direct VLAN port access that bypasses the dashboard proxy. The dashboard
// signs X-Foundation-* headers; only requests carrying a valid signature (i.e.
// routed through the gate) are allowed through. No-op until GATE_HMAC_SECRET is
// set, so it is safe to ship before activation.
import { NextRequest, NextResponse } from 'next/server';

const SECRET = process.env.GATE_HMAC_SECRET ?? '';

async function verifyHmac(userId: string, email: string, role: string, sig: string) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(SECRET),
    { name: 'HMAC', hash: 'SHA-256' },
    false, ['sign']
  );
  const raw = await crypto.subtle.sign('HMAC', key, enc.encode(`${userId}|${email}|${role}`));
  const expected = Array.from(new Uint8Array(raw))
    .map(b => b.toString(16).padStart(2, '0')).join('');
  return sig === expected;
}

export async function middleware(req: NextRequest) {
  if (!SECRET) return NextResponse.next();

  const userId = req.headers.get('x-foundation-user') ?? '';
  const email  = req.headers.get('x-foundation-email') ?? '';
  const role   = req.headers.get('x-foundation-role') ?? '';
  const sig    = req.headers.get('x-foundation-sig') ?? '';

  if (!sig || !await verifyHmac(userId, email, role, sig)) {
    return new NextResponse('Access this tool through the Foundation AI Dashboard.', { status: 403 });
  }
  return NextResponse.next();
}

export const config = {
  // Include the basePath root ('/') explicitly — the catch-all alone misses it
  // under basePath, which would leave the tool's main page ungated.
  matcher: ['/', '/((?!_next/static|_next/image|favicon.ico).*)'],
};
