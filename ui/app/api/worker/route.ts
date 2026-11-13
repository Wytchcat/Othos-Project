import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { AGENT_DIR } from '@/lib/othos-local';

// Othos: the CPU workers as the Circuit Board shows them, each with its own chip:
//   firestarter  site work (Joomla/ZOO drafts, writing)   state\worker-status.json
//   smithy       coding (reviews, fixes, scripts)           state\smithy-status.json
// Each is boiled down to one of:
//   off      not running (no status, or none for 30 s)
//   working  on a task Othos handed off (asked by voice)
//   ready    a result is waiting for Othos to report
//   idle     running, nothing to show
// Read-only, local files only.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const WORKERS = {
  firestarter: path.join(AGENT_DIR, 'state', 'worker-status.json'),
  smithy: path.join(AGENT_DIR, 'state', 'smithy-status.json'),
} as const;
const STALE_S = 30;

type Mode = 'off' | 'idle' | 'working' | 'ready';
type WorkerStatus = {
  state?: string;
  updated_ts?: number;
  task_type?: string | null;
  task_requested_by?: string | null;
  review?: number;
  results_to_report?: number;
};

function fromOthosPage(req: Request): boolean {
  // same-origin fetches from the Othos page; refuse other sites
  const site = req.headers.get('sec-fetch-site');
  const origin = req.headers.get('origin');
  if (origin) return origin === 'http://127.0.0.1:3000' || origin === 'http://localhost:3000';
  return site === null || site === 'same-origin';
}

async function modeOf(file: string): Promise<{ mode: Mode; task: string | null }> {
  let s: WorkerStatus;
  try {
    // the workers may write the file with a BOM; strip it before parsing
    s = JSON.parse((await readFile(file, 'utf8')).replace(/^\uFEFF/, ''));
  } catch {
    return { mode: 'off', task: null };
  }
  if (!s.updated_ts || Date.now() / 1000 - s.updated_ts > STALE_S)
    return { mode: 'off', task: null };
  // "waiting for the CPU": a handed-off task queued while the other worker has the CPU
  const busy =
    s.state === 'working' || s.state === 'held back' || s.state === 'waiting for the CPU';
  // only tasks Othos handed off (asked by voice) light the chip
  const handedOff = s.task_requested_by == null || s.task_requested_by === 'voice';
  const toReport = s.results_to_report ?? s.review ?? 0;
  const mode: Mode = busy && handedOff ? 'working' : toReport > 0 ? 'ready' : 'idle';
  return { mode, task: s.task_type ?? null };
}

export async function GET(req: Request) {
  if (!fromOthosPage(req)) return new Response('Forbidden', { status: 403 });
  // only installed workers are reported (Firestarter is optional: no worker.py, no chip)
  const smithy = await modeOf(WORKERS.smithy);
  if (!existsSync(path.join(AGENT_DIR, 'worker.py'))) return Response.json({ smithy });
  const firestarter = await modeOf(WORKERS.firestarter);
  return Response.json({ firestarter, smithy });
}
