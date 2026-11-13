import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { DRAFTS_DIR, NOTEPAD_PP, fromOthosPage } from '@/lib/othos-local';

// Othos code chat: saves a code block as a new file in the drafts folder and opens
// it in Notepad++. It only ever creates new files there; it never edits or runs anything.
export const runtime = 'nodejs';

const EXT: Record<string, string> = {
  powershell: 'ps1',
  ps1: 'ps1',
  pwsh: 'ps1',
  python: 'py',
  py: 'py',
  javascript: 'js',
  js: 'js',
  typescript: 'ts',
  ts: 'ts',
  tsx: 'tsx',
  jsx: 'jsx',
  html: 'html',
  css: 'css',
  scss: 'scss',
  php: 'php',
  json: 'json',
  yaml: 'yml',
  yml: 'yml',
  sql: 'sql',
  bash: 'sh',
  sh: 'sh',
  batch: 'bat',
  bat: 'bat',
  cmd: 'bat',
  xml: 'xml',
  markdown: 'md',
  md: 'md',
  csharp: 'cs',
  cs: 'cs',
  ini: 'ini',
  regex: 'txt',
  text: 'txt',
  txt: 'txt',
};

export async function POST(req: Request) {
  if (!fromOthosPage(req)) return new Response('Forbidden', { status: 403 });
  let code: string, lang: string;
  try {
    const body = await req.json();
    code = String(body.code ?? '');
    lang = String(body.lang ?? '')
      .toLowerCase()
      .trim();
  } catch {
    return new Response('Bad request', { status: 400 });
  }
  if (!code.trim() || code.length > 200_000) return new Response('Bad request', { status: 400 });

  const ext = EXT[lang] ?? 'txt';
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  await mkdir(DRAFTS_DIR, { recursive: true });
  let file = path.join(DRAFTS_DIR, `${stamp}.${ext}`);
  for (let n = 2; existsSync(file); n++) file = path.join(DRAFTS_DIR, `${stamp}-${n}.${ext}`);
  await writeFile(file, code.endsWith('\n') ? code : code + '\n', { encoding: 'utf8', flag: 'wx' });

  let opened = false;
  if (existsSync(NOTEPAD_PP)) {
    const child = spawn(NOTEPAD_PP, [file], { detached: true, stdio: 'ignore' });
    child.unref();
    opened = true;
  }
  return Response.json({ file, opened });
}
