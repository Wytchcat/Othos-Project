'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useSessionContext } from '@livekit/components-react';
import { HANDOFF_KEY } from '@/components/app/code-chat';

// Othos: "Othos, switch to code chat (and ...)" in a voice call. The agent says
// "Opening code chat" and then calls this RPC; the page ends the call and opens
// the typed code chat, passing along anything said after "and".
export const CODE_CHAT_RPC = 'othos.openCodeChat';

export function useCodeChatHandoff() {
  const { room, end } = useSessionContext();
  const router = useRouter();

  useEffect(() => {
    room.registerRpcMethod(CODE_CHAT_RPC, async (data) => {
      let q = '';
      try {
        q = String(JSON.parse(data.payload || '{}').q ?? '');
      } catch {
        /* no request passed along */
      }
      try {
        if (q) sessionStorage.setItem(HANDOFF_KEY, q);
      } catch {
        /* storage blocked: the chat opens empty */
      }
      // reply first, then leave the call (ending it inside the RPC would cut the reply)
      setTimeout(async () => {
        try {
          await end();
        } finally {
          router.push('/code');
        }
      }, 100);
      return 'ok';
    });
    return () => room.unregisterRpcMethod(CODE_CHAT_RPC);
  }, [room, end, router]);
}
