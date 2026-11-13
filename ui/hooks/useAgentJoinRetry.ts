'use client';

import { useEffect, useRef } from 'react';
import { toast } from 'sonner';
import { useAgent, useSessionContext } from '@livekit/components-react';

// Othos: the local LiveKit dev server sometimes turns down the agent dispatch for a
// call started right after the previous one ended ("no servers available"), and the
// agent never joins. If no agent arrives in time, end the call and start a new one
// (new room, new dispatch). Normal joins take about 1 second on this machine.
const JOIN_TIMEOUT_MS = 6_000;
const MAX_RETRIES = 2;

export function useAgentJoinRetry() {
  const { isConnected, start, end } = useSessionContext();
  const agent = useAgent();
  const retries = useRef(0);
  const retrying = useRef(false);

  // Fresh retry budget whenever the agent does join, or the user ends the call.
  useEffect(() => {
    if (agent.isConnected) retries.current = 0;
  }, [agent.isConnected]);
  useEffect(() => {
    if (!isConnected && !retrying.current) retries.current = 0;
  }, [isConnected]);

  useEffect(() => {
    if (!isConnected || agent.isConnected) return;

    const timer = setTimeout(async () => {
      if (retrying.current) return; // a retry is already under way
      if (retries.current >= MAX_RETRIES) return; // let the normal "agent failed" message show
      retries.current += 1;
      retrying.current = true;
      console.info(
        `[othos] agent did not join within ${JOIN_TIMEOUT_MS} ms; retry ${retries.current}`
      );
      toast.message('Othos is taking a moment, reconnecting…', { duration: 3_000 });
      try {
        await end();
        await start();
      } finally {
        retrying.current = false;
      }
    }, JOIN_TIMEOUT_MS);

    return () => clearTimeout(timer);
  }, [isConnected, agent.isConnected, start, end]);
}
