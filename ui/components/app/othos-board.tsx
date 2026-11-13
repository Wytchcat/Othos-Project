'use client';

import { useEffect, useRef } from 'react';
import { useLocalParticipant, useVoiceAssistant } from '@livekit/components-react';
import { useIdentity } from '@/hooks/useIdentity';
import { cn } from '@/lib/shadcn/utils';

/**
 * Othos call-screen visualizer: "The Circuit Board" from ai-visualizer
 * (github.com/jaredrhod/ai-visualizer, (c) 2026 Jared Rhodenizer, AGPL-3.0-or-later).
 * The face itself lives in public/othos-board/ (see its README for the Othos changes).
 *
 * This wrapper drives it: the agent's state and voice waveform, plus your microphone
 * level, are read here from LiveKit and posted into the iframe ~30 times a second.
 * Firestarter's and The Smithy's states come from this page's own /api/worker (local status files),
 * every 3 s. Nothing is fetched from or sent anywhere else.
 */

// Frame-rate / resolution caps live in core.js (?maxfps=30, ?idlefps=8, ?res=0.5).
// the name comes from personal.json (useIdentity); only the name changes, not the artwork
const boardSrc = (name: string) =>
  `/othos-board/board.html?embed=1&name=${encodeURIComponent(name.toUpperCase())}&dots=space`;
const POST_INTERVAL_MS = 33;
const SAMPLE_COUNT = 64;

type BoardState = 'idle' | 'listening' | 'thinking' | 'speaking';
type WorkerMode = 'off' | 'idle' | 'working' | 'ready';
// only the installed workers (Firestarter is optional); a missing key means no chip
type Workers = { firestarter?: WorkerMode; smithy?: WorkerMode };
const WORKER_POLL_MS = 3_000;

function toWorkerMode(mode: unknown): WorkerMode {
  return mode === 'idle' || mode === 'working' || mode === 'ready' ? mode : 'off';
}

function toBoardState(state: string): BoardState {
  return state === 'listening' || state === 'thinking' || state === 'speaking' ? state : 'idle';
}

/** Time-domain analyser on one MediaStreamTrack (cheap: 128-sample FFT window). */
function makeAnalyser(ctx: AudioContext, track: MediaStreamTrack | undefined) {
  if (!track) return null;
  const source = ctx.createMediaStreamSource(new MediaStream([track]));
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 128;
  source.connect(analyser);
  return { source, analyser, buf: new Float32Array(analyser.fftSize) };
}

export function OthosBoard({ className }: { className?: string }) {
  const { state, audioTrack } = useVoiceAssistant();
  const { name } = useIdentity();
  const { microphoneTrack } = useLocalParticipant();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const stateRef = useRef<BoardState>('idle');
  useEffect(() => {
    stateRef.current = toBoardState(state);
  }, [state]);

  // The CPU workers (Firestarter: site work; The Smithy: coding): each has a background
  // chip that lights up while it works on a task Othos handed off, and changes colour
  // when a result is ready
  const workersRef = useRef<Workers>({ firestarter: 'off', smithy: 'off' });
  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try {
        const r = await fetch('/api/worker', { cache: 'no-store' });
        const j = await r.json();
        if (!stop) {
          const w: Workers = {};
          if (j.firestarter) w.firestarter = toWorkerMode(j.firestarter.mode);
          if (j.smithy) w.smithy = toWorkerMode(j.smithy.mode);
          workersRef.current = w;
        }
      } catch {
        if (!stop)
          // keep the same chips, all off
          workersRef.current = Object.fromEntries(
            Object.keys(workersRef.current).map((k) => [k, 'off'])
          ) as Workers;
      }
    };
    void poll();
    const t = window.setInterval(poll, WORKER_POLL_MS);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, []);

  const agentTrack = audioTrack?.publication?.track?.mediaStreamTrack;
  const micTrack = microphoneTrack?.track?.mediaStreamTrack;

  useEffect(() => {
    const ctx = new AudioContext();
    void ctx.resume().catch(() => {});
    const agent = makeAnalyser(ctx, agentTrack);
    const mic = makeAnalyser(ctx, micTrack);
    const samples = new Array<number>(SAMPLE_COUNT).fill(0);

    const timer = window.setInterval(() => {
      const target = iframeRef.current?.contentWindow;
      if (!target) return;
      const boardState = stateRef.current;

      let level = 0;
      if (agent && boardState === 'speaking') {
        agent.analyser.getFloatTimeDomainData(agent.buf);
        const n = agent.buf.length;
        let sum = 0;
        for (let i = 0; i < SAMPLE_COUNT; i++) {
          // int16-scale samples, as the board's signal bus expects
          const v = agent.buf[Math.floor((i * (n - 1)) / (SAMPLE_COUNT - 1))] * 32768;
          samples[i] = v;
          sum += Math.abs(v);
        }
        level = Math.min(1, sum / SAMPLE_COUNT / 3000);
      } else {
        samples.fill(0);
      }

      let micLevel = 0;
      if (mic && boardState === 'listening') {
        mic.analyser.getFloatTimeDomainData(mic.buf);
        let sq = 0;
        for (const v of mic.buf) sq += v * v;
        micLevel = Math.min(1, Math.sqrt(sq / mic.buf.length) * 8);
      }

      target.postMessage(
        {
          type: 'othos-av',
          state: boardState,
          level,
          samples: level > 0 ? samples : null,
          micLevel,
          workers: workersRef.current,
        },
        window.location.origin
      );
    }, POST_INTERVAL_MS);

    return () => {
      window.clearInterval(timer);
      agent?.source.disconnect();
      mic?.source.disconnect();
      void ctx.close();
    };
  }, [agentTrack, micTrack]);

  return (
    <iframe
      ref={iframeRef}
      src={boardSrc(name)}
      title={`${name} visualizer`}
      aria-hidden
      tabIndex={-1}
      className={cn('pointer-events-none border-0', className)}
    />
  );
}
