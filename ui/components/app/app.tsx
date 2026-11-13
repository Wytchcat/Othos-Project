'use client';

import { useMemo } from 'react';
import { TokenSource } from 'livekit-client';
import { useSession } from '@livekit/components-react';
import { WarningIcon } from '@phosphor-icons/react/dist/ssr';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { StartAudioButton } from '@/components/agents-ui/start-audio-button';
import { ViewController } from '@/components/app/view-controller';
import { Toaster } from '@/components/ui/sonner';
import { useAgentErrors } from '@/hooks/useAgentErrors';
import { useAgentJoinRetry } from '@/hooks/useAgentJoinRetry';
import { useCloseWhenOthosStops } from '@/hooks/useCloseWhenOthosStops';
import { useCodeChatHandoff } from '@/hooks/useCodeChatHandoff';
import { useDebugMode } from '@/hooks/useDebug';
import { useIdentity } from '@/hooks/useIdentity';

const IN_DEVELOPMENT = process.env.NODE_ENV !== 'production';

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  useAgentErrors();
  useAgentJoinRetry();
  useCodeChatHandoff();

  return null;
}

interface AppProps {
  tokenServerId?: string;
  tokenEndpoint: string;
  agentName?: string;
  isVideoInputSupported: boolean;
}

export function App({ tokenServerId, tokenEndpoint, agentName, isVideoInputSupported }: AppProps) {
  const tokenSource = useMemo(
    () =>
      tokenServerId
        ? TokenSource.developmentTokenServer(tokenServerId)
        : TokenSource.endpoint(tokenEndpoint),
    [tokenServerId, tokenEndpoint]
  );

  const session = useSession(tokenSource, agentName ? { agentName } : undefined);
  const othosStopped = useCloseWhenOthosStops();
  const { name } = useIdentity();

  if (othosStopped) {
    // Othos: the browser didn't let the page close itself; say so instead of an empty call screen
    return (
      <main className="grid h-svh place-content-center text-center">
        <p className="text-lg font-semibold">{name} has stopped.</p>
        <p className="text-muted-foreground mt-2 text-sm">
          You can close this window. It will refresh by itself if {name} starts again.
        </p>
      </main>
    );
  }

  return (
    <AgentSessionProvider session={session}>
      <AppSetup />
      <main className="grid h-svh grid-cols-1 place-content-center">
        <ViewController isVideoInputSupported={isVideoInputSupported} />
      </main>
      <StartAudioButton label="Start Audio" />
      <Toaster
        icons={{
          warning: <WarningIcon weight="bold" />,
        }}
        position="top-center"
        className="toaster group"
        style={
          {
            '--normal-bg': 'var(--popover)',
            '--normal-text': 'var(--popover-foreground)',
            '--normal-border': 'var(--border)',
          } as React.CSSProperties
        }
      />
    </AgentSessionProvider>
  );
}
