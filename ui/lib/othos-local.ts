import { existsSync } from 'node:fs';
import path from 'node:path';

// Othos: the code chat routes talk to Ollama and write draft files, so they only
// answer the Othos page itself. Browsers send an Origin header on POST, so a web
// page from any other site (or another device) is refused.
const ALLOWED_ORIGINS = new Set(['http://127.0.0.1:3000', 'http://localhost:3000']);

export function fromOthosPage(req: Request): boolean {
  const origin = req.headers.get('origin');
  return origin !== null && ALLOWED_ORIGINS.has(origin);
}

export const OLLAMA_URL = process.env.OLLAMA_URL ?? 'http://127.0.0.1:11434';
// The coding model: change it here in .env.local (OTHOS_CODE_MODEL=...), nowhere else.
export const CODE_MODEL = process.env.OTHOS_CODE_MODEL ?? 'qwen2.5-coder:3b';
// The Othos-agent folder: OTHOS_AGENT_DIR in .env.local, or else the sibling of this
// folder (the page server runs from Othos-ui, so that's ../Othos-agent; in the shared
// copy it runs from Othos/ui, so ../agent).
function findAgentDir(): string {
  const parent = path.resolve(process.cwd(), '..');
  const named = path.join(parent, 'Othos-agent');
  if (existsSync(path.join(named, 'agent.py'))) return named;
  const shared = path.join(parent, 'agent');
  return existsSync(path.join(shared, 'agent.py')) ? shared : named;
}
export const AGENT_DIR = process.env.OTHOS_AGENT_DIR ?? findAgentDir();
export const DRAFTS_DIR = process.env.OTHOS_DRAFTS_DIR ?? path.join(AGENT_DIR, 'drafts');
export const NOTEPAD_PP = process.env.OTHOS_EDITOR ?? 'C:\\Program Files\\Notepad++\\notepad++.exe';
