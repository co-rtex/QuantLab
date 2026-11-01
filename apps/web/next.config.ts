import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // No cacheComponents or partialPrefetching (Partial Prerendering): pages render per
  // request so that their scripts carry the Content-Security-Policy nonce (#166). A
  // prerendered shell's scripts would be refused. Next's CSP guide: nonces need dynamic
  // rendering, and PPR is incompatible with them.
  output: "standalone",
  poweredByHeader: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
