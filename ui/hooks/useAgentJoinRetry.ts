'use client';

import { useEffect, useRef } from 'react';
import { toast } from 'sonner';
import { useAgent, useSessionContext } from '@livekit/components-react';

// Othos: the local LiveKit dev server sometimes turns down the agent dispatch ("no servers
// available"), and the agent never joins: right after the previous call ended, and on Nov 16
// while The Smithy was starting a Windows Sandbox test (all three tries in 14 s were turned
// away). If no agent arrives in time, end the call and start a new one (new room, new
// dispatch), waiting a little longer each time. Normal joins take about 1 second here.
// Each turned-away try is written to the console as a warning, so it lands in logs\ui.log.
const JOIN_WAITS_MS = [6_000, 8_000, 12_000, 16_000];
const MAX_RETRIES = JOIN_WAITS_MS.length;

export function useAgentJoinRetry() {
  const { isConnected, start, end } = useSessionContext();
  const agent = useAgent();
  const retries = useRef(0);
  const retrying = useRef(false);

  // Fresh retry budget whenever the agent does join, or the user ends the call.
  useEffect(() => {
    if (agent.isConnected) {
      if (retries.current > 0) {
        console.warn(
          `[othos] agent joined after ${retries.current} retr${retries.current === 1 ? 'y' : 'ies'}`
        );
      }
      retries.current = 0;
    }
  }, [agent.isConnected]);
  useEffect(() => {
    if (!isConnected && !retrying.current) retries.current = 0;
  }, [isConnected]);

  useEffect(() => {
    if (!isConnected || agent.isConnected) return;
    const wait = JOIN_WAITS_MS[Math.min(retries.current, JOIN_WAITS_MS.length - 1)];

    const timer = setTimeout(async () => {
      if (retrying.current) return; // a retry is already under way
      if (retries.current >= MAX_RETRIES) {
        console.warn(`[othos] agent did not join after ${MAX_RETRIES} retries; gave up`);
        toast.message(
          "Othos didn't pick up. If the computer is busy (a test or a download running), wait a minute and start the call again.",
          {
            duration: 10_000,
          }
        );
        return; // the normal "agent failed" message may follow
      }
      retries.current += 1;
      retrying.current = true;
      console.warn(
        `[othos] agent did not join within ${wait} ms (turned away?); retry ${retries.current} of ${MAX_RETRIES}`
      );
      toast.message(
        retries.current === 1
          ? 'Othos is taking a moment, reconnecting…'
          : 'Othos is busy, trying again…',
        { duration: 3_000 }
      );
      try {
        await end();
        await start();
      } finally {
        retrying.current = false;
      }
    }, wait);

    return () => clearTimeout(timer);
  }, [isConnected, agent.isConnected, start, end]);
}
