'use client';

import { useEffect, useState } from 'react';

// Othos: when Othos is stopped (Othos window closed / Stop Othos), this page's
// server goes away and the page would sit there as an empty call screen. So the
// page checks every few seconds; once the server has been gone for two checks it
// closes its own window (allowed for a window opened straight from Start Othos).
// If the browser won't close it, it shows "Othos has stopped", and if Othos comes
// back while it's still open, it refreshes itself.
const CHECK_MS = 3_000;
const MISSES_BEFORE_STOPPED = 2;

async function serverUp(): Promise<boolean> {
  try {
    // a tiny static file: asking for '/' would make the dev server render the page every
    // few seconds, taking CPU from the model
    const res = await fetch('/alive.txt', { method: 'HEAD', cache: 'no-store' });
    return res.status > 0;
  } catch {
    return false;
  }
}

export function useCloseWhenOthosStops(): boolean {
  const [stopped, setStopped] = useState(false);

  useEffect(() => {
    let misses = 0;
    let wasStopped = false;
    const timer = window.setInterval(async () => {
      const up = await serverUp();
      if (up) {
        misses = 0;
        if (wasStopped) window.location.reload(); // Othos is back: fresh page, ready for a call
        return;
      }
      misses += 1;
      if (misses === MISSES_BEFORE_STOPPED && !wasStopped) {
        wasStopped = true;
        window.close();
        setStopped(true); // only seen if the browser kept the window open
      }
    }, CHECK_MS);
    return () => window.clearInterval(timer);
  }, []);

  return stopped;
}
