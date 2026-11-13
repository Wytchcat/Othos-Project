'use client';

import { useState } from 'react';
import { useTheme } from 'next-themes';
import { AnimatePresence, motion } from 'motion/react';
import { useAgent, useSessionContext } from '@livekit/components-react';
import { AgentSessionView_01 } from '@/components/agents-ui/blocks/agent-session-view-01';
import { OthosBoard } from '@/components/app/othos-board';
import { WelcomeView } from '@/components/app/welcome-view';
import { useIdentity } from '@/hooks/useIdentity';

const MotionWelcomeView = motion.create(WelcomeView);
const MotionSessionView = motion.create(AgentSessionView_01);

const VIEW_MOTION_PROPS = {
  variants: {
    visible: {
      opacity: 1,
    },
    hidden: {
      opacity: 0,
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
  transition: {
    duration: 0.5,
    ease: 'linear',
  },
};

interface ViewControllerProps {
  isVideoInputSupported: boolean;
}

export function ViewController({ isVideoInputSupported }: ViewControllerProps) {
  const { name } = useIdentity();
  const { isConnected, start } = useSessionContext();
  const agent = useAgent();
  const { resolvedTheme } = useTheme();
  // Othos: open http://127.0.0.1:3000/?board=off for a call without the visualizer
  // (to compare reply delay). Only read in the browser; the board renders after
  // "Start call", so the first server render is the same either way.
  const [boardOff] = useState(
    () =>
      typeof window !== 'undefined' &&
      new URLSearchParams(window.location.search).get('board') === 'off'
  );

  return (
    <>
      {/* Othos: Circuit Board visualizer, full window behind the call screen.
          Unmounted between calls so it uses no GPU on the welcome screen. */}
      {isConnected && !boardOff && <OthosBoard className="fixed inset-0 z-0 h-full w-full" />}
      <AnimatePresence mode="wait">
        {/* Welcome view */}
        {!isConnected && (
          <MotionWelcomeView
            key="welcome"
            {...VIEW_MOTION_PROPS}
            startButtonText="Start call"
            onStartCall={start}
          />
        )}
        {/* Session view */}
        {isConnected && (
          <MotionSessionView
            key="session-view"
            {...VIEW_MOTION_PROPS}
            preConnectMessage={
              agent.isConnected ? `${name} is listening, ask it anything` : `Waiting for ${name}`
            }
            supportsChatInput={true}
            supportsVideoInput={isVideoInputSupported}
            // Othos: camera only (off at the start of every call); no screen sharing
            supportsScreenShare={false}
            isPreConnectBufferEnabled={true}
            themeMode={resolvedTheme === 'dark' ? 'dark' : 'light'}
            // Othos: the built-in visualizer is off; the Circuit Board above replaces it.
            // To go back to Aura: audioVisualizerType="aura" and remove the board.
            audioVisualizerType="none"
            audioVisualizerColor="#1FD5F9"
            // transparent so the board shows through behind the transcript and controls
            className="fixed inset-0 bg-transparent"
          />
        )}
      </AnimatePresence>
    </>
  );
}
