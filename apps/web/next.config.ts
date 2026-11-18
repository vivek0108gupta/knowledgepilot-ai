import type { NextConfig } from "next";

// Sent with every page. Script sources are not restricted here: Next.js inlines its own
// bootstrap scripts, so a script-src policy needs per-request nonces (see
// docs/phase-5.7-security-review.md). These still stop framing (clickjacking), MIME sniffing,
// plugin content, <base> hijacking and form posts to other sites.
const securityHeaders = [
  {
    key: "Content-Security-Policy",
    value: "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
  },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  // Ignored by browsers over plain http (local development); enforced once served over TLS.
  { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
];

// On Vercel the API URL is compiled into the browser bundle at build time. Fail the build
// rather than ship a site that silently calls http://localhost:8000.
if (process.env.VERCEL) {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "";
  if (!/^https:\/\//.test(apiUrl) || /localhost|127\.0\.0\.1/.test(apiUrl)) {
    throw new Error(
      "NEXT_PUBLIC_API_URL must be set to the deployed API's https:// URL in the Vercel project settings.",
    );
  }
  for (const name of ["NEXT_PUBLIC_SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY"]) {
    if (!process.env[name]) throw new Error(`${name} must be set in the Vercel project settings.`);
  }
}

const nextConfig: NextConfig = {
  // Emits .next/standalone so the Docker image runs without node_modules.
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
