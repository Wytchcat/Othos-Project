import { readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { AGENT_DIR } from '@/lib/othos-local';

// Othos: the assistant's name, logo and colour from the agent's personal.json
// ("assistant": {"name", "logo", "color"}), the same settings personal.py reads.
// Nothing set = "Othos", the green chip and the Circuit Board's green. Read on each
// request (a small local file), so a change shows after a page refresh.

export type Identity = { name: string; color: string; logo: string };

const LOGO_TYPES: Record<string, string> = {
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
};

export function logoType(file: string): string | undefined {
  return LOGO_TYPES[path.extname(file).toLowerCase()];
}

export function readIdentity(): Identity {
  let a: Record<string, unknown> = {};
  try {
    const data = JSON.parse(readFileSync(path.join(AGENT_DIR, 'personal.json'), 'utf-8'));
    if (data && typeof data.assistant === 'object' && data.assistant) a = data.assistant;
  } catch {
    // no personal.json (or not valid JSON): the defaults
  }
  const name = typeof a.name === 'string' && a.name.trim() ? a.name.trim() : 'Othos';
  const color =
    typeof a.color === 'string' && /^#[0-9a-fA-F]{6}$/.test(a.color.trim()) ? a.color.trim() : '';
  let logo = '';
  if (typeof a.logo === 'string' && a.logo.trim()) {
    const p = path.resolve(AGENT_DIR, a.logo.trim()); // relative paths start from the agent folder
    try {
      if (logoType(p) && statSync(p).isFile()) logo = p;
    } catch {
      // the file isn't there: no logo
    }
  }
  return { name, color, logo };
}
