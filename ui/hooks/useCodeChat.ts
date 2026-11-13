'use client';

import { useCallback, useRef, useState } from 'react';

// Othos code chat: the conversation with the local coding model (/api/code), used by
// the /code page and by the call screen's code mode. Nothing here talks to LiveKit.
export type CodeMsg = { role: 'user' | 'assistant'; content: string };

const FENCE = /```([\w#+.-]*)[^\n]*\n([\s\S]*?)```/g;

export function codeBlocks(text: string): { lang: string; code: string }[] {
  return [...text.matchAll(FENCE)].map((m) => ({ lang: m[1] || 'txt', code: m[2] }));
}

export function useCodeChat() {
  const [messages, setMessages] = useState<CodeMsg[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const abort = useRef<AbortController | null>(null);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || busy) return;
      const history: CodeMsg[] = [...messages, { role: 'user', content: question }];
      setMessages([...history, { role: 'assistant', content: '' }]);
      setBusy(true);
      setNote('');
      const ctrl = new AbortController();
      abort.current = ctrl;
      try {
        const res = await fetch('/api/code', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: history }),
          signal: ctrl.signal,
        });
        if (!res.ok || !res.body) throw new Error(await res.text());
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let answer = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          answer += decoder.decode(value, { stream: true });
          setMessages([...history, { role: 'assistant', content: answer }]);
        }
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {
          setMessages([
            ...history,
            { role: 'assistant', content: `_Couldn't get an answer: ${(e as Error).message}_` },
          ]);
        }
      } finally {
        setBusy(false);
        abort.current = null;
      }
    },
    [busy, messages]
  );

  const stop = useCallback(() => abort.current?.abort(), []);

  const clear = useCallback(() => {
    abort.current?.abort();
    setMessages([]);
    setNote('');
  }, []);

  const saveDrafts = useCallback(async (text: string) => {
    const blocks = codeBlocks(text);
    if (!blocks.length) return;
    const saved: string[] = [];
    for (const b of blocks) {
      const res = await fetch('/api/code/draft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(b),
      });
      if (res.ok) saved.push((await res.json()).file);
    }
    setNote(saved.length ? `Saved ${saved.join(', ')}` : 'Saving failed.');
  }, []);

  return { messages, busy, note, send, stop, clear, saveDrafts };
}
