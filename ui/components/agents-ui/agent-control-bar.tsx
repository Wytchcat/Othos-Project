'use client';

import { type ComponentProps, useEffect, useRef, useState } from 'react';
import { Track } from 'livekit-client';
import { Loader, MessageSquareTextIcon, SendHorizontal } from 'lucide-react';
import { type MotionProps, motion } from 'motion/react';
import { useChat } from '@livekit/components-react';
import { AgentDisconnectButton } from '@/components/agents-ui/agent-disconnect-button';
import { AgentTrackControl } from '@/components/agents-ui/agent-track-control';
import {
  AgentTrackToggle,
  agentTrackToggleVariants,
} from '@/components/agents-ui/agent-track-toggle';
import { CallCodePanel } from '@/components/app/call-code-panel';
import { SmithyIcon } from '@/components/app/smithy-icon';
import { Button } from '@/components/ui/button';
import { Toggle } from '@/components/ui/toggle';
import {
  type UseInputControlsProps,
  useInputControls,
  usePublishPermissions,
} from '@/hooks/agents-ui/use-agent-control-bar';
import { useCodeChat } from '@/hooks/useCodeChat';
import { useIdentity } from '@/hooks/useIdentity';
import { cn } from '@/lib/shadcn/utils';

// Othos: the code-chat toggle in the board's purple (#9d7bff) when on
const CODE_TOGGLE_ON = [
  'data-[state=on]:bg-[#9d7bff]/15 data-[state=on]:hover:bg-[#9d7bff]/25',
  'data-[state=on]:border-[#9d7bff]/40 data-[state=on]:text-[#9d7bff]',
  'data-[state=on]:hover:text-[#9d7bff] data-[state=on]:focus:text-[#9d7bff]',
  'dark:data-[state=on]:bg-[#9d7bff]/15 dark:data-[state=on]:text-[#9d7bff]',
];

// Othos: mic and camera in the Circuit Board's colours: off = gold #e7c368, on = blue #35e0ff
const LK_TOGGLE_VARIANT_1 = [
  'data-[state=off]:bg-[#e7c368]/15 data-[state=off]:hover:bg-[#e7c368]/25',
  'data-[state=off]:[&_~_button]:bg-[#e7c368]/15 data-[state=off]:[&_~_button]:hover:bg-[#e7c368]/25',
  'data-[state=off]:border-[#e7c368]/40 data-[state=off]:hover:border-[#e7c368]/60',
  'data-[state=off]:[&_~_button]:border-[#e7c368]/40 data-[state=off]:[&_~_button]:hover:border-[#e7c368]/60',
  'data-[state=off]:text-[#e7c368] data-[state=off]:hover:text-[#e7c368] data-[state=off]:focus:text-[#e7c368]',
  'data-[state=off]:focus-visible:ring-[#e7c368]/40 data-[state=off]:focus-visible:border-[#e7c368]',
  'dark:data-[state=off]:[&_~_button]:bg-[#e7c368]/15 dark:data-[state=off]:[&_~_button]:hover:bg-[#e7c368]/25',
  'data-[state=on]:bg-[#35e0ff]/15 data-[state=on]:hover:bg-[#35e0ff]/25',
  'data-[state=on]:[&_~_button]:bg-[#35e0ff]/15 data-[state=on]:[&_~_button]:hover:bg-[#35e0ff]/25',
  'data-[state=on]:border-[#35e0ff]/40 data-[state=on]:[&_~_button]:border-[#35e0ff]/40',
  'data-[state=on]:text-[#35e0ff] data-[state=on]:hover:text-[#35e0ff] data-[state=on]:focus:text-[#35e0ff]',
  'data-[state=on]:focus-visible:ring-[#35e0ff]/40 data-[state=on]:focus-visible:border-[#35e0ff]',
  'dark:data-[state=on]:[&_~_button]:bg-[#35e0ff]/15 dark:data-[state=on]:[&_~_button]:hover:bg-[#35e0ff]/25',
];

const LK_TOGGLE_VARIANT_2 = [
  'data-[state=off]:bg-accent data-[state=off]:hover:bg-foreground/10',
  'data-[state=off]:border-border data-[state=off]:hover:border-foreground/12',
  'data-[state=off]:focus-visible:border-ring data-[state=off]:focus-visible:ring-foreground/12',
  'data-[state=off]:text-foreground data-[state=off]:hover:text-foreground data-[state=off]:focus:text-foreground',
  'data-[state=on]:bg-blue-500/20 data-[state=on]:hover:bg-blue-500/30',
  'data-[state=on]:border-blue-700/10 data-[state=on]:text-blue-700 data-[state=on]:ring-blue-700/30',
  'data-[state=on]:focus-visible:border-blue-700/50',
  'dark:data-[state=on]:bg-blue-500/20 dark:data-[state=on]:text-blue-300',
];

