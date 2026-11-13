'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Streamdown } from 'streamdown';
import { useCloseWhenOthosStops } from '@/hooks/useCloseWhenOthosStops';
import { codeBlocks, useCodeChat } from '@/hooks/useCodeChat';
import { spaced, useIdentity } from '@/hooks/useIdentity';

// Othos code chat: typed questions, answers from the local coding model, code
// blocks you can save to the drafts folder (opens in Notepad++). No voice.
// The call screen has the same chat in its control bar (code mode).
export const HANDOFF_KEY = 'othos-code-handoff';

export function CodeChat() {
  const { messages, busy, note, send, stop, clear, saveDrafts } = useCodeChat();
  const [input, setInput] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  const sentInitial = useRef(false);
  const othosStopped = useCloseWhenOthosStops();
  const { name, color } = useIdentity();

  const ask = (text: string) => {
    if (!text.trim() || busy) return;
    setInput('');
    void send(text);
  };

  // Handed over from a voice call ("switch to code chat and ..."): the request comes
  // through this tab's sessionStorage (not the address bar), and is asked straight away
  useEffect(() => {
    if (sentInitial.current) return;
    sentInitial.current = true;
    let q: string | null = null;
    try {
      q = sessionStorage.getItem(HANDOFF_KEY);
      sessionStorage.removeItem(HANDOFF_KEY);
    } catch {
      /* storage blocked: nothing handed over */
    }
    if (q) void send(q);
  }, [send]);

  useEffect(() => {
    // braces: newer browsers return a Promise from scrollIntoView, and an effect must return nothing
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  if (othosStopped) {
    return (
      <main className="grid h-svh place-content-center text-center">
        <p className="text-lg font-semibold">{name} has stopped.</p>
        <p className="text-muted-foreground mt-2 text-sm">You can close this window.</p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex h-svh max-w-4xl flex-col px-4">
      <header className="flex items-center justify-between py-4">
        <h1
          className="text-lg font-semibold tracking-[0.35em] text-[#3ddc84]"
          style={color ? { color } : undefined}
        >
          {spaced(name)} code
        </h1>
        <nav className="flex gap-2 text-sm">
          <button
            className="rounded-md border border-[#403569] bg-[#0e1019] px-3 py-1.5 hover:bg-[#19182b]"
            onClick={clear}
          >
            New chat
          </button>
          <Link
            href="/"
            className="rounded-md border border-[#403569] bg-[#0e1019] px-3 py-1.5 hover:bg-[#19182b]"
          >
            Back to voice
          </Link>
        </nav>
      </header>

      <section className="flex-1 space-y-4 overflow-y-auto pb-4">
        {messages.length === 0 && (
          <p className="text-muted-foreground pt-16 text-center text-sm">
            Ask a coding question. Answers come from the coding model on this laptop; nothing is
            sent anywhere.
          </p>
        )}
        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div
              key={i}
              className="ml-auto max-w-[85%] rounded-lg bg-[#19182b] px-4 py-2 whitespace-pre-wrap"
            >
              {m.content}
            </div>
          ) : (
            <div key={i} className="max-w-full">
              <Streamdown isAnimating={busy && i === messages.length - 1}>
                {m.content || '…'}
              </Streamdown>
              {!(busy && i === messages.length - 1) && codeBlocks(m.content).length > 0 && (
                <button
                  className="mt-1 rounded-md border border-[#403569] bg-[#0e1019] px-3 py-1 text-xs hover:bg-[#19182b]"
                  onClick={() => void saveDrafts(m.content)}
                >
                  Save code to drafts
                </button>
              )}
            </div>
          )
        )}
        <div ref={endRef} />
      </section>

      {note && <p className="text-muted-foreground pb-1 text-xs break-all">{note}</p>}
      <form
        className="flex gap-2 pb-4"
        onSubmit={(e) => {
          e.preventDefault();
          ask(input);
        }}
      >
        <textarea
          className="min-h-[3rem] flex-1 resize-y rounded-md border border-[#403569] bg-[#0e1019] px-3 py-2 outline-none focus:border-[#9d7bff]"
          placeholder="Type a question. Enter sends, Shift+Enter adds a line."
          value={input}
          autoFocus
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              ask(input);
            }
          }}
        />
        {busy ? (
          <button
            type="button"
            className="rounded-md border border-[#403569] px-4 hover:bg-[#19182b]"
            onClick={stop}
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            className="rounded-md border border-[#9d7bff] px-4 hover:bg-[#19182b]"
          >
            Send
          </button>
        )}
      </form>
    </main>
  );
}
