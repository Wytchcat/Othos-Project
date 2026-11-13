'use client';

import { useEffect, useRef } from 'react';
import { Streamdown } from 'streamdown';
import { type CodeMsg, codeBlocks } from '@/hooks/useCodeChat';
import { useIdentity } from '@/hooks/useIdentity';

// Othos: the code chat inside the call screen's control bar (code mode). The chat
// box below it sends to the local coding model instead of to voice Othos.
export function CallCodePanel({
  messages,
  busy,
  note,
  onStop,
  onClear,
  onSave,
}: {
  messages: CodeMsg[];
  busy: boolean;
  note: string;
  onStop: () => void;
  onClear: () => void;
  onSave: (text: string) => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const { name } = useIdentity();
  useEffect(() => {
    // braces: newer browsers return a Promise from scrollIntoView, and an effect must return nothing
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [messages]);

  return (
    <div className="w-full border-b border-[#9d7bff]/25 pb-2">
      <div className="flex items-center justify-between px-1 pb-1 text-xs">
        <span className="font-semibold tracking-[0.2em] text-[#9d7bff]">CODE CHAT</span>
        <span className="flex gap-2">
          {busy && (
            <button className="text-[#9d7bff] hover:underline" onClick={onStop}>
              Stop
            </button>
          )}
          {messages.length > 0 && !busy && (
            <button className="text-muted-foreground hover:underline" onClick={onClear}>
              Clear
            </button>
          )}
        </span>
      </div>
      <div className="max-h-[40vh] space-y-3 overflow-y-auto px-1 text-sm">
        {messages.length === 0 && (
          <p className="text-muted-foreground py-1 text-xs">
            Typing now goes to the code chat (the coding model on this laptop), not to {name}. Click
            the code button again to talk to {name}.
          </p>
        )}
        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div
              key={i}
              className="ml-auto w-fit max-w-[85%] rounded-lg bg-[#19182b] px-3 py-1.5 whitespace-pre-wrap"
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
                  className="mt-1 rounded-md border border-[#403569] bg-[#0e1019] px-2 py-0.5 text-xs hover:bg-[#19182b]"
                  onClick={() => onSave(m.content)}
                >
                  Save code to drafts
                </button>
              )}
            </div>
          )
        )}
        <div ref={endRef} />
      </div>
      {note && <p className="text-muted-foreground px-1 pt-1 text-xs break-all">{note}</p>}
    </div>
  );
}