const MOTION_PROPS: MotionProps = {
  variants: {
    hidden: {
      height: 0,
      opacity: 0,
      marginBottom: 0,
    },
    visible: {
      height: 'auto',
      opacity: 1,
      marginBottom: 12,
    },
  },
  initial: 'hidden',
  transition: {
    duration: 0.3,
    ease: 'easeOut',
  },
};

interface AgentChatInputProps {
  chatOpen: boolean;
  onSend?: (message: string) => void;
  className?: string;
  placeholder?: string;
}

function AgentChatInput({
  chatOpen,
  onSend = async () => {},
  className,
  placeholder = 'Type something...',
}: AgentChatInputProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [isSending, setIsSending] = useState(false);
  const [message, setMessage] = useState<string>('');
  const isDisabled = isSending || message.trim().length === 0;

  const handleSend = async () => {
    if (isDisabled) {
      return;
    }

    try {
      setIsSending(true);
      await onSend(message.trim());
      setMessage('');
    } catch (error) {
      console.error(error);
    } finally {
      setIsSending(false);
    }
  };

  const handleKeyDown = async (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleButtonClick = async () => {
    if (isDisabled) return;
    await handleSend();
  };

  useEffect(() => {
    if (chatOpen) return;
    // when not disabled refocus on input
    inputRef.current?.focus();
  }, [chatOpen]);

  return (
    <div className={cn('mb-3 flex grow items-end gap-2 rounded-md pl-1 text-sm', className)}>
      <textarea
        autoFocus
        ref={inputRef}
        value={message}
        disabled={!chatOpen || isSending}
        placeholder={placeholder}
        onKeyDown={handleKeyDown}
        onChange={(e) => setMessage(e.target.value)}
        className="field-sizing-content max-h-16 min-h-8 flex-1 resize-none [scrollbar-width:thin] py-2 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
      />
      <Button
        size="icon"
        type="button"
        disabled={isDisabled}
        variant={isDisabled ? 'secondary' : 'default'}
        title={isSending ? 'Sending...' : 'Send'}
        onClick={handleButtonClick}
        className="self-end disabled:cursor-not-allowed"
      >
        {isSending ? <Loader className="animate-spin" /> : <SendHorizontal />}
      </Button>
    </div>
  );
}

/** Configuration for which controls to display in the AgentControlBar. */
export interface AgentControlBarControls {
  /**
   * Whether to show the leave/disconnect button.
   *
   * @defaultValue true
   */
  leave?: boolean;
  /**
   * Whether to show the camera toggle control.
   *
   * @defaultValue true (if camera publish permission is granted)
   */
  camera?: boolean;
  /**
   * Whether to show the microphone toggle control.
   *
   * @defaultValue true (if microphone publish permission is granted)
   */
  microphone?: boolean;
  /**
   * Whether to show the screen share toggle control.
   *
   * @defaultValue true (if screen share publish permission is granted)
   */
  screenShare?: boolean;
  /**
   * Whether to show the chat toggle control.
   *
   * @defaultValue true (if data publish permission is granted)
   */
  chat?: boolean;
}

export interface AgentControlBarProps extends UseInputControlsProps {
  /**
   * The visual style of the control bar.
   *
   * @default 'default'
   */
  variant?: 'default' | 'outline' | 'livekit';
  /**
   * This takes an object with the following keys: `leave`, `microphone`, `screenShare`, `camera`,
   * `chat`. Each key maps to a boolean value that determines whether the control is displayed.
   *
   * @default
   * {
   *   leave: true,
   *   microphone: true,
   *   screenShare: true,
   *   camera: true,
   *   chat: true,
   * }
   */
  controls?: AgentControlBarControls;
  /**
   * Whether to save user choices.
   *
   * @default true
   */
  saveUserChoices?: boolean;
  /**
   * Whether the agent is connected to a session.
   *
   * @default false
   */
  isConnected?: boolean;
  /**
   * Whether the chat input interface is open.
   *
   * @default false
   */
  isChatOpen?: boolean;
  /** The callback for when the user disconnects. */
  onDisconnect?: () => void;
  /** The callback for when the chat is opened or closed. */
  onIsChatOpenChange?: (open: boolean) => void;
  /** The callback for when a device error occurs. */
  onDeviceError?: (error: { source: Track.Source; error: Error }) => void;
}

/**
 * A control bar specifically designed for voice assistant interfaces. Provides controls for
 * microphone, camera, screen share, chat, and disconnect. Includes an expandable chat input for
 * text-based interaction with the agent.
 *
 * @example
 *
 * ```tsx
 * <AgentControlBar
 *   variant="livekit"
 *   isConnected={true}
 *   onDisconnect={() => handleDisconnect()}
 *   controls={{
 *     microphone: true,
 *     camera: true,
 *     screenShare: false,
 *     chat: true,
 *     leave: true,
 *   }}
 * />;
 * ```
 *
 * @extends ComponentProps<'div'>
 */
export function AgentControlBar({
  variant = 'default',
  controls,
  isChatOpen = false,
  isConnected = false,
  saveUserChoices = true,
  onDisconnect,
  onDeviceError,
  onIsChatOpenChange,
  className,
  ...props
}: AgentControlBarProps & ComponentProps<'div'>) {
  const { send } = useChat();
  const publishPermissions = usePublishPermissions();
  const [isChatOpenUncontrolled, setIsChatOpenUncontrolled] = useState(isChatOpen);
  const {
    microphoneTrack,
    cameraToggle,
    microphoneToggle,
    screenShareToggle,
    handleAudioDeviceChange,
    handleVideoDeviceChange,
    handleMicrophoneDeviceSelectError,
    handleCameraDeviceSelectError,
  } = useInputControls({ onDeviceError, saveUserChoices });

  // Othos: code mode. The code button sends what's typed to the code chat (the local
  // coding model) instead of to voice Othos, with its answers above the chat box.
  const codeChat = useCodeChat();
  const { name } = useIdentity();
  const [codeModeWanted, setCodeMode] = useState(false);
  const chatOpenNow = isChatOpen || isChatOpenUncontrolled;
  const codeMode = codeModeWanted && chatOpenNow; // only while the chat box is open
  const setChatOpen = (open: boolean) => {
    if (!onIsChatOpenChange) setIsChatOpenUncontrolled(open);
    else onIsChatOpenChange(open);
  };

  const handleSendMessage = async (message: string) => {
    if (codeMode) {
      void codeChat.send(message); // the answer streams into the code panel
      return;
    }
    await send(message);
  };

  const visibleControls = {
    leave: controls?.leave ?? true,
    microphone: controls?.microphone ?? publishPermissions.microphone,
    screenShare: controls?.screenShare ?? publishPermissions.screenShare,
    camera: controls?.camera ?? publishPermissions.camera,
    chat: controls?.chat ?? publishPermissions.data,
  };

  const isEmpty = Object.values(visibleControls).every((value) => !value);

  if (isEmpty) {
    console.warn('AgentControlBar: `visibleControls` contains only false values.');
    return null;
  }

  return (
    <div
      aria-label="Voice assistant controls"
      className={cn(
        'bg-background border-input/50 dark:border-muted flex flex-col border p-3 drop-shadow-md/3',
        variant === 'livekit' ? 'rounded-[31px]' : 'rounded-lg',
        className
      )}
      {...props}
    >
      <motion.div
        {...MOTION_PROPS}
        inert={!(isChatOpen || isChatOpenUncontrolled)}
        animate={isChatOpen || isChatOpenUncontrolled ? 'visible' : 'hidden'}
        className="border-input/50 flex w-full flex-col items-start overflow-hidden border-b"
      >
        {codeMode && (
          <CallCodePanel
            messages={codeChat.messages}
            busy={codeChat.busy}
            note={codeChat.note}
            onStop={codeChat.stop}
            onClear={codeChat.clear}
            onSave={(t) => void codeChat.saveDrafts(t)}
          />
        )}
        <AgentChatInput
          chatOpen={chatOpenNow}
          onSend={handleSendMessage}
          placeholder={codeMode ? `Ask the code chat... (not ${name})` : 'Type something...'}
          className={cn('w-full', variant === 'livekit' && '[&_button]:rounded-full')}
        />
      </motion.div>

      <div className="flex gap-1">
        <div className="flex grow gap-1">
          {/* Toggle Microphone */}
          {visibleControls.microphone && (
            <AgentTrackControl
              variant={variant === 'outline' ? 'outline' : 'default'}
              kind="audioinput"
              aria-label="Toggle microphone"
              source={Track.Source.Microphone}
              pressed={microphoneToggle.enabled}
              disabled={microphoneToggle.pending}
              audioTrack={microphoneTrack}
              onPressedChange={microphoneToggle.toggle}
              onActiveDeviceChange={handleAudioDeviceChange}
              onMediaDeviceError={handleMicrophoneDeviceSelectError}
              className={cn(
                variant === 'livekit' && [
                  LK_TOGGLE_VARIANT_1,
                  'rounded-full [&_button:first-child]:rounded-l-full [&_button:last-child]:rounded-r-full',
                ]
              )}
            />
          )}

          {/* Toggle Camera */}
          {visibleControls.camera && (
            <AgentTrackControl
              variant={variant === 'outline' ? 'outline' : 'default'}
              kind="videoinput"
              aria-label="Toggle camera"
              source={Track.Source.Camera}
              pressed={cameraToggle.enabled}
              pending={cameraToggle.pending}
              disabled={cameraToggle.pending}
              onPressedChange={cameraToggle.toggle}
              onMediaDeviceError={handleCameraDeviceSelectError}
              onActiveDeviceChange={handleVideoDeviceChange}
              className={cn(
                variant === 'livekit' && [
                  LK_TOGGLE_VARIANT_1,
                  'rounded-full [&_button:first-child]:rounded-l-full [&_button:last-child]:rounded-r-full',
                ]
              )}
            />
          )}

          {/* Othos: Code chat. Typed messages go to the local coding model while it's on */}
          {visibleControls.chat && (
            <Toggle
              variant={variant === 'outline' ? 'outline' : 'default'}
              pressed={codeMode}
              aria-label="Toggle code chat"
              title={codeMode ? 'Code chat on: typing goes to the code chat' : 'Code chat'}
              onPressedChange={(on) => {
                setCodeMode(on);
                if (on) setChatOpen(true);
              }}
              className={agentTrackToggleVariants({
                variant: variant === 'outline' ? 'outline' : 'default',
                className: cn(
                  variant === 'livekit' && [LK_TOGGLE_VARIANT_2, CODE_TOGGLE_ON, 'rounded-full']
                ),
              })}
            >
              <SmithyIcon />
            </Toggle>
          )}

          {/* Toggle Screen Share */}
          {visibleControls.screenShare && (
            <AgentTrackToggle
              variant={variant === 'outline' ? 'outline' : 'default'}
              aria-label="Toggle screen share"
              source={Track.Source.ScreenShare}
              pressed={screenShareToggle.enabled}
              disabled={screenShareToggle.pending}
              onPressedChange={screenShareToggle.toggle}
              className={cn(variant === 'livekit' && [LK_TOGGLE_VARIANT_2, 'rounded-full'])}
            />
          )}

          {/* Toggle Transcript */}
          {visibleControls.chat && (
            <Toggle
              variant={variant === 'outline' ? 'outline' : 'default'}
              pressed={isChatOpen || isChatOpenUncontrolled}
              aria-label="Toggle transcript"
              onPressedChange={(state) => {
                if (!state) setCodeMode(false); // closing the chat box leaves code mode
                if (!onIsChatOpenChange) setIsChatOpenUncontrolled(state);
                else onIsChatOpenChange(state);
              }}
              className={agentTrackToggleVariants({
                variant: variant === 'outline' ? 'outline' : 'default',
                className: cn(variant === 'livekit' && [LK_TOGGLE_VARIANT_2, 'rounded-full']),
              })}
            >
              <MessageSquareTextIcon />
            </Toggle>
          )}
        </div>

        {/* Disconnect */}
        {visibleControls.leave && (
          <AgentDisconnectButton
            onClick={onDisconnect}
            disabled={!isConnected}
            className={cn(
              variant === 'livekit' &&
                'bg-destructive/10 dark:bg-destructive/10 text-destructive hover:bg-destructive/20 dark:hover:bg-destructive/20 focus:bg-destructive/20 focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/4 rounded-full font-mono text-xs font-bold tracking-wider',
              // Othos: End call in the Circuit Board's gold (#e7c368) instead of red
              'border border-[#e7c368]/60 bg-[#e7c368]/20 text-[#e7c368] hover:bg-[#e7c368]/35 hover:text-[#f3dc9a]',
              'focus:bg-[#e7c368]/35 dark:bg-[#e7c368]/20 dark:hover:bg-[#e7c368]/35',
              'focus-visible:ring-[#e7c368]/40 dark:focus-visible:ring-[#e7c368]/40'
            )}
          >
            <span className="hidden uppercase md:inline">End call</span>
            <span className="inline uppercase md:hidden">End</span>
          </AgentDisconnectButton>
        )}
      </div>
    </div>
  );
}
