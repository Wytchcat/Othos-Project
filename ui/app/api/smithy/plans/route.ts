import { execFile } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { AGENT_DIR, fromOthosPage } from '@/lib/othos-local';

// Othos: The Smithy's build plans waiting for an answer, on the code page too (Nov 14, suggestion 3).
// The same answers as The Smithy's Plan window, through the same command (smithy.py plans / plan ID /
// reply ID), so whichever place answers first, the others see it's answered. Only the Othos page itself
// may ask; nothing here runs a build's code.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const PY = path.join(AGENT_DIR, '.venv', 'Scripts', 'python.exe');
const SMITHY = path.join(AGENT_DIR, 'smithy.py');

function sameSite(req: Request): boolean {
  const origin = req.headers.get('origin');
  if (origin) return fromOthosPage(req);
  const site = req.headers.get('sec-fetch-site');
  return site === null || site === 'same-origin';
}

function smithy(args: string[], input?: string): Promise<{ ok: boolean; out: string }> {
  return new Promise((resolve) => {
    if (!existsSync(PY) || !existsSync(SMITHY)) {
      resolve({ ok: false, out: "The Smithy isn't installed in this copy of Othos." });
      return;
    }
    const child = execFile(
      PY,
      [SMITHY, ...args],
      { cwd: AGENT_DIR, timeout: 120_000, windowsHide: true, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        const out = (stdout || '').trim();
        resolve({ ok: !err, out: err ? (stderr || '').trim().split('\n').pop() || out : out });
      }
    );
    if (input !== undefined) {
      child.stdin?.end(input, 'utf8');
    }
  });
}

export async function GET(req: Request) {
  if (!sameSite(req)) return new Response('Forbidden', { status: 403 });
  const list = await smithy(['plans']);
  if (!list.ok) return Response.json({ plans: [], note: list.out });
  let ids: { id: number }[] = [];
  try {
    ids = JSON.parse(list.out);
  } catch {
    return Response.json({ plans: [], note: "The Smithy's plans couldn't be read." });
  }
  const plans = [];
  for (const { id } of ids.slice(0, 10)) {
    const one = await smithy(['plan', String(id)]);
    try {
      if (one.ok) plans.push(JSON.parse(one.out));
    } catch {
      /* one unreadable plan: the others still show */
    }
  }
  return Response.json({ plans });
}

// { id, answer: "yes" | "no" | "answers", text?, buildNow? }
export async function POST(req: Request) {
  if (!fromOthosPage(req)) return new Response('Forbidden', { status: 403 });
  let body: { id?: unknown; answer?: unknown; text?: unknown; buildNow?: unknown };
  try {
    body = await req.json();
  } catch {
    return new Response('Bad request', { status: 400 });
  }
  const id = Number(body.id);
  if (!Number.isInteger(id) || id <= 0) return new Response('Bad request', { status: 400 });
  const kind = body.answer;
  const text =
    kind === 'yes'
      ? 'yes'
      : kind === 'no'
        ? 'no'
        : typeof body.text === 'string'
          ? body.text.trim()
          : '';
  if (!text || text.length > 6000) return new Response('Bad request', { status: 400 });
  const args = [
    'reply',
    String(id),
    ...(kind === 'answers' && body.buildNow === true ? ['--build'] : []),
    '-',
  ];
  const r = await smithy(args, text);
  return Response.json({
    ok: r.ok && !r.out.startsWith("can't"),
    said: r.out || 'No answer came back from The Smithy.',
  });
}
