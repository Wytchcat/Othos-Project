import { readFileSync } from 'node:fs';
import { logoType, readIdentity } from '@/lib/identity';

// Othos: the logo file named in personal.json's assistant.logo (PNG, SVG, JPG, WebP or
// ICO). Only that one file is served; with no logo set this answers 404.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET() {
  const { logo } = readIdentity();
  const type = logo && logoType(logo);
  if (!type) return new Response('No logo', { status: 404 });
  try {
    return new Response(readFileSync(logo), {
      headers: {
        'Content-Type': type,
        'Cache-Control': 'no-store',
        // an SVG logo can't run scripts when opened on its own
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
      },
    });
  } catch {
    return new Response('No logo', { status: 404 });
  }
}
