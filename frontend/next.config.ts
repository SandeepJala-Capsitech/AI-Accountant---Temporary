import type { NextConfig } from 'next'

const apiUrl = process.env.API_URL ?? 'http://127.0.0.1:8085'
// Must match the API's LEDGERSYNC_MAX_UPLOAD_MB (default 20).
const maxUploadMb = Number(process.env.LEDGERSYNC_MAX_UPLOAD_MB ?? 20)

const nextConfig: NextConfig = {
  experimental: {
    // The rewrite proxy gives up after 30s by default; a cold local model can take ~40s.
    proxyTimeout: 120_000,
    // The proxy buffers request bodies and silently cuts them at 10MB by default, which
    // leaves the API waiting for bytes that never arrive. Allow the API's limit plus
    // multipart overhead so oversized files reach the API and get its clear 413.
    proxyClientMaxBodySize: `${maxUploadMb + 5}mb`,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${apiUrl}/api/:path*`,
      },
    ]
  },
}

export default nextConfig
