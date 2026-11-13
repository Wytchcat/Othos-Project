import { readIdentity } from '@/lib/identity';
import { CODE_MODEL, OLLAMA_URL, fromOthosPage } from '@/lib/othos-local';

// Othos code chat: streams an answer from the local coding model in Ollama.
// Nothing leaves the laptop (no LiveKit, Deepgram, Rime or Tavily).
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// the assistant's name from personal.json ("Othos" if not set)
const system = (name: string) =>
  [
    `You are ${name} Code, a local coding helper on a Windows 11 laptop.`,
    'The user works in PowerShell and Notepad++, with Python 3.12, Next.js, and a Joomla 6 site',
    'built with YOOtheme Pro. Give short, direct answers. Put code in fenced blocks with a',
    'language tag. For Windows commands, prefer PowerShell. If you are not sure, say so.',
    "You cannot run code or see the user's files: never claim to have run, tested, or edited anything.",
  ].join(' ');

const MAX_TURNS = 12; // older turns are dropped so the prompt stays quick to read on this CPU

type ChatMessage = { role: 'user' | 'assistant'; content: string };

export async function POST(req: Request) {
  if (!fromOthosPage(req)) return new Response('Forbidden', { status: 403 });

  let messages: ChatMessage[];
  try {
    const body = await req.json();
    messages = (Array.isArray(body.messages) ? body.messages : [])
      .filter(
        (m: ChatMessage) =>
          (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string'
      )
      .slice(-MAX_TURNS)
      .map((m: ChatMessage) => ({ role: m.role, content: m.content.slice(0, 20_000) }));
  } catch {
    return new Response('Bad request', { status: 400 });
  }
  if (!messages.length) return new Response('Bad request', { status: 400 });

  let upstream: Response;
  try {
    upstream = await fetch(`${OLLAMA_URL}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: req.signal, // "Stop" in the page cancels the model too
      body: JSON.stringify({
        model: CODE_MODEL,
        stream: true,
        keep_alive: '15m', // unloads 15 minutes after the last question, freeing memory for voice
        options: { num_ctx: 8192 },
        messages: [{ role: 'system', content: system(readIdentity().name) }, ...messages],
      }),
    });
  } catch {
    return new Response(`Ollama is not answering. Is ${readIdentity().name} running?`, {
      status: 502,
    });
  }
  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.text().catch(() => '');
    return new Response(`Ollama error (${upstream.status}) for model ${CODE_MODEL}: ${detail}`, {
      status: 502,
    });
  }

  // Ollama streams one JSON object per line; pass on just the text
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = '';
  const text = upstream.body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        buffer += decoder.decode(chunk, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const piece = JSON.parse(line)?.message?.content;
            if (piece) controller.enqueue(encoder.encode(piece));
          } catch {
            /* partial line: ignore */
          }
        }
      },
    })
  );
  return new Response(text, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8', 'X-Othos-Model': CODE_MODEL },
  });
}
