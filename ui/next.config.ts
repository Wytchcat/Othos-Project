import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Othos is opened at http://127.0.0.1:3000 (not localhost). Next.js 16 blocks dev resources
  // such as the hot-reload socket for any other origin, which stops the page from hydrating.
  allowedDevOrigins: ['127.0.0.1'],
  // Othos: hide the round "N" / "Issues" development badge in the bottom-left corner.
  devIndicators: false,
};

export default nextConfig;
