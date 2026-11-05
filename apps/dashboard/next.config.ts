import type { NextConfig } from "next";
const config: NextConfig = {
  output: "standalone",
  experimental: { proxyClientMaxBodySize: 32 * 1024 * 1024 },
  async rewrites() {
    return [
      {
        source: "/health/ready",
        destination: `${process.env.API_URL || "http://127.0.0.1:8000"}/health/ready`,
      },
      {
        source: "/api/:path*",
        destination: `${process.env.API_URL || "http://127.0.0.1:8000"}/api/:path*`,
      },
      {
        source: "/ws/:path*",
        destination: `${process.env.API_URL || "http://127.0.0.1:8000"}/ws/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};
export default config;
