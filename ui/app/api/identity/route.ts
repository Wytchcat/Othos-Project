import { readIdentity } from '@/lib/identity';

// Othos: the assistant's name and colour for the call page, and whether there's a logo
// (served by ./logo). Read from the agent's personal.json on this PC.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET() {
  const { name, color, logo } = readIdentity();
  return Response.json(
    { name, color, hasLogo: logo !== '' },
    { headers: { 'Cache-Control': 'no-store' } }
  );
}
