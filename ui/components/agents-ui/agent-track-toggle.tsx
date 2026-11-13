import { type ComponentProps, Fragment, useMemo, useState } from 'react';
import { type VariantProps, cva } from 'class-variance-authority';
import { Track } from 'livekit-client';
import {
  LoaderIcon,
  MicIcon,
  MicOffIcon,
  MonitorOffIcon,
  MonitorUpIcon,
  VideoIcon,
  VideoOffIcon,
} from 'lucide-react';
import { Toggle } from '@/components/ui/toggle';
import { cn } from '@/lib/shadcn/utils';

export const agentTrackToggleVariants = cva(['size-9'], {
  variants: {
    size: {
      default: 'h-9 px-2 min-w-9',
      sm: 'h-8 px-1.5 min-w-8',
      lg: 'h-10 px-2.5 min-w-10',
    },
    // Othos: mic and camera buttons in the Circuit Board's colours: off = its gold
    // (#e7c368), on = its lighter listening blue (#35e0ff), instead of red / gray
    variant: {
      default: [
        'data-[state=off]:bg-[#e7c368]/15 data-[state=off]:text-[#e7c368]',
        'data-[state=off]:hover:bg-[#e7c368]/25',
        'data-[state=off]:focus-visible:ring-[#e7c368]/40',
        'data-[state=on]:bg-[#35e0ff]/15 data-[state=on]:text-[#35e0ff]',
        'data-[state=on]:hover:bg-[#35e0ff]/25',
        'data-[state=on]:focus-visible:ring-[#35e0ff]/40',
      ],
      outline: [
        'data-[state=off]:bg-[#e7c368]/15 data-[state=off]:text-[#e7c368] data-[state=off]:border-[#e7c368]/40',
        'data-[state=off]:hover:bg-[#e7c368]/25 data-[state=off]:hover:text-[#e7c368]',
        'data-[state=off]:focus:text-[#e7c368]',
        'data-[state=off]:focus-visible:border-[#e7c368] data-[state=off]:focus-visible:ring-[#e7c368]/40',
        'data-[state=on]:bg-[#35e0ff]/15 data-[state=on]:text-[#35e0ff] data-[state=on]:border-[#35e0ff]/40',
        'data-[state=on]:hover:bg-[#35e0ff]/25 data-[state=on]:hover:border-[#35e0ff]/60',
        'dark:data-[state=on]:hover:bg-[#35e0ff]/25',
      ],
    },
  },
  defaultVariants: {
    variant: 'default',
  },
});

function getSourceIcon(source: Track.Source, enabled: boolean, pending = false) {
  if (pending) {
    return LoaderIcon;
  }

  switch (source) {
    case Track.Source.Microphone:
      return enabled ? MicIcon : MicOffIcon;
    case Track.Source.Camera:
      return enabled ? VideoIcon : VideoOffIcon;
    case Track.Source.ScreenShare:
      return enabled ? MonitorUpIcon : MonitorOffIcon;
    default:
      return Fragment;
  }
}

/**
 * Props for the AgentTrackToggle component.
 */
export type AgentTrackToggleProps = VariantProps<typeof agentTrackToggleVariants> &
  ComponentProps<'button'> & {
    /**
     * The size of the toggle.
     */
    size?: 'sm' | 'default' | 'lg';
    /**
     * The variant of the toggle.
     * @defaultValue 'default'
     */
    variant?: 'default' | 'outline';
    /**
     * The track source to toggle (Microphone, Camera, or ScreenShare).
     */
    source: 'camera' | 'microphone' | 'screen_share';
    /**
     * Whether the toggle is in a pending/loading state.
     * When true, displays a loading spinner icon.
     * @defaultValue false
     */
    pending?: boolean;
    /**
     * Whether the toggle is currently pressed/enabled.
     * @defaultValue false
     */
    pressed?: boolean;
    /**
     * The default pressed state when uncontrolled.
     * @defaultValue false
     */
    defaultPressed?: boolean;
    /**
     * Callback fired when the pressed state changes.
     */
    onPressedChange?: (pressed: boolean) => void;
  };

/**
 * A toggle button for controlling track publishing state.
 * Displays appropriate icons based on the track source and state.
 *
 * @extends ComponentProps<'button'>
 *
 * @example
 * ```tsx
 * <AgentTrackToggle
 *   source={Track.Source.Microphone}
 *   pressed={isMicEnabled}
 *   onPressedChange={(pressed) => setMicEnabled(pressed)}
 * />
 * ```
 */
export function AgentTrackToggle({
  size = 'default',
  variant = 'default',
  source,
  pending = false,
  pressed,
  defaultPressed = false,
  className,
  onPressedChange,
  ...props
}: AgentTrackToggleProps) {
  const [uncontrolledPressed, setUncontrolledPressed] = useState(defaultPressed ?? false);
  const isControlled = pressed !== undefined;
  const resolvedPressed = useMemo(
    () => (isControlled ? pressed : uncontrolledPressed) ?? false,
    [isControlled, pressed, uncontrolledPressed]
  );
  const IconComponent = getSourceIcon(source as Track.Source, resolvedPressed, pending);
  const handlePressedChange = (nextPressed: boolean) => {
    if (!isControlled) {
      setUncontrolledPressed(nextPressed);
    }
    onPressedChange?.(nextPressed);
  };

  return (
    <Toggle
      size={size}
      variant={variant}
      pressed={isControlled ? pressed : undefined}
      defaultPressed={isControlled ? undefined : defaultPressed}
      aria-label={`Toggle ${source}`}
      onPressedChange={handlePressedChange}
      className={cn(
        agentTrackToggleVariants({
          size,
          variant: variant ?? 'default',
          className,
        })
      )}
      {...props}
    >
      <IconComponent className={cn(pending && 'animate-spin')} />
      {props.children}
    </Toggle>
  );
}
