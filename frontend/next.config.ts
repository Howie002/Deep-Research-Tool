import type { NextConfig } from "next";

// Served behind the dashboard's NPM routing plane at
// aisandbox.txamfoundation.com/DeepResearch. The Next.js app owns the basePath;
// proxy_routes for /DeepResearch is flipped to strip_prefix=0 (the old
// FastAPI-only deploy used strip_prefix=1 + a <base href> shim).
const BASE_PATH = "/DeepResearch";

const nextConfig: NextConfig = {
  basePath: BASE_PATH,
  env: { NEXT_PUBLIC_BASE_PATH: BASE_PATH },

  // The rewrite proxy defaults to a 30s timeout — far too short for the
  // long-lived SSE research stream (/api/jobs/{id}/stream runs for minutes) and
  // long agent calls. Raise it to 1 hour (matches the original route's ceiling)
  // so streams and long runs aren't cut at 30s.
  // proxyClientMaxBodySize: Next 16 caps proxied request bodies at 10MB by
  // default, which silently truncates large uploads before they reach the
  // FastAPI backend (HyperFrames generation incident, 2026-07-02).
  experimental: { proxyTimeout: 3_600_000, proxyClientMaxBodySize: '200mb' },

  // The FastAPI backend serves its routes at ROOT (/api/..., /health). Proxy
  // both to the internal backend on :8765. The SSE endpoint
  // (/api/jobs/:id/stream) rides the /api/* rule — Next streams the chunked
  // text/event-stream response through. The Next.js /api/feedback filesystem
  // route takes precedence over the /api/* rewrite (feedback stays local).
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8765/api/:path*" },
      { source: "/health", destination: "http://127.0.0.1:8765/health" },
    ];
  },
};

export default nextConfig;
